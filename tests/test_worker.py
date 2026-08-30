"""Tests for the deferred resolution worker."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import time
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

from bridge.chatwoot import CanonicalConversationSnapshot, ChatwootProtocolError
from bridge.messaging import FirstTouchResult
from bridge.recovery_agent import FollowupHandoffSuggestion, FollowupMessageProposal
from bridge.supabase import (
    ChatwootAuthorityContext,
    DeliveryAttempt,
    FollowupExecutionContext,
    OptOutProjectionClaim,
    PilotBoundaryConfig,
    ReevaluationDecision,
    ScheduledAction,
    SupabaseClient,
    SupabaseCommittedResponseError,
    SupabaseError,
)
import bridge.worker as worker_module
from bridge.worker import (
    DurableDispatcher,
    HotmartAbandonmentTimerWorker,
    OptOutProjectionWorker,
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
        _validate_reserved_delivery_attempt(
            action,  # type: ignore[arg-type]
            decision, malformed, "freeform"
        )


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


def test_dispatcher_finalizes_reserved_attempt_when_proposal_is_unavailable() -> None:
    finalizations: list[dict[str, object]] = []
    action = ScheduledAction(
        action_id="action-no-proposal",
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
    attempt = DeliveryAttempt(
        attempt_id="attempt-no-proposal",
        action_id=action.action_id,
        idempotency_key=action.idempotency_key,
        attempt_number=1,
        channel="whatsapp",
        mode="freeform",
        phase="reserved",
        lease_generation=3,
        expected_case_version=1,
        expected_sequence_revision=1,
    )

    class SupabaseStub:
        async def claim_due_followup_actions(
            self, **_: object
        ) -> list[ScheduledAction]:
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
                decision="execute",
                reason_code="eligible_for_execution",
                case_version=1,
                sequence_revision=1,
            )

        async def reserve_followup_delivery_attempt(
            self, **_: object
        ) -> DeliveryAttempt:
            return attempt

        async def get_followup_execution_context(
            self, **_: object
        ) -> FollowupExecutionContext:
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

        async def finalize_followup_delivery_attempt(
            self, **kwargs: object
        ) -> object:
            finalizations.append(kwargs)
            return SimpleNamespace(status="retryable_failed")

        async def mark_followup_request_started(self, **_: object) -> object:
            raise AssertionError("request must not start without a proposal")

    class AgentWithoutProposal:
        async def request_followup_message(self, **_: object) -> None:
            return None

    class SenderStub:
        async def send_first_touch(self, **_: object) -> object:
            raise AssertionError("sender must not run without a proposal")

        async def send_followup(self, **_: object) -> object:
            raise AssertionError("sender must not run without a proposal")

    dispatcher = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        recovery_agent=AgentWithoutProposal(),  # type: ignore[arg-type]
        sender=SenderStub(),  # type: ignore[arg-type]
        allowed_jid="15555550100@s.whatsapp.net",
        clock=lambda: "2026-08-03T13:01:00+00:00",
    )

    _run(dispatcher.dispatch_due(now="2026-08-03T13:00:00+00:00"))

    assert finalizations == [{
        "action_id": action.action_id,
        "attempt_id": attempt.attempt_id,
        "worker_id": "dispatcher-test",
        "lease_generation": 3,
        "outcome": "failed_before_request",
        "remote_message_id": None,
        "accepted_message_id": None,
        "reason_code": "agent_proposal_unavailable",
        "next_attempt_at": "2026-08-03T13:02:00+00:00",
        "reconciliation_deadline": None,
        "now": "2026-08-03T13:01:00+00:00",
    }]

    finalizations.clear()
    entered = asyncio.Event()

    class BlockingAgent:
        async def request_followup_message(self, **_: object) -> None:
            entered.set()
            await asyncio.Event().wait()

    cancelled_dispatcher = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        recovery_agent=BlockingAgent(),  # type: ignore[arg-type]
        sender=SenderStub(),  # type: ignore[arg-type]
        allowed_jid="15555550100@s.whatsapp.net",
        clock=lambda: "2026-08-03T13:01:00+00:00",
    )

    async def cancel_during_proposal() -> None:
        task = asyncio.create_task(
            cancelled_dispatcher.dispatch_due(
                now="2026-08-03T13:00:00+00:00"
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(cancel_during_proposal())

    assert len(finalizations) == 1
    cancelled = finalizations[0]
    assert cancelled["outcome"] == "failed_before_request"
    assert cancelled["reason_code"] == "pre_request_cancelled"
    assert cancelled["next_attempt_at"] == "2026-08-03T13:02:00+00:00"


def test_dispatcher_commits_allowlisted_handoff_before_request_start() -> None:
    action = ScheduledAction(
        action_id="action-handoff",
        recovery_case_id="case-handoff",
        followup_sequence_id="sequence-handoff",
        action_type="first_contact_review",
        status="pending",
        due_at="2026-08-03T13:00:00+00:00",
        expires_at="2026-08-10T12:00:00+00:00",
        expected_case_version=1,
        policy_key="cart-recovery-test",
        policy_version=1,
        step_key="first_contact",
        anchor_type="cart_abandonment",
        anchor_subject_internal_id="event-handoff",
        anchor_observed_at="2026-08-03T12:00:00+00:00",
        lease_owner="dispatcher-test",
        lease_generation=3,
        lease_expires_at="2026-08-03T13:05:00+00:00",
        idempotency_key="cart_recovery:first_contact:case-handoff",
    )
    attempt = DeliveryAttempt(
        attempt_id="attempt-handoff",
        action_id=action.action_id,
        idempotency_key=action.idempotency_key,
        attempt_number=1,
        channel="whatsapp",
        mode="freeform",
        phase="reserved",
        lease_generation=3,
        expected_case_version=1,
        expected_sequence_revision=1,
    )
    handoff_calls: list[dict[str, object]] = []

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
                decision="execute",
                reason_code="eligible_for_execution",
                case_version=1,
                sequence_revision=1,
            )

        async def reserve_followup_delivery_attempt(
            self, **_: object
        ) -> DeliveryAttempt:
            return attempt

        async def get_followup_execution_context(
            self, **_: object
        ) -> FollowupExecutionContext:
            return FollowupExecutionContext(
                action_id=action.action_id,
                action_type=action.action_type,
                step_key=action.step_key,
                recovery_case_id=action.recovery_case_id,
                contact_id="contact-handoff",
                source_event_id="event-handoff",
                buyer_name="Ana",
                buyer_email="ana@example.test",
                buyer_phone="15555550100",
                product_name="Curso Uno",
                offer_code="OFERTA1",
                current_goal="resolver excepción comercial",
                lead_stage="new",
            )

        async def request_human_handoff(self, **kwargs: object) -> object:
            handoff_calls.append(kwargs)
            return SimpleNamespace(outcome="requested")

        async def finalize_followup_delivery_attempt(self, **_: object) -> object:
            raise AssertionError("accepted handoff must close the attempt atomically")

        async def mark_followup_request_started(self, **_: object) -> object:
            raise AssertionError("request must not start after suggest_handoff")

    class HandoffAgent:
        async def request_followup_message(self, **_: object) -> object:
            return FollowupHandoffSuggestion(reason_code="commercial_exception")

    class SenderStub:
        async def send_first_touch(self, **_: object) -> object:
            raise AssertionError("handoff must remain externally silent")

        async def send_followup(self, **_: object) -> object:
            raise AssertionError("handoff must remain externally silent")

    dispatcher = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        recovery_agent=HandoffAgent(),  # type: ignore[arg-type]
        sender=SenderStub(),  # type: ignore[arg-type]
        allowed_jid="15555550100@s.whatsapp.net",
        clock=lambda: "2026-08-03T13:01:00+00:00",
        human_handoff_admission_enabled=True,
        handoff_projection_policy_key="lancemos-handoff",
        handoff_projection_policy_version=1,
    )

    _run(dispatcher.dispatch_due(now="2026-08-03T13:00:00+00:00"))

    assert handoff_calls == [{
        "recovery_case_id": "case-handoff",
        "command_key": "handoff:attempt-handoff",
        "reason_code": "commercial_exception",
        "requested_by": "agent",
        "projection_policy_key": "lancemos-handoff",
        "projection_policy_version": 1,
        "source_action_id": "action-handoff",
        "source_attempt_id": "attempt-handoff",
        "worker_id": "dispatcher-test",
        "lease_generation": 3,
        "now": "2026-08-03T13:01:00+00:00",
    }]


def test_dispatcher_resolves_delivery_unknown_when_acceptance_finalization_fails() -> None:
    """A failing acceptance RPC (e.g. HTTP 400) after the message was already sent
    must be resolved to a durable, reconcilable delivery_unknown instead of
    propagating and stranding the attempt at request_started forever."""
    events: list[str] = []
    finalizations: list[dict[str, object]] = []
    action = ScheduledAction(
        action_id="action-send", recovery_case_id="case-001",
        followup_sequence_id="sequence-001", action_type="no_reply_review",
        status="pending", due_at="2026-08-03T13:00:00+00:00",
        expires_at="2026-08-10T12:00:00+00:00", expected_case_version=1,
        policy_key="cart-recovery-test", policy_version=1,
        step_key="followup_1", anchor_type="accepted_message",
        anchor_subject_internal_id="message-001",
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

        async def get_followup_chatwoot_context(self, **_: object) -> ChatwootAuthorityContext:
            return ChatwootAuthorityContext(
                action_id=action.action_id, action_type=action.action_type,
                chatwoot_account_id=1, external_conversation_id=7001,
                expected_inbox_id=1, anchor_external_message_id=6001,
            )

        async def reevaluate_followup_action(self, **_: object) -> ReevaluationDecision:
            self.reevaluations += 1
            return ReevaluationDecision(
                action_id=action.action_id, decision="execute",
                reason_code="eligible_for_execution", case_version=1,
                sequence_revision=1,
            )

        async def reserve_followup_delivery_attempt(self, **_: object) -> DeliveryAttempt:
            return attempt

        async def get_followup_execution_context(self, **_: object) -> FollowupExecutionContext:
            return context

        async def mark_followup_request_started(self, **_: object) -> DeliveryAttempt:
            events.append("request_started")
            return DeliveryAttempt(**{**attempt.__dict__, "phase": "request_started"})

        async def finalize_followup_delivery_attempt(self, **kwargs: object) -> object:
            events.append("finalize")
            finalizations.append(kwargs)
            return SimpleNamespace(status="delivery_unknown")

        async def record_and_finalize_followup_acceptance(self, **_: object) -> object:
            events.append("accepted-attempt")
            raise SupabaseError(
                "record_and_finalize_followup_acceptance_failed: HTTP 400"
            )

    class AgentStub:
        async def request_followup_message(self, **_: object) -> FollowupMessageProposal:
            return FollowupMessageProposal(
                strategy="recordatorio consultivo",
                message="Hola Ana, ¿te quedó alguna duda?",
            )

    sender_conversation_id = {"value": 7001}

    class SenderStub:
        async def send_first_touch(self, **_: object) -> FirstTouchResult:
            raise AssertionError("follow-up must not create a new conversation")

        async def send_followup(self, **kwargs: object) -> FirstTouchResult:
            assert kwargs["conversation_id"] == 7001
            events.append("sender")
            return FirstTouchResult(
                status="sent", reason="sent",
                conversation_id=sender_conversation_id["value"],
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

    async def canonical_evidence(**_: object) -> dict[str, object]:
        return {
            "p_chatwoot_conversation_id": "7001",
            "p_chatwoot_checkpoint_message_id": "6001",
        }

    dispatcher._load_chatwoot_evidence = canonical_evidence  # type: ignore[method-assign]

    # The message was already delivered by the sender; the acceptance finalize
    # then fails. The dispatcher must NOT strand the attempt: it must record a
    # durable delivery_unknown so reconciliation can resolve it.
    _run(dispatcher.dispatch_due(now="2026-08-03T13:00:00+00:00"))

    assert events.count("sender") == 1, "the ambiguous finalize must never resend"
    assert "accepted-attempt" in events, "acceptance finalize must have been attempted"
    assert finalizations, (
        "acceptance-finalize failure after a real send must be resolved to a "
        "durable delivery_unknown, not left stranded at request_started"
    )
    last = finalizations[-1]
    assert last["outcome"] == "delivery_unknown"
    assert last["remote_message_id"] == "8001", (
        "preserve the provider message id already returned by Chatwoot so "
        "reconciliation does not discard known external evidence"
    )
    assert last["accepted_message_id"] is None
    assert last["reason_code"] == "acceptance_finalization_failed"
    assert last["next_attempt_at"] is None
    assert last["reconciliation_deadline"] == "2026-08-03T13:16:00+00:00"

    sender_conversation_id["value"] = 7002
    events.clear()
    finalizations.clear()
    _run(dispatcher.dispatch_due(now="2026-08-03T13:00:00+00:00"))

    assert events == ["request_started", "sender", "finalize"]
    mismatch = finalizations[-1]
    assert mismatch["outcome"] == "delivery_unknown"
    assert mismatch["remote_message_id"] == "8001"
    assert mismatch["accepted_message_id"] is None
    assert mismatch["reason_code"] == "sender_conversation_mismatch"
    assert mismatch["next_attempt_at"] is None


def test_dispatcher_marks_started_immediately_before_sender_and_finalizes_acceptance() -> None:
    events: list[str] = []
    context_times: list[object] = []
    reevaluation_times: list[object] = []
    request_start_times: list[object] = []
    request_start_boundaries: list[object] = []
    reservation_modes: list[object] = []
    request_start_hook: Callable[[], Any] | None = None
    finalizations: list[dict[str, object]] = []
    sender_calls: list[dict[str, object]] = []
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
        channel="whatsapp", mode="approved_template", phase="reserved",
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

        async def reserve_followup_delivery_attempt(
            self, **kwargs: object
        ) -> DeliveryAttempt:
            reservation_modes.append(kwargs["mode"])
            events.append("reserve")
            return DeliveryAttempt(
                **{**attempt.__dict__, "mode": kwargs["mode"]}
            )

        async def get_followup_execution_context(self, **_: object) -> FollowupExecutionContext:
            return context

        async def mark_followup_request_started(self, **kwargs: object) -> DeliveryAttempt:
            request_start_times.append(kwargs["now"])
            request_start_boundaries.append(kwargs.get("pilot_boundary"))
            events.append("request_started")
            if request_start_hook is not None:
                return await request_start_hook()
            return DeliveryAttempt(**{
                **attempt.__dict__,
                "mode": reservation_modes[-1],
                "phase": "request_started",
            })

        async def finalize_followup_delivery_attempt(self, **kwargs: object) -> object:
            finalizations.append(kwargs)
            status = (
                "retryable_failed"
                if kwargs.get("outcome") == "failed_before_request"
                else "delivery_unknown"
            )
            return SimpleNamespace(status=status)

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
        async def send_first_touch(self, **kwargs: object) -> FirstTouchResult:
            sender_calls.append(kwargs)
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
        pilot_boundary=PilotBoundaryConfig(
            scope_key="lancemos-cart-recovery",
            scope_version=1,
            tenant_key="lancemos",
            channel_provider="waba",
            channel_account_ref="opaque-account-ref",
        ),
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
    assert reservation_modes == ["approved_template"]
    assert sender_calls[0]["product_name"] == "Curso Uno"
    assert request_start_boundaries[0] is not None

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
    finalizations.clear()
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
    assert len(finalizations) == 1
    assert finalizations[0]["outcome"] == "failed_before_request"
    assert finalizations[0]["reason_code"] == "pre_request_failed"

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
            return DeliveryAttempt(**{
                **attempt.__dict__,
                "mode": reservation_modes[-1],
                "phase": "request_started",
            })

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
        _run(worker._process_one({
            "id": "event-001",
            "event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
            "payload": {},
        }))

    assert "recovery_agent_not_configured event_id=event-001" in caplog.text


def test_worker_stops_after_durable_planning_without_invoking_agent_or_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_resolve_event(**kwargs: object) -> object:
        assert kwargs["policy_key"] == "cart-recovery-test"
        assert kwargs["policy_version"] == 1
        assert kwargs["allowed_jid"] == "5531999999999@s.whatsapp.net"
        assert kwargs["chatwoot_account_id"] == 1
        assert kwargs["chatwoot_inbox_id"] == 7
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
        allowed_jid="5531999999999@s.whatsapp.net",
        chatwoot_account_id=1,
        chatwoot_inbox_id=7,
        policy_key="cart-recovery-test",
        policy_version=1,
    )

    _run(worker._process_one({
        "id": "event-planned",
        "event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
        "payload": {},
    }))


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
        _run(worker._process_one({
            "id": "event-002",
            "event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
            "payload": {},
        }))

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
        _run(worker._process_one({
            "id": "event-003",
            "event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
            "payload": {},
        }))

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
            "event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
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
            "event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
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


class StubProjectionSupabase:
    def __init__(self) -> None:
        self.finalizations: list[dict[str, object]] = []

    async def claim_chatwoot_opt_out_projections(
        self, **_: object
    ) -> list[OptOutProjectionClaim]:
        return [OptOutProjectionClaim(
            opt_out_event_id="opt-out-event-1",
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            chatwoot_conversation_id=42,
            external_user_id="12025550124",
            lease_generation=3,
        )]

    async def finalize_chatwoot_opt_out_projection(
        self, **kwargs: object
    ) -> str:
        self.finalizations.append(kwargs)
        return "applied" if kwargs["applied"] else "retryable_failed"


class StubProjectionChatwoot:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def apply_opt_out_macro(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if self.fail:
            raise ChatwootProtocolError("macro failed")


def test_opt_out_projection_worker_finalizes_confirmed_macro() -> None:
    supabase = StubProjectionSupabase()
    chatwoot = StubProjectionChatwoot()
    worker = OptOutProjectionWorker(
        supabase=supabase,  # type: ignore[arg-type]
        chatwoot=chatwoot,  # type: ignore[arg-type]
        worker_id="projection-worker-1",
    )

    assert asyncio.run(worker.run_once()) == 1
    assert chatwoot.calls == [{
        "conversation_id": 42,
        "expected_account_id": 1,
        "expected_inbox_id": 9,
        "expected_jid": "12025550124@s.whatsapp.net",
    }]
    assert supabase.finalizations[0]["applied"] is True
    assert supabase.finalizations[0]["error_code"] is None


def test_opt_out_projection_worker_records_retryable_chatwoot_failure() -> None:
    supabase = StubProjectionSupabase()
    chatwoot = StubProjectionChatwoot(fail=True)
    worker = OptOutProjectionWorker(
        supabase=supabase,  # type: ignore[arg-type]
        chatwoot=chatwoot,  # type: ignore[arg-type]
        worker_id="projection-worker-1",
    )

    assert asyncio.run(worker.run_once()) == 1
    assert supabase.finalizations[0]["applied"] is False
    assert supabase.finalizations[0]["error_code"] == (
        "chatwoot_ChatwootProtocolError"
    )


class StubHotmartTimerSupabase:
    def __init__(self, *, fail_id: str | None = None) -> None:
        self.fail_id = fail_id
        self.list_calls: list[dict[str, object]] = []
        self.reevaluation_calls: list[dict[str, object]] = []

    async def list_due_hotmart_abandonment_reevaluations(
        self, **kwargs: object
    ) -> list[str]:
        self.list_calls.append(kwargs)
        return ["timer-001", "timer-002"]

    async def reevaluate_hotmart_abandonment_timer(
        self, **kwargs: object
    ) -> object:
        self.reevaluation_calls.append(kwargs)
        if kwargs["reevaluation_id"] == self.fail_id:
            raise SupabaseError("timer_probe_failure")
        return SimpleNamespace(
            reevaluation_id=kwargs["reevaluation_id"],
            outcome="blocked_not_authorized",
        )


def test_hotmart_timer_worker_processes_only_due_ids() -> None:
    supabase = StubHotmartTimerSupabase()
    worker = HotmartAbandonmentTimerWorker(
        supabase=supabase,  # type: ignore[arg-type]
        poll_interval_seconds=0.1,
        batch_size=25,
        clock=lambda: "2026-08-21T16:00:00+00:00",
    )

    assert asyncio.run(worker.run_once()) == 2
    assert supabase.list_calls == [
        {
            "now": "2026-08-21T16:00:00+00:00",
            "batch_size": 25,
            "include_precheckout": False,
        }
    ]
    assert supabase.reevaluation_calls == [
        {
            "reevaluation_id": "timer-001",
            "now": "2026-08-21T16:00:00+00:00",
        },
        {
            "reevaluation_id": "timer-002",
            "now": "2026-08-21T16:00:00+00:00",
        },
    ]


def test_hotmart_timer_worker_continues_after_one_rpc_failure() -> None:
    supabase = StubHotmartTimerSupabase(fail_id="timer-001")
    worker = HotmartAbandonmentTimerWorker(
        supabase=supabase,  # type: ignore[arg-type]
        batch_size=10,
        clock=lambda: "2026-08-21T16:00:00+00:00",
    )

    assert asyncio.run(worker.run_once()) == 1
    assert [call["reevaluation_id"] for call in supabase.reevaluation_calls] == [
        "timer-001",
        "timer-002",
    ]


class StubPrecheckoutTimerSupabase:
    def __init__(
        self,
        *,
        sender_outcome: str = "accepted_by_chatwoot",
        fail_acceptance_finish: bool = False,
        send_authorized: bool = True,
        authorization_reason: str | None = None,
        fail_projection_once: bool = False,
        recover_inflight: bool = False,
    ) -> None:
        self.sender_outcome = sender_outcome
        self.fail_acceptance_finish = fail_acceptance_finish
        self.send_authorized = send_authorized
        self.authorization_reason = authorization_reason
        self.fail_projection_once = fail_projection_once
        self.recover_inflight = recover_inflight
        self.list_calls: list[dict[str, object]] = []
        self.reevaluation_calls = 0
        self.projection_calls: list[dict[str, object]] = []
        self.finish_calls: list[dict[str, object]] = []

    async def list_due_hotmart_abandonment_reevaluations(
        self, **kwargs: object
    ) -> list[str]:
        self.list_calls.append(kwargs)
        if self.finish_calls:
            return []
        return ["00000000-0000-0000-0000-000000000101"]

    async def reevaluate_hotmart_abandonment_timer(
        self, **kwargs: object
    ) -> object:
        self.reevaluation_calls += 1
        return SimpleNamespace(
            reevaluation_id=kwargs["reevaluation_id"],
            outcome="command_reserved",
            replayed=self.reevaluation_calls > 1,
        )

    async def get_precheckout_delayed_one_shot_command(
        self, **kwargs: object
    ) -> object:
        self.projection_calls.append(kwargs)
        if self.fail_projection_once:
            self.fail_projection_once = False
            raise SupabaseError("projection_transient_failure")
        command_status = (
            "delivery_unknown"
            if self.recover_inflight
            else str(self.finish_calls[-1]["outcome"])
            if self.finish_calls
            else "request_started"
        )
        return SimpleNamespace(
            command_id="00000000-0000-0000-0000-000000000201",
            command_status=command_status,
            target_phone="593999999999",
            buyer_name="Nombre",
            buyer_email="buyer@example.invalid",
            product_name="Libre de Ansiedad",
            template_name="johanna_interes_precheckout_01",
            template_language="es_EC",
            template_category="MARKETING",
            copy_version="johanna-precheckout-delayed-first-touch-v1",
            send_authorized=self.send_authorized,
            authorization_reason=self.authorization_reason,
        )

    async def finish_johanna_abandonment_one_shot(
        self, **kwargs: object
    ) -> object:
        self.finish_calls.append(kwargs)
        if (
            self.fail_acceptance_finish
            and kwargs["outcome"] == "accepted_by_chatwoot"
        ):
            raise SupabaseError("acceptance_finish_failed")
        return SimpleNamespace(
            command_id=kwargs["command_id"], command_status=kwargs["outcome"]
        )


class CancellationDuringFinishSupabase(StubPrecheckoutTimerSupabase):
    def __init__(self) -> None:
        super().__init__()
        self.acceptance_started = asyncio.Event()
        self.block = asyncio.Event()

    async def finish_johanna_abandonment_one_shot(
        self, **kwargs: object
    ) -> object:
        self.finish_calls.append(kwargs)
        if kwargs["outcome"] == "accepted_by_chatwoot":
            self.acceptance_started.set()
            await self.block.wait()
            raise AssertionError("cancelled acceptance must not complete normally")
        return SimpleNamespace(
            command_id=kwargs["command_id"], command_status=kwargs["outcome"]
        )


class StubPrecheckoutSender:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def send_first_touch(self, **kwargs: object) -> Any:
        self.calls.append(kwargs)
        return self.result

    async def send_first_touch_to_conversation(self, **_: object) -> object:
        raise AssertionError("precheckout must provision first touch")

    async def send_followup(self, **_: object) -> object:
        raise AssertionError("precheckout must not send follow-ups")


class CancellationProbePrecheckoutSender:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.block = asyncio.Event()

    async def send_first_touch(self, **_: object) -> FirstTouchResult:
        self.started.set()
        await self.block.wait()
        raise AssertionError("cancelled sender must not complete normally")

    async def send_first_touch_to_conversation(self, **_: object) -> object:
        raise AssertionError("precheckout must provision first touch")

    async def send_followup(self, **_: object) -> object:
        raise AssertionError("precheckout must not send follow-ups")


class CancellationSuppressingPrecheckoutSender:
    def __init__(self) -> None:
        context = multiprocessing.get_context("spawn")
        self.started = context.Event()
        self.cancel_seen = context.Event()
        self.release = context.Event()

    async def send_first_touch(self, **_: object) -> FirstTouchResult:
        self.started.set()
        while not self.release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.cancel_seen.set()
        return FirstTouchResult("failed", None, None, "chatwoot_http_error")

    async def send_first_touch_to_conversation(self, **_: object) -> object:
        raise AssertionError("precheckout must provision first touch")

    async def send_followup(self, **_: object) -> object:
        raise AssertionError("precheckout must not send follow-ups")


class ProcessRecordingPrecheckoutSender:
    def __init__(self) -> None:
        context = multiprocessing.get_context("spawn")
        self.receiving, self.sending = context.Pipe(duplex=False)

    async def send_first_touch(self, **kwargs: object) -> FirstTouchResult:
        self.sending.send(kwargs)
        return FirstTouchResult("sent", 701, 801)

    async def send_first_touch_to_conversation(self, **_: object) -> object:
        raise AssertionError("precheckout must provision first touch")

    async def send_followup(self, **_: object) -> object:
        raise AssertionError("precheckout must not send follow-ups")


class RepeatedCancellationFinishSupabase(StubPrecheckoutTimerSupabase):
    def __init__(self) -> None:
        super().__init__()
        self.acceptance_started = asyncio.Event()
        self.unknown_started = asyncio.Event()
        self.release_unknown = asyncio.Event()

    async def finish_johanna_abandonment_one_shot(
        self, **kwargs: object
    ) -> object:
        self.finish_calls.append(kwargs)
        if kwargs["outcome"] == "accepted_by_chatwoot":
            self.acceptance_started.set()
            await asyncio.Event().wait()
        self.unknown_started.set()
        await self.release_unknown.wait()
        return SimpleNamespace(
            command_id=kwargs["command_id"], command_status=kwargs["outcome"]
        )


def test_precheckout_timer_worker_sends_once_and_replay_is_silent() -> None:
    supabase = StubPrecheckoutTimerSupabase()
    sender = StubPrecheckoutSender(FirstTouchResult("sent", 701, 801))
    worker = HotmartAbandonmentTimerWorker(
        supabase=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
        precheckout_first_touch_enabled=True,
        precheckout_outbound_enabled=True,
        isolate_precheckout_sender_process=False,
        clock=lambda: "2026-08-29T18:00:00+00:00",
    )

    assert asyncio.run(worker.run_once()) == 1
    assert asyncio.run(worker.run_once()) == 0

    assert supabase.list_calls == [
        {
            "now": "2026-08-29T18:00:00+00:00",
            "batch_size": 10,
            "include_precheckout": True,
        },
        {
            "now": "2026-08-29T18:00:00+00:00",
            "batch_size": 10,
            "include_precheckout": True,
        },
    ]
    assert supabase.projection_calls == [
        {"reevaluation_id": "00000000-0000-0000-0000-000000000101"}
    ]
    assert sender.calls == [
        {
            "phone": "593999999999",
            "buyer_name": "Nombre",
            "buyer_email": "buyer@example.invalid",
            "product_name": "Libre de Ansiedad",
            "content": (
                "Hola, Nombre. Te escribe el equipo de la Psic. Johanna. "
                "Vimos que completaste el formulario de Libre de Ansiedad. "
                "¿Quieres que te ayudemos a continuar? Si no deseas recibir "
                "más mensajes, responde “No más mensajes”."
            ),
            "delivery_id": "00000000-0000-0000-0000-000000000201",
        }
    ]
    assert supabase.finish_calls == [
        {
            "command_id": "00000000-0000-0000-0000-000000000201",
            "outcome": "accepted_by_chatwoot",
            "chatwoot_conversation_id": 701,
            "chatwoot_message_id": 801,
            "failure_code": None,
        }
    ]


def test_precheckout_timer_worker_reserves_without_request_start_when_outbound_off() -> None:
    supabase = StubPrecheckoutTimerSupabase()
    sender = StubPrecheckoutSender(FirstTouchResult("sent", 701, 801))
    worker = HotmartAbandonmentTimerWorker(
        supabase=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
        precheckout_first_touch_enabled=True,
        precheckout_outbound_enabled=False,
        isolate_precheckout_sender_process=False,
        clock=lambda: "2026-08-29T18:00:00+00:00",
    )

    assert asyncio.run(worker.run_once()) == 1

    assert supabase.list_calls == [
        {
            "now": "2026-08-29T18:00:00+00:00",
            "batch_size": 10,
            "include_precheckout": True,
        }
    ]
    assert supabase.reevaluation_calls == 1
    assert supabase.projection_calls == []
    assert sender.calls == []
    assert supabase.finish_calls == []


def test_precheckout_timer_worker_process_boundary_posts_once() -> None:
    supabase = StubPrecheckoutTimerSupabase()
    sender = ProcessRecordingPrecheckoutSender()
    worker = HotmartAbandonmentTimerWorker(
        supabase=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
        precheckout_first_touch_enabled=True,
        precheckout_outbound_enabled=True,
    )

    assert asyncio.run(worker.run_once()) == 1
    assert sender.receiving.poll(1)
    assert sender.receiving.recv()["delivery_id"] == (
        "00000000-0000-0000-0000-000000000201"
    )
    assert supabase.finish_calls == [
        {
            "command_id": "00000000-0000-0000-0000-000000000201",
            "outcome": "accepted_by_chatwoot",
            "chatwoot_conversation_id": 701,
            "chatwoot_message_id": 801,
            "failure_code": None,
        }
    ]


def test_precheckout_timer_worker_retries_reserved_projection_without_losing_command() -> None:
    supabase = StubPrecheckoutTimerSupabase(fail_projection_once=True)
    sender = StubPrecheckoutSender(FirstTouchResult("sent", 701, 801))
    worker = HotmartAbandonmentTimerWorker(
        supabase=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
        precheckout_first_touch_enabled=True,
        precheckout_outbound_enabled=True,
        isolate_precheckout_sender_process=False,
    )

    assert asyncio.run(worker.run_once()) == 1
    assert sender.calls == []
    assert asyncio.run(worker.run_once()) == 1
    assert len(supabase.projection_calls) == 2
    assert len(sender.calls) == 1
    assert supabase.finish_calls[-1]["outcome"] == "accepted_by_chatwoot"


def test_precheckout_timer_worker_recovers_inflight_without_resend() -> None:
    supabase = StubPrecheckoutTimerSupabase(recover_inflight=True)
    sender = StubPrecheckoutSender(FirstTouchResult("sent", 701, 801))
    worker = HotmartAbandonmentTimerWorker(
        supabase=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
        precheckout_first_touch_enabled=True,
        precheckout_outbound_enabled=True,
        isolate_precheckout_sender_process=False,
    )

    assert asyncio.run(worker.run_once()) == 1
    assert len(supabase.projection_calls) == 1
    assert sender.calls == []


def test_precheckout_timer_worker_records_sender_ambiguity_without_retry() -> None:
    supabase = StubPrecheckoutTimerSupabase()
    sender = StubPrecheckoutSender(
        FirstTouchResult("failed", None, None, "chatwoot_http_error")
    )
    worker = HotmartAbandonmentTimerWorker(
        supabase=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
        precheckout_first_touch_enabled=True,
        precheckout_outbound_enabled=True,
        isolate_precheckout_sender_process=False,
    )

    assert asyncio.run(worker.run_once()) == 1
    assert asyncio.run(worker.run_once()) == 0
    assert len(sender.calls) == 1
    assert supabase.finish_calls == [
        {
            "command_id": "00000000-0000-0000-0000-000000000201",
            "outcome": "delivery_unknown",
            "chatwoot_conversation_id": None,
            "chatwoot_message_id": None,
            "failure_code": "chatwoot_http_error",
        }
    ]


def test_precheckout_timer_worker_rejects_malformed_sender_result() -> None:
    supabase = StubPrecheckoutTimerSupabase()
    sender = StubPrecheckoutSender(object())
    worker = HotmartAbandonmentTimerWorker(
        supabase=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
        precheckout_first_touch_enabled=True,
        precheckout_outbound_enabled=True,
        isolate_precheckout_sender_process=False,
    )

    assert asyncio.run(worker.run_once()) == 1
    assert supabase.finish_calls == [
        {
            "command_id": "00000000-0000-0000-0000-000000000201",
            "outcome": "delivery_unknown",
            "chatwoot_conversation_id": None,
            "chatwoot_message_id": None,
            "failure_code": "sender_invalid_result",
        }
    ]


def test_precheckout_timer_worker_honors_pre_send_authority_stop() -> None:
    supabase = StubPrecheckoutTimerSupabase(
        send_authorized=False,
        authorization_reason="cancelled_purchased",
    )
    sender = StubPrecheckoutSender(FirstTouchResult("sent", 701, 801))
    worker = HotmartAbandonmentTimerWorker(
        supabase=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
        precheckout_first_touch_enabled=True,
        precheckout_outbound_enabled=True,
        isolate_precheckout_sender_process=False,
    )

    assert asyncio.run(worker.run_once()) == 1
    assert sender.calls == []
    assert supabase.finish_calls == [
        {
            "command_id": "00000000-0000-0000-0000-000000000201",
            "outcome": "delivery_unknown",
            "chatwoot_conversation_id": None,
            "chatwoot_message_id": None,
            "failure_code": "cancelled_purchased",
        }
    ]


def test_precheckout_timer_worker_finalizes_unknown_when_stopped_during_send() -> None:
    async def scenario() -> tuple[StubPrecheckoutTimerSupabase, float]:
        supabase = StubPrecheckoutTimerSupabase()
        sender = CancellationProbePrecheckoutSender()
        worker = HotmartAbandonmentTimerWorker(
            supabase=supabase,  # type: ignore[arg-type]
            message_sender=sender,  # type: ignore[arg-type]
            precheckout_first_touch_enabled=True,
            precheckout_outbound_enabled=True,
            isolate_precheckout_sender_process=False,
            poll_interval_seconds=60,
        )
        await worker.start()
        await asyncio.wait_for(sender.started.wait(), timeout=1)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await worker.stop(timeout=0.1)
        return supabase, loop.time() - started

    supabase, shutdown_seconds = asyncio.run(scenario())

    assert shutdown_seconds < 1
    assert supabase.finish_calls == [
        {
            "command_id": "00000000-0000-0000-0000-000000000201",
            "outcome": "delivery_unknown",
            "chatwoot_conversation_id": None,
            "chatwoot_message_id": None,
            "failure_code": "sender_cancelled",
        }
    ]


def test_precheckout_timer_worker_hard_stops_sender_that_suppresses_cancel() -> None:
    async def scenario() -> tuple[StubPrecheckoutTimerSupabase, bool]:
        supabase = StubPrecheckoutTimerSupabase()
        sender = CancellationSuppressingPrecheckoutSender()
        worker = HotmartAbandonmentTimerWorker(
            supabase=supabase,  # type: ignore[arg-type]
            message_sender=sender,  # type: ignore[arg-type]
            precheckout_first_touch_enabled=True,
            precheckout_outbound_enabled=True,
            poll_interval_seconds=60,
        )
        await worker.start()
        assert await asyncio.to_thread(sender.started.wait, 3)
        try:
            await worker.stop(timeout=0.05)
            return supabase, worker._task is None
        finally:
            sender.release.set()
            if worker._task is not None:
                await asyncio.wait_for(worker._task, timeout=1)

    supabase, worker_released = asyncio.run(scenario())

    assert worker_released is True
    assert supabase.finish_calls == [
        {
            "command_id": "00000000-0000-0000-0000-000000000201",
            "outcome": "delivery_unknown",
            "chatwoot_conversation_id": None,
            "chatwoot_message_id": None,
            "failure_code": "sender_cancelled",
        }
    ]


def test_precheckout_timer_worker_preserves_ids_when_stopped_during_finish() -> None:
    async def scenario() -> CancellationDuringFinishSupabase:
        supabase = CancellationDuringFinishSupabase()
        sender = StubPrecheckoutSender(FirstTouchResult("sent", 701, 801))
        worker = HotmartAbandonmentTimerWorker(
            supabase=supabase,  # type: ignore[arg-type]
            message_sender=sender,  # type: ignore[arg-type]
            precheckout_first_touch_enabled=True,
            precheckout_outbound_enabled=True,
            isolate_precheckout_sender_process=False,
            poll_interval_seconds=60,
        )
        await worker.start()
        await asyncio.wait_for(supabase.acceptance_started.wait(), timeout=1)
        await worker.stop(timeout=0.1)
        return supabase

    supabase = asyncio.run(scenario())

    assert supabase.finish_calls[-1] == {
        "command_id": "00000000-0000-0000-0000-000000000201",
        "outcome": "delivery_unknown",
        "chatwoot_conversation_id": 701,
        "chatwoot_message_id": 801,
        "failure_code": "acceptance_finalization_cancelled",
    }


def test_precheckout_cancellation_finalization_survives_repeated_cancel() -> None:
    async def scenario() -> RepeatedCancellationFinishSupabase:
        supabase = RepeatedCancellationFinishSupabase()
        sender = StubPrecheckoutSender(FirstTouchResult("sent", 701, 801))
        worker = HotmartAbandonmentTimerWorker(
            supabase=supabase,  # type: ignore[arg-type]
            message_sender=sender,  # type: ignore[arg-type]
            precheckout_first_touch_enabled=True,
            precheckout_outbound_enabled=True,
            isolate_precheckout_sender_process=False,
            poll_interval_seconds=60,
        )
        await worker.start()
        await asyncio.wait_for(supabase.acceptance_started.wait(), timeout=1)
        stop_task = asyncio.create_task(worker.stop(timeout=0.05))
        await asyncio.wait_for(supabase.unknown_started.wait(), timeout=1)
        stop_task.cancel()
        stop_task.cancel()
        await asyncio.sleep(0)
        assert not stop_task.done()
        supabase.release_unknown.set()
        await asyncio.wait_for(stop_task, timeout=1)
        return supabase

    supabase = asyncio.run(scenario())

    assert supabase.finish_calls[-1] == {
        "command_id": "00000000-0000-0000-0000-000000000201",
        "outcome": "delivery_unknown",
        "chatwoot_conversation_id": 701,
        "chatwoot_message_id": 801,
        "failure_code": "acceptance_finalization_cancelled",
    }


def test_precheckout_timer_worker_preserves_remote_ids_when_finish_is_ambiguous() -> None:
    supabase = StubPrecheckoutTimerSupabase(fail_acceptance_finish=True)
    sender = StubPrecheckoutSender(FirstTouchResult("sent", 701, 801))
    worker = HotmartAbandonmentTimerWorker(
        supabase=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
        precheckout_first_touch_enabled=True,
        precheckout_outbound_enabled=True,
        isolate_precheckout_sender_process=False,
    )

    assert asyncio.run(worker.run_once()) == 1
    assert len(sender.calls) == 1
    assert supabase.finish_calls[-1] == {
        "command_id": "00000000-0000-0000-0000-000000000201",
        "outcome": "delivery_unknown",
        "chatwoot_conversation_id": 701,
        "chatwoot_message_id": 801,
        "failure_code": "acceptance_finalization_failed",
    }


def test_supabase_lists_precheckout_only_through_explicit_v2_flag() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[{"reevaluation_id": "00000000-0000-0000-0000-000000000101"}],
            request=request,
        )

    client = _make_supabase(httpx.MockTransport(handler))  # type: ignore[arg-type]
    result = _run(
        client.list_due_hotmart_abandonment_reevaluations(
            now="2026-08-29T18:00:00+00:00",
            batch_size=10,
            include_precheckout=True,
        )
    )

    assert result == ["00000000-0000-0000-0000-000000000101"]
    assert seen[0].url.path.endswith(
        "/rpc/list_due_hotmart_abandonment_reevaluations_v2"
    )
    assert json.loads(seen[0].content) == {
        "p_now": "2026-08-29T18:00:00+00:00",
        "p_batch_size": 10,
        "p_include_precheckout": True,
    }


def test_supabase_parses_command_reserved_reevaluation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "reevaluation_id": "00000000-0000-0000-0000-000000000101",
                    "reevaluation_status": "completed",
                    "reevaluation_outcome": "command_reserved",
                    "completed_at": "2026-08-29T18:00:00+00:00",
                    "replayed": False,
                }
            ],
            request=request,
        )

    client = _make_supabase(httpx.MockTransport(handler))  # type: ignore[arg-type]
    result = _run(
        client.reevaluate_hotmart_abandonment_timer(
            reevaluation_id="00000000-0000-0000-0000-000000000101",
            now="2026-08-29T18:00:00+00:00",
        )
    )

    assert result.outcome == "command_reserved"


@pytest.mark.parametrize(
    "outcome",
    [
        "superseded_by_provider_event",
        "blocked_contact",
        "blocked_identity",
        "blocked_handoff",
        "budget_consumed",
    ],
)
def test_supabase_parses_every_terminal_precheckout_reevaluation_outcome(
    outcome: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "reevaluation_id": "00000000-0000-0000-0000-000000000101",
                    "reevaluation_status": "completed",
                    "reevaluation_outcome": outcome,
                    "completed_at": "2026-08-29T18:00:00+00:00",
                    "replayed": False,
                }
            ],
            request=request,
        )

    client = _make_supabase(httpx.MockTransport(handler))  # type: ignore[arg-type]
    result = _run(
        client.reevaluate_hotmart_abandonment_timer(
            reevaluation_id="00000000-0000-0000-0000-000000000101",
            now="2026-08-29T18:00:00+00:00",
        )
    )

    assert result.outcome == outcome
    assert result.replayed is False


def test_supabase_parses_exact_precheckout_sender_projection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            "/rpc/get_precheckout_delayed_one_shot_command"
        )
        return httpx.Response(
            200,
            json=[
                {
                    "command_id": "00000000-0000-0000-0000-000000000201",
                    "command_status": "request_started",
                    "target_phone": "593999999999",
                    "buyer_name": "Nombre",
                    "buyer_email": "buyer@example.invalid",
                    "product_name": "Libre de Ansiedad",
                    "template_name": "johanna_interes_precheckout_01",
                    "template_language": "es_EC",
                    "template_category": "MARKETING",
                    "copy_version": "johanna-precheckout-delayed-first-touch-v1",
                    "send_authorized": True,
                    "authorization_reason": None,
                }
            ],
            request=request,
        )

    client = _make_supabase(httpx.MockTransport(handler))  # type: ignore[arg-type]
    command = _run(
        client.get_precheckout_delayed_one_shot_command(
            reevaluation_id="00000000-0000-0000-0000-000000000101"
        )
    )

    assert command.command_id == "00000000-0000-0000-0000-000000000201"
    assert command.command_status == "request_started"
    assert command.template_name == "johanna_interes_precheckout_01"
    assert command.send_authorized is True
    assert command.authorization_reason is None
