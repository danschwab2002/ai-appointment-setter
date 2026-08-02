"""Tests for the deferred resolution worker."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from bridge.supabase import SupabaseClient
from bridge.worker import ResolutionWorker


NOW_MS = int(time.time() * 1000)

PAYLOAD: dict[str, object] = {
    "id": "evt-worker-001",
    "creation_date": NOW_MS,
    "event": "PURCHASE_OUT_OF_SHOPPING_CART",
    "version": "2.0.0",
    "data": {
        "affiliate": True,
        "product": {"id": 3526906, "name": "Test Product"},
        "buyer": {
            "name": "Test Buyer",
            "email": "buyer@test.com",
            "phone": "5531999999999",
        },
        "offer": {"code": "testcode"},
        "checkout_country": {"name": "Brasil", "iso": "BR"},
    },
}


class MockTransport(httpx.AsyncBaseTransport):
    """Configurable mock that routes PostgREST requests to handlers."""

    def __init__(self) -> None:
        self.routes: dict[
            tuple[str, str], list[dict[str, object] | Exception]
        ] = {}
        self.requests: list[tuple[str, str]] = []

    def set(
        self,
        method: str,
        path_prefix: str,
        responses: list[dict[str, object] | Exception],
    ) -> None:
        self.routes[(method, path_prefix)] = responses

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        path = request.url.path
        method = request.method
        self.requests.append((method, path))

        for (r_method, prefix), responses in self.routes.items():
            if method == r_method and path.startswith(prefix):
                if not responses:
                    # Default: 200 with empty array for GET, 204 for PATCH
                    if method == "GET":
                        return httpx.Response(
                            200, content=b"[]", request=request
                        )
                    return httpx.Response(204, request=request)
                item = responses.pop(0)
                if isinstance(item, Exception):
                    raise httpx.ConnectError("mock error", request=request)
                status = int(item.pop("_status", 201))  # type: ignore[union-attr]
                body = json.dumps([item]) if item else "[]"
                return httpx.Response(
                    status,
                    content=body.encode(),
                    request=request,
                    headers={"Content-Type": "application/json"},
                )
        if method == "GET":
            return httpx.Response(200, content=b"[]", request=request)
        return httpx.Response(204, request=request)


def _make_supabase(transport: MockTransport) -> SupabaseClient:
    return SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=transport,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_worker_logs_when_recovery_agent_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_resolve_event(**_: object) -> object:
        return SimpleNamespace(event_id="event-001")

    monkeypatch.setattr("bridge.worker.resolve_event", fake_resolve_event)
    worker = ResolutionWorker(supabase=object())  # type: ignore[arg-type]

    with caplog.at_level("WARNING", logger="bridge.worker"):
        _run(worker._process_one({"id": "event-001", "payload": {}}))

    assert "recovery_agent_not_configured event_id=event-001" in caplog.text


def test_worker_logs_when_recovery_proposal_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_resolve_event(**_: object) -> object:
        return SimpleNamespace(
            event_id="event-002",
            to_dict=lambda: {"event_id": "event-002"},
        )

    class AgentWithoutProposal:
        async def request_proposal(self, **_: object) -> None:
            return None

    monkeypatch.setattr("bridge.worker.resolve_event", fake_resolve_event)
    worker = ResolutionWorker(
        supabase=object(),  # type: ignore[arg-type]
        recovery_agent=AgentWithoutProposal(),  # type: ignore[arg-type]
    )

    with caplog.at_level("WARNING", logger="bridge.worker"):
        _run(worker._process_one({"id": "event-002", "payload": {}}))

    assert "recovery_proposal_unavailable event_id=event-002" in caplog.text


def test_worker_logs_when_first_touch_sender_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fake_resolve_event(**_: object) -> object:
        return SimpleNamespace(
            event_id="event-003",
            phone_available=True,
            buyer_phone="12025550123",
            to_dict=lambda: {
                "event_id": "event-003",
                "authoritative_context_complete": True,
                "contact_blocked": False,
                "phone_available": True,
                "any_conversation_human_takeover": False,
                "has_active_conversation": False,
                "has_open_recovery_case": False,
            },
        )

    class FirstTouchAgent:
        async def request_proposal(self, **_: object) -> dict[str, object]:
            return {
                "action": "send_first_touch",
                "reason_code": "first_touch",
                "message": "Test message",
            }

    monkeypatch.setattr("bridge.worker.resolve_event", fake_resolve_event)
    worker = ResolutionWorker(
        supabase=object(),  # type: ignore[arg-type]
        recovery_agent=FirstTouchAgent(),  # type: ignore[arg-type]
    )

    with caplog.at_level("WARNING", logger="bridge.worker"):
        _run(worker._process_one({"id": "event-003", "payload": {}}))

    assert "first_touch_sender_not_configured event_id=event-003" in caplog.text


def test_worker_blocks_send_when_authoritative_context_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    report_dict = {
        "authoritative_context_complete": False,
        "contact_blocked": False,
        "phone_available": True,
        "any_conversation_human_takeover": False,
        "has_active_conversation": False,
        "has_open_recovery_case": False,
    }

    async def fake_resolve_event(**_: object) -> object:
        return SimpleNamespace(
            event_id="event-incomplete-context",
            phone_available=True,
            buyer_phone="12025550123",
            to_dict=lambda: report_dict,
        )

    class UnexpectedAgent:
        async def request_proposal(self, **_: object) -> dict[str, object]:
            return {
                "action": "send_first_touch",
                "reason_code": "first_touch",
                "message": "Test message",
            }

    class SenderThatMustNotRun:
        async def send_first_touch(self, **_: object) -> object:
            raise AssertionError("message sender must not be invoked")

    monkeypatch.setattr("bridge.worker.resolve_event", fake_resolve_event)
    worker = ResolutionWorker(
        supabase=object(),  # type: ignore[arg-type]
        recovery_agent=UnexpectedAgent(),  # type: ignore[arg-type]
        message_sender=SenderThatMustNotRun(),  # type: ignore[arg-type]
    )

    with caplog.at_level("ERROR", logger="bridge.worker"):
        _run(worker._process_one({
            "id": "event-incomplete-context",
            "payload": {},
        }))

    assert "first_touch_decision_guard_blocked" in caplog.text


# ── Test: worker processes a pending event ──────────────────────────


def test_worker_processes_pending_event() -> None:
    transport = MockTransport()
    # fetch_pending_events → one event
    transport.set("GET", "/rest/v1/webhook_events", [
        {"_status": 200, "id": "we-001", "source": "hotmart",
         "external_event_id": "evt-worker-001", "event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
         "payload": PAYLOAD},
    ])
    # find_contact_by_email → empty
    transport.set("GET", "/rest/v1/contact_points", [
        {"_status": 200},
        {"_status": 200},  # phone lookup
    ])
    # create_contact
    transport.set("POST", "/rest/v1/contacts", [
        {"_status": 201, "id": "contact-001"},
    ])
    # create_contact_point (email + phone)
    transport.set("POST", "/rest/v1/contact_points", [
        {"_status": 201},
        {"_status": 201},
    ])
    # create_recovery_case
    transport.set("POST", "/rest/v1/recovery_cases", [
        {"_status": 201, "id": "rc-001"},
    ])
    # log_resolution_attempt
    transport.set("POST", "/rest/v1/identity_resolution_attempts", [
        {"_status": 201},
    ])
    # fetch_conversations
    transport.set("GET", "/rest/v1/conversations", [{"_status": 200}])
    # fetch_recovery_cases
    transport.set("GET", "/rest/v1/recovery_cases", [{"_status": 200}])
    # fetch_channel_identities
    transport.set("GET", "/rest/v1/channel_identities", [{"_status": 200}])
    # update_event_status → processed
    transport.set("PATCH", "/rest/v1/webhook_events", [{"_status": 204}])

    sb = _make_supabase(transport)
    worker = ResolutionWorker(
        supabase=sb,
        poll_interval_seconds=0.1,
        batch_size=5,
    )

    async def run_test():
        await worker.start()
        # Wait for the event to be processed
        await asyncio.sleep(0.5)
        await worker.stop()

    _run(run_test())

    # Verify the event was processed (PATCH to webhook_events was called)
    patch_requests = [r for r in transport.requests if r[0] == "PATCH"]
    assert len(patch_requests) > 0
    assert any("webhook_events" in r[1] for r in patch_requests)


# ── Test: worker handles empty queue gracefully ─────────────────────


def test_worker_handles_empty_queue() -> None:
    transport = MockTransport()
    # fetch_pending_events → empty
    transport.set("GET", "/rest/v1/webhook_events", [{"_status": 200}])

    sb = _make_supabase(transport)
    worker = ResolutionWorker(
        supabase=sb,
        poll_interval_seconds=0.1,
        batch_size=5,
    )

    async def run_test():
        await worker.start()
        await asyncio.sleep(0.3)
        await worker.stop()

    _run(run_test())

    # Only GET requests, no PATCH (nothing to process)
    patch_requests = [r for r in transport.requests if r[0] == "PATCH"]
    assert len(patch_requests) == 0


# ── Test: worker handles invalid payload ────────────────────────────


def test_worker_marks_invalid_payload_as_failed() -> None:
    transport = MockTransport()
    # fetch_pending_events → one event with invalid payload
    transport.set("GET", "/rest/v1/webhook_events", [
        {"_status": 200, "id": "we-bad", "source": "hotmart",
         "external_event_id": "evt-bad", "event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
         "payload": {"not": "valid"}},
    ])
    # update_event_status → failed
    transport.set("PATCH", "/rest/v1/webhook_events", [{"_status": 204}])

    sb = _make_supabase(transport)
    worker = ResolutionWorker(
        supabase=sb,
        poll_interval_seconds=0.1,
        batch_size=5,
    )

    async def run_test():
        await worker.start()
        await asyncio.sleep(0.5)
        await worker.stop()

    _run(run_test())

    # Verify the event was marked as failed
    patch_requests = [r for r in transport.requests if r[0] == "PATCH"]
    assert len(patch_requests) > 0


# ── Test: worker survives Supabase errors ───────────────────────────


def test_worker_survives_supabase_errors() -> None:
    transport = MockTransport()
    # fetch_pending_events → raise error
    transport.set("GET", "/rest/v1/webhook_events", [
        httpx.ConnectError("connection refused"),
    ])

    sb = _make_supabase(transport)
    worker = ResolutionWorker(
        supabase=sb,
        poll_interval_seconds=0.1,
        batch_size=5,
    )

    async def run_test():
        await worker.start()
        await asyncio.sleep(0.5)
        await worker.stop()

    _run(run_test())

    # Worker should not crash — it should keep trying
    # If we get here without hanging, the test passes


# ── Test: worker start/stop is idempotent ──────────────────────────


def test_worker_start_stop_idempotent() -> None:
    transport = MockTransport()
    transport.set("GET", "/rest/v1/webhook_events", [{"_status": 200}])

    sb = _make_supabase(transport)
    worker = ResolutionWorker(
        supabase=sb,
        poll_interval_seconds=0.5,
        batch_size=5,
    )

    async def run_test():
        # Double start should not create two tasks
        await worker.start()
        await worker.start()
        await asyncio.sleep(0.1)
        # Double stop should be fine
        await worker.stop()
        await worker.stop()

    _run(run_test())
