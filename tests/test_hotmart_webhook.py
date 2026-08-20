"""Tests for the Hotmart webhook receiver."""

from __future__ import annotations

import asyncio
import copy
import json
import time

import httpx
import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from bridge.app import Settings, create_app
from bridge.hotmart import parse_hotmart_payload

# ── Fixtures ────────────────────────────────────────────────────────

HOTMART_TOKEN = "test-hottok-secret"

EXAMPLE_PAYLOAD: dict[str, object] = {
    "id": "0d7aa966-b887-4617-8c56-9e865bfc8ce4",
    "creation_date": int(time.time() * 1000),
    "event": "PURCHASE_OUT_OF_SHOPPING_CART",
    "version": "2.0.0",
    "data": {
        "affiliate": True,
        "product": {"id": 3526906, "name": "Product Name"},
        "buyer": {
            "name": "Buyer name",
            "email": "buyer@email.com.br",
            "phone": "5531999999999",
        },
        "offer": {"code": "n82b9jqz"},
        "checkout_country": {"name": "Brasil", "iso": "BR"},
    },
}

PURCHASE_APPROVED_PAYLOAD: dict[str, object] = {
    "id": "purchase-event-001",
    "creation_date": int(time.time() * 1000),
    "event": "PURCHASE_APPROVED",
    "version": "2.0.0",
    "data": {
        "product": {
            "id": 3526906,
            "ucode": "product-ucode-001",
            "name": "Product Name",
        },
        "buyer": {
            "name": "Buyer name",
            "email": "buyer@email.com.br",
            "checkout_phone": "5531999999999",
        },
        "purchase": {
            "approved_date": int(time.time() * 1000),
            "status": "APPROVED",
            "transaction": "HP17715690036014",
            "offer": {"code": "n82b9jqz"},
        },
    },
}


def test_parse_hotmart_payload_rejects_phone_with_non_phone_suffix() -> None:
    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    data = payload["data"]
    assert isinstance(data, dict)
    buyer = data["buyer"]
    assert isinstance(buyer, dict)
    buyer["phone"] = "5531999999999@evil"

    parsed = parse_hotmart_payload(payload)

    assert parsed is not None
    assert parsed.buyer_phone is None


def test_parse_hotmart_payload_accepts_formatted_phone() -> None:
    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    data = payload["data"]
    assert isinstance(data, dict)
    buyer = data["buyer"]
    assert isinstance(buyer, dict)
    buyer["phone"] = "+55 (31) 99999-9999"

    parsed = parse_hotmart_payload(payload)

    assert parsed is not None
    assert parsed.buyer_phone == "5531999999999"


def test_parse_hotmart_payload_accepts_checkout_phone_fallback() -> None:
    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    data = payload["data"]
    assert isinstance(data, dict)
    buyer = data["buyer"]
    assert isinstance(buyer, dict)
    buyer.pop("phone")
    buyer["checkout_phone"] = "+55 (31) 98888-7777"

    parsed = parse_hotmart_payload(payload)

    assert parsed is not None
    assert parsed.buyer_phone == "5531988887777"


@pytest.mark.parametrize("phone", ["0", "1234567", "0123456789", "1" * 16])
def test_parse_hotmart_payload_rejects_non_e164_digit_shape(phone: str) -> None:
    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    data = payload["data"]
    assert isinstance(data, dict)
    buyer = data["buyer"]
    assert isinstance(buyer, dict)
    buyer["email"] = None
    buyer["phone"] = phone

    assert parse_hotmart_payload(payload) is None


def test_parse_hotmart_payload_rejects_zero_creation_date() -> None:
    payload = copy.deepcopy(EXAMPLE_PAYLOAD)
    payload["creation_date"] = 0

    assert parse_hotmart_payload(payload) is None


