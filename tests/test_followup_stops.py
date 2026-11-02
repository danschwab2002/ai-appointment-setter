"""Los frenos del seguimiento con cupon y del recordatorio de la ficha (E-263).

F-149 (despues de Z) y F-150 (un «no» claro, una ficha vigente), decisiones de
Dan del 2026-11-01 (D2 a D5 de
``documentacion/2026-11-01-tres-errores-de-mensajes-en-conversaciones-reales.md``).
Lo de la base (blocked_after_last_touch y blocked_pending_payment) lo prueba
``tests/sql/followup_engine/validate_followup_stops.mjs`` con las RPC reales;
aca va lo del bridge:

* el seguimiento con cupon no sale si la persona escribio un rechazo claro
  despues de nuestra ultima plantilla (``lead_declined``), solo con manifiesto;
* los dos resultados nuevos de la reserva son conocidos, no un claim_failed;
* cada freno se loguea una vez por conversacion (la medicion de 7 dias, D3);
* la lectura canonica de Chatwoot marca el «no» despues del ancla, y el
  recordatorio de la ficha (C3) cierra con ``lead_declined`` sin mandar nada.

Datos: la 21 de ATT1 capturada (``chatwoot_followup_candidate_inbox_11_20261010.json``,
la respuesta a la plantilla del carrito redactada en la captura) y la 194 de
Johanna (``chatwoot_followup_candidates_inbox_9_20260928.json``). El texto de la
persona se reemplaza por lo que escribieron de verdad en ATT1 #542 y #491 el
28/10 (sin nombres ni telefonos, ver ``tests/test_lead_decline.py``). La #491
entera (Z, la objecion, el rechazo) se arma sobre la 21: la captura no tiene un
Z; las marcas de plantilla son las que escribe el bridge.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from test_durable_dispatcher_approved_template import ACCOUNT_ID, INBOX_ID
from test_followup_discount import (
    ATT1_AGENT_LINK,
    ATT1_CANDIDATE,
    ATT1_LEAD_REPLIED_AT,
    HOUR,
    _att1_chatwoot,
    _att1_sweeper,
    _conversation,
    _FakeChatwoot,
    _FakeSupabase,
    _sweeper,
)
from test_payment_slip_first_touch import _dispatcher
from test_payment_slip_reminders import (
    C3_AT,
    CATALOG,
    EVIDENCE,
    WITH_STEPS,
    _nothing_went_out,
    _ReminderAuthority,
    _ReminderChatwoot,
)

from bridge.chatwoot import CanonicalConversationSnapshot, ChatwootClient
from bridge.followup_discount import FOLLOWUP_STOP_REASONS, evaluate_followup_candidate
from bridge.lead_decline import LEAD_DECLINED
from bridge.payment_slip_reminders import PAYMENT_SLIP_DUE_DAY_STEP
from bridge.supabase import FOLLOWUP_CLAIM_OUTCOMES, ChatwootAuthorityContext

ATT1_542 = "Hola buenas tardes. Cambie de opinión. Ya no quiero el curso.. gracias"
ATT1_491_PRICE = "Mil gracias pero si es ahorita muy caro para mi saludos"
ATT1_491_NO = "Gracias 🙏, será en otra ocasión Saludos"


def _att1_21(lead_text: str) -> tuple[dict, list[dict]]:
    """La 21 de ATT1 con lo que escribio la persona en lugar de lo redactado."""
    entry = copy.deepcopy(ATT1_CANDIDATE["conversation"])
    for message in entry["messages"]:
        if message["message_type"] == 0:
            message["content"] = lead_text
    return entry["conversation"], entry["messages"]


def _message(message_id: int, created_at: int, kind: str, content: str) -> dict:
    """Un mensaje con la forma de la API de Chatwoot de la 21."""
    if kind == "contact":
        return {"id": message_id, "created_at": created_at, "message_type": 0,
                "private": False, "content": content, "content_type": "text",
                "content_attributes": {}, "sender": {"type": "contact", "id": 1}}
    attributes = (
        {"recovery_followup_hash": "f" * 64}
        if kind == "template"
        else {"appointment_setter_reply_hash": "a" * 64}
    )
    return {"id": message_id, "created_at": created_at, "message_type": 1,
            "private": False, "content": content, "content_type": "text",
            "content_attributes": attributes, "sender": {"type": "agent_bot", "id": 99}}


def _att1_491() -> tuple[dict, list[dict]]:
    """La #491: Z, la objecion de precio, el link, el rechazo y la despedida."""
    details, messages = _att1_21("[redactado]")
    start = ATT1_LEAD_REPLIED_AT + 2 * HOUR
    messages += [
        _message(2770, start, "template", "Te reactivé tu 10 % de descuento por última vez."),
        _message(2771, start + 240, "contact", ATT1_491_PRICE),
        _message(2772, start + 300, "agent", f"Te entiendo. Te envío el enlace: {ATT1_AGENT_LINK}"),
        _message(2773, start + 1_380, "contact", ATT1_491_NO),
        _message(2774, start + 1_440, "agent", "No hay problema, que tengas un lindo día."),
    ]
    return details, messages


