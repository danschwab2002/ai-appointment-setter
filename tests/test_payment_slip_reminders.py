"""C2 y C3 de ATT1: los recordatorios del vencimiento de la ficha de pago.

Contrato: docs/contracts/portable-payment-slip-reminders-v1.md. Especificacion:
2b de «los flujos comerciales de ATT1 como tienen que quedar» (2026-10-17): C2
el dia anterior al vencimiento, C3 el dia del vencimiento a las 09:00 de la
persona, cada uno con su plantilla aprobada y la ficha en el boton.

Datos capturados, no inventados:

* ``hotmart_payment_slip_printed_att1_20261018.json``: la ficha 3 (OXXO de
  Mexico, generada el 12/10 a las 21:56 de CDMX, vence el jueves 15 segun su
  referencia), la misma que usan las pruebas de C1.
* ``chatwoot_message_templates_inbox_11_ficha_recordatorios_20261019.json``: el
  catalogo del inbox 11 con C1, C2 (MARKETING, cuatro variables) y C3 (UTILITY,
  tres), con el URL de la ficha primero y «No más mensajes» despues.

La lectura de la conversacion de Chatwoot antes de reevaluar
(``_load_chatwoot_evidence``) se reemplaza por la evidencia de la conversacion
200 de C1: no cambia con los recordatorios y la prueba de SQL
(validate_portable_payment_slip_reminders.mjs) cubre la reevaluacion real.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_durable_dispatcher_approved_template import FIXTURES, _Chatwoot
from test_payment_slip_first_touch import (
    BOUNDARY,
    MX_SLIP,
    TEMPLATE,
    _dispatcher,
    _SlipAuthority,
)

from bridge.chatwoot import ChatwootClient
from bridge.messaging import (
    FIRST_TOUCH_TEMPLATE_NOT_CONFIGURED,
    ChatwootMessageSender,
    PaymentSlipStepTemplate,
    WhatsAppTemplateConfig,
)
from bridge.payment_slip_reminders import (
    FOLLOWING_STEP_TRIGGERS,
    PAYMENT_SLIP_DUE_DAY_STEP,
    PAYMENT_SLIP_DUE_EVE_STEP,
    PAYMENT_SLIP_STEP_LATE,
    PAYMENT_SLIP_STEP_NOT_DUE,
    PAYMENT_SLIP_STEPS,
    payment_slip_step_wait,
)
from bridge.worker import APPROVED_TEMPLATE_DIRECT_UNSUPPORTED_ACTION, _flow_trigger

CATALOG: list[dict[str, Any]] = json.loads(
    (FIXTURES / "chatwoot_message_templates_inbox_11_ficha_recordatorios_20261019.json")
    .read_text(encoding="utf-8")
)["message_templates"]
C2 = "att1_ficha_vence_manana_01"
C3 = "att1_ficha_vence_hoy_01"
STEPS = (
    PaymentSlipStepTemplate(
        step_key=PAYMENT_SLIP_DUE_EVE_STEP,
        name=C2,
        category="MARKETING",
        body_parameters=("nombre", "tienda", "producto", "vencimiento"),
    ),
    PaymentSlipStepTemplate(
        step_key=PAYMENT_SLIP_DUE_DAY_STEP,
        name=C3,
        category="UTILITY",
        body_parameters=("nombre", "tienda", "producto"),
    ),
)
WITH_STEPS = replace(TEMPLATE, payment_slip_steps=STEPS)
MX = "America/Mexico_City"
DUE = date(2026, 10, 15)
# El momento de C2: el miercoles 14 a las 10:00 de CDMX (16:00Z); el de C3, el
# jueves 15 a las 09:00 (15:00Z). Mexico no tiene horario de verano desde 2022.
C2_AT = "2026-10-14T16:00:00+00:00"
C3_AT = "2026-10-15T15:00:00+00:00"


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _body(name: str, values: list[str]) -> str:
    [template] = [t for t in CATALOG if t["name"] == name]
    [body] = [c["text"] for c in template["components"] if c["type"] == "BODY"]
    for position, value in enumerate(values, start=1):
        body = body.replace("{{%d}}" % position, value)
    return body


# ---------------------------------------------------------------- el momento


@pytest.mark.parametrize(
    ("step_key", "now", "expected"),
    [
        # C2, compuesto apenas aceptado C1 (una hora despues de la ficha): espera
        # hasta el dia anterior al vencimiento a las 10:00.
        (PAYMENT_SLIP_DUE_EVE_STEP, "2026-10-13T04:57:00+00:00",
         _at(C2_AT) - _at("2026-10-13T04:57:00+00:00")),
        (PAYMENT_SLIP_DUE_EVE_STEP, C2_AT, None),
        # Ese dia mas tarde (una ventana que abrio tarde) todavia sale.
        (PAYMENT_SLIP_DUE_EVE_STEP, "2026-10-15T02:00:00+00:00", None),
        # El dia del vencimiento ya no: «vence mañana» seria falso.
        (PAYMENT_SLIP_DUE_EVE_STEP, "2026-10-15T06:00:00+00:00", PAYMENT_SLIP_STEP_LATE),
        (PAYMENT_SLIP_DUE_DAY_STEP, "2026-10-15T14:59:00+00:00", timedelta(minutes=1)),
        (PAYMENT_SLIP_DUE_DAY_STEP, C3_AT, None),
        (PAYMENT_SLIP_DUE_DAY_STEP, "2026-10-16T03:00:00+00:00", None),
        (PAYMENT_SLIP_DUE_DAY_STEP, "2026-10-16T06:00:00+00:00", PAYMENT_SLIP_STEP_LATE),
    ],
)
def test_each_reminder_waits_for_its_moment_and_is_late_the_day_after(
    step_key: str, now: str, expected: object
) -> None:
    wait = payment_slip_step_wait(
        PAYMENT_SLIP_STEPS[step_key], due_date=DUE, zone=MX, now=_at(now)
    )
    assert wait == expected


def test_the_moment_needs_an_aware_datetime() -> None:
    with pytest.raises(ValueError, match="payment_slip_naive_datetime"):
        payment_slip_step_wait(
            PAYMENT_SLIP_STEPS[PAYMENT_SLIP_DUE_EVE_STEP],
            due_date=DUE, zone=MX, now=datetime(2026, 10, 14, 10),
        )


def test_the_steps_are_the_two_reminders_of_the_slip() -> None:
    assert set(PAYMENT_SLIP_STEPS) == {"payment_slip_due_eve", "payment_slip_due_day"}
    assert {step.slot for step in PAYMENT_SLIP_STEPS.values()} == {
        "ficha_vence_manana", "ficha_vence_hoy",
    }
    assert FOLLOWING_STEP_TRIGGERS == {
        "payment_slip_due_eve": "payment_slip",
        "payment_slip_due_day": "payment_slip",
    }


# ------------------------------------------------------------ el dispatcher


class _ReminderAuthority(_SlipAuthority):
    """C2 o C3: el no_reply_review que inserto el motor al aceptarse el paso anterior."""

    def __init__(self, step_key: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.action = replace(
            self.action,
            action_type="no_reply_review",
            step_key=step_key,
            anchor_type="accepted_outbound_message",
            anchor_subject_internal_id="message-c1",
        )
        self.context = replace(self.context, action_type="no_reply_review", step_key=step_key)


class _ReminderChatwoot(_Chatwoot):
    """El inbox 11 de C1, y el seguimiento como lo devuelve Chatwoot: del AgentBot.

    Medido el 2026-10-19 en el Chatwoot de produccion: 389 mensajes salientes
    del inbox 11 en 7 dias tienen sender_type AgentBot (las respuestas del
    agente de ATT1), asi que el bridge de ATT1 tiene el token del bot.
    """

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = super().handler(request)
        body = json.loads(request.content) if request.content else None
        if (
            request.method == "POST"
            and request.url.path.endswith("/conversations/200/messages")
            and body is not None
            and "recovery_followup_hash" in body.get("content_attributes", {})
        ):
            payload = response.json()
            payload["sender"] = {"type": "agent_bot", "id": 99}
            return httpx.Response(200, json=payload)
        return response


EVIDENCE: dict[str, object] = {
    "p_chatwoot_conversation_id": "200",
    "p_chatwoot_checkpoint_message_id": "888",
    "p_chatwoot_checkpoint_at": "2026-10-13T04:57:00+00:00",
    "p_chatwoot_status": "open",
    # Despues de C1 (una plantilla sin respuesta) la ventana de 24 h esta
    # cerrada; la reevaluacion real de esto la cubre la prueba de SQL.
    "p_chatwoot_can_reply": False,
    "p_chatwoot_anchor_found": True,
    "p_chatwoot_automation_paused": False,
    "p_chatwoot_inbound_after_anchor": False,
    "p_chatwoot_human_activity_after_anchor": False,
}


def _run(
    authority: _SlipAuthority,
    tmp_path: Path,
    *,
    clock: str,
    template: WhatsAppTemplateConfig = WITH_STEPS,
) -> _ReminderChatwoot:
    chatwoot = _ReminderChatwoot(CATALOG)
    dispatcher = _dispatcher(authority, chatwoot, tmp_path, clock=clock, template=template)

    async def evidence(**_: object) -> dict[str, object]:
        return dict(EVIDENCE)

    dispatcher._load_chatwoot_evidence = evidence  # type: ignore[method-assign]
    asyncio.run(dispatcher.dispatch_due(now=clock))
    return chatwoot


def _nothing_went_out(chatwoot: _Chatwoot, authority: _SlipAuthority) -> dict[str, object]:
    assert chatwoot.posts("/messages") == []
    assert chatwoot.posts("/contacts") == []
    assert chatwoot.posts("/conversations") == []
    assert authority.started == []
    [closed] = authority.finalizations
    assert closed["outcome"] == "failed_before_request"
    return closed


def test_c2_composed_right_after_c1_waits_until_the_day_before_the_due_date(
    tmp_path: Path,
) -> None:
    authority = _ReminderAuthority(PAYMENT_SLIP_DUE_EVE_STEP)
    clock = "2026-10-13T04:57:00+00:00"

    chatwoot = _run(authority, tmp_path, clock=clock)

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == PAYMENT_SLIP_STEP_NOT_DUE
    # Un solo diferimiento, hasta el momento exacto: no gasta un reintento por
    # minuto.
    assert closed["next_attempt_at"] == C2_AT
    assert chatwoot.inbox_reads() == 1


def test_c2_goes_out_the_day_before_in_the_conversation_of_c1(tmp_path: Path) -> None:
    authority = _ReminderAuthority(PAYMENT_SLIP_DUE_EVE_STEP)

    chatwoot = _run(authority, tmp_path, clock=C2_AT)

    values = ["Edith García Pérez", "OXXO", "Alimenta tu Tiroides", "jueves 15 de octubre"]
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["content"] == _body(C2, values)
    assert message["template_params"] == {
        "name": C2,
        "category": "MARKETING",
        "language": "es_MX",
        "processed_params": {
            "body": {str(i): v for i, v in enumerate(values, start=1)},
            "buttons": [{"type": "url", "parameter": "f" * 48}],
        },
    }
    # No abre otra conversacion ni busca el contacto: es un seguimiento.
    assert chatwoot.posts("/contacts") == []
    assert [p for p in chatwoot.posts("/conversations") if "content" not in p] == []
    # Arranca por la RPC de la ficha, contra su scope.
    [started] = authority.started
    assert started["anchor_type"] == "payment_slip"
    assert started["pilot_boundary"] == BOUNDARY
    assert authority.events[-2:] == ["request_started", "accepted"]


def test_c3_goes_out_on_the_due_date_at_nine(tmp_path: Path) -> None:
    authority = _ReminderAuthority(PAYMENT_SLIP_DUE_DAY_STEP)

    chatwoot = _run(authority, tmp_path, clock=C3_AT)

    values = ["Edith García Pérez", "OXXO", "Alimenta tu Tiroides"]
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["content"] == _body(C3, values)
    assert message["template_params"]["name"] == C3
    assert message["template_params"]["category"] == "UTILITY"
    assert message["template_params"]["processed_params"] == {
        "body": {str(i): v for i, v in enumerate(values, start=1)},
        "buttons": [{"type": "url", "parameter": "f" * 48}],
    }


def test_c3_before_nine_waits_and_the_day_after_does_not_go_out(tmp_path: Path) -> None:
    early = _ReminderAuthority(PAYMENT_SLIP_DUE_DAY_STEP)
    closed = _nothing_went_out(
        _run(early, tmp_path / "early", clock="2026-10-15T14:00:00+00:00"), early
    )
    assert closed["reason_code"] == PAYMENT_SLIP_STEP_NOT_DUE
    assert closed["next_attempt_at"] == C3_AT

    late = _ReminderAuthority(PAYMENT_SLIP_DUE_DAY_STEP)
    closed = _nothing_went_out(
        _run(late, tmp_path / "late", clock="2026-10-16T15:00:00+00:00"), late
    )
    # Al dia siguiente la ficha ya vencio: no sale, y no se reintenta.
    assert closed["reason_code"] == "payment_slip_expired"
    assert closed["next_attempt_at"] is None


def test_c2_on_the_due_date_is_late_and_does_not_retry(tmp_path: Path) -> None:
    authority = _ReminderAuthority(PAYMENT_SLIP_DUE_EVE_STEP)

    chatwoot = _run(authority, tmp_path, clock="2026-10-15T16:00:00+00:00")

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == PAYMENT_SLIP_STEP_LATE
    assert closed["next_attempt_at"] is None


def test_a_reminder_without_its_template_closes_without_a_message(tmp_path: Path) -> None:
    authority = _ReminderAuthority(PAYMENT_SLIP_DUE_DAY_STEP)
    only_c2 = replace(TEMPLATE, payment_slip_steps=STEPS[:1])

    chatwoot = _run(authority, tmp_path, clock=C3_AT, template=only_c2)

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == FIRST_TOUCH_TEMPLATE_NOT_CONFIGURED
    assert closed["next_attempt_at"] is None


def test_a_following_step_of_another_flow_still_closes_unsupported(tmp_path: Path) -> None:
    # Un no_reply_review del carrito con A2 y Z apagados (el dispatcher de esta
    # prueba no tiene cart_followups): cierra como antes.
    authority = _ReminderAuthority("cart_second_touch", anchor_type="cart_abandonment")

    chatwoot = _run(authority, tmp_path, clock=C2_AT)

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == APPROVED_TEMPLATE_DIRECT_UNSUPPORTED_ACTION


def test_the_flow_of_an_action_comes_from_its_anchor_or_from_its_step() -> None:
    first = _SlipAuthority().action
    reminder = _ReminderAuthority(PAYMENT_SLIP_DUE_EVE_STEP).action
    unknown = replace(reminder, step_key="some_undeclared_step")
    payment_failure = replace(first, anchor_type="payment_failure")

    assert _flow_trigger(first) == "payment_slip"
    assert _flow_trigger(reminder) == "payment_slip"
    # Un paso siguiente que nadie declaro conserva el ancla, como antes.
    assert _flow_trigger(unknown) == "accepted_outbound_message"
    # El primer toque de otro flujo sigue decidiendo por su ancla.
    assert _flow_trigger(payment_failure) == "payment_failure"


# ------------------------------------------------------------ la config


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"payment_slip_name": None, "payment_slip_category": None,
          "payment_slip_body_parameters": None}, "payment_slip_steps_without_template"),
        ({"payment_slip_steps": (STEPS[0], STEPS[0])}, "duplicate_payment_slip_step"),
        ({"payment_slip_steps": [STEPS[0]]}, "invalid_payment_slip_steps"),
    ],
)
def test_the_reminder_templates_fail_closed(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(WITH_STEPS, **changes)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"category": "AUTHENTICATION"}, "invalid_payment_slip_step_category"),
        ({"name": " "}, "invalid_payment_slip_step_template"),
        ({"body_parameters": ("nombre", "cupon")}, "invalid_template_body_parameters"),
        ({"body_parameters": ()}, "invalid_template_body_parameters"),
    ],
)
def test_a_reminder_template_fails_closed(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(STEPS[0], **changes)


def test_the_config_finds_each_reminder_by_its_step() -> None:
    assert WITH_STEPS.payment_slip_step(PAYMENT_SLIP_DUE_EVE_STEP) == STEPS[0]
    assert WITH_STEPS.payment_slip_step(PAYMENT_SLIP_DUE_DAY_STEP) == STEPS[1]
    assert WITH_STEPS.payment_slip_step("payment_slip_first_contact") is None
    assert TEMPLATE.payment_slip_step(PAYMENT_SLIP_DUE_EVE_STEP) is None


# ------------------------------------------------------------ el envio


class _FollowupChatwoot:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        return httpx.Response(200, json={
            "id": 901, "conversation_id": 200, "message_type": 1, "private": False,
            "content": body["content"], "content_attributes": body["content_attributes"],
            "sender": {"type": "agent_bot", "id": 99},
        })

    def sender(self, template: WhatsAppTemplateConfig) -> ChatwootMessageSender:
        client = ChatwootClient(
            base_url="https://chatwoot.test", account_id=1, access_token="control-token",
            agent_bot_access_token="bot-token", agent_bot_id=99,
            transport=httpx.MockTransport(self.handler),
        )
        return ChatwootMessageSender(
            chatwoot=client, inbox_id=11, allowed_jid=None,
            dynamic_recipient_enabled=True, template=template,
        )


def test_the_followup_sends_the_template_it_is_given_without_a_followup_template() -> None:
    chatwoot = _FollowupChatwoot()
    params = {"name": C3, "category": "UTILITY", "language": "es_MX",
              "processed_params": {"body": {"1": "Edith"}}}
    # ATT1 no tiene plantilla de seguimiento (followup_name = None).
    result = asyncio.run(chatwoot.sender(TEMPLATE).send_followup(
        conversation_id=200, phone="5215512345678", content="Hola, Edith.",
        delivery_id="attempt-c3", template_params=params,
    ))

    assert result.status == "sent"
    [body] = chatwoot.bodies
    assert body["template_params"] == params


def test_without_template_params_the_followup_is_what_it_was() -> None:
    chatwoot = _FollowupChatwoot()

    result = asyncio.run(chatwoot.sender(TEMPLATE).send_followup(
        conversation_id=200, phone="5215512345678", content="Hola.", delivery_id="attempt-x",
    ))

    assert result.status == "blocked"
    assert result.reason == "followup_template_disabled"
    assert chatwoot.bodies == []


def test_the_slip_of_the_fixture_is_the_one_of_the_dates_above() -> None:
    # La ficha 3: generada el 12/10 a las 21:56 de CDMX, vence el 15 por su
    # referencia (digitos 19 a 26).
    barcode = MX_SLIP["data"]["purchase"]["payment"]["billet_barcode"]
    assert barcode[18:26] == "20261015"
    generated = datetime.fromtimestamp(MX_SLIP["data"]["purchase"]["order_date"] / 1000, tz=UTC)
    assert generated.astimezone().date() <= DUE