def _hotmart_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "webhook_secret": "unused",
        "allowed_jid": "unused@s.whatsapp.net",
        "capture_dir": "/tmp/unused-captures",
        "max_age_seconds": 300,
        "hotmart_hottok": HOTMART_TOKEN,
        "hotmart_max_age_seconds": 300,
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 7,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _pilot_boundary_settings() -> dict[str, object]:
    return {
        "pilot_boundary_enabled": True,
        "pilot_scope_key": "lancemos-cart-recovery",
        "pilot_scope_version": 1,
        "pilot_tenant_key": "lancemos",
        "pilot_channel_provider": "waba",
        "pilot_channel_account_ref": "opaque-account-ref",
        "waba_first_touch_template_name": "cart_recovery_first",
        "waba_followup_template_name": "cart_recovery_followup",
        "waba_template_language": "es_AR",
        "waba_template_category": "MARKETING",
    }


def test_worker_requires_complete_durable_policy_configuration() -> None:
    with pytest.raises(ValueError, match="FOLLOWUP_POLICY_KEY and FOLLOWUP_POLICY_VERSION"):
        create_app(_hotmart_settings(
            worker_enabled=True,
            supabase_base_url="https://fake.supabase.co",
            supabase_service_role_key="service-role",
            followup_policy_version=None,
        ))


def test_durable_cart_planning_requires_enabled_pilot_boundary() -> None:
    with pytest.raises(ValueError, match="LANCEMOS_PILOT_BOUNDARY_ENABLED"):
        create_app(_hotmart_settings(
            worker_enabled=True,
            supabase_base_url="https://fake.supabase.co",
            supabase_service_role_key="service-role",
            followup_policy_key="cart-recovery-test",
            followup_policy_version=1,
        ))


@pytest.mark.parametrize(
    ("missing_field", "expected_name"),
    [
        ("allowed_jid", "ALLOWED_WHATSAPP_JID"),
        ("chatwoot_account_id", "CHATWOOT_ACCOUNT_ID"),
        ("chatwoot_inbox_id", "CHATWOOT_INBOX_ID"),
    ],
)
def test_resolution_worker_requires_complete_whatsapp_identity_configuration(
    missing_field: str,
    expected_name: str,
) -> None:
    overrides: dict[str, object] = {
        "worker_enabled": True,
        "supabase_base_url": "https://fake.supabase.co",
        "supabase_service_role_key": "service-role",
        "followup_policy_key": "cart-recovery-test",
        "followup_policy_version": 1,
        missing_field: None,
    }

    with pytest.raises(ValueError, match=expected_name):
        create_app(_hotmart_settings(**overrides))


def test_worker_fails_closed_without_supabase() -> None:
    with pytest.raises(ValueError, match="Supabase is required"):
        create_app(_hotmart_settings(
            worker_enabled=True,
            followup_policy_key="cart-recovery-test",
            followup_policy_version=1,
        ))


def test_dispatcher_requires_worker_id_when_enabled() -> None:
    with pytest.raises(ValueError, match="DURABLE_DISPATCHER_WORKER_ID"):
        create_app(_hotmart_settings(
            dispatcher_enabled=True,
            supabase_base_url="https://fake.supabase.co",
            supabase_service_role_key="service-role",
        ))


def test_dispatcher_is_wired_only_when_explicitly_enabled() -> None:
    disabled = create_app(_hotmart_settings(
        dispatcher_enabled=False,
    ))
    enabled = create_app(_hotmart_settings(
        dispatcher_enabled=True,
        dispatcher_worker_id="dispatcher-test",
        supabase_base_url="https://fake.supabase.co",
        supabase_service_role_key="service-role",
        chatwoot_base_url="https://chatwoot.example.test",
        chatwoot_account_id=1,
        chatwoot_control_api_access_token="control-token",
        chatwoot_pause_macro_id=1,
    ))
    assert disabled.state.durable_dispatcher is None
    assert enabled.state.durable_dispatcher is not None


def test_dispatcher_outbound_requires_complete_explicit_dependencies() -> None:
    with pytest.raises(ValueError, match="durable outbound requires Hermes and sender"):
        create_app(_hotmart_settings(
            dispatcher_enabled=True,
            dispatcher_outbound_enabled=True,
            dispatcher_worker_id="dispatcher-test",
            supabase_base_url="https://fake.supabase.co",
            supabase_service_role_key="service-role",
            chatwoot_base_url="https://chatwoot.example.test",
            chatwoot_account_id=1,
            chatwoot_control_api_access_token="control-token",
            chatwoot_pause_macro_id=1,
            messaging_channel="waba",
            **_pilot_boundary_settings(),
        ))


