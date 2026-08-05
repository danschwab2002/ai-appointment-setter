"""Tests for the Hotmart webhook receiver."""

from __future__ import annotations

import asyncio
import copy
import json
import time

import httpx
import pytest

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


def test_worker_requires_complete_durable_policy_configuration() -> None:
    with pytest.raises(ValueError, match="FOLLOWUP_POLICY_KEY and FOLLOWUP_POLICY_VERSION"):
        create_app(_hotmart_settings(
            worker_enabled=True,
            supabase_base_url="https://fake.supabase.co",
            supabase_service_role_key="service-role",
            followup_policy_version=None,
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
        ))


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
        ),
        recovery_agent_client=agent,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
    )
    dispatcher = app.state.durable_dispatcher
    assert dispatcher._recovery_agent is agent
    assert dispatcher._sender is sender
    assert dispatcher._allowed_jid == "15555550100@s.whatsapp.net"


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


def test_ignores_unsupported_event_type(tmp_path) -> None:
    app = create_app(_hotmart_settings(capture_dir=tmp_path))
    payload = {**EXAMPLE_PAYLOAD, "event": "PURCHASE_APPROVED"}
    raw = json.dumps(payload).encode()
    response = _post_hotmart(app, raw)
    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "unsupported_event_type",
    }


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
        self.response_body = response_body or []
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
    transport = _MockSupabaseTransport(status_code=201)
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
    assert req.url.path == "/rest/v1/webhook_events"
    body = json.loads(req.content)
    assert body["source"] == "hotmart"
    assert body["external_event_id"] == "0d7aa966-b887-4617-8c56-9e865bfc8ce4"
    assert body["event_type"] == "PURCHASE_OUT_OF_SHOPPING_CART"
    assert body["processing_status"] == "received"
    assert body["payload"]["data"]["buyer"]["email"] == "buyer@email.com.br"


def test_returns_duplicate_for_already_stored_event(tmp_path) -> None:
    transport = _MockSupabaseTransport(status_code=409)
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
