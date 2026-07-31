"""Tests for identity resolution of Hotmart cart-abandonment events."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from bridge.resolution import resolve_event


# ── Payload fixture ─────────────────────────────────────────────────

NOW_MS = int(time.time() * 1000)

PAYLOAD: dict[str, object] = {
    "id": "evt-001",
    "creation_date": NOW_MS,
    "event": "PURCHASE_OUT_OF_SHOPPING_CART",
    "version": "2.0.0",
    "data": {
        "affiliate": True,
        "product": {"id": 3526906, "name": "IA para empresarios"},
        "buyer": {
            "name": "Juan Perez",
            "email": "juan@example.com",
            "phone": "5531999999999",
        },
        "offer": {"code": "n82b9jqz"},
        "checkout_country": {"name": "Brasil", "iso": "BR"},
    },
}


# ── Mock Supabase transport ─────────────────────────────────────────


class MockSupabaseTransport(httpx.AsyncBaseTransport):
    """Routes PostgREST requests to configurable handlers."""

    def __init__(self) -> None:
        self.routes: dict[
            tuple[str, str], list[dict[str, object] | Exception]
        ] = {}
        self.requests: list[tuple[str, str, dict[str, str] | None]] = []

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
        # Strip query string already stripped by .path
        self.requests.append((method, path, dict(request.url.params)))

        for (r_method, prefix), responses in self.routes.items():
            if method == r_method and path.startswith(prefix):
                if not responses:
                    return httpx.Response(404, request=request)
                item = responses.pop(0)
                if isinstance(item, Exception):
                    raise httpx.ConnectError("mock error", request=request)
                status = int(item.pop("_status", 201))  # type: ignore[union-attr]
                body = json.dumps([item]) if item else "[]"
                return httpx.Response(
                    status, content=body.encode(), request=request,
                    headers={"Content-Type": "application/json"},
                )
        return httpx.Response(404, request=request)


def _make_supabase(transport: MockSupabaseTransport) -> Any:
    from bridge.supabase import SupabaseClient
    return SupabaseClient(
        base_url="https://fake.supabase.co",
        anon_key="fake-key",
        transport=transport,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── Test: new contact (no existing match) ──────────────────────────


def test_creates_new_contact_when_no_match(tmp_path) -> None:
    transport = MockSupabaseTransport()
    # find_contact_by_email → empty
    transport.set("GET", "/rest/v1/contact_points", [
        {"_status": 200},  # email lookup: no rows
    ])
    # find_contact_by_phone → empty (but won't be called if email returns empty)
    # Actually email returns [], so phone is tried too
    transport.set("GET", "/rest/v1/contact_points", [
        {"_status": 200},  # email: []
        {"_status": 200},  # phone: []
    ])
    # create_contact → returns new contact
    transport.set("POST", "/rest/v1/contacts", [
        {"_status": 201, "id": "contact-new-001"},
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
    # fetch_conversations → empty
    transport.set("GET", "/rest/v1/conversations", [
        {"_status": 200},
    ])
    # fetch_recovery_cases → just the one we created
    transport.set("GET", "/rest/v1/recovery_cases", [
        {"_status": 200, "id": "rc-001", "status": "grace_period",
         "lead_stage": "new", "current_goal": None, "product_name": "IA para empresarios"},
    ])
    # fetch_channel_identities → empty
    transport.set("GET", "/rest/v1/channel_identities", [
        {"_status": 200},
    ])
    # update_event_status → processed
    transport.set("PATCH", "/rest/v1/webhook_events", [
        {"_status": 204},
    ])

    sb = _make_supabase(transport)
    report = _run(resolve_event(
        webhook_event_id="we-001",
        payload=PAYLOAD,
        supabase=sb,
    ))

    assert report.contact_id == "contact-new-001"
    assert report.identity_resolution_status == "resolved"
    assert report.identity_resolution_strategy == "new_contact_from_hotmart"
    assert report.contact_match is None
    assert report.phone_available is True
    assert report.has_active_conversation is False
    assert report.has_open_recovery_case is True
    assert report.contact_blocked is False
    assert report.buyer_name == "Juan Perez"
    assert report.buyer_email == "juan@example.com"
    assert report.buyer_phone == "5531999999999"
    assert report.product_name == "IA para empresarios"
    assert report.offer_code == "n82b9jqz"


# ── Test: existing contact found by email ───────────────────────────


def test_matches_existing_contact_by_email(tmp_path) -> None:
    transport = MockSupabaseTransport()
    # find_contact_by_email → match!
    transport.set("GET", "/rest/v1/contact_points", [
        {
            "_status": 200,
            "contact_id": "contact-existing-001",
            "normalized_value": "juan@example.com",
            "type": "email",
            "contacts": {
                "id": "contact-existing-001",
                "full_name": "Juan Perez",
                "email": "juan@example.com",
                "phone": "5531999999999",
                "contact_permission": "allowed",
                "lifecycle_status": "qualified_lead",
            },
        },
    ])
    # create_contact_point (email + phone, idempotent)
    transport.set("POST", "/rest/v1/contact_points", [
        {"_status": 201},
        {"_status": 201},
    ])
    # create_recovery_case
    transport.set("POST", "/rest/v1/recovery_cases", [
        {"_status": 201, "id": "rc-002"},
    ])
    # log_resolution_attempt
    transport.set("POST", "/rest/v1/identity_resolution_attempts", [
        {"_status": 201},
    ])
    # fetch_conversations → one active
    transport.set("GET", "/rest/v1/conversations", [
        {"_status": 200, "id": "conv-001", "status": "active",
         "automation_status": "enabled", "human_takeover": False,
         "last_message_direction": "inbound", "last_inbound_at": "2026-07-31T10:00:00Z",
         "last_outbound_at": None, "paused_until": None},
    ])
    # fetch_recovery_cases
    transport.set("GET", "/rest/v1/recovery_cases", [
        {"_status": 200, "id": "rc-002", "status": "grace_period",
         "lead_stage": "new", "current_goal": None, "product_name": "IA para empresarios"},
    ])
    # fetch_channel_identities → one whatsapp
    transport.set("GET", "/rest/v1/channel_identities", [
        {"_status": 200, "id": "ci-001", "channel": "whatsapp",
         "external_user_id": "5531999999999", "identity_status": "active"},
    ])
    # update_event_status
    transport.set("PATCH", "/rest/v1/webhook_events", [
        {"_status": 204},
    ])

    sb = _make_supabase(transport)
    report = _run(resolve_event(
        webhook_event_id="we-002",
        payload=PAYLOAD,
        supabase=sb,
    ))

    assert report.contact_id == "contact-existing-001"
    assert report.contact_match is not None
    assert report.contact_match.matched_by == "email"
    assert report.contact_match.lifecycle_status == "qualified_lead"
    assert report.identity_resolution_strategy == "existing_identity_by_email"
    assert report.has_active_conversation is True
    assert report.has_open_recovery_case is True
    assert report.contact_blocked is False


# ── Test: contact blocked by contact_permission ────────────────────


def test_detects_blocked_contact(tmp_path) -> None:
    transport = MockSupabaseTransport()
    # find by email → match with do_not_contact
    transport.set("GET", "/rest/v1/contact_points", [
        {
            "_status": 200,
            "contact_id": "blocked-001",
            "normalized_value": "juan@example.com",
            "type": "email",
            "contacts": {
                "id": "blocked-001",
                "full_name": "Juan Perez",
                "email": "juan@example.com",
                "phone": None,
                "contact_permission": "opted_out",
                "lifecycle_status": "closed_lost",
            },
        },
    ])
    # create_contact_point (email only, no phone in this match)
    transport.set("POST", "/rest/v1/contact_points", [
        {"_status": 201},
        {"_status": 201},
    ])
    # create_recovery_case
    transport.set("POST", "/rest/v1/recovery_cases", [
        {"_status": 201, "id": "rc-003"},
    ])
    # log_resolution_attempt
    transport.set("POST", "/rest/v1/identity_resolution_attempts", [
        {"_status": 201},
    ])
    # fetch_conversations → empty
    transport.set("GET", "/rest/v1/conversations", [{"_status": 200}])
    # fetch_recovery_cases
    transport.set("GET", "/rest/v1/recovery_cases", [{"_status": 200}])
    # fetch_channel_identities → empty
    transport.set("GET", "/rest/v1/channel_identities", [{"_status": 200}])
    # update_event_status
    transport.set("PATCH", "/rest/v1/webhook_events", [{"_status": 204}])

    sb = _make_supabase(transport)
    report = _run(resolve_event(
        webhook_event_id="we-003",
        payload=PAYLOAD,
        supabase=sb,
    ))

    assert report.contact_blocked is True
    assert report.contact_match is not None
    assert report.contact_match.contact_permission == "opted_out"


# ── Test: invalid payload ─────────────────────────────────────────


def test_invalid_payload_marks_event_failed(tmp_path) -> None:
    transport = MockSupabaseTransport()
    # update_event_status → failed
    transport.set("PATCH", "/rest/v1/webhook_events", [{"_status": 204}])

    sb = _make_supabase(transport)
    with pytest.raises(Exception, match="invalid_payload_structure"):
        _run(resolve_event(
            webhook_event_id="we-bad",
            payload={"not": "valid"},
            supabase=sb,
        ))


# ── Test: phone missing from payload ───────────────────────────────


def test_handles_missing_phone(tmp_path) -> None:
    payload_no_phone = {
        **PAYLOAD,
        "data": {
            **PAYLOAD["data"],  # type: ignore[dict-item]
            "buyer": {"name": "Juan Perez", "email": "juan@example.com"},
        },
    }
    transport = MockSupabaseTransport()
    # email lookup → empty
    transport.set("GET", "/rest/v1/contact_points", [
        {"_status": 200},
        {"_status": 200},  # phone lookup won't run since phone is None
    ])
    # create_contact
    transport.set("POST", "/rest/v1/contacts", [
        {"_status": 201, "id": "contact-no-phone"},
    ])
    # create_contact_point (email only, no phone)
    transport.set("POST", "/rest/v1/contact_points", [
        {"_status": 201},
    ])
    # create_recovery_case
    transport.set("POST", "/rest/v1/recovery_cases", [
        {"_status": 201, "id": "rc-no-phone"},
    ])
    # log_resolution_attempt
    transport.set("POST", "/rest/v1/identity_resolution_attempts", [
        {"_status": 201},
    ])
    # fetch_conversations → empty
    transport.set("GET", "/rest/v1/conversations", [{"_status": 200}])
    # fetch_recovery_cases
    transport.set("GET", "/rest/v1/recovery_cases", [{"_status": 200}])
    # fetch_channel_identities
    transport.set("GET", "/rest/v1/channel_identities", [{"_status": 200}])
    # update_event_status
    transport.set("PATCH", "/rest/v1/webhook_events", [{"_status": 204}])

    sb = _make_supabase(transport)
    report = _run(resolve_event(
        webhook_event_id="we-no-phone",
        payload=payload_no_phone,
        supabase=sb,
    ))

    assert report.phone_available is False
    assert report.buyer_phone is None