def test_dispatcher_outbound_requires_enabled_pilot_boundary() -> None:
    with pytest.raises(ValueError, match="LANCEMOS_PILOT_BOUNDARY_ENABLED"):
        create_app(_hotmart_settings(
            dispatcher_enabled=True,
            dispatcher_outbound_enabled=True,
            dispatcher_worker_id="dispatcher-test",
            supabase_base_url="https://fake.supabase.co",
            supabase_service_role_key="service-role",
            chatwoot_base_url="https://chatwoot.example.test",
            chatwoot_account_id=1,
            chatwoot_control_api_access_token="control-token",
            chatwoot_pause_macro_id=1,
        ))


@pytest.mark.parametrize(
    "missing_field,expected_name",
    [
        ("pilot_scope_key", "LANCEMOS_PILOT_SCOPE_KEY"),
        ("pilot_scope_version", "LANCEMOS_PILOT_SCOPE_VERSION"),
        ("pilot_tenant_key", "LANCEMOS_PILOT_TENANT_KEY"),
        ("pilot_channel_provider", "LANCEMOS_PILOT_CHANNEL_PROVIDER"),
        ("pilot_channel_account_ref", "LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF"),
    ],
)
def test_enabled_pilot_boundary_requires_complete_configuration(
    missing_field: str,
    expected_name: str,
) -> None:
    overrides = _pilot_boundary_settings()
    overrides[missing_field] = None

    with pytest.raises(ValueError, match=expected_name):
        create_app(_hotmart_settings(**overrides))


@pytest.mark.parametrize(
    "missing_field,expected_name",
    [
        ("waba_first_touch_template_name", "WABA_FIRST_TOUCH_TEMPLATE_NAME"),
        ("waba_followup_template_name", "WABA_FOLLOWUP_TEMPLATE_NAME"),
        ("waba_template_language", "WABA_TEMPLATE_LANGUAGE"),
        ("waba_template_category", "WABA_TEMPLATE_CATEGORY"),
    ],
)
def test_waba_outbound_requires_approved_template_configuration(
    missing_field: str,
    expected_name: str,
) -> None:
    overrides = _pilot_boundary_settings()
    overrides[missing_field] = None
    with pytest.raises(ValueError, match=expected_name):
        create_app(
            _hotmart_settings(
                dispatcher_outbound_enabled=True,
                **overrides,
            )
        )


def test_dispatcher_outbound_injects_agent_sender_and_allowlist() -> None:
    agent = object()
    sender = object()
    app = create_app(
        _hotmart_settings(
            allowed_jid="15555550100@s.whatsapp.net",
            dispatcher_enabled=True,
            dispatcher_outbound_enabled=True,
            dispatcher_worker_id="dispatcher-test",
            supabase_base_url="https://fake.supabase.co",
            supabase_service_role_key="service-role",
            chatwoot_base_url="https://chatwoot.example.test",
            chatwoot_account_id=1,
            chatwoot_control_api_access_token="control-token",
            chatwoot_pause_macro_id=1,
            messaging_channel="waba",
            **_pilot_boundary_settings(),
        ),
        recovery_agent_client=agent,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
    )
    dispatcher = app.state.durable_dispatcher
    assert dispatcher._recovery_agent is agent
    assert dispatcher._sender is sender
    assert dispatcher._allowed_jid == "15555550100@s.whatsapp.net"
    assert dispatcher._pilot_boundary is not None
    assert dispatcher._pilot_boundary.scope_key == "lancemos-cart-recovery"


