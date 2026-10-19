"""El seguimiento del pago rechazado de ATT1: B2 o Z, 24 h despues de B1.

Contrato: ``docs/contracts/portable-payment-failure-followup-v1.md``. El paso
``payment_failure_followup`` es B2 (``att1_pago_rechazado_descuento_01``) si
B1 salio sin cupon y Z (``att1_ultima_renovacion_01``, la misma del carrito)
si salio con cupon; cual de los dos lo dice la base
(``get_payment_failure_followup_context_v1``, la prueba de SQL). Los dos llevan
el cupon 2 y nuestro link; sin el cupon valido a tiempo, o sin el link, cierra
sin mensaje.

La base y Chatwoot son los de ``test_cart_followups`` (Z del carrito); el
catalogo es la captura del inbox 11 del 19/10
(``chatwoot_message_templates_inbox_11_pago_rechazado_seguimiento_20261019.json``),
con B2 y Z aprobadas tal cual. La del 18/10 (la del carrito, sin B2) prueba que
sin B2 en el catalogo no sale Z en su lugar.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_cart_discount_first_touch import CARRITO_CATALOG, CODE, ISSUANCE_ID, LINK
from test_cart_followups import EVIDENCE, _StepAuthority
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
from test_payment_failure_b1_dispatch import CONFIG as VARIANTS
from test_payment_slip_reminders import _ReminderChatwoot

from bridge.messaging import ChatwootMessageSender, FinalMetaEffectGate
from bridge.payment_failure_followups import (
    FOLLOWUP_KIND_LAST_TOUCH,
    FOLLOWUP_KIND_SECOND_TOUCH,
    PAYMENT_FAILURE_FOLLOWUP_STEP,
    PaymentFailureFollowupConfig,
)
from bridge.supabase import (
    DeliveryAttempt,
    PaymentFailureFollowupContext,
    SupabaseError,
    SupabasePermanentError,
)
from bridge.worker import (
    APPROVED_TEMPLATE_DIRECT_UNSUPPORTED_ACTION,
    DurableDispatcher,
    _flow_trigger,
)

B2 = "att1_pago_rechazado_descuento_01"
Z = "att1_ultima_renovacion_01"
FOLLOWUP = PaymentFailureFollowupConfig(
    language="es_MX", second_touch_template=B2, last_touch_template=Z
)
SEGUIMIENTO_CATALOG: list[dict[str, Any]] = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "chatwoot_message_templates_inbox_11_pago_rechazado_seguimiento_20261019.json"
    ).read_text(encoding="utf-8")
)["message_templates"]


class _FollowupAuthority(_StepAuthority):
    """El seguimiento: el no_reply_review que inserto el motor al aceptarse B1."""

    def __init__(
        self,
        *,
        kind: str = FOLLOWUP_KIND_LAST_TOUCH,
        variant: str | None = "insufficient_funds",
        context_error: SupabaseError | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(PAYMENT_FAILURE_FOLLOWUP_STEP, offer_code="2uafw5bg", **kwargs)
        self.followup_context = PaymentFailureFollowupContext(
            followup_kind=kind, first_touch_variant=variant, payment_failure_count=1
        )
        self.context_error = context_error
        self.start_anchors: list[object] = []

    async def get_payment_failure_followup_context(
        self, **kwargs: object
    ) -> PaymentFailureFollowupContext:
        assert kwargs["action_id"] == self.action.action_id
        assert kwargs["lease_generation"] == self.action.lease_generation
        self.events.append("followup_context")
        if self.context_error is not None:
            raise self.context_error
        return self.followup_context

    async def mark_followup_request_started(self, **kwargs: object) -> DeliveryAttempt:
        self.start_anchors.append(kwargs.get("anchor_type"))
        return await super().mark_followup_request_started(**kwargs)


def _run(
    authority: _FollowupAuthority,
    tmp_path: Path,
    *,
    followup: PaymentFailureFollowupConfig | None = FOLLOWUP,
    catalog: list[dict[str, Any]] | None = None,
) -> _Chatwoot:
    # El seguimiento vuelve de Chatwoot como del AgentBot (medido el 19/10).
    templates = SEGUIMIENTO_CATALOG if catalog is None else catalog
    chatwoot = _ReminderChatwoot([*CATALOG, *templates])
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
        payment_failure_variants=VARIANTS,
        payment_failure_followup=followup,
    )

    async def evidence(**_: object) -> dict[str, object]:
        return dict(EVIDENCE)

    dispatcher._load_chatwoot_evidence = evidence  # type: ignore[method-assign]
    asyncio.run(dispatcher.dispatch_due(now=NOW))
    return chatwoot


def _body(name: str, values: list[str]) -> str:
    [template] = [t for t in SEGUIMIENTO_CATALOG if t["name"] == name]
    [body] = [c["text"] for c in template["components"] if c["type"] == "BODY"]
    for position, value in enumerate(values, start=1):
        body = body.replace("{{%d}}" % position, value)
    assert "{{" not in body
    return body


def _nothing_went_out(chatwoot: _Chatwoot, authority: _FollowupAuthority) -> dict[str, object]:
    assert chatwoot.posts("/messages") == []
    assert chatwoot.posts("/conversations") == []
    [closed] = authority.finalizations
    assert closed["outcome"] == "failed_before_request"
    return closed


# ------------------------------------------------------------------- Z


def test_z_goes_out_when_b1_carried_a_coupon(tmp_path: Path) -> None:
    authority = _FollowupAuthority(kind=FOLLOWUP_KIND_LAST_TOUCH)

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
    # La base decide primero; despues el cupon 2 y el link de esta accion. El
    # arranque es el del pago rechazado, contra su scope.
    assert authority.events.index("followup_context") < authority.events.index("coupon")
    assert authority.coupon_ordinals == [2]
    assert authority.start_anchors == ["payment_failure"]
    assert authority.issuance_finalizations == [{
        "issuance_id": ISSUANCE_ID,
        "status": "accepted_by_chatwoot",
        "chatwoot_message_id": 888,
        "failure_code": None,
        "now": FINAL_NOW,
    }]


@pytest.mark.parametrize("name", [B2, Z])
def test_the_captured_b2_and_z_have_the_buttons_of_the_decision(name: str) -> None:
    [template] = [t for t in SEGUIMIENTO_CATALOG if t["name"] == name]
    assert (template["status"], template["category"], template["language"]) == (
        "APPROVED", "MARKETING", "es_MX",
    )
    [buttons] = [c["buttons"] for c in template["components"] if c["type"] == "BUTTONS"]
    assert [(button["type"], button["text"]) for button in buttons] == [
        ("URL", "Usar mi descuento"), ("QUICK_REPLY", "No más mensajes"),
    ]
    assert buttons[0]["url"] == "https://pay.hotmart.com/{{1}}"


def test_the_z_of_the_follow_up_is_the_z_of_the_cart() -> None:
    [today] = [t for t in SEGUIMIENTO_CATALOG if t["name"] == Z]
    [cart] = [t for t in CARRITO_CATALOG if t["name"] == Z]
    assert today["components"] == cart["components"]


# ------------------------------------------------------------------ B2


def test_b2_goes_out_when_b1_carried_no_coupon(tmp_path: Path) -> None:
    authority = _FollowupAuthority(kind=FOLLOWUP_KIND_SECOND_TOUCH, variant="declined")

    chatwoot = _run(authority, tmp_path)

    values = ["Edith García Pérez", "Alimenta tu Tiroides", "mañana a las 11:00 a. m."]
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["content"] == _body(B2, values)
    assert message["template_params"] == {
        "name": B2,
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
    # El mismo cupon 2 y el mismo arranque que Z: solo cambia la plantilla.
    assert authority.coupon_ordinals == [2]
    assert authority.start_anchors == ["payment_failure"]


def test_without_b2_in_the_catalog_it_never_sends_z_instead(tmp_path: Path) -> None:
    # El catalogo del 18/10 (el del carrito) trae Z y no B2: la base dijo B2, el
    # despachador la busca, no la encuentra y no manda nada.
    authority = _FollowupAuthority(kind=FOLLOWUP_KIND_SECOND_TOUCH, variant="declined")

    chatwoot = _run(authority, tmp_path, catalog=CARRITO_CATALOG)

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == "approved_template_unavailable"
    assert closed["next_attempt_at"] is not None


# -------------------------------------------------------- el cupon 2


@pytest.mark.parametrize("coupon_status", [None, "reserved", "created", "create_unknown"])
def test_it_waits_eight_minutes_for_coupon_two(
    tmp_path: Path, coupon_status: str | None
) -> None:
    authority = _FollowupAuthority(coupon_status=coupon_status)

    chatwoot = _run(authority, tmp_path)

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == "recovery_coupon_not_ready"
    assert closed["next_attempt_at"] == (
        datetime.fromisoformat(FINAL_NOW) + timedelta(minutes=8)
    ).isoformat()
    assert "reserve_link" not in authority.events


def test_without_coupon_two_in_time_it_closes_without_a_message(tmp_path: Path) -> None:
    # Nunca sale una promesa de descuento sin el cupon valido detras.
    authority = _FollowupAuthority(coupon_status="created", attempt_number=3)

    closed = _nothing_went_out(_run(authority, tmp_path), authority)
    assert closed["reason_code"] == "payment_failure_followup_coupon_not_ready_in_time"
    assert closed["next_attempt_at"] is None


@pytest.mark.parametrize("coupon_status", ["create_failed", "expired", "redeemed"])
def test_a_dead_coupon_closes_without_waiting(tmp_path: Path, coupon_status: str) -> None:
    authority = _FollowupAuthority(coupon_status=coupon_status)

    closed = _nothing_went_out(_run(authority, tmp_path), authority)
    assert closed["reason_code"] == f"recovery_coupon_{coupon_status}"
    assert closed["next_attempt_at"] is None


@pytest.mark.parametrize(
    "outcome", ["missing_offer", "blocked_binding", "purchase_already_approved", "blocked_case"]
)
def test_without_our_link_it_closes_without_a_message(tmp_path: Path, outcome: str) -> None:
    authority = _FollowupAuthority(reserve_outcome=outcome)

    closed = _nothing_went_out(_run(authority, tmp_path), authority)
    assert closed["reason_code"] == f"recovery_checkout_{outcome}"
    assert closed["next_attempt_at"] is None


# ------------------------------------------------------ el contexto


def test_a_context_the_base_refuses_closes_without_reading_the_coupon(tmp_path: Path) -> None:
    authority = _FollowupAuthority(
        context_error=SupabasePermanentError(
            "rejected", reason="payment_failure_followup_action_required"
        )
    )

    closed = _nothing_went_out(_run(authority, tmp_path), authority)
    assert closed["reason_code"] == "payment_failure_followup_action_required"
    assert closed["next_attempt_at"] is None
    assert authority.coupon_ordinals == []


def test_a_context_that_cannot_be_read_retries(tmp_path: Path) -> None:
    authority = _FollowupAuthority(context_error=SupabaseError("HTTP 503"))

    closed = _nothing_went_out(_run(authority, tmp_path), authority)
    assert closed["reason_code"] == "payment_failure_followup_context_unavailable"
    assert closed["next_attempt_at"] is not None


# ------------------------------------------------------- sin la config


def test_without_the_config_the_step_closes_as_before(tmp_path: Path) -> None:
    authority = _FollowupAuthority()

    chatwoot = _run(authority, tmp_path, followup=None)

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == APPROVED_TEMPLATE_DIRECT_UNSUPPORTED_ACTION
    assert "followup_context" not in authority.events
    assert authority.coupon_ordinals == []


def test_the_step_belongs_to_the_refused_payment() -> None:
    assert _flow_trigger(_FollowupAuthority().action) == "payment_failure"


def test_the_followup_needs_the_direct_mode_and_the_variants() -> None:
    client = _Chatwoot().client()
    with pytest.raises(ValueError, match="payment failure variants"):
        DurableDispatcher(
            supabase=_FollowupAuthority(),  # type: ignore[arg-type]
            worker_id="att1-dispatcher",
            chatwoot=client,
            chatwoot_account_id=ACCOUNT_ID,
            chatwoot_inbox_id=INBOX_ID,
            sender=None,
            allowed_jid=None,
            payment_failure_followup=FOLLOWUP,
        )


# ------------------------------------------------------------ la config


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"language": " "}, "payment_failure_followup_language_invalid"),
        ({"second_touch_template": " "}, "payment_failure_followup_template_invalid"),
        ({"last_touch_template": B2}, "payment_failure_followup_templates_not_distinct"),
        ({"category": "UTILITY "}, "payment_failure_followup_category_invalid"),
    ],
)
def test_the_config_fails_closed(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(FOLLOWUP, **changes)


def test_the_config_finds_each_template_by_the_kind_the_base_gives() -> None:
    assert FOLLOWUP.template_for(FOLLOWUP_KIND_SECOND_TOUCH) == B2
    assert FOLLOWUP.template_for(FOLLOWUP_KIND_LAST_TOUCH) == Z
    assert FOLLOWUP.template_for("third_touch") is None
