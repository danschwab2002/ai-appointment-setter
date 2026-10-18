"""A1 del carrito de ATT1 con el cupon personal y el link de recuperacion.

Decision de Dan del 18/10 («A1 espera al cupón, que dura 24 h, en la hora del
país»): A1 sale con ``att1_carrito_descuento_01`` cuando el cupon figura
``valid``; si no, espera 5 minutos en los intentos 1 y 2, dentro de los 15
minutos de su hora, y despues sale la plantilla de hoy sin descuento. La base
da 3 reintentos a un primer contacto (att1-db, 18/10): A1 nunca se pierde por
el cupon.

La base y Chatwoot son los de ``test_durable_dispatcher_approved_template``;
el catalogo suma las plantillas aprobadas el 18/10
(``chatwoot_message_templates_inbox_11_carrito_20261018.json``).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_recovery_coupon import _supabase
from test_durable_dispatcher_approved_template import (
    ACCOUNT_ID,
    MANIFEST,
    ALLY,
    BOUNDARY,
    CART_TEMPLATE,
    CATALOG,
    FINAL_NOW,
    FIXTURES,
    INBOX_ID,
    NOW,
    TEMPLATE,
    _Authority,
    _Chatwoot,
    _expected,
)

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.cart_first_touch import CartDiscountConfig
from bridge.instance_manifest import Template
from bridge.messaging import ChatwootMessageSender, FinalMetaEffectGate
from bridge.supabase import (
    RecoveryCheckoutAuthorization,
    RecoveryCheckoutReservation,
    RecoveryCouponForCase,
    SupabaseError,
)
from bridge.worker import DurableDispatcher

A1 = "att1_carrito_descuento_01"
CARRITO_CATALOG: list[dict[str, Any]] = json.loads(
    (FIXTURES / "chatwoot_message_templates_inbox_11_carrito_20261018.json").read_text(
        encoding="utf-8"
    )
)["message_templates"]
CODE = "ATT1A1B2C3D4E5F6"
ISSUANCE_ID = "6f1d0c2a-7a4b-4c4e-9d1e-0b6a2f5c8e91"
LINK = (
    "https://pay.hotmart.com/D98014973Y?off=gopi6lh7&checkoutMode=10"
    "&src=hermes&sck=hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ0"
)
CONFIG = CartDiscountConfig(template_name=A1, language="es_MX", price="47", currency="USD")


class _CartAuthority(_Authority):
    def __init__(
        self,
        *,
        coupon_status: str | None = "valid",
        attempt_number: int = 1,
        due_at: str = NOW,
        reserve_outcome: str = "reserved",
        authorize_outcome: str = "request_started",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.action = replace(self.action, due_at=due_at)
        self.attempt = replace(self.attempt, attempt_number=attempt_number)
        self.coupon = (
            None
            if coupon_status is None
            else RecoveryCouponForCase(
                coupon_id="2c9b1f4e-1d3a-4f6b-8a7c-5e4d3c2b1a09",
                code=CODE,
                status=coupon_status,
                expires_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
                verified_at=None,
            )
        )
        self.reserve_outcome = reserve_outcome
        self.authorize_outcome = authorize_outcome
        self.issuance_finalizations: list[dict[str, object]] = []

    async def get_recovery_coupon_for_case(self, **_: object) -> RecoveryCouponForCase | None:
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
            "reserved", ISSUANCE_ID, LINK, "hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ0"
        )

    async def authorize_recovery_checkout_issuance(
        self, **kwargs: object
    ) -> RecoveryCheckoutAuthorization:
        assert kwargs["issuance_id"] == ISSUANCE_ID
        self.events.append("authorize_link")
        if self.authorize_outcome != "request_started":
            return RecoveryCheckoutAuthorization(self.authorize_outcome, None, None)
        return RecoveryCheckoutAuthorization("request_started", ISSUANCE_ID, "request_started")

    async def finalize_chatwoot_checkout_issuance_v2(self, **kwargs: object) -> object:
        self.issuance_finalizations.append(kwargs)
        return object()


def _dispatcher(
    authority: _CartAuthority,
    chatwoot: _Chatwoot,
    tmp_path: Path,
    *,
    cart_discount: CartDiscountConfig | None = CONFIG,
    gate_open: bool = True,
) -> DurableDispatcher:
    client = chatwoot.client()
    return DurableDispatcher(
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
            enabled=gate_open, evidence_dir=tmp_path / "meta-effects"
        ),
        waba_template=TEMPLATE,
        approved_template_direct=True,
        cart_discount=cart_discount,
    )


def _run(authority: _CartAuthority, tmp_path: Path, **kwargs: Any) -> _Chatwoot:
    chatwoot = _Chatwoot([*CATALOG, *CARRITO_CATALOG])
    asyncio.run(_dispatcher(authority, chatwoot, tmp_path, **kwargs).dispatch_due(now=NOW))
    return chatwoot


def _a1_body(values: list[str]) -> str:
    [template] = [t for t in CARRITO_CATALOG if t["name"] == A1]
    [body] = [c["text"] for c in template["components"] if c["type"] == "BODY"]
    for position, value in enumerate(values, start=1):
        body = body.replace("{{%d}}" % position, value)
    return body


def _the_old_template_went_out(chatwoot: _Chatwoot) -> None:
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == CART_TEMPLATE
    assert message["content"] == _expected(
        CART_TEMPLATE, first="Edith García Pérez", product="Alimenta tu Tiroides"
    )


def test_a1_goes_out_with_the_coupon_the_link_and_the_hour_of_the_country(
    tmp_path: Path,
) -> None:
    authority = _CartAuthority()

    chatwoot = _run(authority, tmp_path)

    # El telefono del arnes es de EE. UU.: 14:00Z son las 10:00 en Nueva York.
    values = ["Edith García Pérez", "Alimenta tu Tiroides", "hoy a las 10:00 a. m.",
              "USD 47", "mañana a las 11:00 a. m."]
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["content"] == _a1_body(values)
    assert message["template_params"] == {
        "name": A1,
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
    # El link se autoriza con el pedido ya arrancado, justo antes del envio.
    assert authority.events == [
        "reevaluate", "reserve", "coupon", "reserve_link", "reevaluate",
        "request_started", "authorize_link", "accepted",
    ]
    assert authority.issuance_finalizations == [{
        "issuance_id": ISSUANCE_ID,
        "status": "accepted_by_chatwoot",
        "chatwoot_message_id": 888,
        "failure_code": None,
        "now": FINAL_NOW,
    }]


def test_the_closed_gate_records_a1_and_sends_nothing(tmp_path: Path) -> None:
    authority = _CartAuthority()

    chatwoot = _run(authority, tmp_path, gate_open=False)

    assert chatwoot.posts("/conversations/200/messages") == []
    [evidence] = list((tmp_path / "meta-effects").iterdir())
    assert json.loads(evidence.read_text(encoding="utf-8"))["template_name"] == A1
    assert "authorize_link" not in authority.events


@pytest.mark.parametrize("coupon_status", [None, "reserved", "created", "create_unknown"])
@pytest.mark.parametrize("attempt_number", [1, 2])
def test_a_coupon_not_valid_yet_defers_a1_five_minutes(
    tmp_path: Path, coupon_status: str | None, attempt_number: int
) -> None:
    authority = _CartAuthority(coupon_status=coupon_status, attempt_number=attempt_number)

    chatwoot = _run(authority, tmp_path)

    assert chatwoot.posts("/conversations/200/messages") == []
    [closed] = authority.finalizations
    assert closed["outcome"] == "failed_before_request"
    assert closed["reason_code"] == "recovery_coupon_not_ready"
    assert closed["next_attempt_at"] == (
        datetime.fromisoformat(FINAL_NOW) + timedelta(minutes=5)
    ).isoformat()
    assert "reserve_link" not in authority.events


def test_on_the_third_attempt_a1_goes_out_without_discount(tmp_path: Path) -> None:
    authority = _CartAuthority(coupon_status="created", attempt_number=3)

    chatwoot = _run(authority, tmp_path)

    _the_old_template_went_out(chatwoot)
    assert "reserve_link" not in authority.events
    assert authority.issuance_finalizations == []


def test_past_the_fifteen_minutes_a1_goes_out_without_discount(tmp_path: Path) -> None:
    # Vencio a las 14:50: esperar otros 5 minutos lo llevaria a las 15:06.
    authority = _CartAuthority(coupon_status="created", due_at="2026-09-30T14:50:00+00:00")

    _the_old_template_went_out(_run(authority, tmp_path))


@pytest.mark.parametrize("coupon_status", ["create_failed", "expired", "redeemed"])
def test_a_coupon_that_will_not_be_valid_does_not_make_a1_wait(
    tmp_path: Path, coupon_status: str
) -> None:
    _the_old_template_went_out(_run(_CartAuthority(coupon_status=coupon_status), tmp_path))


@pytest.mark.parametrize("outcome", ["purchase_already_approved", "blocked_case", "blocked_contact"])
def test_a_reservation_that_says_the_case_is_over_sends_nothing(
    tmp_path: Path, outcome: str
) -> None:
    authority = _CartAuthority(reserve_outcome=outcome)

    chatwoot = _run(authority, tmp_path)

    assert chatwoot.posts("/conversations/200/messages") == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == f"recovery_checkout_{outcome}"
    assert closed["next_attempt_at"] is None


@pytest.mark.parametrize("outcome", ["missing_offer", "blocked_binding"])
def test_without_a_link_a1_goes_out_without_discount(tmp_path: Path, outcome: str) -> None:
    _the_old_template_went_out(_run(_CartAuthority(reserve_outcome=outcome), tmp_path))


def test_an_opt_out_in_the_last_seconds_stops_a1_after_the_request_started(
    tmp_path: Path,
) -> None:
    authority = _CartAuthority(authorize_outcome="blocked_opt_out")

    chatwoot = _run(authority, tmp_path)

    assert chatwoot.posts("/conversations/200/messages") == []
    [closed] = authority.finalizations
    assert closed["outcome"] == "delivery_unknown"
    assert closed["reason_code"] == "recovery_checkout_blocked_opt_out"
    assert authority.issuance_finalizations == []


def test_without_the_flag_the_coupon_is_never_read(tmp_path: Path) -> None:
    authority = _CartAuthority()

    _the_old_template_went_out(_run(authority, tmp_path, cart_discount=None))

    assert "coupon" not in authority.events


def test_the_payment_failure_keeps_its_own_template(tmp_path: Path) -> None:
    authority = _CartAuthority(offer_code="2uafw5bg", anchor_type="payment_failure")

    chatwoot = _run(authority, tmp_path)

    assert "coupon" not in authority.events
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] != A1


def test_the_cart_discount_needs_the_direct_mode(tmp_path: Path) -> None:
    chatwoot = _Chatwoot()
    client = chatwoot.client()
    with pytest.raises(ValueError, match="direct mode"):
        DurableDispatcher(
            supabase=_CartAuthority(),  # type: ignore[arg-type]
            worker_id="att1-dispatcher",
            chatwoot=client,
            chatwoot_account_id=ACCOUNT_ID,
            chatwoot_inbox_id=INBOX_ID,
            sender=None,
            allowed_jid=None,
            cart_discount=CONFIG,
        )



# ------------------------------------------------------ built by create_app


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    manifest = replace(MANIFEST, flows={**MANIFEST.flows, "carrito": True})
    manifest = replace(
        manifest,
        templates={**manifest.templates, "carrito": Template(name=CART_TEMPLATE, language="es_EC")},
    )
    credentials = tmp_path / "hotmart-api-credentials"
    # Valores inventados con la forma de la credencial (test_recovery_coupon).
    credentials.write_text(
        "HOTMART_CLIENT_ID=00000000-0000-4000-8000-000000000001\n"
        "HOTMART_CLIENT_SECRET=00000000-0000-4000-8000-000000000002\n"
        "HOTMART_BASIC=MDAwMDAwMDAtMDAwMDo=\n"
    )
    fields: dict[str, Any] = {
        "webhook_secret": "test-secret",
        "allowed_jid": None,
        "capture_dir": tmp_path / "captures",
        "max_age_seconds": 300,
        "commercial_ally_config": manifest.to_commercial_ally_config(),
        "commercial_ally_manifest_path": Path("/instancia/instancia.toml"),
        "instance_manifest": manifest,
        "hermes_model_name": manifest.agent_model_name,
        "chatwoot_account_id": ACCOUNT_ID,
        "chatwoot_inbox_id": INBOX_ID,
        "portable_hotmart_recovery_enabled": True,
        "dispatcher_enabled": True,
        "dispatcher_worker_id": "att1-dispatcher",
        "dispatcher_outbound_enabled": True,
        "dispatcher_approved_template_direct_enabled": True,
        "meta_final_effect_enabled": True,
        "meta_final_effect_evidence_dir": tmp_path / "meta-effects",
        "pilot_boundary_enabled": True,
        "pilot_scope_key": BOUNDARY.scope_key,
        "pilot_scope_version": BOUNDARY.scope_version,
        "pilot_tenant_key": BOUNDARY.tenant_key,
        "pilot_channel_provider": "waba",
        "pilot_channel_account_ref": BOUNDARY.channel_account_ref,
        "waba_first_touch_template_name": CART_TEMPLATE,
        "waba_template_language": "es_EC",
        "waba_template_category": "MARKETING",
        "recovery_coupon_worker_enabled": True,
        "hotmart_api_credentials_file": credentials,
        "recovery_discount_first_touch_enabled": True,
        "recovery_discount_template_name": A1,
        "recovery_discount_price": "47",
    }
    fields.update(overrides)
    return Settings(**fields)


def _app(settings: Settings) -> Any:
    return create_app(
        settings,
        supabase_client=_CartAuthority(),  # type: ignore[arg-type]
        chatwoot_client=_Chatwoot().client(),
    )


def test_create_app_gives_the_dispatcher_the_cart_discount(tmp_path: Path) -> None:
    app = _app(_settings(tmp_path))

    assert app.state.durable_dispatcher._cart_discount == CartDiscountConfig(
        template_name=A1, language="es_EC", price="47", currency="USD"
    )
    assert "recovery_discount_first_touch_enabled" in (
        app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    )


def test_the_flag_is_off_by_default(tmp_path: Path) -> None:
    app = _app(_settings(tmp_path, recovery_discount_first_touch_enabled=False))

    assert app.state.durable_dispatcher._cart_discount is None
    assert Settings.__dataclass_fields__["recovery_discount_first_touch_enabled"].default is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"recovery_coupon_worker_enabled": False}, "RECOVERY_COUPON_WORKER_ENABLED"),
        ({"recovery_discount_template_name": None}, "RECOVERY_DISCOUNT_TEMPLATE_NAME"),
        ({"recovery_discount_price": None}, "RECOVERY_DISCOUNT_PRICE"),
        ({"recovery_discount_price": "cuarenta"}, "cart_discount_invalid_price"),
        ({"recovery_discount_currency": "usd"}, "cart_discount_invalid_currency"),
    ],
)
def test_an_incomplete_wiring_does_not_start(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _app(_settings(tmp_path, **overrides))



# ------------------------------------------ las RPC del link (SupabaseClient)

ULID = "01JABCDEFGHJKMNPQRSTVWXYZ0"
ACTION_ID = "3a4b5c6d-7e8f-4a1b-9c2d-3e4f5a6b7c8d"
ATTEMPT_ID = "4b5c6d7e-8f9a-4b1c-8d2e-3f4a5b6c7d8e"


def _reserve(client: Any) -> RecoveryCheckoutReservation:
    return asyncio.run(client.reserve_recovery_checkout_issuance(
        action_id=ACTION_ID, worker_id="att1-dispatcher", lease_generation=3,
        attempt_id=ATTEMPT_ID, issuance_ulid=ULID, now=FINAL_NOW,
    ))


def test_the_reservation_returns_the_link_only_when_reserved() -> None:
    client, requests = _supabase([
        (200, [{"outcome": "reserved", "issuance_id": ISSUANCE_ID,
                "checkout_url_final": LINK, "sck_value": "hermes~v1~" + ULID}]),
        (200, [{"outcome": "request_started_replay", "issuance_id": ISSUANCE_ID,
                "checkout_url_final": None, "sck_value": None}]),
    ])

    reserved = _reserve(client)
    replay = _reserve(client)

    assert reserved == RecoveryCheckoutReservation("reserved", ISSUANCE_ID, LINK, "hermes~v1~" + ULID)
    assert replay == RecoveryCheckoutReservation("request_started_replay", None, None, None)
    assert requests[0].url.path == "/rest/v1/rpc/reserve_recovery_checkout_issuance_v1"
    assert json.loads(requests[0].content) == {
        "p_action_id": ACTION_ID, "p_worker_id": "att1-dispatcher", "p_lease_generation": 3,
        "p_attempt_id": ATTEMPT_ID, "p_issuance_ulid": ULID, "p_now": FINAL_NOW,
    }


@pytest.mark.parametrize(
    "rows",
    [[{"outcome": "sent_twice", "issuance_id": None, "checkout_url_final": None, "sck_value": None}],
     [], [{"outcome": "reserved", "issuance_id": ISSUANCE_ID, "checkout_url_final": None,
           "sck_value": None}]],
    ids=["resultado-desconocido", "sin-filas", "reservada-sin-link"],
)
def test_a_reservation_that_does_not_close_is_an_error(rows: list[dict[str, Any]]) -> None:
    client, _ = _supabase([(200, rows)])
    with pytest.raises(SupabaseError):
        _reserve(client)


def test_the_authorization_checks_it_is_the_same_link() -> None:
    client, requests = _supabase([
        (200, [{"outcome": "request_started", "issuance_id": ISSUANCE_ID,
                "status": "request_started"}]),
        (200, [{"outcome": "blocked_opt_out", "issuance_id": ISSUANCE_ID, "status": "reserved"}]),
        (200, [{"outcome": "request_started", "issuance_id": ACTION_ID,
                "status": "request_started"}]),
    ])

    def authorize() -> RecoveryCheckoutAuthorization:
        return asyncio.run(client.authorize_recovery_checkout_issuance(
            issuance_id=ISSUANCE_ID, action_id=ACTION_ID, worker_id="att1-dispatcher",
            lease_generation=3, now=FINAL_NOW,
        ))

    assert authorize() == RecoveryCheckoutAuthorization(
        "request_started", ISSUANCE_ID, "request_started")
    assert authorize() == RecoveryCheckoutAuthorization("blocked_opt_out", None, None)
    with pytest.raises(SupabaseError):
        authorize()
    assert requests[0].url.path == "/rest/v1/rpc/authorize_recovery_checkout_issuance_v1"