def test_dispatcher_outbound_builds_chatwoot_sender_for_waba_scope() -> None:
    app = create_app(
        _hotmart_settings(
            allowed_jid="15555550100@s.whatsapp.net",
            dispatcher_enabled=True,
            dispatcher_outbound_enabled=True,
            dispatcher_worker_id="dispatcher-test",
            supabase_base_url="https://fake.supabase.co",
            supabase_service_role_key="service-role",
            chatwoot_base_url="https://chatwoot.example.test",
            chatwoot_account_id=1,
            chatwoot_control_api_access_token="control-token",
            chatwoot_pause_macro_id=1,
            chatwoot_inbox_id=20,
            messaging_channel="evolution",
            **_pilot_boundary_settings(),
        ),
        recovery_agent_client=object(),  # type: ignore[arg-type]
    )

    from bridge.messaging import ChatwootMessageSender, WhatsAppTemplateConfig

    sender = app.state.durable_dispatcher._sender
    assert isinstance(sender, ChatwootMessageSender)
    assert sender._template == WhatsAppTemplateConfig(
        first_touch_name="cart_recovery_first",
        followup_name="cart_recovery_followup",
        language="es_AR",
        category="MARKETING",
    )


def _post_hotmart(
    app: object,
    raw_body: bytes,
    *,
    hottok: str = HOTMART_TOKEN,
) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/webhooks/hotmart",
                content=raw_body,
                headers={
                    "Content-Type": "application/json",
                    "X-HOTMART-HOTTOK": hottok,
                },
            )

    return asyncio.run(send())


# ── Token validation ────────────────────────────────────────────────


def test_rejects_request_without_token(tmp_path) -> None:
    app = create_app(_hotmart_settings(capture_dir=tmp_path))
    raw = json.dumps(EXAMPLE_PAYLOAD).encode()
    response = _post_hotmart(app, raw, hottok="")
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_token"


def test_rejects_request_with_wrong_token(tmp_path) -> None:
    app = create_app(_hotmart_settings(capture_dir=tmp_path))
    raw = json.dumps(EXAMPLE_PAYLOAD).encode()
    response = _post_hotmart(app, raw, hottok="wrong-token")
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_token"


def test_rejects_wrong_token_without_reading_request_body(tmp_path) -> None:
    app = create_app(_hotmart_settings(capture_dir=tmp_path))
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", None) == "/webhooks/hotmart"
    )

    async def fail_if_body_is_read() -> dict[str, object]:
        raise AssertionError("unauthenticated_body_was_read")

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/webhooks/hotmart",
            "headers": [],
        },
        receive=fail_if_body_is_read,
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            route.endpoint(
                request,
                Response(),
                x_hotmart_hottok="wrong-token",
            )
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "invalid_token"


def test_returns_503_when_hotmart_not_configured(tmp_path) -> None:
    app = create_app(
        _hotmart_settings(capture_dir=tmp_path, hotmart_hottok=None)
    )
    raw = json.dumps(EXAMPLE_PAYLOAD).encode()
    response = _post_hotmart(app, raw)
    assert response.status_code == 503
    assert response.json()["detail"] == "hotmart_not_configured"


# ── Payload validation ─────────────────────────────────────────────


def test_rejects_invalid_json(tmp_path) -> None:
    app = create_app(_hotmart_settings(capture_dir=tmp_path))
    response = _post_hotmart(app, b"not-json")
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_json"


def test_rejects_hotmart_body_larger_than_one_mebibyte(tmp_path) -> None:
    app = create_app(_hotmart_settings(capture_dir=tmp_path))

    response = _post_hotmart(app, b"{" + b"x" * (1024 * 1024))

    assert response.status_code == 413
    assert response.json()["detail"] == "hotmart_webhook_body_too_large"


