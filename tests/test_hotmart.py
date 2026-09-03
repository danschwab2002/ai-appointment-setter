from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import Settings, create_app
from bridge.commercial_ally import CommercialAllyConfig
from bridge.hotmart import parse_hotmart_purchase_payload
from bridge.supabase import SupabaseClient, SupabaseError


def _config() -> CommercialAllyConfig:
    return CommercialAllyConfig(
        tenant_ref="att1",
        funnel_ref="att1-main",
        binding_version=1,
        ally_ref="ally-one",
        lead_ally_name="Ally One",
        lead_site="ally-one-site",
        lead_landing_id="main",
        lead_page_host="ally-one.example",
        lead_page_path="/offer/main",
        product_hotlink="ATT1HOTLINK",
        product_name="ATT1 Offer",
        product_price=Decimal("49"),
        currency="USD",
        offer_code="att1offer",
        consent_copy_version="att1-whatsapp-v1",
        hotmart_product_id=123456,
        chatwoot_account_id=42,
        chatwoot_inbox_id=24,
        inbound_scope_key="att1-inbound",
        inbound_scope_version=1,
    )


def _purchase(event_id: str = "portable-purchase-001") -> dict[str, object]:
    return {
        "id": event_id,
        "creation_date": int(time.time() * 1000),
        "event": "PURCHASE_APPROVED",
        "version": "2.0.0",
        "data": {
            "product": {"id": 123456, "ucode": "ATT1-UCODE"},
            "buyer": {
                "email": " Buyer@Example.test ",
                "checkout_phone": "+1 (202) 555-0123",
            },
            "purchase": {
                "approved_date": int(time.time() * 1000) - 1000,
                "status": "APPROVED",
                "transaction": "HPATT1123456",
                "offer": {"code": "att1offer"},
            },
        },
    }


def _cart_abandonment(
    event_id: str = "portable-cart-abandonment-001",
) -> dict[str, object]:
    return {
        "id": event_id,
        "creation_date": int(time.time() * 1000),
        "event": "PURCHASE_OUT_OF_SHOPPING_CART",
        "version": "2.0.0",
        "data": {
            "product": {"id": 123456, "name": "ATT1 Offer"},
            "buyer": {
                "email": " Buyer@Example.test ",
                "phone": "+1 (202) 555-0123",
            },
            "offer": {"code": "att1offer"},
            "checkout_country": {"iso": "MX", "name": "México"},
        },
    }


def _payment_failure(
    event_id: str = "portable-payment-failure-001",
) -> dict[str, object]:
    return {
        "id": event_id,
        "creation_date": int(time.time() * 1000),
        "event": "PURCHASE_CANCELED",
        "version": "2.0.0",
        "data": {
            "product": {"id": 123456, "name": "ATT1 Offer"},
            "buyer": {
                "name": "Synthetic Buyer",
                "email": " Buyer@Example.test ",
                "phone": "+1 (202) 555-0123",
            },
            "purchase": {
                "status": "CANCELED",
                "transaction": "HPATT1123456",
                "offer": {"code": "att1offer"},
                "payment": {"refusal_reason": "NO_FUNDS"},
            },
            "checkout_country": {"iso": "MX", "name": "México"},
        },
    }


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": "chatwoot-test-secret",
        "allowed_jid": None,
        "capture_dir": Path("/tmp/portable-hotmart-tests"),
        "max_age_seconds": 300,
        "commercial_ally_config": _config(),
        "commercial_ally_manifest_path": Path("/runtime/commercial-ally.json"),
        "portable_hotmart_purchase_stop_enabled": True,
        "hotmart_hottok": "portable-test-token",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_portable_purchase_parser_requires_exact_configured_product_and_offer() -> None:
    assert parse_hotmart_purchase_payload(_purchase(), config=_config()) is not None

    wrong_product = _purchase("wrong-product")
    wrong_product["data"]["product"]["id"] = 999999  # type: ignore[index]
    wrong_offer = _purchase("wrong-offer")
    wrong_offer["data"]["purchase"]["offer"]["code"] = "other"  # type: ignore[index]

    assert parse_hotmart_purchase_payload(wrong_product, config=_config()) is None
    assert parse_hotmart_purchase_payload(wrong_offer, config=_config()) is None


def test_explicit_manifest_permits_hottok_only_with_portable_purchase_flag() -> None:
    assert create_app(_settings(), supabase_client=object()) is not None  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="ATT1 runtime capabilities are not portable"):
        create_app(_settings(portable_hotmart_purchase_stop_enabled=False))