def _evaluate(details: dict, messages: list[dict], **options: object):
    newest = max(m["created_at"] for m in messages if m["message_type"] == 0)
    return evaluate_followup_candidate(
        details, messages, expected_inbox_id=11, now_epoch=newest + 30 * HOUR, **options
    )


# --- el criterio --------------------------------------------------------------


def test_the_redacted_capture_is_still_a_candidate() -> None:
    details, messages = _att1_21("[mensaje del lead redactado]")
    assert _evaluate(details, messages).candidate is not None


def test_542_said_no_after_our_template_and_gets_no_coupon() -> None:
    details, messages = _att1_21(ATT1_542)
    decision = _evaluate(details, messages)
    assert decision.candidate is None
    assert decision.skip_reason == LEAD_DECLINED


def test_491_said_no_after_z_and_gets_no_coupon() -> None:
    details, messages = _att1_491()
    assert _evaluate(details, messages).skip_reason == LEAD_DECLINED


def test_a_price_objection_alone_still_gets_the_coupon() -> None:
    details, messages = _att1_491()
    # Sin el rechazo final: la objecion de precio la atiende el agente.
    messages = [m for m in messages if m.get("content") != ATT1_491_NO]
    assert _evaluate(details, messages).candidate is not None


def test_a_no_before_our_last_template_does_not_count() -> None:
    # Dijo que no a A1, y despues de Z contesto que si: el no es viejo.
    details, messages = _att1_21("Ya no me interesa")
    start = ATT1_LEAD_REPLIED_AT + 2 * HOUR
    messages += [
        _message(2770, start, "template", "Te reactivé tu 10 % de descuento por última vez."),
        _message(2771, start + 240, "contact", "Hola, sí me interesa, ¿cómo pago?"),
        _message(2772, start + 300, "agent", f"Te envío el enlace: {ATT1_AGENT_LINK}"),
    ]
    assert _evaluate(details, messages).candidate is not None


def test_without_the_stop_the_criterion_is_what_it_was() -> None:
    details, messages = _att1_21(ATT1_542)
    assert _evaluate(details, messages, stop_on_lead_decline=False).candidate is not None


# --- el barredor --------------------------------------------------------------


def _att1_542_chatwoot() -> _FakeChatwoot:
    chatwoot = _att1_chatwoot()
    details, messages = _att1_21(ATT1_542)
    chatwoot.entries = [{"conversation": details, "messages": messages}]
    return chatwoot