def test_persists_purchase_approved_for_deferred_processing(tmp_path) -> None:
    transport = _MockSupabaseTransport(
        status_code=200,
        response_body=[{
            "outcome": "inserted",
            "webhook_event_id": "inserted-event",
        }],
    )
    import bridge.supabase as supabase_mod

    original_init = supabase_mod.SupabaseClient.__init__

    def _patched_init(self, **kwargs):
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    supabase_mod.SupabaseClient.__init__ = _patched_init
    try:
        app = create_app(
            _hotmart_settings(
                capture_dir=tmp_path,
                supabase_base_url="https://fake-supabase.supabase.co",
                supabase_service_role_key="fake-service-role-key",
            )
        )
        payload = copy.deepcopy(PURCHASE_APPROVED_PAYLOAD)
        data = payload["data"]
        assert isinstance(data, dict)
        buyer = data["buyer"]
        assert isinstance(buyer, dict)
        buyer["email"] = " Buyer@Email.com.br "
        buyer["checkout_phone"] = "+55 (31) 99999-9999"
        response = _post_hotmart(app, json.dumps(payload).encode())
    finally:
        supabase_mod.SupabaseClient.__init__ = original_init

    assert response.status_code == 202
    assert response.json() == {
        "status": "received",
        "event_id": "purchase-event-001",
    }
    assert len(transport.requests) == 1
    assert transport.requests[0].url.path == (
        "/rest/v1/rpc/admit_and_correlate_hotmart_purchase_approved"
    )
    body = json.loads(transport.requests[0].content)
    assert body["p_external_event_id"] == "purchase-event-001"
    assert body["p_payload"]["event"] == "PURCHASE_APPROVED"
    assert body["p_normalized_email"] == "buyer@email.com.br"
    assert body["p_normalized_phone"] == "5531999999999"


def test_purchase_semantic_conflict_is_durable_and_not_reported_as_duplicate(
    tmp_path,
) -> None:
    transport = _MockSupabaseTransport(
        status_code=200,
        response_body=[{
            "outcome": "semantic_conflict",
            "webhook_event_id": "existing-purchase-event",
        }],
    )
    import bridge.supabase as supabase_mod

    original_init = supabase_mod.SupabaseClient.__init__

    def _patched_init(self, **kwargs):
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    supabase_mod.SupabaseClient.__init__ = _patched_init
    try:
        app = create_app(
            _hotmart_settings(
                capture_dir=tmp_path,
                supabase_base_url="https://fake-supabase.supabase.co",
                supabase_service_role_key="fake-service-role-key",
            )
        )
        response = _post_hotmart(
            app,
            json.dumps(PURCHASE_APPROVED_PAYLOAD).encode(),
        )
    finally:
        supabase_mod.SupabaseClient.__init__ = original_init

    assert response.status_code == 202
    assert response.json() == {
        "status": "conflict",
        "event_id": "purchase-event-001",
        "reason": "purchase_semantic_conflict",
    }
    assert transport.requests[0].url.path == (
        "/rest/v1/rpc/admit_and_correlate_hotmart_purchase_approved"
    )


def test_rejects_unprocessable_purchase_before_semantic_admission(tmp_path) -> None:
    transport = _MockSupabaseTransport(status_code=500)
    import bridge.supabase as supabase_mod

    original_init = supabase_mod.SupabaseClient.__init__

    def _patched_init(self, **kwargs):
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    malformed = copy.deepcopy(PURCHASE_APPROVED_PAYLOAD)
    data = malformed["data"]
    assert isinstance(data, dict)
    product = data["product"]
    assert isinstance(product, dict)
    product["id"] = "3526906"
    supabase_mod.SupabaseClient.__init__ = _patched_init
    try:
        app = create_app(
            _hotmart_settings(
                capture_dir=tmp_path,
                supabase_base_url="https://fake-supabase.supabase.co",
                supabase_service_role_key="fake-service-role-key",
            )
        )
        response = _post_hotmart(app, json.dumps(malformed).encode())
    finally:
        supabase_mod.SupabaseClient.__init__ = original_init

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "invalid_purchase_payload",
    }
    assert transport.requests == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["data"].__setitem__("buyer", {}),
        lambda payload: payload["data"].__setitem__("product", {}),
        lambda payload: payload["data"].__setitem__("offer", {}),
    ],
)
def test_rejects_unprocessable_cart_abandonment_before_admission(
    tmp_path,
    mutation,
) -> None:
    transport = _MockSupabaseTransport(status_code=500)
    import bridge.supabase as supabase_mod

    original_init = supabase_mod.SupabaseClient.__init__

    def _patched_init(self, **kwargs):
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    malformed = copy.deepcopy(EXAMPLE_PAYLOAD)
    mutation(malformed)
    supabase_mod.SupabaseClient.__init__ = _patched_init
    try:
        app = create_app(
            _hotmart_settings(
                capture_dir=tmp_path,
                supabase_base_url="https://fake-supabase.supabase.co",
                supabase_service_role_key="fake-service-role-key",
            )
        )
        response = _post_hotmart(app, json.dumps(malformed).encode())
    finally:
        supabase_mod.SupabaseClient.__init__ = original_init

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "invalid_cart_abandonment_payload",
    }
    assert transport.requests == []