def test_portable_purchase_stop_environment_contract_is_default_off() -> None:
    root = Path(__file__).resolve().parents[1]
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    compose = (root / "compose.yaml").read_text(encoding="utf-8")

    assert "PORTABLE_HOTMART_PURCHASE_STOP_ENABLED=false" in env_example
    assert (
        "PORTABLE_HOTMART_PURCHASE_STOP_ENABLED: "
        "${PORTABLE_HOTMART_PURCHASE_STOP_ENABLED:-false}" in compose
    )


def test_portable_hotmart_recovery_environment_contract_is_default_off() -> None:
    root = Path(__file__).resolve().parents[1]
    env_example = (root / ".env.example").read_text(encoding="utf-8")
    compose = (root / "compose.yaml").read_text(encoding="utf-8")

    assert "PORTABLE_HOTMART_RECOVERY_ENABLED=false" in env_example
    assert (
        "PORTABLE_HOTMART_RECOVERY_ENABLED: "
        "${PORTABLE_HOTMART_RECOVERY_ENABLED:-false}" in compose
    )


def test_portable_purchase_client_sends_server_owned_binding_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "outcome": "inserted",
            "webhook_event_id": "8de61be1-81ae-4dcf-9f18-d24b8d71db5d",
        }])

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    payload = _purchase()
    parsed = parse_hotmart_purchase_payload(payload, config=_config())
    assert parsed is not None

    asyncio.run(client.admit_portable_hotmart_purchase_approved(
        config=_config(),
        external_event_id=parsed.event_id,
        payload=payload,
        normalized_email=parsed.buyer_email,
        normalized_phone=parsed.buyer_phone,
    ))

    assert requests[0].url.path == (
        "/rest/v1/rpc/admit_portable_hotmart_purchase_approved"
    )
    assert json.loads(requests[0].content) == {
        "p_tenant_ref": "att1",
        "p_funnel_ref": "att1-main",
        "p_binding_version": 1,
        "p_external_event_id": "portable-purchase-001",
        "p_payload": payload,
        "p_normalized_email": "buyer@example.test",
        "p_normalized_phone": "12025550123",
    }


def test_portable_cart_client_sends_server_owned_binding_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "outcome": "inserted",
            "webhook_event_id": "cf5ba605-2d3a-4e09-85da-1227632c598d",
        }])

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    payload = _cart_abandonment()

    asyncio.run(client.admit_portable_hotmart_cart_abandonment(
        config=_config(),
        external_event_id="portable-cart-abandonment-001",
        payload=payload,
        normalized_email="buyer@example.test",
        normalized_phone="12025550123",
    ))

    assert requests[0].url.path == (
        "/rest/v1/rpc/admit_portable_hotmart_cart_abandonment"
    )
    assert json.loads(requests[0].content) == {
        "p_tenant_ref": "att1",
        "p_funnel_ref": "att1-main",
        "p_binding_version": 1,
        "p_external_event_id": "portable-cart-abandonment-001",
        "p_payload": payload,
        "p_normalized_email": "buyer@example.test",
        "p_normalized_phone": "12025550123",
    }


@pytest.mark.parametrize(
    "row",
    [
        {
            "outcome": "inserted",
            "webhook_event_id": "not-a-uuid",
        },
        {
            "outcome": "inserted",
            "webhook_event_id": "cf5ba605-2d3a-4e09-85da-1227632c598d",
            "unexpected": "field",
        },
    ],
)
def test_portable_cart_client_rejects_malformed_committed_identity(
    row: dict[str, str],
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[row])

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SupabaseError, match="portable_cart_abandonment_admission_invalid"):
        asyncio.run(client.admit_portable_hotmart_cart_abandonment(
            config=_config(),
            external_event_id="portable-cart-abandonment-001",
            payload=_cart_abandonment(),
            normalized_email="buyer@example.test",
            normalized_phone="12025550123",
        ))


def test_legacy_cart_client_uses_scope_fixed_johanna_wrapper() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "outcome": "inserted",
            "webhook_event_id": "cf5ba605-2d3a-4e09-85da-1227632c598d",
        }])

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(client.admit_and_correlate_hotmart_cart_abandonment(
        external_event_id="legacy-cart-001",
        payload=_cart_abandonment(),
        normalized_email="buyer@example.test",
        normalized_phone="12025550123",
    ))

    assert requests[0].url.path.endswith(
        "/rest/v1/rpc/admit_johanna_hotmart_cart_abandonment"
    )


