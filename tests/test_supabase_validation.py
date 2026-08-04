"""Fail-closed validation for authoritative Supabase lookup responses."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from bridge.supabase import (
    DeliveryAttempt,
    SupabaseClient,
    SupabaseCommittedResponseError,
    SupabaseError,
)


_LOOKUP_METHODS = (
    "find_contact_by_email",
    "find_contact_by_phone",
    "fetch_conversations",
    "fetch_recovery_cases",
    "fetch_channel_identities",
)


def _client(response_body: object) -> SupabaseClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body, request=request)

    return SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=httpx.MockTransport(handler),
    )


def _invoke(client: SupabaseClient, method_name: str) -> Any:
    if method_name == "find_contact_by_email":
        coroutine = client.find_contact_by_email("test@example.com")
    elif method_name == "find_contact_by_phone":
        coroutine = client.find_contact_by_phone("15550000000")
    else:
        coroutine = getattr(client, method_name)(contact_id="contact-test")
    return asyncio.run(coroutine)


@pytest.mark.parametrize("method_name", _LOOKUP_METHODS)
def test_authoritative_lookup_rejects_non_list_200_body(
    method_name: str,
) -> None:
    with pytest.raises(SupabaseError):
        _invoke(_client({"unexpected": "object"}), method_name)


@pytest.mark.parametrize(
    ("method_name", "incomplete_row"),
    [
        ("find_contact_by_email", {"contacts": None}),
        ("find_contact_by_phone", {"contacts": {"id": None}}),
        (
            "fetch_conversations",
            {
                "id": "conversation-test",
                "status": "active",
                "automation_status": "enabled",
            },
        ),
        (
            "fetch_recovery_cases",
            {"id": "case-test", "status": "active"},
        ),
        (
            "fetch_channel_identities",
            {"id": "identity-test", "channel": "whatsapp"},
        ),
    ],
)
def test_authoritative_lookup_rejects_incomplete_row(
    method_name: str,
    incomplete_row: dict[str, object],
) -> None:
    with pytest.raises(SupabaseError):
        _invoke(_client([incomplete_row]), method_name)


def test_plan_cart_recovery_calls_authoritative_rpc() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[{
                "recovery_case_id": "case-001",
                "followup_sequence_id": "sequence-001",
                "scheduled_action_id": "action-001",
                "created": True,
            }],
            request=request,
        )

    client = SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(client.plan_cart_recovery(
        webhook_event_id="event-001",
        contact_id="contact-001",
        external_product_id="3526906",
        product_name="Test Product",
        offer_code="test-offer",
        policy_key="cart-recovery-test",
        policy_version=1,
        abandoned_at="2026-08-03T12:00:00+00:00",
    ))

    assert result.recovery_case_id == "case-001"
    assert result.followup_sequence_id == "sequence-001"
    assert result.scheduled_action_id == "action-001"
    assert result.created is True
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/rest/v1/rpc/plan_cart_recovery"
    assert json.loads(requests[0].content) == {
        "p_webhook_event_id": "event-001",
        "p_contact_id": "contact-001",
        "p_external_product_id": "3526906",
        "p_product_name": "Test Product",
        "p_offer_code": "test-offer",
        "p_policy_key": "cart-recovery-test",
        "p_policy_version": 1,
        "p_abandoned_at": "2026-08-03T12:00:00+00:00",
    }


def test_claim_due_followup_actions_calls_rpc_and_validates_claim() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "id": "action-001",
            "recovery_case_id": "case-001",
            "followup_sequence_id": "sequence-001",
            "action_type": "first_contact_review",
            "status": "pending",
            "due_at": "2026-08-03T13:00:00+00:00",
            "expires_at": "2026-08-10T12:00:00+00:00",
            "expected_case_version": 1,
            "policy_key": "cart-recovery-test",
            "policy_version": 1,
            "step_key": "first_contact",
            "anchor_type": "cart_abandonment",
            "anchor_subject_internal_id": "event-001",
            "anchor_observed_at": "2026-08-03T12:00:00+00:00",
            "lease_owner": "dispatcher-1",
            "lease_generation": 1,
            "lease_expires_at": "2026-08-03T13:05:00+00:00",
            "idempotency_key": "cart_recovery:first_contact:case-001",
        }], request=request)

    client = SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=httpx.MockTransport(handler),
    )
    actions = asyncio.run(client.claim_due_followup_actions(
        worker_id="dispatcher-1",
        now="2026-08-03T13:00:00+00:00",
        lease_duration="5 minutes",
        batch_size=10,
    ))

    assert actions[0].action_id == "action-001"
    assert actions[0].lease_generation == 1
    assert requests[0].url.path == "/rest/v1/rpc/claim_due_followup_actions"
    assert json.loads(requests[0].content) == {
        "p_worker_id": "dispatcher-1",
        "p_now": "2026-08-03T13:00:00+00:00",
        "p_lease_duration": "5 minutes",
        "p_batch_size": 10,
    }


def test_claim_due_followup_actions_rejects_unleased_row() -> None:
    row = {
        "id": "action-001",
        "recovery_case_id": "case-001",
        "followup_sequence_id": "sequence-001",
        "action_type": "first_contact_review",
        "status": "pending",
        "due_at": "2026-08-03T13:00:00+00:00",
        "expires_at": "2026-08-10T12:00:00+00:00",
        "expected_case_version": 1,
        "policy_key": "cart-recovery-test",
        "policy_version": 1,
        "step_key": "first_contact",
        "anchor_type": "cart_abandonment",
        "anchor_subject_internal_id": "event-001",
        "anchor_observed_at": "2026-08-03T12:00:00+00:00",
        "lease_owner": None,
        "lease_generation": 1,
        "lease_expires_at": None,
        "idempotency_key": "cart_recovery:first_contact:case-001",
    }
    with pytest.raises(SupabaseError):
        asyncio.run(_client([row]).claim_due_followup_actions(
            worker_id="dispatcher-1",
            now="2026-08-03T13:00:00+00:00",
            lease_duration="5 minutes",
            batch_size=10,
        ))


def test_reevaluate_followup_action_calls_authoritative_rpc() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "action_id": "action-001",
            "decision": "execute",
            "reason_code": "eligible_for_execution",
            "case_version": 1,
            "sequence_revision": 1,
        }], request=request)

    client = SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=httpx.MockTransport(handler),
    )
    decision = asyncio.run(client.reevaluate_followup_action(
        action_id="action-001",
        worker_id="dispatcher-1",
        lease_generation=2,
        now="2026-08-03T13:00:00+00:00",
    ))

    assert decision.decision == "execute"
    assert decision.reason_code == "eligible_for_execution"
    assert requests[0].url.path == "/rest/v1/rpc/reevaluate_followup_action"
    assert json.loads(requests[0].content) == {
        "p_action_id": "action-001",
        "p_worker_id": "dispatcher-1",
        "p_lease_generation": 2,
        "p_now": "2026-08-03T13:00:00+00:00",
        "p_chatwoot_checked": False,
    }


def test_get_followup_chatwoot_context_is_fenced_and_typed() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "action_id": "action-001",
            "action_type": "no_reply_review",
            "chatwoot_account_id": "1",
            "external_conversation_id": "22",
            "expected_inbox_id": 7,
            "anchor_external_message_id": "40",
        }], request=request)

    client = SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=httpx.MockTransport(handler),
    )
    context = asyncio.run(client.get_followup_chatwoot_context(
        action_id="action-001",
        worker_id="dispatcher-1",
        lease_generation=2,
        now="2026-08-03T13:00:00+00:00",
    ))

    assert context.chatwoot_account_id == 1
    assert context.external_conversation_id == 22
    assert context.expected_inbox_id == 7
    assert context.anchor_external_message_id == 40
    assert requests[0].url.path == "/rest/v1/rpc/get_followup_chatwoot_context"
    assert json.loads(requests[0].content) == {
        "p_action_id": "action-001",
        "p_worker_id": "dispatcher-1",
        "p_lease_generation": 2,
        "p_now": "2026-08-03T13:00:00+00:00",
    }


def test_get_followup_execution_context_is_fenced_and_typed() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "action_id": "action-001",
            "action_type": "first_contact_review",
            "step_key": "first_contact",
            "recovery_case_id": "case-001",
            "contact_id": "contact-001",
            "source_event_id": "event-001",
            "buyer_name": "Test Buyer",
            "buyer_email": "buyer@example.com",
            "buyer_phone": "15550000000",
            "product_name": "Test Product",
            "offer_code": "offer-001",
            "current_goal": None,
            "lead_stage": "new",
        }], request=request)

    client = SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=httpx.MockTransport(handler),
    )
    context = asyncio.run(client.get_followup_execution_context(
        action_id="action-001",
        worker_id="dispatcher-1",
        lease_generation=2,
        now="2026-08-03T13:00:00+00:00",
    ))

    assert context.action_id == "action-001"
    assert context.buyer_phone == "15550000000"
    assert context.product_name == "Test Product"
    assert requests[0].url.path == "/rest/v1/rpc/get_followup_execution_context"
    assert json.loads(requests[0].content) == {
        "p_action_id": "action-001",
        "p_worker_id": "dispatcher-1",
        "p_lease_generation": 2,
        "p_now": "2026-08-03T13:00:00+00:00",
    }


def test_reserve_followup_delivery_attempt_calls_fenced_rpc_and_validates_attempt() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "id": "attempt-001",
            "action_id": "action-001",
            "idempotency_key": "cart_recovery:first_contact:case-001",
            "attempt_number": 1,
            "channel": "whatsapp",
            "mode": "freeform",
            "phase": "reserved",
            "lease_generation": 2,
            "expected_case_version": 3,
            "expected_sequence_revision": 4,
        }], request=request)

    client = SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=httpx.MockTransport(handler),
    )
    attempt = asyncio.run(client.reserve_followup_delivery_attempt(
        action_id="action-001",
        worker_id="dispatcher-1",
        lease_generation=2,
        expected_case_version=3,
        expected_sequence_revision=4,
        channel="whatsapp",
        mode="freeform",
        now="2026-08-03T13:00:00+00:00",
    ))

    assert isinstance(attempt, DeliveryAttempt)
    assert attempt.attempt_id == "attempt-001"
    assert attempt.phase == "reserved"
    assert requests[0].url.path == "/rest/v1/rpc/reserve_followup_delivery_attempt"
    assert json.loads(requests[0].content) == {
        "p_action_id": "action-001",
        "p_worker_id": "dispatcher-1",
        "p_lease_generation": 2,
        "p_expected_case_version": 3,
        "p_expected_sequence_revision": 4,
        "p_channel": "whatsapp",
        "p_mode": "freeform",
        "p_now": "2026-08-03T13:00:00+00:00",
    }


def test_reserve_followup_delivery_attempt_rejects_mismatched_action() -> None:
    client = _client([{
        "id": "attempt-001",
        "action_id": "different-action",
        "idempotency_key": "cart_recovery:first_contact:case-001",
        "attempt_number": 1,
        "channel": "whatsapp",
        "mode": "freeform",
        "phase": "reserved",
        "lease_generation": 2,
        "expected_case_version": 3,
        "expected_sequence_revision": 4,
    }])

    with pytest.raises(SupabaseError, match="action_mismatch"):
        asyncio.run(client.reserve_followup_delivery_attempt(
            action_id="action-001",
            worker_id="dispatcher-1",
            lease_generation=2,
            expected_case_version=3,
            expected_sequence_revision=4,
            channel="whatsapp",
            mode="freeform",
            now="2026-08-03T13:00:00+00:00",
        ))


def test_mark_followup_request_started_calls_fenced_rpc_and_validates_attempt() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "id": "attempt-001",
            "action_id": "action-001",
            "idempotency_key": "cart_recovery:first_contact:case-001",
            "attempt_number": 1,
            "channel": "whatsapp",
            "mode": "freeform",
            "phase": "request_started",
            "lease_generation": 2,
            "expected_case_version": 3,
            "expected_sequence_revision": 4,
        }], request=request)

    client = SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=httpx.MockTransport(handler),
    )
    attempt = asyncio.run(client.mark_followup_request_started(
        action_id="action-001",
        attempt_id="attempt-001",
        worker_id="dispatcher-1",
        lease_generation=2,
        now="2026-08-03T13:00:01+00:00",
    ))

    assert attempt.attempt_id == "attempt-001"
    assert attempt.action_id == "action-001"
    assert attempt.phase == "request_started"
    assert requests[0].url.path == "/rest/v1/rpc/mark_followup_request_started"
    assert json.loads(requests[0].content) == {
        "p_action_id": "action-001",
        "p_attempt_id": "attempt-001",
        "p_worker_id": "dispatcher-1",
        "p_lease_generation": 2,
        "p_now": "2026-08-03T13:00:01+00:00",
    }


def test_mark_followup_request_started_rejects_mismatched_attempt() -> None:
    client = _client([{
        "id": "different-attempt",
        "action_id": "action-001",
        "idempotency_key": "cart_recovery:first_contact:case-001",
        "attempt_number": 1,
        "channel": "whatsapp",
        "mode": "freeform",
        "phase": "request_started",
        "lease_generation": 2,
        "expected_case_version": 3,
        "expected_sequence_revision": 4,
    }])

    with pytest.raises(SupabaseCommittedResponseError, match="attempt_mismatch"):
        asyncio.run(client.mark_followup_request_started(
            action_id="action-001",
            attempt_id="attempt-001",
            worker_id="dispatcher-1",
            lease_generation=2,
            now="2026-08-03T13:00:01+00:00",
        ))


def test_finalize_followup_delivery_attempt_rejects_accepted_outcome_locally() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, request=request)

    client = SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SupabaseError, match="canonical_acceptance_required"):
        asyncio.run(client.finalize_followup_delivery_attempt(
            action_id="action-001",
            attempt_id="attempt-001",
            worker_id="dispatcher-1",
            lease_generation=2,
            outcome="accepted_by_chatwoot",
            remote_message_id="12345",
            accepted_message_id="00000000-0000-0000-0000-000000000123",
            reason_code="accepted_by_chatwoot",
            next_attempt_at=None,
            reconciliation_deadline=None,
            now="2026-08-03T13:00:02+00:00",
        ))

    assert requests == []


def test_record_and_finalize_followup_acceptance_calls_atomic_rpc() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "id": "action-001",
            "status": "accepted_by_chatwoot",
            "terminal_reason": "accepted_by_chatwoot",
        }], request=request)

    client = SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client.record_and_finalize_followup_acceptance(
        action_id="action-001",
        attempt_id="attempt-001",
        worker_id="dispatcher-1",
        lease_generation=2,
        external_conversation_id="7001",
        remote_message_id="8001",
        message_content="Mensaje aprobado",
        now="2026-08-03T13:00:02+00:00",
    ))

    assert result.status == "accepted_by_chatwoot"
    assert requests[0].url.path == "/rest/v1/rpc/record_and_finalize_followup_acceptance"
    assert json.loads(requests[0].content) == {
        "p_action_id": "action-001",
        "p_attempt_id": "attempt-001",
        "p_worker_id": "dispatcher-1",
        "p_lease_generation": 2,
        "p_external_conversation_id": "7001",
        "p_remote_message_id": "8001",
        "p_message_content": "Mensaje aprobado",
        "p_now": "2026-08-03T13:00:02+00:00",
    }


def test_reevaluate_followup_action_rejects_action_mismatch() -> None:
    client = _client([{
        "action_id": "different-action",
        "decision": "execute",
        "reason_code": "eligible_for_execution",
        "case_version": 1,
        "sequence_revision": 1,
    }])
    with pytest.raises(SupabaseError, match="action_mismatch"):
        asyncio.run(client.reevaluate_followup_action(
            action_id="action-001",
            worker_id="dispatcher-1",
            lease_generation=1,
            now="2026-08-03T13:00:00+00:00",
        ))


def test_conversation_lookup_rejects_non_boolean_human_takeover() -> None:
    row = {
        "id": "conversation-test",
        "status": "active",
        "automation_status": "enabled",
        "human_takeover": "false",
    }

    with pytest.raises(SupabaseError):
        _invoke(_client([row]), "fetch_conversations")


def test_conversation_lookup_rejects_invalid_message_direction() -> None:
    row = {
        "id": "conversation-test",
        "status": "active",
        "automation_status": "enabled",
        "human_takeover": False,
        "last_message_direction": "sideways",
    }

    with pytest.raises(SupabaseError):
        _invoke(_client([row]), "fetch_conversations")


def test_recovery_case_lookup_requires_product_name() -> None:
    row = {
        "id": "case-test",
        "status": "active",
        "lead_stage": "new",
        "product_name": None,
    }

    with pytest.raises(SupabaseError):
        _invoke(_client([row]), "fetch_recovery_cases")