def test_ignores_unsupported_version(tmp_path) -> None:
    app = create_app(_hotmart_settings(capture_dir=tmp_path))
    payload = {**EXAMPLE_PAYLOAD, "version": "1.0.0"}
    raw = json.dumps(payload).encode()
    response = _post_hotmart(app, raw)
    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "unsupported_version",
    }


def test_rejects_missing_event_id(tmp_path) -> None:
    app = create_app(_hotmart_settings(capture_dir=tmp_path))
    payload = {k: v for k, v in EXAMPLE_PAYLOAD.items() if k != "id"}
    raw = json.dumps(payload).encode()
    response = _post_hotmart(app, raw)
    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "missing_event_id",
    }


def test_rejects_invalid_creation_date(tmp_path) -> None:
    app = create_app(_hotmart_settings(capture_dir=tmp_path))
    payload = {**EXAMPLE_PAYLOAD, "creation_date": "not-a-number"}
    raw = json.dumps(payload).encode()
    response = _post_hotmart(app, raw)
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_creation_date"


def test_rejects_stale_webhook(tmp_path) -> None:
    app = create_app(
        _hotmart_settings(capture_dir=tmp_path, hotmart_max_age_seconds=60)
    )
    payload = {
        **EXAMPLE_PAYLOAD,
        "creation_date": int((time.time() - 120) * 1000),
    }
    raw = json.dumps(payload).encode()
    response = _post_hotmart(app, raw)
    assert response.status_code == 401
    assert response.json()["detail"] == "stale_webhook"


# ── Supabase persistence (with mock transport) ─────────────────────


class _MockSupabaseTransport(httpx.AsyncBaseTransport):
    """Captures the POST to PostgREST and returns a configurable response."""

    def __init__(
        self,
        *,
        status_code: int = 201,
        response_body: list[dict[str, object]] | None = None,
    ) -> None:
        self.status_code = status_code
        self.response_body = (
            [{"id": "inserted-event"}]
            if response_body is None
            else response_body
        )
        self.requests: list[httpx.Request] = []

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self.status_code,
            json=self.response_body,
            request=request,
        )


def test_persists_valid_event_to_supabase(tmp_path) -> None:
    transport = _MockSupabaseTransport(
        status_code=200,
        response_body=[{
            "outcome": "inserted",
            "webhook_event_id": "inserted-event",
        }],
    )
    # Patch SupabaseClient to use our mock transport
    import bridge.supabase as supabase_mod

    original_init = supabase_mod.SupabaseClient.__init__

    def _patched_init(self, **kwargs):
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    supabase_mod.SupabaseClient.__init__ = _patched_init
    try:
        app = create_app(
            _hotmart_settings(
                capture_dir=tmp_path,
                supabase_base_url="https://fake-supabase.supabase.co",
                supabase_service_role_key="fake-service-role-key",
            )
        )
        raw = json.dumps(EXAMPLE_PAYLOAD).encode()
        response = _post_hotmart(app, raw)
    finally:
        supabase_mod.SupabaseClient.__init__ = original_init

    assert response.status_code == 202
    assert response.json() == {
        "status": "received",
        "event_id": "0d7aa966-b887-4617-8c56-9e865bfc8ce4",
    }
    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert req.url.path == (
        "/rest/v1/rpc/admit_and_correlate_hotmart_cart_abandonment"
    )
    body = json.loads(req.content)
    assert body["p_external_event_id"] == "0d7aa966-b887-4617-8c56-9e865bfc8ce4"
    assert body["p_payload"]["data"]["buyer"]["email"] == "buyer@email.com.br"
    assert body["p_normalized_email"] == "buyer@email.com.br"
    assert body["p_normalized_phone"] == "5531999999999"


