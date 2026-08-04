"""Tests for the deferred resolution worker."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

from bridge.chatwoot import CanonicalConversationSnapshot
from bridge.messaging import FirstTouchResult
from bridge.recovery_agent import FollowupMessageProposal
from bridge.supabase import (
    ChatwootAuthorityContext,
    DeliveryAttempt,
    FollowupExecutionContext,
    ReevaluationDecision,
    ScheduledAction,
    SupabaseClient,
    SupabaseCommittedResponseError,
    SupabaseError,
)
import bridge.worker as worker_module
from bridge.worker import (
    DurableDispatcher,
    ResolutionWorker,
    _await_despite_cancellation,
    _validate_followup_execution_context,
    _validate_followup_message_proposal,
    _validate_reserved_delivery_attempt,
)


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


def _execution_context() -> FollowupExecutionContext:
    return FollowupExecutionContext(
        action_id="action-001",
        action_type="first_contact_review",
        step_key="first_contact",
        recovery_case_id="case-001",
        contact_id="contact-001",
        source_event_id="event-001",
        buyer_name="Ana",
        buyer_email=None,
        buyer_phone=None,
        product_name="Curso Uno",
        offer_code=None,
        current_goal=None,
        lead_stage="new",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("action_id", "other-action"),
        ("action_type", "no_reply_review"),
        ("step_key", "other-step"),
        ("recovery_case_id", "other-case"),
    ],
)
def test_execution_context_must_match_claimed_action(
    field: str, value: str
) -> None:
    context = _execution_context()
    mismatched = FollowupExecutionContext(
        **{**context.__dict__, field: value}
    )
    action = SimpleNamespace(
        action_id="action-001",
        action_type="first_contact_review",
        step_key="first_contact",
        recovery_case_id="case-001",
    )

    with pytest.raises(SupabaseError, match="followup_execution_context_mismatch"):
        _validate_followup_execution_context(action, mismatched)


def test_dispatcher_rejects_tampered_followup_message_proposal() -> None:
    proposal = FollowupMessageProposal(
        strategy="consultiva",
        message="x" * 501,
    )

    with pytest.raises(SupabaseError, match="invalid_followup_message_proposal"):
        _validate_followup_message_proposal(proposal)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("idempotency_key", "wrong-key"),
        ("channel", "email"),
        ("mode", "approved_template"),
        ("phase", "request_started"),
    ],
)
def test_dispatcher_rejects_malformed_reserved_attempt(
    field: str, value: str
) -> None:
    action = SimpleNamespace(
        action_id="action-001",
        idempotency_key="cart_recovery:first_contact:case-001",
        lease_generation=3,
    )
    decision = ReevaluationDecision(
        action_id="action-001",
        decision="execute",
        reason_code="eligible_for_execution",
        case_version=1,
        sequence_revision=1,
    )
    attempt = DeliveryAttempt(
        attempt_id="attempt-001",
        action_id="action-001",
        idempotency_key="cart_recovery:first_contact:case-001",
        attempt_number=1,
        channel="whatsapp",
        mode="freeform",
        phase="reserved",
        lease_generation=3,
        expected_case_version=1,
        expected_sequence_revision=1,
    )
    malformed = DeliveryAttempt(**{**attempt.__dict__, field: value})

    with pytest.raises(SupabaseError, match="reserved_delivery_attempt_mismatch"):
        _validate_reserved_delivery_attempt(action, decision, malformed)


def test_durable_dispatcher_claims_due_actions_without_external_effects() -> None:
    calls: list[dict[str, object]] = []

    class SupabaseStub:
        async def claim_due_followup_actions(self, **kwargs: object) -> list[object]:
            calls.append(kwargs)
            return []

    dispatcher = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        lease_duration="5 minutes",
        batch_size=3,
    )

    assert _run(dispatcher.claim_due(now="2026-08-03T13:00:00+00:00")) == []
    assert calls == [{
        "worker_id": "dispatcher-test",
        "now": "2026-08-03T13:00:00+00:00",
        "lease_duration": "5 minutes",
        "batch_size": 3,
    }]


def test_durable_dispatcher_reevaluates_every_claim_without_external_effects() -> None:
    calls: list[dict[str, object]] = []
    reservation_calls: list[dict[str, object]] = []
    execution_context_calls: list[dict[str, object]] = []
    agent_calls: list[dict[str, object]] = []
    action = ScheduledAction(
        action_id="action-001",
        recovery_case_id="case-001",
        followup_sequence_id="sequence-001",
        action_type="first_contact_review",
        status="pending",
        due_at="2026-08-03T13:00:00+00:00",
        expires_at="2026-08-10T12:00:00+00:00",
        expected_case_version=1,
        policy_key="cart-recovery-test",
        policy_version=1,
        step_key="first_contact",
        anchor_type="cart_abandonment",
        anchor_subject_internal_id="event-001",
        anchor_observed_at="2026-08-03T12:00:00+00:00",
        lease_owner="dispatcher-test",
        lease_generation=3,
        lease_expires_at="2026-08-03T13:05:00+00:00",
        idempotency_key="cart_recovery:first_contact:case-001",
    )

    class SupabaseStub:
        async def claim_due_followup_actions(self, **_: object) -> list[ScheduledAction]:
            return [action]

        async def get_followup_chatwoot_context(
            self, **_: object
        ) -> ChatwootAuthorityContext:
            return ChatwootAuthorityContext(
                action_id="action-001",
                action_type="first_contact_review",
                chatwoot_account_id=None,
                external_conversation_id=None,
                expected_inbox_id=None,
                anchor_external_message_id=None,
            )

        async def reevaluate_followup_action(self, **kwargs: object) -> ReevaluationDecision:
            calls.append(kwargs)
            return ReevaluationDecision(
                action_id="action-001",
                decision="execute",
                reason_code="eligible_for_execution",
                case_version=1,
                sequence_revision=1,
            )

        async def reserve_followup_delivery_attempt(
            self, **kwargs: object
        ) -> DeliveryAttempt:
            reservation_calls.append(kwargs)
            return DeliveryAttempt(
                attempt_id="attempt-001",
                action_id="action-001",
                idempotency_key=action.idempotency_key,
                attempt_number=1,
                channel="whatsapp",
                mode="freeform",
                phase="reserved",
                lease_generation=3,
                expected_case_version=1,
                expected_sequence_revision=1,
            )

        async def get_followup_execution_context(
            self, **kwargs: object
        ) -> FollowupExecutionContext:
            execution_context_calls.append(kwargs)
            return FollowupExecutionContext(
                action_id=action.action_id,
                action_type=action.action_type,
                step_key=action.step_key,
                recovery_case_id=action.recovery_case_id,
                contact_id="contact-001",
                source_event_id="event-001",
                buyer_name="Ana",
                buyer_email="ana@example.test",
                buyer_phone="15555550100",
                product_name="Curso Uno",
                offer_code="OFERTA1",
                current_goal="iniciar conversación",
                lead_stage="new",
            )

    class AgentStub:
        async def request_followup_message(
            self, **kwargs: object
        ) -> FollowupMessageProposal:
            agent_calls.append(kwargs)
            return FollowupMessageProposal(
                strategy="recordatorio consultivo",
                message="Hola Ana, ¿te quedó alguna duda?",
            )

    dispatcher = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        recovery_agent=AgentStub(),  # type: ignore[arg-type]
    )
    decisions = _run(dispatcher.dispatch_due(now="2026-08-03T13:00:00+00:00"))

    assert [decision.decision for decision in decisions] == ["execute"]
    assert calls == [{
        "action_id": "action-001",
        "worker_id": "dispatcher-test",
        "lease_generation": 3,
        "now": "2026-08-03T13:00:00+00:00",
        "chatwoot_evidence": None,
    }]
    assert reservation_calls == [{
        "action_id": "action-001",
        "worker_id": "dispatcher-test",
        "lease_generation": 3,
        "expected_case_version": 1,
        "expected_sequence_revision": 1,
        "channel": "whatsapp",
        "mode": "freeform",
        "now": "2026-08-03T13:00:00+00:00",
    }]
    assert execution_context_calls == [{
        "action_id": "action-001",
        "worker_id": "dispatcher-test",
        "lease_generation": 3,
        "now": "2026-08-03T13:00:00+00:00",
    }]
    assert agent_calls == [{
        "attempt_id": "attempt-001",
        "execution_context": FollowupExecutionContext(
            action_id="action-001",
            action_type="first_contact_review",
            step_key="first_contact",
            recovery_case_id="case-001",
            contact_id="contact-001",
            source_event_id="event-001",
            buyer_name="Ana",
            buyer_email="ana@example.test",
            buyer_phone="15555550100",
            product_name="Curso Uno",
            offer_code="OFERTA1",
            current_goal="iniciar conversación",
            lead_stage="new",
        ),
    }]


def test_dispatcher_marks_started_immediately_before_sender_and_finalizes_acceptance() -> None:
    events: list[str] = []
    context_times: list[object] = []
    reevaluation_times: list[object] = []
    request_start_times: list[object] = []
    request_start_hook: Callable[[], Any] | None = None
    finalizations: list[dict[str, object]] = []
    final_override: ReevaluationDecision | None = None
    action = ScheduledAction(
        action_id="action-send", recovery_case_id="case-001",
        followup_sequence_id="sequence-001", action_type="first_contact_review",
        status="pending", due_at="2026-08-03T13:00:00+00:00",
        expires_at="2026-08-10T12:00:00+00:00", expected_case_version=1,
        policy_key="cart-recovery-test", policy_version=1,
        step_key="first_contact", anchor_type="cart_abandonment",
        anchor_subject_internal_id="event-001",
        anchor_observed_at="2026-08-03T12:00:00+00:00",
        lease_owner="dispatcher-test", lease_generation=3,
        lease_expires_at="2026-08-03T13:05:00+00:00",
        idempotency_key="cart_recovery:first_contact:case-001",
    )
    attempt = DeliveryAttempt(
        attempt_id="attempt-001", action_id=action.action_id,
        idempotency_key=action.idempotency_key, attempt_number=1,
        channel="whatsapp", mode="freeform", phase="reserved",
        lease_generation=3, expected_case_version=1,
        expected_sequence_revision=1,
    )
    context = FollowupExecutionContext(
        action_id=action.action_id, action_type=action.action_type,
        step_key=action.step_key, recovery_case_id=action.recovery_case_id,
        contact_id="contact-001", source_event_id="event-001",
        buyer_name="Ana", buyer_email="ana@example.test",
        buyer_phone="15555550100", product_name="Curso Uno",
        offer_code="OFERTA1", current_goal="iniciar conversación",
        lead_stage="new",
    )

    class SupabaseStub:
        reevaluations = 0

        async def claim_due_followup_actions(self, **_: object) -> list[ScheduledAction]:
            return [action]

        async def get_followup_chatwoot_context(self, **kwargs: object) -> ChatwootAuthorityContext:
            context_times.append(kwargs["now"])
            return ChatwootAuthorityContext(
                action_id=action.action_id, action_type=action.action_type,
                chatwoot_account_id=None, external_conversation_id=None,
                expected_inbox_id=None, anchor_external_message_id=None,
            )

        async def reevaluate_followup_action(self, **kwargs: object) -> ReevaluationDecision:
            reevaluation_times.append(kwargs["now"])
            self.reevaluations += 1
            events.append(f"reevaluate-{self.reevaluations}")
            if self.reevaluations == 2 and final_override is not None:
                return final_override
            return ReevaluationDecision(
                action_id=action.action_id, decision="execute",
                reason_code="eligible_for_execution", case_version=1,
                sequence_revision=1,
            )

        async def reserve_followup_delivery_attempt(self, **_: object) -> DeliveryAttempt:
            events.append("reserve")
            return attempt

        async def get_followup_execution_context(self, **_: object) -> FollowupExecutionContext:
            return context

        async def mark_followup_request_started(self, **kwargs: object) -> DeliveryAttempt:
            request_start_times.append(kwargs["now"])
            events.append("request_started")
            if request_start_hook is not None:
                return await request_start_hook()
            return DeliveryAttempt(**{**attempt.__dict__, "phase": "request_started"})

        async def finalize_followup_delivery_attempt(self, **kwargs: object) -> object:
            finalizations.append(kwargs)
            return SimpleNamespace(status="delivery_unknown")

        async def record_and_finalize_followup_acceptance(self, **kwargs: object) -> object:
            events.append("accepted")
            assert kwargs["external_conversation_id"] == "7001"
            assert kwargs["remote_message_id"] == "8001"
            assert kwargs["message_content"] == "Hola Ana, ¿te quedó alguna duda?"
            return SimpleNamespace(status="accepted_by_chatwoot")

    class AgentStub:
        async def request_followup_message(self, **_: object) -> FollowupMessageProposal:
            events.append("hermes")
            return FollowupMessageProposal(
                strategy="recordatorio consultivo",
                message="Hola Ana, ¿te quedó alguna duda?",
            )

    class SenderStub:
        async def send_first_touch(self, **_: object) -> FirstTouchResult:
            events.append("sender")
            return FirstTouchResult(
                status="sent", reason="sent", conversation_id=7001,
                message_id=8001,
            )

    dispatcher = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        recovery_agent=AgentStub(),  # type: ignore[arg-type]
        sender=SenderStub(),  # type: ignore[arg-type]
        allowed_jid="15555550100@s.whatsapp.net",
        clock=lambda: "2026-08-03T13:01:00+00:00",
    )
    decisions = _run(dispatcher.dispatch_due(now="2026-08-03T13:00:00+00:00"))

    assert decisions[-1].decision == "execute"
    assert events == [
        "reevaluate-1", "reserve", "hermes",
        "reevaluate-2", "request_started", "sender", "accepted",
    ]
    assert context_times == [
        "2026-08-03T13:00:00+00:00",
        "2026-08-03T13:01:00+00:00",
    ]
    assert reevaluation_times == [
        "2026-08-03T13:00:00+00:00",
        "2026-08-03T13:01:00+00:00",
    ]
    assert request_start_times == ["2026-08-03T13:01:00+00:00"]

    events.clear()
    final_override = ReevaluationDecision(
        action_id=action.action_id, decision="cancel",
        reason_code="contact_opted_out", case_version=1,
        sequence_revision=1,
    )
    blocked_by_revalidation = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        recovery_agent=AgentStub(),  # type: ignore[arg-type]
        sender=SenderStub(),  # type: ignore[arg-type]
        allowed_jid="15555550100@s.whatsapp.net",
    )
    final_decisions = _run(
        blocked_by_revalidation.dispatch_due(now="2026-08-03T13:00:00+00:00")
    )
    assert final_decisions[-1].decision == "cancel"
    assert events == ["reevaluate-1", "reserve", "hermes", "reevaluate-2"]

    events.clear()
    outside_allowlist = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        recovery_agent=AgentStub(),  # type: ignore[arg-type]
        sender=SenderStub(),  # type: ignore[arg-type]
        allowed_jid="15555550999@s.whatsapp.net",
    )
    with pytest.raises(SupabaseError, match="followup_recipient_not_allowlisted"):
        _run(outside_allowlist.dispatch_due(now="2026-08-03T13:00:00+00:00"))
    assert events == ["reevaluate-1", "reserve", "hermes"]

    async def cancelled_request_start_scenario() -> None:
        nonlocal request_start_hook, final_override
        entered = asyncio.Event()
        release = asyncio.Event()
        final_override = None
        finalizations.clear()
        events.clear()

        async def delayed_started() -> DeliveryAttempt:
            entered.set()  # model the database commit before the response arrives
            await release.wait()
            return DeliveryAttempt(**{**attempt.__dict__, "phase": "request_started"})

        request_start_hook = delayed_started
        guarded = DurableDispatcher(
            supabase=SupabaseStub(),  # type: ignore[arg-type]
            worker_id="dispatcher-test",
            recovery_agent=AgentStub(),  # type: ignore[arg-type]
            sender=SenderStub(),  # type: ignore[arg-type]
            allowed_jid="15555550100@s.whatsapp.net",
            clock=lambda: "2026-08-03T13:01:00+00:00",
        )
        task = asyncio.create_task(
            guarded.dispatch_due(now="2026-08-03T13:00:00+00:00")
        )
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(cancelled_request_start_scenario())
    assert len(finalizations) == 1
    assert finalizations[0]["outcome"] == "delivery_unknown"
    assert finalizations[0]["reason_code"] == "request_start_cancelled_after_commit"
    assert "sender" not in events

    finalizations.clear()
    events.clear()

    async def invalid_started() -> DeliveryAttempt:
        return attempt

    request_start_hook = invalid_started
    invalid_response = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        recovery_agent=AgentStub(),  # type: ignore[arg-type]
        sender=SenderStub(),  # type: ignore[arg-type]
        allowed_jid="15555550100@s.whatsapp.net",
        clock=lambda: "2026-08-03T13:01:00+00:00",
    )
    with pytest.raises(SupabaseError, match="started_delivery_attempt_mismatch"):
        _run(invalid_response.dispatch_due(now="2026-08-03T13:00:00+00:00"))
    assert len(finalizations) == 1
    assert finalizations[0]["outcome"] == "delivery_unknown"
    assert finalizations[0]["reason_code"] == "request_start_response_invalid"
    assert "sender" not in events

    finalizations.clear()
    events.clear()

    async def committed_invalid_response() -> DeliveryAttempt:
        raise SupabaseCommittedResponseError(
            "mark_followup_request_started_invalid_shape"
        )

    request_start_hook = committed_invalid_response
    invalid_client_response = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        recovery_agent=AgentStub(),  # type: ignore[arg-type]
        sender=SenderStub(),  # type: ignore[arg-type]
        allowed_jid="15555550100@s.whatsapp.net",
        clock=lambda: "2026-08-03T13:01:00+00:00",
    )
    with pytest.raises(
        SupabaseCommittedResponseError,
        match="mark_followup_request_started_invalid_shape",
    ):
        _run(
            invalid_client_response.dispatch_due(
                now="2026-08-03T13:00:00+00:00"
            )
        )
    assert len(finalizations) == 1
    assert finalizations[0]["outcome"] == "delivery_unknown"
    assert finalizations[0]["reason_code"] == "request_start_response_invalid"
    assert "sender" not in events


@pytest.mark.parametrize(
    ("sender_result", "expected_outcome", "expected_reason", "expect_cancelled"),
    [
        (
            FirstTouchResult("blocked", None, None, "provider_rejected"),
            "rejected",
            "sender_rejected_before_delivery",
            False,
        ),
        (
            FirstTouchResult("failed", None, None, "chatwoot_http_error"),
            "delivery_unknown",
            "sender_result_inconclusive",
            False,
        ),
        (
            FirstTouchResult("sent", None, None, "malformed_success"),
            "delivery_unknown",
            "sender_result_inconclusive",
            False,
        ),
        (
            SimpleNamespace(status="sent"),
            "delivery_unknown",
            "sender_result_inconclusive",
            False,
        ),
        (
            httpx.ReadTimeout("sender timeout"),
            "delivery_unknown",
            "sender_exception_after_request_started",
            False,
        ),
        (
            asyncio.CancelledError(),
            "delivery_unknown",
            "sender_cancelled_after_request_started",
            True,
        ),
    ],
)
def test_dispatcher_durably_finalizes_nonaccepted_sender_results(
    sender_result: object,
    expected_outcome: str,
    expected_reason: str,
    expect_cancelled: bool,
) -> None:
    finalizations: list[dict[str, object]] = []
    action = ScheduledAction(
        action_id="action-result", recovery_case_id="case-001",
        followup_sequence_id="sequence-001", action_type="first_contact_review",
        status="pending", due_at="2026-08-03T13:00:00+00:00",
        expires_at="2026-08-10T12:00:00+00:00", expected_case_version=1,
        policy_key="cart-recovery-test", policy_version=1,
        step_key="first_contact", anchor_type="cart_abandonment",
        anchor_subject_internal_id="event-001",
        anchor_observed_at="2026-08-03T12:00:00+00:00",
        lease_owner="dispatcher-test", lease_generation=3,
        lease_expires_at="2026-08-03T13:05:00+00:00",
        idempotency_key="cart_recovery:first_contact:case-001",
    )
    attempt = DeliveryAttempt(
        attempt_id="attempt-result", action_id=action.action_id,
        idempotency_key=action.idempotency_key, attempt_number=1,
        channel="whatsapp", mode="freeform", phase="reserved",
        lease_generation=3, expected_case_version=1,
        expected_sequence_revision=1,
    )

    class SupabaseStub:
        async def claim_due_followup_actions(self, **_: object) -> list[ScheduledAction]:
            return [action]

        async def get_followup_chatwoot_context(self, **_: object) -> ChatwootAuthorityContext:
            return ChatwootAuthorityContext(
                action.action_id, action.action_type, None, None, None, None
            )

        async def reevaluate_followup_action(self, **_: object) -> ReevaluationDecision:
            return ReevaluationDecision(
                action.action_id, "execute", "eligible_for_execution", 1, 1
            )

        async def reserve_followup_delivery_attempt(self, **_: object) -> DeliveryAttempt:
            return attempt

        async def get_followup_execution_context(self, **_: object) -> FollowupExecutionContext:
            return FollowupExecutionContext(
                action.action_id, action.action_type, action.step_key,
                action.recovery_case_id, "contact-001", "event-001", "Ana",
                None, "15555550100", "Curso", None, None, "new",
            )

        async def mark_followup_request_started(self, **_: object) -> DeliveryAttempt:
            return DeliveryAttempt(**{**attempt.__dict__, "phase": "request_started"})

        async def finalize_followup_delivery_attempt(self, **kwargs: object) -> object:
            finalizations.append(kwargs)
            return SimpleNamespace(status=(
                "permanent_failed" if kwargs["outcome"] == "rejected"
                else "delivery_unknown"
            ))

    class AgentStub:
        async def request_followup_message(self, **_: object) -> FollowupMessageProposal:
            return FollowupMessageProposal("recordatorio", "Mensaje aprobado")

    class SenderStub:
        async def send_first_touch(self, **_: object) -> object:
            if isinstance(sender_result, BaseException):
                raise sender_result
            return sender_result

    dispatcher = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        recovery_agent=AgentStub(),  # type: ignore[arg-type]
        sender=SenderStub(),  # type: ignore[arg-type]
        allowed_jid="15555550100@s.whatsapp.net",
        clock=lambda: "2026-08-03T13:00:00+00:00",
    )
    if expect_cancelled:
        with pytest.raises(asyncio.CancelledError):
            _run(dispatcher.dispatch_due(now="2026-08-03T13:00:00+00:00"))
    else:
        _run(dispatcher.dispatch_due(now="2026-08-03T13:00:00+00:00"))

    assert len(finalizations) == 1
    assert finalizations[0]["outcome"] == expected_outcome
    assert finalizations[0]["reason_code"] == expected_reason
    if expected_outcome == "delivery_unknown":
        assert finalizations[0]["reconciliation_deadline"] == (
            "2026-08-03T13:15:00+00:00"
        )
        assert finalizations[0]["next_attempt_at"] is None
    else:
        assert finalizations[0]["reconciliation_deadline"] is None


def test_critical_finalization_survives_repeated_parent_cancellation() -> None:
    async def scenario() -> tuple[str, int]:
        started = asyncio.Event()
        release = asyncio.Event()
        completions = 0

        async def finalize() -> str:
            nonlocal completions
            started.set()
            await release.wait()
            completions += 1
            return "done"

        task = asyncio.create_task(_await_despite_cancellation(finalize()))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        return await task, completions

    assert _run(scenario()) == ("done", 1)


def test_outcome_commit_propagates_cancellation_only_after_durable_completion() -> None:
    async def scenario() -> int:
        started = asyncio.Event()
        release = asyncio.Event()
        completions = 0

        async def finalize() -> str:
            nonlocal completions
            started.set()
            await release.wait()
            completions += 1
            return "committed"

        task = asyncio.create_task(
            worker_module._commit_outcome_despite_cancellation(finalize())
        )
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return completions

    assert _run(scenario()) == 1


def test_dispatcher_rejects_execute_for_reconciliation_action() -> None:
    action = ScheduledAction(
        action_id="action-reconcile", recovery_case_id="case-001",
        followup_sequence_id="sequence-001", action_type="reconcile_delivery",
        status="pending", due_at="2026-08-03T13:00:00+00:00",
        expires_at="2026-08-10T12:00:00+00:00", expected_case_version=1,
        policy_key="cart-recovery-test", policy_version=1,
        step_key="reconcile", anchor_type="delivery_attempt",
        anchor_subject_internal_id="attempt-001",
        anchor_observed_at="2026-08-03T12:00:00+00:00",
        lease_owner="dispatcher-test", lease_generation=3,
        lease_expires_at="2026-08-03T13:05:00+00:00",
        idempotency_key="cart_recovery:reconcile:case-001",
    )

    class SupabaseStub:
        async def claim_due_followup_actions(self, **_: object) -> list[ScheduledAction]:
            return [action]

        async def get_followup_chatwoot_context(
            self, **_: object
        ) -> ChatwootAuthorityContext:
            return ChatwootAuthorityContext(
                action_id=action.action_id, action_type=action.action_type,
                chatwoot_account_id=None, external_conversation_id=None,
                expected_inbox_id=None, anchor_external_message_id=None,
            )

        async def reevaluate_followup_action(self, **_: object) -> ReevaluationDecision:
            return ReevaluationDecision(
                action_id=action.action_id, decision="execute",
                reason_code="invalid_execute", case_version=1,
                sequence_revision=1,
            )

        async def reserve_followup_delivery_attempt(self, **_: object) -> DeliveryAttempt:
            raise AssertionError("reconciliation action reached outbound reservation")

    dispatcher = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
    )

    with pytest.raises(SupabaseError, match="reconcile_delivery_execute_forbidden"):
        _run(dispatcher.dispatch_due(now="2026-08-03T13:00:00+00:00"))


def test_dispatcher_passes_canonical_chatwoot_evidence_to_reevaluation() -> None:
    action = ScheduledAction(
        action_id="action-001", recovery_case_id="case-001",
        followup_sequence_id="sequence-001", action_type="no_reply_review",
        status="pending", due_at="2026-08-03T13:00:00+00:00",
        expires_at="2026-08-10T12:00:00+00:00", expected_case_version=1,
        policy_key="cart-recovery-test", policy_version=1,
        step_key="no_reply_1", anchor_type="accepted_outbound_message",
        anchor_subject_internal_id="message-internal-001",
        anchor_observed_at="2026-08-03T12:00:00+00:00",
        lease_owner="dispatcher-test", lease_generation=3,
        lease_expires_at="2026-08-03T13:05:00+00:00",
        idempotency_key="cart_recovery:no_reply:case-001",
    )
    reevaluation_calls: list[dict[str, object]] = []

    class SupabaseStub:
        async def claim_due_followup_actions(self, **_: object) -> list[ScheduledAction]:
            return [action]

        async def get_followup_chatwoot_context(
            self, **_: object
        ) -> ChatwootAuthorityContext:
            return ChatwootAuthorityContext(
                action_id="action-001", action_type="no_reply_review",
                chatwoot_account_id=1, external_conversation_id=22,
                expected_inbox_id=7, anchor_external_message_id=40,
            )

        async def reevaluate_followup_action(
            self, **kwargs: object
        ) -> ReevaluationDecision:
            reevaluation_calls.append(kwargs)
            return ReevaluationDecision(
                action_id="action-001", decision="cancel",
                reason_code="prospect_replied", case_version=1,
                sequence_revision=2,
            )

    class ChatwootStub:
        async def get_canonical_conversation_snapshot(
            self, **kwargs: object
        ) -> CanonicalConversationSnapshot:
            assert kwargs == {
                "conversation_id": 22,
                "expected_inbox_id": 7,
                "anchor_message_id": 40,
                "anchor_observed_at_epoch": 1785758400,
            }
            return CanonicalConversationSnapshot(
                conversation_id=22, inbox_id=7, status="open", can_reply=True,
                labels=(), anchor_found=True, inbound_after_anchor=True,
                human_activity_after_anchor=False, checkpoint_message_id=41,
                checkpoint_created_at=1785762000,
            )

    dispatcher = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        chatwoot=ChatwootStub(),  # type: ignore[arg-type]
        chatwoot_account_id=1,
    )
    decisions = _run(dispatcher.dispatch_due(now="2026-08-03T13:00:00+00:00"))

    assert decisions[0].reason_code == "prospect_replied"
    evidence = reevaluation_calls[0]["chatwoot_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["p_chatwoot_conversation_id"] == "22"
    assert evidence["p_chatwoot_inbound_after_anchor"] is True
    assert evidence["p_chatwoot_anchor_found"] is True


def test_dispatcher_does_not_reserve_non_execute_decision() -> None:
    action = ScheduledAction(
        action_id="action-001", recovery_case_id="case-001",
        followup_sequence_id="sequence-001", action_type="no_reply_review",
        status="pending", due_at="2026-08-03T13:00:00+00:00",
        expires_at="2026-08-10T12:00:00+00:00", expected_case_version=1,
        policy_key="cart-recovery-test", policy_version=1,
        step_key="no_reply_1", anchor_type="accepted_outbound_message",
        anchor_subject_internal_id="message-internal-001",
        anchor_observed_at="2026-08-03T12:00:00+00:00",
        lease_owner="dispatcher-test", lease_generation=3,
        lease_expires_at="2026-08-03T13:05:00+00:00",
        idempotency_key="cart_recovery:no_reply:case-001",
    )

    class SupabaseStub:
        async def claim_due_followup_actions(self, **_: object) -> list[ScheduledAction]:
            return [action]

        async def get_followup_chatwoot_context(
            self, **_: object
        ) -> ChatwootAuthorityContext:
            return ChatwootAuthorityContext(
                action_id=action.action_id,
                action_type=action.action_type,
                chatwoot_account_id=None,
                external_conversation_id=None,
                expected_inbox_id=None,
                anchor_external_message_id=None,
            )

        async def reevaluate_followup_action(
            self, **_: object
        ) -> ReevaluationDecision:
            return ReevaluationDecision(
                action_id=action.action_id,
                decision="cancel",
                reason_code="prospect_replied",
                case_version=1,
                sequence_revision=2,
            )

        async def reserve_followup_delivery_attempt(self, **_: object) -> DeliveryAttempt:
            raise AssertionError("non-execute decisions must not reserve an attempt")

    dispatcher = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
    )

    decisions = _run(dispatcher.dispatch_due(now="2026-08-03T13:00:00+00:00"))

    assert decisions[0].decision == "cancel"


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


def test_worker_stops_after_durable_planning_without_invoking_agent_or_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve_event(**kwargs: object) -> object:
        assert kwargs["policy_key"] == "cart-recovery-test"
        assert kwargs["policy_version"] == 1
        return SimpleNamespace(event_id="event-planned")

    class AgentThatMustNotRun:
        async def request_proposal(self, **_: object) -> object:
            raise AssertionError("agent must not run during durable planning")

    class SenderThatMustNotRun:
        async def send_first_touch(self, **_: object) -> object:
            raise AssertionError("sender must not run during durable planning")

    monkeypatch.setattr("bridge.worker.resolve_event", fake_resolve_event)
    worker = ResolutionWorker(
        supabase=object(),  # type: ignore[arg-type]
        recovery_agent=AgentThatMustNotRun(),  # type: ignore[arg-type]
        message_sender=SenderThatMustNotRun(),  # type: ignore[arg-type]
        policy_key="cart-recovery-test",
        policy_version=1,
    )

    _run(worker._process_one({"id": "event-planned", "payload": {}}))


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


def test_worker_blocks_injected_sender_for_unauthorized_target(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    report_dict = {
        "authoritative_context_complete": True,
        "contact_blocked": False,
        "phone_available": True,
        "any_conversation_human_takeover": False,
        "has_active_conversation": False,
        "has_open_recovery_case": False,
    }

    async def fake_resolve_event(**_: object) -> object:
        return SimpleNamespace(
            event_id="event-unauthorized-target",
            phone_available=True,
            buyer_phone="12025550123",
            buyer_name="Test",
            buyer_email="test@test.com",
            to_dict=lambda: report_dict,
        )

    class Agent:
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
        recovery_agent=Agent(),  # type: ignore[arg-type]
        message_sender=SenderThatMustNotRun(),  # type: ignore[arg-type]
        allowed_jid="15555550100@s.whatsapp.net",
    )

    with caplog.at_level("ERROR", logger="bridge.worker"):
        _run(worker._process_one({
            "id": "event-unauthorized-target",
            "payload": {},
        }))

    assert "first_touch_target_not_allowed" in caplog.text


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