def test_with_a_manifest_the_sweeper_does_not_claim_after_a_clear_no(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chatwoot = _att1_542_chatwoot()
    supabase = _FakeSupabase(issued_url=ATT1_AGENT_LINK)
    sweeper = _att1_sweeper(chatwoot, supabase, now=ATT1_LEAD_REPLIED_AT + 30 * HOUR)

    with caplog.at_level(logging.INFO, logger="bridge.followup_discount"):
        assert asyncio.run(sweeper.run_once()) == 0
        assert asyncio.run(sweeper.run_once()) == 0

    assert supabase.claims == []
    assert chatwoot.sent == []
    assert sweeper.last_scan_summary == "scanned=1 sent=0 lead_declined=1"
    # Una sola linea por conversacion para la medicion, con el numero y nada mas.
    stops = [r.getMessage() for r in caplog.records if "conversation_followup_stopped" in r.getMessage()]
    assert stops == ["conversation_followup_stopped reason=lead_declined conversation=21"]
    assert ATT1_542 not in caplog.text


def test_without_a_manifest_the_sweeper_is_what_it_was() -> None:
    # Johanna (sin manifiesto): el barredor no mira el texto.
    chatwoot = _FakeChatwoot(["194"])
    for message in chatwoot.entries[0]["messages"]:
        if message["message_type"] == 0:
            message["content"] = "Ya no me interesa"
    supabase = _FakeSupabase()
    assert asyncio.run(_sweeper(chatwoot, supabase, hours_after=25).run_once()) == 1
    assert len(supabase.claims) == 1


@pytest.mark.parametrize("outcome", ["blocked_after_last_touch", "blocked_pending_payment"])
def test_the_new_claim_outcomes_are_known_and_counted(
    outcome: str, caplog: pytest.LogCaptureFixture
) -> None:
    assert outcome in FOLLOWUP_CLAIM_OUTCOMES
    assert outcome in FOLLOWUP_STOP_REASONS
    chatwoot = _att1_chatwoot()
    supabase = _FakeSupabase(claim_outcome=outcome, issued_url=ATT1_AGENT_LINK)
    sweeper = _att1_sweeper(chatwoot, supabase, now=ATT1_LEAD_REPLIED_AT + 30 * HOUR)

    with caplog.at_level(logging.INFO, logger="bridge.followup_discount"):
        assert asyncio.run(sweeper.run_once()) == 0
        assert asyncio.run(sweeper.run_once()) == 0

    assert len(supabase.claims) == 2
    assert supabase.authorizations == []
    assert chatwoot.sent == []
    assert sweeper.last_scan_summary == f"scanned=1 sent=0 {outcome}=1"
    assert sweeper.last_scan_state == "healthy"
    stops = [r.getMessage() for r in caplog.records if "conversation_followup_stopped" in r.getMessage()]
    assert stops == [f"conversation_followup_stopped reason={outcome} conversation=21"]


def test_the_johanna_capture_is_unchanged_by_the_new_outcomes() -> None:
    details, messages = _conversation("194")
    assert evaluate_followup_candidate(
        details, messages, expected_inbox_id=9,
        now_epoch=max(m["created_at"] for m in messages if m["message_type"] == 0) + 25 * HOUR,
    ).candidate is not None


# --- la lectura canonica de Chatwoot ------------------------------------------


def _snapshot(messages: list[dict]) -> CanonicalConversationSnapshot:
    scoped_jid = "5215500000542@s.whatsapp.net"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"payload": messages})
        return httpx.Response(200, json={
            "id": 542, "inbox_id": 11, "status": "open", "can_reply": True,
            "labels": [], "meta": {"sender": {"identifier": scoped_jid}},
        })

    client = ChatwootClient(
        base_url="https://chatwoot.example.test", account_id=2, access_token="control-token",
        transport=httpx.MockTransport(handler),
    )
    return asyncio.run(client.get_canonical_conversation_snapshot(
        conversation_id=542, expected_inbox_id=11, anchor_message_id=40, expected_jid=scoped_jid,
    ))


def _c1_and_after(*after: tuple[int, int, str, str]) -> list[dict]:
    """C1 (el ancla de C3) y lo que vino despues, como en la #542."""
    c1 = {"id": 40, "created_at": 100, "message_type": 1, "private": False,
          "content": "Te dejo aquí tu ficha para pagar en OXXO.",
          "sender": {"type": "agent_bot", "id": 1}}
    rows = [c1]
    for message_id, message_type, sender_type, content in after:
        rows.append({"id": message_id, "created_at": 100 + message_id, "message_type": message_type,
                     "private": False, "content": content, "sender": {"type": sender_type, "id": 2}})
    return rows


def test_the_snapshot_sees_the_no_after_the_anchor() -> None:
    snapshot = _snapshot(_c1_and_after(
        (41, 0, "contact", ATT1_542),
        (42, 1, "agent_bot", "Gracias por avisarme, no hay ningún problema."),
        (43, 0, "contact", "Muchas gracias"),
    ))
    assert snapshot.inbound_after_anchor is True
    assert snapshot.declined_after_anchor is True


def test_the_snapshot_without_a_no_is_what_it_was() -> None:
    snapshot = _snapshot(_c1_and_after((41, 0, "contact", "¿Puedo pagar mañana?")))
    assert snapshot.inbound_after_anchor is True
    assert snapshot.declined_after_anchor is False


def test_a_no_before_the_anchor_does_not_count() -> None:
    messages = _c1_and_after((41, 0, "contact", "¿Puedo pagar mañana?"))
    messages.insert(0, {"id": 39, "created_at": 90, "message_type": 0, "private": False,
                        "content": "Ya no me interesa", "sender": {"type": "contact", "id": 2}})
    assert _snapshot(messages).declined_after_anchor is False


