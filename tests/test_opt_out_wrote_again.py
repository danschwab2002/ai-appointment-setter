"""Quien se dio de baja y vuelve a escribir por su cuenta (LAN-132 y F-036).

El caso real (ATT1, inbox 11): la persona de la conversacion 103 toco «No más
mensajes» el 12/10 a las 15:01Z. El 17/10 a las 07:41Z escribio «Envíame el
enlace»; como la 103 estaba resuelta, Chatwoot abrio la 257 sin etiquetas ni
agente, el bridge callo por la baja durable y nadie del equipo la vio. La 257
quedo admitida a las 07:42:06Z (att1-db, leido el 19/10). El 22/10 la 425 paso
por la derivacion de la v1.10.2.

Desde F-036 (decisiones D9 y D10 de Dan, 24/10), con manifiesto (ATT1, Maribel
y toda instancia nueva, que cargan el SOUL comun) el agente le contesta: la
entrada lleva ``opted_out_contact: true``, el SOUL dice contestar normal, sin
ofrecer seguimiento, y el link solo si lo pide. La baja sigue para todo lo que
sale de nosotros: la reconciliacion la vuelve a aplicar en cada mensaje, y una
baja nueva se registra y no se contesta. Si la conversacion la tiene el equipo
(derivada o pausada), la admision da ``blocked`` y el agente no corre.

Sin manifiesto (Johanna) sigue la derivacion de la v1.10.2: su SOUL no tiene la
regla. Los telefonos son del rango de ficcion.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

from test_webhook import (
    StubChatwootClient,
    StubInboundCommercialSupabase,
    StubOptOutSupabase,
    StubShadowProcessor,
    _post,
    _signed_headers,
)
from test_whatsapp_inbound_equivalence import (
    FORM_ID,
    PAYMENT_LINK_PROPOSAL,
    WA_ID,
    _blocked,
    _manifest_settings,
    _PaymentLinkSupabase,
    _process,
    _Supabase,
    _text_webhook,
)

from bridge.app import Settings, create_app

SECRET = "webhook-secret"
JID = "5210000000257@s.whatsapp.net"
ANSWER = {
    "decision": "ask_question",
    "qualification_status": "in_progress",
    "reason_code": "commercial_response",
    "reply": "El programa dura cuatro semanas.",
    "captured_fields": {},
    "missing_fields": [],
}


class _OptedOutSupabase(StubInboundCommercialSupabase):
    """La admision del entrante y una baja que ya estaba registrada."""

    def __init__(self, *, stopped: bool) -> None:
        super().__init__()
        self._opt_out = StubOptOutSupabase(stopped=stopped)

    async def has_chatwoot_opt_out_stop(self, **kwargs: object) -> bool:
        return await self._opt_out.has_chatwoot_opt_out_stop(**kwargs)

    async def reconcile_chatwoot_opt_out_stop(self, **kwargs: object) -> object:
        return await self._opt_out.reconcile_chatwoot_opt_out_stop(**kwargs)

    async def apply_chatwoot_inbound_opt_out(self, **kwargs: object) -> object:
        return await self._opt_out.apply_chatwoot_inbound_opt_out(**kwargs)

    async def claim_chatwoot_opt_out_projections(self, **kwargs: object) -> tuple[()]:
        return await self._opt_out.claim_chatwoot_opt_out_projections(**kwargs)

    @property
    def reconcile_calls(self) -> list[dict[str, object]]:
        return self._opt_out.reconcile_calls


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": SECRET,
        "allowed_jid": JID,
        "capture_dir": tmp_path / "captures",
        "max_age_seconds": 300,
        "agent_bot_id": 1,
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 9,
        "chatwoot_cut_b_admission_enabled": True,
        "chatwoot_cut_b_scope_key": "libre-de-ansiedad-inbound",
        "chatwoot_cut_b_scope_version": 2,
        "chatwoot_cut_b_agent_enabled": True,
        "automated_replies_enabled": True,
        "chatwoot_durable_opt_out_enabled": True,
        "chatwoot_opt_out_macro_id": 9,
        "opt_out_projection_worker_id": "opt-out-projection-test",
        "human_handoff_admission_enabled": True,
        "human_handoff_projection_enabled": True,
        "handoff_projection_policy_key": "lancemos-inbound-handoff",
        "handoff_projection_policy_version": 1,
        "human_handoff_projection_worker_id": "handoff-projection-test",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _procesar(
    tmp_path: Path, *, texto: str, stopped: bool = True, **overrides: object
) -> tuple[StubShadowProcessor, StubChatwootClient, _OptedOutSupabase]:
    payload = {
        "event": "message_created",
        "id": 4101,
        "content": texto,
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 9},
        "conversation": {
            "id": 257,
            "inbox_id": 9,
            "contact_inbox": {"source_id": JID},
        },
    }
    shadow = StubShadowProcessor({"decision": "reply", "reply": "Te paso el link."})
    chatwoot = StubChatwootClient(
        messages=[{
            "id": 4101,
            "created_at": 1792223260,
            "message_type": 0,
            "private": False,
            "content": texto,
            "sender": {"type": "contact", "id": 20},
        }]
    )
    supabase = _OptedOutSupabase(stopped=stopped)
    app = create_app(
        _settings(tmp_path, **overrides),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response = _post(app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="lan-132"))
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return shadow, chatwoot, supabase


def _context(shadow: StubShadowProcessor) -> dict[str, object]:
    [(_, context)] = shadow.calls
    return context


# ------------------------------------- con manifiesto: el agente contesta (F-036)


def test_an_opted_out_person_who_writes_again_gets_an_answer(tmp_path: Path) -> None:
    # La baja quedo bajo el wa_id (521…) y la persona escribe una pregunta en
    # una conversacion nueva. Antes: derivacion sin respuesta.
    supabase = _Supabase(identities=(FORM_ID,), stopped_user_ids=(WA_ID,))
    webhook, history = _text_webhook("¿Cuánto dura el programa?")

    chatwoot, shadow, _ = _process(
        tmp_path, supabase, webhook=webhook, history=history, proposal=ANSWER
    )

    # La baja sigue: se reconcilia en cada mensaje.
    assert [call["external_user_id"] for call in supabase.reconciliations] == [WA_ID]
    assert supabase.opt_outs == []
    # El agente corre con el dato de la baja y la respuesta sale.
    assert _context(shadow)["opted_out_contact"] is True
    [reply] = chatwoot.reply_calls
    assert reply["content"] == "El programa dura cuatro semanas."
    # No es una derivacion: ni pedido de derivacion ni pausa.
    assert supabase.handoff_calls == []
    assert chatwoot.calls == []


def test_an_opted_out_person_who_asks_for_the_link_receives_it(tmp_path: Path) -> None:
    # «Envíame el enlace», el texto de la 257. El link sale con la emision de
    # siempre; la base lo deja pasar porque el mensaje es posterior a la baja
    # (migracion 20261024000400, validate_opted_out_reply_issuance.mjs).
    supabase = _PaymentLinkSupabase(identities=(FORM_ID,), stopped_user_ids=(WA_ID,))
    webhook, history = _text_webhook("Envíame el enlace")

    chatwoot, shadow, _ = _process(
        tmp_path,
        supabase,
        settings=replace(_manifest_settings(tmp_path), payment_link_enabled=True),
        webhook=webhook,
        history=history,
        proposal=PAYMENT_LINK_PROPOSAL,
    )

    assert _context(shadow)["opted_out_contact"] is True
    [reservation] = supabase.candidate_calls
    assert reservation["external_user_id"] == FORM_ID
    [reply] = chatwoot.reply_calls
    assert "src=hermes" in str(reply["content"])
    assert supabase.handoff_calls == []


def test_a_new_opt_out_request_is_recorded_and_not_answered(tmp_path: Path) -> None:
    supabase = _Supabase(identities=(FORM_ID,), stopped_user_ids=(WA_ID,))
    webhook, history = _text_webhook("No me escriban más")

    chatwoot, shadow, _ = _process(
        tmp_path, supabase, webhook=webhook, history=history, proposal=ANSWER
    )

    [opt_out] = supabase.opt_outs
    assert opt_out["rule_key"] == "do_not_write_again"
    assert shadow.calls == []
    assert chatwoot.reply_calls == []
    assert supabase.handoff_calls == []


def test_when_the_team_has_the_conversation_the_agent_does_not_answer(
    tmp_path: Path,
) -> None:
    # Derivada o pausada por una persona: la admision da 'blocked'. La baja se
    # reconcilia y la conversacion sigue con el equipo.
    supabase = _blocked(identities=(FORM_ID,), stopped_user_ids=(WA_ID,))
    webhook, history = _text_webhook("Envíame el enlace")

    chatwoot, shadow, _ = _process(
        tmp_path, supabase, webhook=webhook, history=history, proposal=ANSWER
    )

    assert [call["external_user_id"] for call in supabase.reconciliations] == [WA_ID]
    assert shadow.calls == []
    assert chatwoot.reply_calls == []


def test_a_person_without_an_opt_out_does_not_get_the_marker(tmp_path: Path) -> None:
    supabase = _Supabase(identities=(FORM_ID,))
    webhook, history = _text_webhook("¿Cuánto dura el programa?")

    chatwoot, shadow, _ = _process(
        tmp_path, supabase, webhook=webhook, history=history, proposal=ANSWER
    )

    assert "opted_out_contact" not in _context(shadow)
    assert len(chatwoot.reply_calls) == 1


# -------------------------- sin manifiesto (Johanna): la derivacion de la v1.10.2


def test_without_a_manifest_an_opted_out_person_who_writes_again_goes_to_a_person(
    tmp_path: Path,
) -> None:
    shadow, chatwoot, supabase = _procesar(tmp_path, texto="Envíame el enlace")

    assert len(supabase.reconcile_calls) == 1  # la baja sigue
    assert shadow.calls == []  # el agente no contesta
    assert chatwoot.reply_calls == []  # a la persona no le llega nada
    [handoff] = supabase.handoff_calls
    assert handoff["reason_code"] == "commercial_exception"
    assert handoff["detail_reason_code"] == "opted_out_contact_wrote_again"
    assert chatwoot.calls == [(257, "automation_paused")]


def test_without_a_manifest_a_new_opt_out_request_is_not_handed_off(
    tmp_path: Path,
) -> None:
    shadow, chatwoot, supabase = _procesar(tmp_path, texto="No me escriban más")

    assert len(supabase.reconcile_calls) == 1
    assert supabase.handoff_calls == []
    assert chatwoot.reply_calls == []
    assert shadow.calls == []


def test_without_the_handoff_admission_nothing_changes(tmp_path: Path) -> None:
    shadow, chatwoot, supabase = _procesar(
        tmp_path, texto="Envíame el enlace", human_handoff_admission_enabled=False,
        human_handoff_projection_enabled=False,
    )

    assert len(supabase.reconcile_calls) == 1
    assert supabase.handoff_calls == []
    assert chatwoot.reply_calls == []
    assert shadow.calls == []


def test_a_person_without_an_opt_out_is_not_handed_off(tmp_path: Path) -> None:
    shadow, _, supabase = _procesar(tmp_path, texto="Envíame el enlace", stopped=False)

    assert supabase.reconcile_calls == []
    assert supabase.handoff_calls == []
    assert len(shadow.calls) == 1  # el camino de siempre