def test_returns_duplicate_for_already_stored_event(tmp_path) -> None:
    transport = _MockSupabaseTransport(
        status_code=200,
        response_body=[{
            "outcome": "duplicate",
            "webhook_event_id": "existing-event",
        }],
    )
    import bridge.supabase as supabase_mod

    original_init = supabase_mod.SupabaseClient.__init__

    def _patched_init(self, **kwargs):
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    supabase_mod.SupabaseClient.__init__ = _patched_init
    try:
        app = create_app(
            _hotmart_settings(
                capture_dir=tmp_path,
                supabase_base_url="https://fake-supabase.supabase.co",
                supabase_service_role_key="fake-service-role-key",
            )
        )
        raw = json.dumps(EXAMPLE_PAYLOAD).encode()
        response = _post_hotmart(app, raw)
    finally:
        supabase_mod.SupabaseClient.__init__ = original_init

    assert response.status_code == 200
    assert response.json() == {
        "status": "duplicate",
        "event_id": "0d7aa966-b887-4617-8c56-9e865bfc8ce4",
    }


def test_cart_abandonment_semantic_conflict_is_not_reported_as_duplicate(
    tmp_path,
) -> None:
    transport = _MockSupabaseTransport(
        status_code=200,
        response_body=[{
            "outcome": "semantic_conflict",
            "webhook_event_id": "existing-event",
        }],
    )
    import bridge.supabase as supabase_mod

    original_init = supabase_mod.SupabaseClient.__init__

    def _patched_init(self, **kwargs):
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    supabase_mod.SupabaseClient.__init__ = _patched_init
    try:
        app = create_app(
            _hotmart_settings(
                capture_dir=tmp_path,
                supabase_base_url="https://fake-supabase.supabase.co",
                supabase_service_role_key="fake-service-role-key",
            )
        )
        response = _post_hotmart(app, json.dumps(EXAMPLE_PAYLOAD).encode())
    finally:
        supabase_mod.SupabaseClient.__init__ = original_init

    assert response.status_code == 200
    assert response.json() == {
        "status": "conflict",
        "event_id": "0d7aa966-b887-4617-8c56-9e865bfc8ce4",
        "reason": "cart_abandonment_semantic_conflict",
    }


def test_returns_503_when_supabase_fails(tmp_path) -> None:
    transport = _MockSupabaseTransport(status_code=500)
    import bridge.supabase as supabase_mod

    original_init = supabase_mod.SupabaseClient.__init__

    def _patched_init(self, **kwargs):
        kwargs["transport"] = transport
        original_init(self, **kwargs)

    supabase_mod.SupabaseClient.__init__ = _patched_init
    try:
        app = create_app(
            _hotmart_settings(
                capture_dir=tmp_path,
                supabase_base_url="https://fake-supabase.supabase.co",
                supabase_service_role_key="fake-service-role-key",
            )
        )
        raw = json.dumps(EXAMPLE_PAYLOAD).encode()
        response = _post_hotmart(app, raw)
    finally:
        supabase_mod.SupabaseClient.__init__ = original_init

    assert response.status_code == 503
    assert response.json()["detail"] == "webhook_persist_unavailable"


def test_returns_503_when_supabase_not_configured(tmp_path) -> None:
    app = create_app(
        _hotmart_settings(
            capture_dir=tmp_path,
            supabase_base_url=None,
            supabase_service_role_key=None,
        )
    )
    raw = json.dumps(EXAMPLE_PAYLOAD).encode()
    response = _post_hotmart(app, raw)
    assert response.status_code == 503
    assert response.json()["detail"] == "supabase_not_configured"