def test_a_private_note_is_not_the_person() -> None:
    messages = _c1_and_after()
    messages.append({"id": 41, "created_at": 141, "message_type": 0, "private": True,
                     "content": "Ya no me interesa", "sender": {"type": "contact", "id": 2}})
    assert _snapshot(messages).declined_after_anchor is False


# --- el recordatorio de la ficha (C3) -----------------------------------------


def _run_c3(tmp_path: Path, *, declined: bool) -> tuple[_ReminderChatwoot, _ReminderAuthority]:
    authority = _ReminderAuthority(PAYMENT_SLIP_DUE_DAY_STEP)
    chatwoot = _ReminderChatwoot(CATALOG)
    dispatcher = _dispatcher(authority, chatwoot, tmp_path, clock=C3_AT, template=WITH_STEPS)

    async def evidence(*, action: object, **_: object) -> dict[str, object]:
        # Lo que hace _load_chatwoot_evidence con la lectura de la #542 (ver la
        # prueba de abajo): la evidencia es la misma y la accion queda marcada.
        if declined:
            dispatcher._declined_after_anchor.add(action.action_id)  # type: ignore[attr-defined]
        return dict(EVIDENCE, p_chatwoot_inbound_after_anchor=declined)

    dispatcher._load_chatwoot_evidence = evidence  # type: ignore[method-assign]
    asyncio.run(dispatcher.dispatch_due(now=C3_AT))
    return chatwoot, authority


def test_c3_does_not_go_out_after_a_clear_no(tmp_path: Path) -> None:
    chatwoot, authority = _run_c3(tmp_path, declined=True)
    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == LEAD_DECLINED
    # No se reintenta: la accion cierra permanent_failed con ese motivo.
    assert closed["next_attempt_at"] is None


def test_c3_still_goes_out_when_the_person_answered_without_a_no(tmp_path: Path) -> None:
    # 20261019000800: contestar no corta el recordatorio; solo el «no» claro.
    chatwoot, _ = _run_c3(tmp_path, declined=False)
    assert len(chatwoot.posts("/conversations/200/messages")) == 1


def test_the_chatwoot_read_marks_the_action_and_keeps_the_evidence(tmp_path: Path) -> None:
    authority = _ReminderAuthority(PAYMENT_SLIP_DUE_DAY_STEP)
    dispatcher = _dispatcher(authority, _ReminderChatwoot(CATALOG), tmp_path, clock=C3_AT,
                             template=WITH_STEPS)

    async def context(**_: object) -> ChatwootAuthorityContext:
        return ChatwootAuthorityContext(
            action_id=authority.action.action_id, action_type="no_reply_review",
            chatwoot_account_id=ACCOUNT_ID, external_conversation_id=200,
            expected_inbox_id=INBOX_ID, anchor_external_message_id=40,
        )

    async def jids(**_: object) -> list[str | None]:
        return [None]

    snapshots: list[CanonicalConversationSnapshot] = []

    async def snapshot(**_: object) -> CanonicalConversationSnapshot:
        return snapshots[-1]

    authority.get_followup_chatwoot_context = context  # type: ignore[method-assign]
    dispatcher._case_conversation_jids = jids  # type: ignore[method-assign]
    dispatcher._chatwoot.get_canonical_conversation_snapshot = snapshot  # type: ignore[union-attr]
    base = CanonicalConversationSnapshot(
        conversation_id=200, inbox_id=INBOX_ID, status="open", can_reply=False, labels=(),
        anchor_found=True, inbound_after_anchor=True, human_activity_after_anchor=False,
        checkpoint_message_id=43, checkpoint_created_at=1_800_000_000,
    )

    snapshots.append(replace(base, declined_after_anchor=True))
    declined = asyncio.run(dispatcher._load_chatwoot_evidence(action=authority.action, now=C3_AT))
    assert authority.action.action_id in dispatcher._declined_after_anchor
    snapshots.append(base)
    answered = asyncio.run(dispatcher._load_chatwoot_evidence(action=authority.action, now=C3_AT))
    assert authority.action.action_id not in dispatcher._declined_after_anchor
    # La reevaluacion de la base recibe lo mismo de siempre: el «no» no viaja a la RPC.
    assert declined == answered
    assert declined is not None and set(declined) == set(EVIDENCE)