@pytest.mark.parametrize(
    "row",
    [
        {"outcome": "inserted", "webhook_event_id": "not-a-uuid"},
        {
            "outcome": "inserted",
            "webhook_event_id": "cf5ba605-2d3a-4e09-85da-1227632c598d",
            "unexpected": "accepted",
        },
    ],
)
def test_legacy_cart_client_rejects_malformed_committed_identity(
    row: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[row])

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(
        SupabaseError,
        match="cart_abandonment_correlation_admission_invalid_row",
    ):
        asyncio.run(client.admit_and_correlate_hotmart_cart_abandonment(
            external_event_id="legacy-cart-001",
            payload=_cart_abandonment(),
            normalized_email="buyer@example.test",
            normalized_phone="12025550123",
        ))


class _SupabaseStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.cart_calls: list[dict[str, object]] = []
        self.payment_failure_calls: list[dict[str, object]] = []

    async def admit_portable_hotmart_purchase_approved(
        self, **kwargs: object
    ) -> object:
        self.calls.append(kwargs)
        return type("Admission", (), {
            "outcome": "inserted",
            "webhook_event_id": "8de61be1-81ae-4dcf-9f18-d24b8d71db5d",
        })()

    async def admit_portable_hotmart_cart_abandonment(
        self, **kwargs: object
    ) -> object:
        self.cart_calls.append(kwargs)
        return type("Admission", (), {
            "outcome": "inserted",
            "webhook_event_id": "cf5ba605-2d3a-4e09-85da-1227632c598d",
        })()

    async def admit_portable_hotmart_payment_failure(
        self, **kwargs: object
    ) -> object:
        self.payment_failure_calls.append(kwargs)
        return type("Admission", (), {
            "outcome": "inserted",
            "webhook_event_id": "77bc81f5-f84a-4a98-b4e1-566074370e5d",
        })()


def _post(app: object, payload: dict[str, object]) -> httpx.Response:
    with TestClient(app) as client:  # type: ignore[arg-type]
        return client.post(
            "/webhooks/hotmart",
            content=json.dumps(payload),
            headers={"X-HOTMART-HOTTOK": "portable-test-token"},
        )


def test_portable_handler_admits_only_purchase_approved() -> None:
    supabase = _SupabaseStub()
    response = _post(
        create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
        _purchase(),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "received",
        "event_id": "portable-purchase-001",
    }
    assert len(supabase.calls) == 1
    assert supabase.calls[0]["config"] == _config()


def test_portable_handler_admits_scoped_cart_abandonment_when_enabled() -> None:
    supabase = _SupabaseStub()
    response = _post(
        create_app(
            _settings(portable_hotmart_recovery_enabled=True),
            supabase_client=supabase,  # type: ignore[arg-type]
        ),
        _cart_abandonment(),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "received",
        "event_id": "portable-cart-abandonment-001",
    }
    assert supabase.calls == []
    assert len(supabase.cart_calls) == 1
    assert supabase.cart_calls[0]["external_event_id"] == (
        "portable-cart-abandonment-001"
    )
    assert supabase.cart_calls[0]["config"] == _config()
    assert supabase.cart_calls[0]["normalized_email"] == "buyer@example.test"
    assert supabase.cart_calls[0]["normalized_phone"] == "12025550123"


def test_portable_handler_admits_scoped_payment_failure_when_enabled() -> None:
    supabase = _SupabaseStub()
    response = _post(
        create_app(
            _settings(portable_hotmart_payment_failure_enabled=True),
            supabase_client=supabase,  # type: ignore[arg-type]
        ),
        _payment_failure(),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "received",
        "event_id": "portable-payment-failure-001",
    }
    assert supabase.calls == []
    assert supabase.cart_calls == []
    assert len(supabase.payment_failure_calls) == 1
    assert supabase.payment_failure_calls[0]["external_event_id"] == (
        "portable-payment-failure-001"
    )
    assert supabase.payment_failure_calls[0]["config"] == _config()
    assert supabase.payment_failure_calls[0]["normalized_email"] == (
        "buyer@example.test"
    )
    assert supabase.payment_failure_calls[0]["normalized_phone"] == "12025550123"


@pytest.mark.parametrize(
    ("event_type", "reason"),
    [
        ("PURCHASE_CANCELED", "portable_purchase_stop_event_ignored"),
        ("PURCHASE_OUT_OF_SHOPPING_CART", "portable_purchase_stop_event_ignored"),
    ],
)
def test_portable_handler_privacy_safely_ignores_non_purchase_events_before_admission(
    event_type: str,
    reason: str,
) -> None:
    supabase = _SupabaseStub()
    payload = deepcopy(_purchase())
    payload["event"] = event_type
    payload["creation_date"] = 0

    response = _post(
        create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
        payload,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": reason}
    assert supabase.calls == []
