"""Tests for identity resolution of Hotmart cart-abandonment events."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from bridge.resolution import resolve_event
from bridge.recovery_agent import required_recovery_decision


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
        service_role_key="fake-service-role-key",
        transport=transport,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _configure_new_contact_resolution(
    transport: MockSupabaseTransport,
    *,
    failed_context_path: str | None = None,
    failed_identity_lookup: str | None = None,
) -> None:
    identity_responses: list[dict[str, object] | Exception] = [
        {"_status": 200},
        {"_status": 200},
    ]
    if failed_identity_lookup == "email":
        identity_responses[0] = Exception("email lookup unavailable")
    elif failed_identity_lookup == "phone":
        identity_responses[1] = Exception("phone lookup unavailable")
    transport.set("GET", "/rest/v1/contact_points", [
        *identity_responses,
    ])
    transport.set("POST", "/rest/v1/contacts", [
        {"_status": 201, "id": "contact-context-test"},
    ])
    transport.set("POST", "/rest/v1/contact_points", [
        {"_status": 201},
        {"_status": 201},
    ])
    transport.set("POST", "/rest/v1/recovery_cases", [
        {"_status": 201, "id": "rc-context-test"},
    ])
    transport.set("POST", "/rest/v1/identity_resolution_attempts", [
        {"_status": 201},
    ])
    for path in (
        "/rest/v1/conversations",
        "/rest/v1/recovery_cases",
        "/rest/v1/channel_identities",
    ):
        response: dict[str, object] | Exception
        response = (
            Exception("context lookup unavailable")
            if path == failed_context_path
            else {"_status": 200}
        )
        transport.set("GET", path, [response])
    transport.set("PATCH", "/rest/v1/webhook_events", [
        {"_status": 204},
    ])


def test_durable_plan_is_committed_before_event_is_marked_processed() -> None:
    transport = MockSupabaseTransport()
    transport.set("GET", "/rest/v1/contact_points", [
        {"_status": 200},
        {"_status": 200},
    ])
    transport.set("POST", "/rest/v1/contacts", [
        {"_status": 201, "id": "contact-durable"},
    ])
    transport.set("POST", "/rest/v1/contact_points", [
        {"_status": 201},
        {"_status": 201},
    ])
    transport.set("POST", "/rest/v1/rpc/plan_cart_recovery", [{
        "_status": 200,
        "recovery_case_id": "case-durable",
        "followup_sequence_id": "sequence-durable",
        "scheduled_action_id": "action-durable",
        "created": True,
    }])
    transport.set("POST", "/rest/v1/identity_resolution_attempts", [
        {"_status": 201},
    ])
    transport.set("GET", "/rest/v1/conversations", [{"_status": 200}])
    transport.set("GET", "/rest/v1/recovery_cases", [{"_status": 200}])
    transport.set("GET", "/rest/v1/channel_identities", [{"_status": 200}])
    transport.set("PATCH", "/rest/v1/webhook_events", [{"_status": 204}])

    _run(resolve_event(
        webhook_event_id="we-durable",
        payload=PAYLOAD,
        supabase=_make_supabase(transport),
        policy_key="cart-recovery-test",
        policy_version=1,
    ))

    paths = [(method, path) for method, path, _ in transport.requests]
    assert ("POST", "/rest/v1/recovery_cases") not in paths
    assert paths.index(("POST", "/rest/v1/rpc/plan_cart_recovery")) < paths.index(
        ("PATCH", "/rest/v1/webhook_events")
    )


def test_durable_plan_failure_does_not_mark_event_processed() -> None:
    transport = MockSupabaseTransport()
    transport.set("GET", "/rest/v1/contact_points", [
        {"_status": 200},
        {"_status": 200},
    ])
    transport.set("POST", "/rest/v1/contacts", [
        {"_status": 201, "id": "contact-plan-failure"},
    ])
    transport.set("POST", "/rest/v1/contact_points", [
        {"_status": 201},
        {"_status": 201},
    ])
    transport.set("POST", "/rest/v1/rpc/plan_cart_recovery", [{
        "_status": 500,
    }])
    transport.set("PATCH", "/rest/v1/webhook_events", [{"_status": 204}])

    with pytest.raises(Exception, match="create_recovery_case_failed"):
        _run(resolve_event(
            webhook_event_id="we-plan-failure",
            payload=PAYLOAD,
            supabase=_make_supabase(transport),
            policy_key="cart-recovery-test",
            policy_version=1,
        ))

    patches = [
        request for request in transport.requests
        if request[0:2] == ("PATCH", "/rest/v1/webhook_events")
    ]
    assert len(patches) == 1


@pytest.mark.parametrize(
    "failed_context_path",
    [
        "/rest/v1/conversations",
        "/rest/v1/recovery_cases",
        "/rest/v1/channel_identities",
    ],
)
def test_context_lookup_failure_forces_insufficient_context(
    failed_context_path: str,
) -> None:
    transport = MockSupabaseTransport()
    _configure_new_contact_resolution(
        transport,
        failed_context_path=failed_context_path,
    )

    report = _run(resolve_event(
        webhook_event_id="we-context-failure",
        payload=PAYLOAD,
        supabase=_make_supabase(transport),
    ))

    assert report.authoritative_context_complete is False
    assert required_recovery_decision(report.to_dict()) == {
        "action": "handoff",
        "reason_code": "insufficient_context",
    }


@pytest.mark.parametrize("failed_identity_lookup", ["email", "phone"])
def test_identity_lookup_failure_forces_insufficient_context(
    failed_identity_lookup: str,
) -> None:
    transport = MockSupabaseTransport()
    _configure_new_contact_resolution(
        transport,
        failed_identity_lookup=failed_identity_lookup,
    )

    report = _run(resolve_event(
        webhook_event_id="we-identity-lookup-failure",
        payload=PAYLOAD,
        supabase=_make_supabase(transport),
    ))

    assert report.authoritative_context_complete is False
    assert required_recovery_decision(report.to_dict()) == {
        "action": "handoff",
        "reason_code": "insufficient_context",
    }


def test_recovery_case_stays_pending_without_selected_channel_identity() -> None:
    captured_body: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(
            201,
            json=[{"id": "case-001"}],
            request=request,
        )

    from bridge.supabase import SupabaseClient

    client = SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=httpx.MockTransport(handler),
    )

    _run(
        client.create_recovery_case(
            contact_id="00000000-0000-0000-0000-000000000001",
            abandonment_event_id="00000000-0000-0000-0000-000000000002",
            external_product_id="3526906",
            product_name="IA para empresarios",
            offer_code="testcode001",
            grace_expires_at="2026-08-01T17:00:00+00:00",
        )
    )

    assert captured_body.get("identity_resolution_status", "pending") == "pending"


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
    assert report.has_open_recovery_case is False
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
    # fetch_conversations → one active with human takeover
    transport.set("GET", "/rest/v1/conversations", [
        {"_status": 200, "id": "conv-001", "status": "active",
         "automation_status": "enabled", "human_takeover": True,
         "last_message_direction": "inbound", "last_inbound_at": "2026-07-31T10:00:00Z",
         "last_outbound_at": None, "paused_until": None},
    ])
    # fetch_recovery_cases → a distinct pre-existing open case
    transport.set("GET", "/rest/v1/recovery_cases", [
        {"_status": 200, "id": "rc-prior", "status": "grace_period",
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
    assert report.authoritative_context_complete is True
    assert report.any_conversation_human_takeover is True
    assert report.has_active_conversation is False
    assert report.has_open_recovery_case is True
    assert report.contact_blocked is False
    assert required_recovery_decision(report.to_dict()) == {
        "action": "abort",
        "reason_code": "human_takeover_active",
    }


# ── Test: contact blocked by contact_permission ────────────────────


def test_detects_do_not_contact_lifecycle(tmp_path) -> None:
    transport = MockSupabaseTransport()
    # find by email → allowed permission but do_not_contact lifecycle
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
                "contact_permission": "allowed",
                "lifecycle_status": "do_not_contact",
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
    assert report.contact_match.contact_permission == "allowed"
    assert report.contact_match.lifecycle_status == "do_not_contact"


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
