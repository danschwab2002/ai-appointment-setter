"""Tests for the Hotmart webhook receiver."""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from bridge.app import Settings, create_app


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


def _hotmart_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "webhook_secret": "unused",
        "allowed_jid": "unused@s.whatsapp.net",
        "capture_dir": "/tmp/unused-captures",
        "max_age_seconds": 300,
        "hotmart_hottok": HOTMART_TOKEN,
        "hotmart_max_age_seconds": 300,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


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
                supabase_anon_key="fake-anon-key",
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
                supabase_anon_key="fake-anon-key",
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
                supabase_anon_key="fake-anon-key",
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
            supabase_anon_key=None,
        )
    )
    raw = json.dumps(EXAMPLE_PAYLOAD).encode()
    response = _post_hotmart(app, raw)
    assert response.status_code == 503
    assert response.json()["detail"] == "supabase_not_configured"
