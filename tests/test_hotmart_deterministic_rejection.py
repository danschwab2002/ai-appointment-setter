"""A deterministic rejection must be discarded with 200, never reported as 503.

Hotmart counts sustained failures to disable the webhook configuration, and the
cart abandonment path is the only business entry point currently enabled. An
event this system cannot process is not an outage: replying 503 asks the sender
to retry something that can never succeed, and spends the counter that switches
the channel off.

The payload under test is captured from the Hotmart panel, not invented here:
tests/fixtures/hotmart_cart_abandonment_rejected_v1.json. It was rejected in
production on 2026-09-20 with `johanna_hotmart_cart_scope_mismatch`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from bridge.app import Settings, create_app
from bridge.supabase import (
    SupabaseError,
    SupabasePermanentError,
    _deterministic_rejection,
)

FIXTURE = Path(__file__).parent / "fixtures/hotmart_cart_abandonment_rejected_v1.json"
CAPTURED = json.loads(FIXTURE.read_text(encoding="utf-8"))
CAPTURED_PAYLOAD = CAPTURED["payload"]
HOTMART_TOKEN = "hottok-test"


def _response(status_code: int, body: object) -> httpx.Response:
    request = httpx.Request("POST", "https://fake-supabase.supabase.co/rest/v1/rpc/x")
    return httpx.Response(status_code, json=body, request=request)


# ── the classifier ──────────────────────────────────────────────────


def test_data_exception_is_a_deterministic_rejection() -> None:
    response = _response(
        400, {"code": "22023", "message": "johanna_hotmart_cart_scope_mismatch"}
    )
    assert _deterministic_rejection(response) == "johanna_hotmart_cart_scope_mismatch"


def test_unknown_data_exception_is_covered_without_being_listed() -> None:
    """Classification is by SQLSTATE class, so a rejection added later is covered."""
    response = _response(
        400, {"code": "22000", "message": "a_rejection_nobody_enumerated_yet"}
    )
    assert _deterministic_rejection(response) == "a_rejection_nobody_enumerated_yet"


def test_server_error_is_not_a_rejection() -> None:
    assert _deterministic_rejection(_response(500, {"code": "22023"})) is None
    assert _deterministic_rejection(_response(503, {"message": "down"})) is None


def test_non_data_sqlstate_is_not_a_rejection() -> None:
    """Class 23 can be a transient race, so it stays retryable on purpose."""
    response = _response(409, {"code": "23505", "message": "duplicate key"})
    assert _deterministic_rejection(response) is None


def test_missing_or_unparseable_body_is_not_a_rejection() -> None:
    request = httpx.Request("POST", "https://fake-supabase.supabase.co/rest/v1/rpc/x")
    assert _deterministic_rejection(
        httpx.Response(400, content=b"not json", request=request)
    ) is None
    assert _deterministic_rejection(_response(400, {"message": "no code"})) is None
    assert _deterministic_rejection(_response(400, ["a list"])) is None


def test_permanent_error_is_still_a_supabase_error() -> None:
    """Existing handlers catching SupabaseError keep catching this one."""
    error = SupabasePermanentError("op_rejected: reason", reason="reason")
    assert isinstance(error, SupabaseError)
    assert error.reason == "reason"


# ── the webhook ─────────────────────────────────────────────────────


class _RejectingTransport(httpx.AsyncBaseTransport):
    """PostgREST answering exactly as it does for a raised data exception."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "code": self.code,
                "message": self.message,
                "details": None,
                "hint": None,
            },
            request=request,
        )


class _UnavailableTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"}, request=request)


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[arg-type]
        webhook_secret="unused",
        allowed_jid="unused@s.whatsapp.net",
        capture_dir=str(tmp_path),
        max_age_seconds=300,
        hotmart_hottok=HOTMART_TOKEN,
        hotmart_max_age_seconds=300,
        chatwoot_account_id=1,
        chatwoot_inbox_id=7,
        supabase_base_url="https://fake-supabase.supabase.co",
        supabase_service_role_key="fake-service-role-key",
    )


def _fresh(payload: dict) -> dict:
    """Move only the timestamp to now, leaving every other field as captured.

    The real event was two seconds old when Hotmart delivered it. Freshness is
    checked against the wall clock (`hotmart_max_age_seconds`, 300s), which is a
    different dimension from what these tests exercise: an event replayed a day
    later is rejected with 401 before reaching Supabase at all.
    """
    revived = json.loads(json.dumps(payload))
    revived["creation_date"] = int(time.time() * 1000)
    return revived


def _post(app: object, payload: dict) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/webhooks/hotmart",
                content=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-HOTMART-HOTTOK": HOTMART_TOKEN,
                },
            )

    return asyncio.run(send())


@pytest.fixture
def patched_transport(monkeypatch):
    def _apply(transport: httpx.AsyncBaseTransport) -> None:
        import bridge.supabase as supabase_mod

        original_init = supabase_mod.SupabaseClient.__init__

        def _patched_init(self, **kwargs):
            kwargs["transport"] = transport
            original_init(self, **kwargs)

        monkeypatch.setattr(supabase_mod.SupabaseClient, "__init__", _patched_init)

    return _apply


def test_captured_rejected_payload_is_discarded_with_200(
    tmp_path, patched_transport
) -> None:
    """The exact event production rejected 103 times in three days."""
    patched_transport(
        _RejectingTransport(
            code="22023", message="johanna_hotmart_cart_scope_mismatch"
        )
    )
    response = _post(create_app(_settings(tmp_path)), _fresh(CAPTURED_PAYLOAD))

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "johanna_hotmart_cart_scope_mismatch"
    assert body["event_id"] == CAPTURED_PAYLOAD["id"]


def test_real_unavailability_still_answers_503(tmp_path, patched_transport) -> None:
    """Retrying is the right advice when the service is actually down."""
    patched_transport(_UnavailableTransport())
    response = _post(create_app(_settings(tmp_path)), _fresh(CAPTURED_PAYLOAD))

    assert response.status_code == 503
    assert response.json()["detail"] == "webhook_persist_unavailable"
