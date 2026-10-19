"""A2 y Z del carrito de ATT1: el segundo y el ultimo toque.

Contrato: ``docs/contracts/portable-cart-followups-v1.md``. A2
(``cart_second_touch``) sale 24 h despues de A1 con ``att1_carrito_dudas_01``,
sin link; Z (``cart_last_touch``) sale 72 h despues de A1 con
``att1_ultima_renovacion_01``, el cupon 2 y nuestro link en el boton. Ninguno
cae a otra plantilla: un paso que no se puede armar cierra sin mensaje.

La base y Chatwoot son los de ``test_cart_discount_first_touch`` (A1); el
catalogo es la captura del inbox 11 del 18/10
(``chatwoot_message_templates_inbox_11_carrito_20261018.json``), que ya trae
las dos plantillas aprobadas.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_cart_discount_first_touch import (
    CARRITO_CATALOG,
    CODE,
    CONFIG,
    ISSUANCE_ID,
    LINK,
    _CartAuthority,
)
from test_durable_dispatcher_approved_template import (
    ACCOUNT_ID,
    ALLY,
    BOUNDARY,
    CATALOG,
    FINAL_NOW,
    INBOX_ID,
    NOW,
    TEMPLATE,
    _Chatwoot,
)
from test_payment_slip_reminders import _ReminderChatwoot

from bridge.cart_followups import (
    CART_LAST_TOUCH_STEP,
    CART_SECOND_TOUCH_STEP,
    CartFollowupConfig,
    is_cart_renewal_request,
)
from bridge.followup_steps import FOLLOWING_STEP_TRIGGERS, _join
from bridge.messaging import ChatwootMessageSender, FinalMetaEffectGate
from bridge.worker import (
    APPROVED_TEMPLATE_DIRECT_UNSUPPORTED_ACTION,
    DurableDispatcher,
    _flow_trigger,
)

A2 = "att1_carrito_dudas_01"
Z = "att1_ultima_renovacion_01"
FOLLOWUPS = CartFollowupConfig(
    language="es_MX", second_touch_template=A2, last_touch_template=Z
)

# Despues de A1 (una plantilla sin respuesta) la ventana de 24 h esta cerrada;
# que la reevaluacion no lo pause es de la base (la prueba de SQL).
EVIDENCE: dict[str, object] = {
    "p_chatwoot_conversation_id": "200",
    "p_chatwoot_checkpoint_message_id": "888",
    "p_chatwoot_checkpoint_at": "2026-09-30T14:57:00+00:00",
    "p_chatwoot_status": "open",
    "p_chatwoot_can_reply": False,
    "p_chatwoot_anchor_found": True,
    "p_chatwoot_automation_paused": False,
    "p_chatwoot_inbound_after_anchor": False,
    "p_chatwoot_human_activity_after_anchor": False,
}


class _StepAuthority(_CartAuthority):
    """A2 o Z: el no_reply_review que inserto el motor al aceptarse el toque anterior."""

    def __init__(self, step_key: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.action = replace(
            self.action,
            action_type="no_reply_review",
            step_key=step_key,
            anchor_type="accepted_outbound_message",
            anchor_subject_internal_id="message-a1",
        )
        self.context = replace(
            self.context, action_type="no_reply_review", step_key=step_key
        )
        self.coupon_ordinals: list[object] = []

    async def get_recovery_coupon_for_case(self, **kwargs: object) -> Any:
        self.coupon_ordinals.append(kwargs.get("ordinal"))
        return await super().get_recovery_coupon_for_case(**kwargs)


def _dispatcher(
    authority: _StepAuthority,
    chatwoot: _Chatwoot,
    tmp_path: Path,
    *,
    followups: CartFollowupConfig | None = FOLLOWUPS,
) -> DurableDispatcher:
    client = chatwoot.client()
    dispatcher = DurableDispatcher(
        supabase=authority,  # type: ignore[arg-type]
        worker_id="att1-dispatcher",
        chatwoot=client,
        chatwoot_account_id=ACCOUNT_ID,
        chatwoot_inbox_id=INBOX_ID,
        sender=ChatwootMessageSender(
            chatwoot=client,
            inbox_id=INBOX_ID,
            allowed_jid=None,
            dynamic_recipient_enabled=True,
            template=TEMPLATE,
        ),
        allowed_jid=None,
        commercial_ally_config=ALLY,
        portable_recipient_enabled=True,
        pilot_boundary=BOUNDARY,
        clock=lambda: FINAL_NOW,
        final_meta_effect_gate=FinalMetaEffectGate(
            enabled=True, evidence_dir=tmp_path / "meta-effects"
        ),
        waba_template=TEMPLATE,
        approved_template_direct=True,
        cart_discount=CONFIG,
        cart_followups=followups,
    )

    async def evidence(**_: object) -> dict[str, object]:
        return dict(EVIDENCE)

    dispatcher._load_chatwoot_evidence = evidence  # type: ignore[method-assign]
    return dispatcher


def _run(authority: _StepAuthority, tmp_path: Path, **kwargs: Any) -> _Chatwoot:
    # El seguimiento vuelve de Chatwoot como del AgentBot (medido el 19/10).
    chatwoot = _ReminderChatwoot([*CATALOG, *CARRITO_CATALOG])
    asyncio.run(_dispatcher(authority, chatwoot, tmp_path, **kwargs).dispatch_due(now=NOW))
    return chatwoot


def _body(name: str, values: list[str]) -> str:
    [template] = [t for t in CARRITO_CATALOG if t["name"] == name]
    [body] = [c["text"] for c in template["components"] if c["type"] == "BODY"]
    for position, value in enumerate(values, start=1):
        body = body.replace("{{%d}}" % position, value)
    return body


def _nothing_went_out(chatwoot: _Chatwoot, authority: _StepAuthority) -> dict[str, object]:
    assert chatwoot.posts("/messages") == []
    assert chatwoot.posts("/conversations") == []
    [closed] = authority.finalizations
    assert closed["outcome"] == "failed_before_request"
    return closed


# ------------------------------------------------------------------ A2


def test_a2_goes_out_in_the_conversation_of_a1_without_a_link(tmp_path: Path) -> None:
    authority = _StepAuthority(CART_SECOND_TOUCH_STEP)

    chatwoot = _run(authority, tmp_path)

    values = ["Edith García Pérez", "Alimenta tu Tiroides"]
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["content"] == _body(A2, values)
    assert message["template_params"] == {
        "name": A2,
        "category": "MARKETING",
        "language": "es_MX",
        "processed_params": {"body": {"1": values[0], "2": values[1]}},
    }
    assert authority.events == [
        "reevaluate", "reserve", "reevaluate", "request_started", "accepted",
    ]
    # A2 no lee cupones ni emite links.
    assert authority.coupon_ordinals == []
    assert "reserve_link" not in authority.events
    assert authority.issuance_finalizations == []


def test_a2_without_its_template_closes_without_a_message(tmp_path: Path) -> None:
    authority = _StepAuthority(CART_SECOND_TOUCH_STEP)

    chatwoot = _run(
        authority, tmp_path, followups=replace(FOLLOWUPS, second_touch_template=None)
    )

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == "first_touch_template_not_configured"
    assert closed["next_attempt_at"] is None


def test_a2_with_a_template_that_is_not_in_the_catalog_retries(tmp_path: Path) -> None:
    authority = _StepAuthority(CART_SECOND_TOUCH_STEP)

    chatwoot = _run(
        authority,
        tmp_path,
        followups=replace(FOLLOWUPS, second_touch_template="att1_no_existe_01"),
    )

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == "approved_template_unavailable"
    assert closed["next_attempt_at"] is not None


# ------------------------------------------------------------------- Z


def test_z_goes_out_with_coupon_two_and_our_link(tmp_path: Path) -> None:
    authority = _StepAuthority(CART_LAST_TOUCH_STEP)

    chatwoot = _run(authority, tmp_path)

    # El telefono del arnes es de EE. UU.: el cupon vence el 01/10 a las 15:00Z,
    # las 11:00 en Nueva York.
    values = ["Edith García Pérez", "Alimenta tu Tiroides", "mañana a las 11:00 a. m."]
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["content"] == _body(Z, values)
    assert message["template_params"] == {
        "name": Z,
        "category": "MARKETING",
        "language": "es_MX",
        "processed_params": {
            "body": {str(i): v for i, v in enumerate(values, start=1)},
            "buttons": [{
                "type": "url",
                "parameter": LINK.removeprefix("https://pay.hotmart.com/")
                + "&offDiscount=" + CODE,
            }],
        },
    }
    assert authority.coupon_ordinals == [2]
    assert authority.issuance_finalizations == [{
        "issuance_id": ISSUANCE_ID,
        "status": "accepted_by_chatwoot",
        "chatwoot_message_id": 888,
        "failure_code": None,
        "now": FINAL_NOW,
    }]


@pytest.mark.parametrize("coupon_status", [None, "reserved", "created", "create_unknown"])
@pytest.mark.parametrize("attempt_number", [1, 2])
def test_z_waits_eight_minutes_for_coupon_two(
    tmp_path: Path, coupon_status: str | None, attempt_number: int
) -> None:
    authority = _StepAuthority(
        CART_LAST_TOUCH_STEP, coupon_status=coupon_status, attempt_number=attempt_number
    )

    chatwoot = _run(authority, tmp_path)

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == "recovery_coupon_not_ready"
    assert closed["next_attempt_at"] == (
        datetime.fromisoformat(FINAL_NOW) + timedelta(minutes=8)
    ).isoformat()
    assert "reserve_link" not in authority.events


def test_z_without_coupon_two_in_time_closes_without_a_message(tmp_path: Path) -> None:
    # No hay plantilla de Z sin descuento: Z no sale sin el cupon que promete.
    authority = _StepAuthority(CART_LAST_TOUCH_STEP, coupon_status="created", attempt_number=3)

    chatwoot = _run(authority, tmp_path)

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == "cart_last_touch_coupon_not_ready_in_time"
    assert closed["next_attempt_at"] is None


def test_z_past_its_thirty_minutes_does_not_wait_again(tmp_path: Path) -> None:
    # Vencio 25 minutos antes: esperar otros 8 lo pasaria de los 30.
    authority = _StepAuthority(
        CART_LAST_TOUCH_STEP,
        coupon_status="created",
        due_at=(datetime.fromisoformat(FINAL_NOW) - timedelta(minutes=25)).isoformat(),
    )

    closed = _nothing_went_out(_run(authority, tmp_path), authority)
    assert closed["reason_code"] == "cart_last_touch_coupon_not_ready_in_time"


@pytest.mark.parametrize("coupon_status", ["create_failed", "expired", "redeemed"])
def test_z_with_a_dead_coupon_closes_without_waiting(
    tmp_path: Path, coupon_status: str
) -> None:
    authority = _StepAuthority(CART_LAST_TOUCH_STEP, coupon_status=coupon_status)

    closed = _nothing_went_out(_run(authority, tmp_path), authority)
    assert closed["reason_code"] == f"recovery_coupon_{coupon_status}"
    assert closed["next_attempt_at"] is None


@pytest.mark.parametrize(
    "outcome", ["missing_offer", "blocked_binding", "purchase_already_approved", "blocked_case"]
)
def test_z_without_our_link_closes_without_a_message(tmp_path: Path, outcome: str) -> None:
    authority = _StepAuthority(CART_LAST_TOUCH_STEP, reserve_outcome=outcome)

    closed = _nothing_went_out(_run(authority, tmp_path), authority)
    assert closed["reason_code"] == f"recovery_checkout_{outcome}"
    assert closed["next_attempt_at"] is None


def test_z_without_its_template_does_not_read_the_coupon(tmp_path: Path) -> None:
    authority = _StepAuthority(CART_LAST_TOUCH_STEP)

    chatwoot = _run(authority, tmp_path, followups=replace(FOLLOWUPS, last_touch_template=None))

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == "first_touch_template_not_configured"
    assert authority.coupon_ordinals == []


# ------------------------------------------------------- sin la config


@pytest.mark.parametrize("step_key", [CART_SECOND_TOUCH_STEP, CART_LAST_TOUCH_STEP])
def test_without_the_config_the_steps_close_as_before(tmp_path: Path, step_key: str) -> None:
    authority = _StepAuthority(step_key)

    chatwoot = _run(authority, tmp_path, followups=None)

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == APPROVED_TEMPLATE_DIRECT_UNSUPPORTED_ACTION
    assert authority.coupon_ordinals == []


def test_an_undeclared_step_of_the_cart_still_closes_unsupported(tmp_path: Path) -> None:
    authority = _StepAuthority("cart_fourth_touch")

    closed = _nothing_went_out(_run(authority, tmp_path), authority)
    assert closed["reason_code"] == APPROVED_TEMPLATE_DIRECT_UNSUPPORTED_ACTION


def test_the_followups_need_the_direct_mode() -> None:
    client = _Chatwoot().client()
    with pytest.raises(ValueError, match="direct mode"):
        DurableDispatcher(
            supabase=_CartAuthority(),  # type: ignore[arg-type]
            worker_id="att1-dispatcher",
            chatwoot=client,
            chatwoot_account_id=ACCOUNT_ID,
            chatwoot_inbox_id=INBOX_ID,
            sender=None,
            allowed_jid=None,
            cart_followups=FOLLOWUPS,
        )


# ------------------------------------------------------ los pasos y la config


def test_the_steps_of_every_flow_are_joined_by_their_key() -> None:
    assert dict(FOLLOWING_STEP_TRIGGERS) == {
        "payment_slip_due_eve": "payment_slip",
        "payment_slip_due_day": "payment_slip",
        "cart_second_touch": "cart_abandonment",
        "cart_last_touch": "cart_abandonment",
    }
    with pytest.raises(ValueError, match="duplicate_following_step:x"):
        _join({"x": "a"}, {"x": "b"})


@pytest.mark.parametrize("step_key", [CART_SECOND_TOUCH_STEP, CART_LAST_TOUCH_STEP])
def test_a_cart_step_belongs_to_the_cart(step_key: str) -> None:
    assert _flow_trigger(_StepAuthority(step_key).action) == "cart_abandonment"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"language": " "}, "cart_followup_language_invalid"),
        ({"second_touch_template": " "}, "cart_followup_template_invalid"),
        ({"category": "AUTHENTICATION"}, "cart_followup_category_invalid"),
        ({"defer_attempts": -1}, "cart_followup_defer_invalid"),
        ({"defer_delay": timedelta(0)}, "cart_followup_defer_invalid"),
    ],
)
def test_the_config_fails_closed(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(FOLLOWUPS, **changes)


def test_the_config_finds_each_template_by_its_step() -> None:
    assert FOLLOWUPS.template_for(CART_SECOND_TOUCH_STEP) == A2
    assert FOLLOWUPS.template_for(CART_LAST_TOUCH_STEP) == Z
    assert FOLLOWUPS.template_for("first_contact") is None


# ------------------------------------------------------- «Quiero mi 10 %»


def test_the_button_is_the_one_of_the_approved_a2() -> None:
    [template] = [t for t in CARRITO_CATALOG if t["name"] == A2]
    buttons = [
        button["text"]
        for component in template["components"]
        if component["type"] == "BUTTONS"
        for button in component["buttons"]
    ]
    assert buttons == ["Quiero mi 10 %", "No más mensajes"]
    assert is_cart_renewal_request(buttons[0])
    assert not is_cart_renewal_request(buttons[1])


@pytest.mark.parametrize(
    "text",
    ["Quiero mi 10 %", "quiero mi 10%", "  QUIERO  MI 10 % ", "«Quiero mi 10 %»", "Quiero mi 10 %!"],
)
def test_the_renewal_request_is_the_button_in_any_case_or_spacing(text: str) -> None:
    assert is_cart_renewal_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "Quiero mi 10 % pero no tengo tarjeta",
        "quiero mi 20 %",
        "Hola, quiero mi 10 %",
        "quiero mi descuento",
        "",
        None,
        "quiero mi 10 %" + " " * 300,
    ],
)
def test_anything_else_is_a_message_for_the_agent(text: object) -> None:
    assert not is_cart_renewal_request(text)
