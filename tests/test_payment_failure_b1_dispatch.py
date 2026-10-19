"""B1 del pago rechazado de ATT1: la variante, el link y el cupon, por el despachador.

Decision de Dan del 18/10 (los flujos de ATT1, flujo 1, y «el pago rechazado le
gana al primer contacto y B1-banco sale sin guia»): a los 15 minutos del ultimo
rechazo sale una sola plantilla, la primera que corresponde entre B1-cupon,
B1-dos-rechazos, B1-saldo y B1-banco, con el boton al checkout con nuestro link
y, cuando la variante lo lleva, el cupon. Como A1, B1 nunca se pierde por el
cupon o el link: sale la plantilla de hoy.

La base y Chatwoot son los de ``test_durable_dispatcher_approved_template``;
el catalogo suma las cuatro B1 aprobadas el 18/10 tal como las sincronizo
Chatwoot (``chatwoot_message_templates_inbox_11_pago_rechazado_20261019.json``,
capturado de produccion el 19/10).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_durable_dispatcher_approved_template import (
    ACCOUNT_ID,
    ALLY,
    BOUNDARY,
    CATALOG,
    FINAL_NOW,
    FIXTURES,
    INBOX_ID,
    NOW,
    PAYMENT_FAILURE_TEMPLATE,
    TEMPLATE,
    _Authority,
    _Chatwoot,
    _expected,
)

from bridge.messaging import ChatwootMessageSender, FinalMetaEffectGate
from bridge.payment_failure_variant import (
    VARIANT_COUPON,
    VARIANT_DECLINED,
    VARIANT_INSUFFICIENT_FUNDS,
    VARIANT_TWO_REFUSALS,
    PaymentFailureVariantsConfig,
)
from bridge.supabase import (
    PaymentFailureSendContext,
    RecoveryCheckoutAuthorization,
    RecoveryCheckoutReservation,
    RecoveryCouponForCase,
    SupabaseError,
    SupabasePermanentError,
)
from bridge.worker import DurableDispatcher

B1_CATALOG: list[dict[str, Any]] = json.loads(
    (FIXTURES / "chatwoot_message_templates_inbox_11_pago_rechazado_20261019.json").read_text(
        encoding="utf-8"
    )
)["message_templates"]
NAMES = {
    VARIANT_COUPON: "att1_pago_rechazado_cupon_01",
    VARIANT_TWO_REFUSALS: "att1_pago_rechazado_dos_veces_01",
    VARIANT_INSUFFICIENT_FUNDS: "att1_pago_rechazado_saldo_01",
    VARIANT_DECLINED: "att1_pago_rechazado_banco_01",
}
CONFIG = PaymentFailureVariantsConfig(templates=NAMES, language="es_MX")
OWN_CODE = "ATT1SALDO0000001"
OTHER_CODE = "ATT1CARRITO00001"
ISSUANCE_ID = "6f1d0c2a-7a4b-4c4e-9d1e-0b6a2f5c8e92"
LINK = (
    "https://pay.hotmart.com/D98014973Y?off=2uafw5bg&checkoutMode=10"
    "&src=hermes&sck=hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ1"
)
# El ultimo rechazo, 15 minutos antes de la hora de la accion (NOW = 15:00Z).
FAILED_AT = datetime(2026, 9, 30, 14, 45, tzinfo=UTC)
OWN_EXPIRES = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
OTHER_EXPIRES = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)


class _RefusalAuthority(_Authority):
    def __init__(
        self,
        *,
        refusal_kind: str = "declined",
        count: int = 1,
        other_coupon: bool = False,
        coupon_status: str | None = "valid",
        attempt_number: int = 1,
        reserve_outcome: str = "reserved",
        context_error: SupabaseError | None = None,
        record_error: SupabaseError | None = None,
    ) -> None:
        super().__init__(offer_code="2uafw5bg", anchor_type="payment_failure")
        self.attempt = replace(self.attempt, attempt_number=attempt_number)
        self.send_context = PaymentFailureSendContext(
            refusal_kind=refusal_kind,
            payment_failure_count=count,
            latest_failed_at=FAILED_AT,
            payment_type="CREDIT_CARD",
            other_coupon_code=OTHER_CODE if other_coupon else None,
            other_coupon_expires_at=OTHER_EXPIRES if other_coupon else None,
        )
        self.context_error = context_error
        self.coupon = (
            None
            if coupon_status is None
            else RecoveryCouponForCase(
                coupon_id="2c9b1f4e-1d3a-4f6b-8a7c-5e4d3c2b1a10",
                code=OWN_CODE,
                status=coupon_status,
                expires_at=OWN_EXPIRES,
                verified_at=None,
            )
        )
        self.reserve_outcome = reserve_outcome
        self.issuance_finalizations: list[dict[str, object]] = []
        self.record_error = record_error
        self.records: list[dict[str, object]] = []

    async def record_payment_failure_first_touch(self, **kwargs: object) -> str:
        # The follow-up (20261019000700) reads what B1 carried from here.
        assert kwargs["action_id"] == self.action.action_id
        assert kwargs["attempt_id"] == self.attempt.attempt_id
        assert kwargs["lease_generation"] == self.action.lease_generation
        self.events.append("record")
        self.records.append(
            {"variant": kwargs["variant"], "coupon_code": kwargs["coupon_code"]}
        )
        if self.record_error is not None:
            raise self.record_error
        return "recorded"

    async def get_portable_payment_failure_send_context(
        self, **kwargs: object
    ) -> PaymentFailureSendContext:
        assert kwargs["action_id"] == self.action.action_id
        assert kwargs["lease_generation"] == self.action.lease_generation
        self.events.append("send_context")
        if self.context_error is not None:
            raise self.context_error
        return self.send_context

    async def get_recovery_coupon_for_case(self, **kwargs: object) -> RecoveryCouponForCase | None:
        assert kwargs == {"recovery_case_id": self.action.recovery_case_id, "ordinal": 1}
        self.events.append("coupon")
        return self.coupon

    async def reserve_recovery_checkout_issuance(
        self, **kwargs: object
    ) -> RecoveryCheckoutReservation:
        assert kwargs["attempt_id"] == self.attempt.attempt_id
        self.events.append("reserve_link")
        if self.reserve_outcome != "reserved":
            return RecoveryCheckoutReservation(self.reserve_outcome, None, None, None)
        return RecoveryCheckoutReservation(
            "reserved", ISSUANCE_ID, LINK, "hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ1"
        )

    async def authorize_recovery_checkout_issuance(
        self, **kwargs: object
    ) -> RecoveryCheckoutAuthorization:
        assert kwargs["issuance_id"] == ISSUANCE_ID
        self.events.append("authorize_link")
        return RecoveryCheckoutAuthorization("request_started", ISSUANCE_ID, "request_started")

    async def finalize_chatwoot_checkout_issuance_v2(self, **kwargs: object) -> object:
        self.issuance_finalizations.append(kwargs)
        return object()


def _run(
    authority: _RefusalAuthority,
    tmp_path: Path,
    *,
    variants: PaymentFailureVariantsConfig | None = CONFIG,
) -> _Chatwoot:
    chatwoot = _Chatwoot([*CATALOG, *B1_CATALOG])
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
        payment_failure_variants=variants,
    )
    asyncio.run(dispatcher.dispatch_due(now=NOW))
    return chatwoot


def _body(name: str, values: list[str]) -> str:
    [template] = [t for t in B1_CATALOG if t["name"] == name]
    [body] = [c["text"] for c in template["components"] if c["type"] == "BODY"]
    for position, value in enumerate(values, start=1):
        body = body.replace("{{%d}}" % position, value)
    assert "{{" not in body
    return body


def _sent(chatwoot: _Chatwoot, variant: str, values: list[str], suffix: str) -> None:
    name = NAMES[variant]
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["content"] == _body(name, values)
    assert message["template_params"] == {
        "name": name,
        "category": "MARKETING" if variant in {VARIANT_COUPON, VARIANT_INSUFFICIENT_FUNDS}
        else "UTILITY",
        "language": "es_MX",
        "processed_params": {
            "body": {str(i): v for i, v in enumerate(values, start=1)},
            "buttons": [{"type": "url", "parameter": suffix}],
        },
    }


# El telefono del arnes es de EE. UU.: 14:45Z son las 10:45 en Nueva York.
NAME = "Edith García Pérez"
PRODUCT = "Alimenta tu Tiroides"
WHEN = "hoy a las 10:45 a. m."
BARE = LINK.removeprefix("https://pay.hotmart.com/")


def test_the_bank_refusal_goes_out_with_our_link_and_no_coupon(tmp_path: Path) -> None:
    authority = _RefusalAuthority(refusal_kind="declined")

    chatwoot = _run(authority, tmp_path)

    _sent(chatwoot, VARIANT_DECLINED, [NAME, WHEN, PRODUCT], BARE)
    # Sin cupon no se lee el cupon del caso; la variante se registra antes del
    # envio y el link se autoriza justo antes.
    assert authority.events == [
        "reevaluate", "reserve", "send_context", "reserve_link", "record",
        "reevaluate", "request_started", "authorize_link", "accepted",
    ]
    assert authority.records == [{"variant": VARIANT_DECLINED, "coupon_code": None}]
    assert authority.issuance_finalizations[0]["status"] == "accepted_by_chatwoot"


def test_two_refusals_win_over_the_reason(tmp_path: Path) -> None:
    authority = _RefusalAuthority(refusal_kind="insufficient_funds", count=2)

    chatwoot = _run(authority, tmp_path)

    _sent(chatwoot, VARIANT_TWO_REFUSALS, [NAME, WHEN, PRODUCT], BARE)
    assert "coupon" not in authority.events
    assert authority.records == [{"variant": VARIANT_TWO_REFUSALS, "coupon_code": None}]


def test_no_balance_goes_out_with_its_own_coupon(tmp_path: Path) -> None:
    authority = _RefusalAuthority(refusal_kind="insufficient_funds")

    chatwoot = _run(authority, tmp_path)

    _sent(
        chatwoot, VARIANT_INSUFFICIENT_FUNDS,
        [NAME, WHEN, PRODUCT, "mañana a las 11:00 a. m."],
        f"{BARE}&offDiscount={OWN_CODE}",
    )
    assert authority.records == [
        {"variant": VARIANT_INSUFFICIENT_FUNDS, "coupon_code": OWN_CODE}
    ]


def test_a_valid_coupon_of_another_case_wins_and_keeps_its_expiry(tmp_path: Path) -> None:
    authority = _RefusalAuthority(refusal_kind="insufficient_funds", count=2, other_coupon=True)

    chatwoot = _run(authority, tmp_path)

    _sent(
        chatwoot, VARIANT_COUPON,
        [NAME, WHEN, PRODUCT, "mañana a las 9:00 a. m."],
        f"{BARE}&offDiscount={OTHER_CODE}",
    )
    assert "coupon" not in authority.events
    assert authority.records == [{"variant": VARIANT_COUPON, "coupon_code": OTHER_CODE}]


@pytest.mark.parametrize("coupon_status", [None, "reserved", "created"])
def test_no_balance_waits_for_its_coupon_on_the_first_attempts(
    tmp_path: Path, coupon_status: str | None
) -> None:
    authority = _RefusalAuthority(refusal_kind="insufficient_funds", coupon_status=coupon_status)

    chatwoot = _run(authority, tmp_path)

    assert chatwoot.posts("/conversations/200/messages") == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == "recovery_coupon_not_ready"
    assert closed["next_attempt_at"] == (
        datetime.fromisoformat(FINAL_NOW) + timedelta(minutes=5)
    ).isoformat()
    assert "reserve_link" not in authority.events


@pytest.mark.parametrize(
    ("coupon_status", "attempt_number"), [("created", 3), ("create_failed", 1)]
)
def test_without_its_coupon_in_time_the_template_of_today_goes_out(
    tmp_path: Path, coupon_status: str, attempt_number: int
) -> None:
    authority = _RefusalAuthority(
        refusal_kind="insufficient_funds",
        coupon_status=coupon_status,
        attempt_number=attempt_number,
    )

    chatwoot = _run(authority, tmp_path)

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == PAYMENT_FAILURE_TEMPLATE
    assert message["content"] == _expected(PAYMENT_FAILURE_TEMPLATE, first=NAME, product=PRODUCT)
    assert "reserve_link" not in authority.events
    # La plantilla de hoy no es una variante: no se registra nada, y el
    # seguimiento sale como B2.
    assert authority.records == []


def test_a_link_that_cannot_be_issued_falls_back_to_the_template_of_today(
    tmp_path: Path,
) -> None:
    authority = _RefusalAuthority(refusal_kind="declined", reserve_outcome="missing_offer")

    chatwoot = _run(authority, tmp_path)

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == PAYMENT_FAILURE_TEMPLATE


def test_a_known_purchase_at_the_link_stops_b1(tmp_path: Path) -> None:
    authority = _RefusalAuthority(
        refusal_kind="declined", reserve_outcome="purchase_already_approved"
    )

    chatwoot = _run(authority, tmp_path)

    assert chatwoot.posts("/conversations/200/messages") == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == "recovery_checkout_purchase_already_approved"
    assert closed["next_attempt_at"] is None


def test_a_send_context_the_base_refuses_sends_nothing(tmp_path: Path) -> None:
    authority = _RefusalAuthority(
        context_error=SupabasePermanentError(
            "rejected", reason="payment_failure_details_missing"
        )
    )

    chatwoot = _run(authority, tmp_path)

    assert chatwoot.posts("/conversations/200/messages") == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == "payment_failure_details_missing"
    assert closed["next_attempt_at"] is None


def test_without_the_variants_the_template_of_today_goes_out(tmp_path: Path) -> None:
    authority = _RefusalAuthority(refusal_kind="insufficient_funds")

    chatwoot = _run(authority, tmp_path, variants=None)

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == PAYMENT_FAILURE_TEMPLATE
    assert "send_context" not in authority.events
