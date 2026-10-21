"""Los pasos siguientes (A2, Z, B2, C3) verifican Chatwoot contra el telefono del caso.

Medido en ATT1 el 2026-10-21: los 17 A2 que vencieron entre el 20/10 17:40Z y
el 21/10 02:39Z cerraron con ``chatwoot_authority_unavailable`` sin mandar
nada. La verificacion canonica comparaba la conversacion con el unico JID que
un runtime viejo deja pasar (``allowed_jid``) y fallaba
``conversation_identity_mismatch`` para cualquier comprador real; sin evidencia,
la reevaluacion escala. Las pruebas de A2, Z y B2 reemplazaban entera esta
verificacion por una que siempre aprueba, por eso no lo vieron.

Aca corre la verificacion real (``_load_chatwoot_evidence`` y el
``ChatwootClient``) contra la conversacion capturada ese dia
(``fixtures/chatwoot/conversation_a2_att1_2026-10-21.json``): el contacto de
WABA no trae ``identifier`` ni ``source_id`` y su ``phone_number`` es la forma
``521`` + 10, mientras el contacto de la base guarda ``52`` + 10.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_durable_dispatcher_approved_template import (
    ACCOUNT_ID,
    ALLY,
    BOUNDARY,
    INBOX_ID,
    NOW,
    _Authority,
)

from bridge.chatwoot import ChatwootClient
from bridge.supabase import ChatwootAuthorityContext
from bridge.worker import DurableDispatcher

FIXTURE = Path(__file__).parent / "fixtures" / "chatwoot" / "conversation_a2_att1_2026-10-21.json"
CONVERSATION: dict[str, Any] = {
    key: value
    for key, value in json.loads(FIXTURE.read_text(encoding="utf-8")).items()
    if not key.startswith("_")
}
CONVERSATION_ID = CONVERSATION["id"]
ANCHOR_ID = 9001  # el A1 aceptado, ancla del A2
CASE_PHONE = "525500000001"  # como lo guarda el formulario: 52 + 10
CASE_PHONE_WA = "5215500000001"  # como lo guarda Hotmart: 521 + 10
OTHER_PHONE = "525500000002"


class _StepAuthority(_Authority):
    """Un A2 vencido cuyo caso ya tiene la conversacion de su A1."""

    def __init__(
        self,
        *,
        phone: str | None = CASE_PHONE,
        step_key: str = "cart_second_touch",
    ) -> None:
        super().__init__()
        self.action = replace(
            self.action,
            action_type="no_reply_review",
            step_key=step_key,
            anchor_type="accepted_outbound_message",
            anchor_subject_internal_id="message-a1",
        )
        self.context = replace(
            self.context,
            action_type="no_reply_review",
            step_key=step_key,
            buyer_phone=phone,
        )
        self.context_reads = 0

    async def get_followup_chatwoot_context(self, **_: object) -> ChatwootAuthorityContext:
        return ChatwootAuthorityContext(
            action_id=self.action.action_id,
            action_type=self.action.action_type,
            chatwoot_account_id=ACCOUNT_ID,
            external_conversation_id=CONVERSATION_ID,
            expected_inbox_id=INBOX_ID,
            anchor_external_message_id=ANCHOR_ID,
        )

    async def get_followup_execution_context(self, **kwargs: object) -> Any:
        self.context_reads += 1
        return await super().get_followup_execution_context(**kwargs)


def _transport(requests: list[str]) -> httpx.MockTransport:
    anchor_at = 1_760_990_000

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        base = f"/api/v1/accounts/{ACCOUNT_ID}/conversations/{CONVERSATION_ID}"
        if request.method == "GET" and request.url.path == base:
            return httpx.Response(200, json=CONVERSATION)
        if request.method == "GET" and request.url.path == f"{base}/messages":
            return httpx.Response(
                200,
                json={
                    "payload": [
                        {
                            "id": ANCHOR_ID,
                            "created_at": anchor_at,
                            "message_type": 1,
                            "private": False,
                            "sender": {"type": "agent_bot"},
                        }
                    ]
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _dispatcher(
    authority: _StepAuthority,
    requests: list[str],
    *,
    portable: bool = True,
) -> DurableDispatcher:
    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=ACCOUNT_ID,
        access_token="token-de-prueba",
        allowed_jid=None,
        transport=_transport(requests),
    )
    return DurableDispatcher(
        supabase=authority,  # type: ignore[arg-type]
        worker_id="att1-dispatcher",
        chatwoot=client,
        chatwoot_account_id=ACCOUNT_ID,
        chatwoot_inbox_id=INBOX_ID,
        allowed_jid=None,
        commercial_ally_config=ALLY if portable else None,
        portable_recipient_enabled=portable,
        whatsapp_equivalence_enabled=portable,
        pilot_boundary=BOUNDARY,
    )


def _evidence(authority: _StepAuthority, **kwargs: Any) -> tuple[dict[str, object] | None, list[str]]:
    requests: list[str] = []
    dispatcher = _dispatcher(authority, requests, **{k: v for k, v in kwargs.items() if k == "portable"})
    evidence = asyncio.run(
        dispatcher._load_chatwoot_evidence(
            action=authority.action,
            now=NOW,
            **{k: v for k, v in kwargs.items() if k == "buyer_phone"},
        )
    )
    return evidence, requests


# A2 y Z del carrito, B2 (o Z) del pago rechazado y C3 de la ficha: todos son
# un no_reply_review sobre la conversacion del caso y pasan por esta lectura.
FOLLOWING_STEPS = (
    "cart_second_touch",
    "cart_last_touch",
    "payment_failure_followup",
    "payment_slip_due_day",
)


@pytest.mark.parametrize("step_key", FOLLOWING_STEPS)
@pytest.mark.parametrize("phone", [CASE_PHONE, CASE_PHONE_WA])
def test_every_following_step_gets_its_evidence_with_the_phone_of_the_case(
    step_key: str, phone: str
) -> None:
    evidence, _ = _evidence(_StepAuthority(phone=phone, step_key=step_key))

    assert evidence is not None
    assert evidence["p_chatwoot_conversation_id"] == str(CONVERSATION_ID)
    assert evidence["p_chatwoot_anchor_found"] is True
    assert evidence["p_chatwoot_can_reply"] is False
    assert evidence["p_chatwoot_inbound_after_anchor"] is False


def test_the_whatsapp_form_is_tried_after_the_canonical_one() -> None:
    # 52 + 10 no es el contacto de Chatwoot; 521 + 10 si: dos lecturas.
    _, requests = _evidence(_StepAuthority())

    conversation = f"/api/v1/accounts/{ACCOUNT_ID}/conversations/{CONVERSATION_ID}"
    assert requests.count(conversation) == 2


def test_the_conversation_of_another_person_gives_no_evidence() -> None:
    evidence, _ = _evidence(_StepAuthority(phone=OTHER_PHONE))

    assert evidence is None


def test_without_a_phone_the_check_fails_closed() -> None:
    evidence, _ = _evidence(_StepAuthority(phone=None))

    assert evidence is None


def test_a_legacy_runtime_keeps_its_allowlisted_jid() -> None:
    # Sin el runtime portable (Johanna), nada cambia: sin allowed_jid no pasa.
    authority = _StepAuthority()
    evidence, _ = _evidence(authority, portable=False)

    assert evidence is None
    assert authority.context_reads == 0


def test_the_final_check_reuses_the_phone_it_already_has() -> None:
    authority = _StepAuthority(phone=None)
    evidence, _ = _evidence(authority, buyer_phone=CASE_PHONE)

    assert evidence is not None
    assert authority.context_reads == 0
