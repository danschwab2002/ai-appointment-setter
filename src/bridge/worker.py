"""Deferred worker that consumes Hotmart webhook events from Supabase.

Polls ``webhook_events`` for rows in ``received`` status, passes each one
through :func:`bridge.resolution.resolve_event` to resolve identity, then
sends the resulting SituationReport to the agent for a recovery decision.
Handles errors without crashing the loop.  Designed to run as a single
background asyncio task inside the FastAPI application.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from bridge.chatwoot import ChatwootClient, ChatwootProtocolError
from bridge.messaging import FirstTouchResult, MessageSender, is_allowed_whatsapp_target
from bridge.recovery_agent import (
    FollowupMessageProposal,
    RecoveryAgentClient,
    is_valid_followup_message_proposal,
    required_recovery_decision,
)
from bridge.resolution import ResolutionError, resolve_event
from bridge.supabase import (
    DeliveryAttempt,
    FollowupExecutionContext,
    ReevaluationDecision,
    ScheduledAction,
    SupabaseClient,
    SupabaseCommittedResponseError,
    SupabaseError,
)

logger = logging.getLogger(__name__)


async def _await_with_cancellation_state(coro: Any) -> tuple[Any, bool]:
    """Finish one critical await and report whether parent cancellation occurred."""
    task = asyncio.create_task(coro)
    cancellation_seen = False
    while True:
        try:
            return await asyncio.shield(task), cancellation_seen
        except asyncio.CancelledError:
            cancellation_seen = True
            if task.done():
                return task.result(), cancellation_seen


async def _await_despite_cancellation(coro: Any) -> Any:
    """Finish one safety-critical await even if cancellation repeats."""
    result, _ = await _await_with_cancellation_state(coro)
    return result


async def _commit_outcome_despite_cancellation(coro: Any) -> Any:
    """Commit an external outcome before propagating parent cancellation."""
    result = await _await_despite_cancellation(coro)
    current = asyncio.current_task()
    if current is not None and current.cancelling():
        raise asyncio.CancelledError
    return result


def _validate_followup_execution_context(
    action: ScheduledAction,
    context: FollowupExecutionContext,
) -> None:
    """Fail closed if fenced reasoning context belongs to another action."""
    if (
        context.action_id != action.action_id
        or context.action_type != action.action_type
        or context.step_key != action.step_key
        or context.recovery_case_id != action.recovery_case_id
    ):
        raise SupabaseError("followup_execution_context_mismatch")


def _validate_followup_message_proposal(
    proposal: FollowupMessageProposal,
) -> None:
    """Revalidate agent output at the dispatcher trust boundary."""
    if not isinstance(proposal, FollowupMessageProposal) or not (
        is_valid_followup_message_proposal({
            "strategy": proposal.strategy,
            "message": proposal.message,
        })
    ):
        raise SupabaseError("invalid_followup_message_proposal")


def _validate_reserved_delivery_attempt(
    action: ScheduledAction,
    decision: ReevaluationDecision,
    attempt: DeliveryAttempt,
) -> None:
    """Accept only the exact reservation authorized for this outbound slice."""
    if (
        attempt.action_id != action.action_id
        or attempt.idempotency_key != action.idempotency_key
        or attempt.channel != "whatsapp"
        or attempt.mode != "freeform"
        or attempt.phase != "reserved"
        or attempt.lease_generation != action.lease_generation
        or attempt.expected_case_version != decision.case_version
        or attempt.expected_sequence_revision != decision.sequence_revision
    ):
        raise SupabaseError("reserved_delivery_attempt_mismatch")


def _validate_started_delivery_attempt(
    action: ScheduledAction,
    decision: ReevaluationDecision,
    reserved: DeliveryAttempt,
    started: DeliveryAttempt,
) -> None:
    """Require the request-start transition to preserve the complete fence."""
    if (
        started.attempt_id != reserved.attempt_id
        or started.action_id != action.action_id
        or started.idempotency_key != action.idempotency_key
        or started.attempt_number != reserved.attempt_number
        or started.channel != "whatsapp"
        or started.mode != "freeform"
        or started.phase != "request_started"
        or started.lease_generation != action.lease_generation
        or started.expected_case_version != decision.case_version
        or started.expected_sequence_revision != decision.sequence_revision
    ):
        raise SupabaseError("started_delivery_attempt_mismatch")


class DurableDispatcher:
    """Claim and re-evaluate due actions without producing external effects."""

    def __init__(
        self,
        *,
        supabase: SupabaseClient,
        worker_id: str,
        lease_duration: str = "5 minutes",
        batch_size: int = 10,
        poll_interval_seconds: float = 5.0,
        chatwoot: ChatwootClient | None = None,
        chatwoot_account_id: int | None = None,
        recovery_agent: RecoveryAgentClient | None = None,
        sender: MessageSender | None = None,
        allowed_jid: str | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._supabase = supabase
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._batch_size = batch_size
        self._poll_interval = poll_interval_seconds
        self._chatwoot = chatwoot
        self._chatwoot_account_id = chatwoot_account_id
        self._recovery_agent = recovery_agent
        self._sender = sender
        self._allowed_jid = allowed_jid
        self._clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat()
        )
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self, *, timeout: float = 10.0) -> None:
        self._stopped.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.dispatch_due(now=datetime.now(timezone.utc).isoformat())
            except SupabaseError:
                logger.exception("durable_dispatcher_supabase_error")
            except Exception:
                logger.exception("durable_dispatcher_unexpected_error")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self._poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def claim_due(self, *, now: str) -> list[ScheduledAction]:
        """Return actions leased to this worker; never call Hermes or Chatwoot."""
        actions = await self._supabase.claim_due_followup_actions(
            worker_id=self._worker_id,
            now=now,
            lease_duration=self._lease_duration,
            batch_size=self._batch_size,
        )
        for action in actions:
            if action.lease_owner != self._worker_id:
                raise SupabaseError("claim_due_followup_actions_owner_mismatch")
            if action.lease_generation < 1:
                raise SupabaseError("claim_due_followup_actions_invalid_generation")
            logger.info(
                "durable_action_claimed action_id=%s lease_generation=%s",
                action.action_id,
                action.lease_generation,
            )
        return actions

    async def _load_chatwoot_evidence(
        self,
        *,
        action: ScheduledAction,
        now: str,
    ) -> dict[str, object] | None:
        evidence: dict[str, object] | None = None
        try:
            context = await self._supabase.get_followup_chatwoot_context(
                action_id=action.action_id,
                worker_id=self._worker_id,
                lease_generation=action.lease_generation,
                now=now,
            )
            if (
                self._chatwoot is not None
                and self._chatwoot_account_id is not None
                and context.chatwoot_account_id == self._chatwoot_account_id
                and context.external_conversation_id is not None
                and context.expected_inbox_id is not None
            ):
                observed_at = datetime.fromisoformat(action.anchor_observed_at)
                snapshot = await self._chatwoot.get_canonical_conversation_snapshot(
                    conversation_id=context.external_conversation_id,
                    expected_inbox_id=context.expected_inbox_id,
                    anchor_message_id=context.anchor_external_message_id,
                    anchor_observed_at_epoch=int(observed_at.timestamp()),
                )
                if (
                    snapshot.checkpoint_message_id is not None
                    and snapshot.checkpoint_created_at is not None
                ):
                    evidence = {
                        "p_chatwoot_conversation_id": str(snapshot.conversation_id),
                        "p_chatwoot_checkpoint_message_id": str(
                            snapshot.checkpoint_message_id
                        ),
                        "p_chatwoot_checkpoint_at": datetime.fromtimestamp(
                            snapshot.checkpoint_created_at, tz=timezone.utc
                        ).isoformat(),
                        "p_chatwoot_status": snapshot.status,
                        "p_chatwoot_can_reply": snapshot.can_reply,
                        "p_chatwoot_anchor_found": snapshot.anchor_found,
                        "p_chatwoot_automation_paused": snapshot.automation_paused,
                        "p_chatwoot_inbound_after_anchor": (
                            snapshot.inbound_after_anchor
                        ),
                        "p_chatwoot_human_activity_after_anchor": (
                            snapshot.human_activity_after_anchor
                        ),
                    }
        except (SupabaseError, ChatwootProtocolError, httpx.HTTPError, ValueError):
            logger.exception(
                "canonical_chatwoot_check_failed action_id=%s",
                action.action_id,
            )
        return evidence

    async def dispatch_due(self, *, now: str) -> list[ReevaluationDecision]:
        """Claim and re-evaluate; return execute candidates without side effects."""
        actions = await self.claim_due(now=now)
        decisions: list[ReevaluationDecision] = []
        for action in actions:
            evidence = await self._load_chatwoot_evidence(action=action, now=now)
            decision = await self._supabase.reevaluate_followup_action(
                action_id=action.action_id,
                worker_id=self._worker_id,
                lease_generation=action.lease_generation,
                now=now,
                chatwoot_evidence=evidence,
            )
            if decision.decision == "execute":
                if action.action_type == "reconcile_delivery":
                    raise SupabaseError("reconcile_delivery_execute_forbidden")
                attempt = await self._supabase.reserve_followup_delivery_attempt(
                    action_id=action.action_id,
                    worker_id=self._worker_id,
                    lease_generation=action.lease_generation,
                    expected_case_version=decision.case_version,
                    expected_sequence_revision=decision.sequence_revision,
                    channel="whatsapp",
                    mode="freeform",
                    now=now,
                )
                _validate_reserved_delivery_attempt(
                    action,
                    decision,
                    attempt,
                )
                logger.info(
                    "durable_delivery_attempt_reserved action_id=%s attempt_id=%s",
                    action.action_id,
                    attempt.attempt_id,
                )
                if self._recovery_agent is not None:
                    execution_context = (
                        await self._supabase.get_followup_execution_context(
                            action_id=action.action_id,
                            worker_id=self._worker_id,
                            lease_generation=action.lease_generation,
                            now=now,
                        )
                    )
                    _validate_followup_execution_context(
                        action,
                        execution_context,
                    )
                    proposal = await self._recovery_agent.request_followup_message(
                        attempt_id=attempt.attempt_id,
                        execution_context=execution_context,
                    )
                    if proposal is None:
                        logger.warning(
                            "durable_followup_proposal_unavailable "
                            "action_id=%s attempt_id=%s",
                            action.action_id,
                            attempt.attempt_id,
                        )
                    elif isinstance(proposal, FollowupMessageProposal):
                        _validate_followup_message_proposal(proposal)
                        logger.info(
                            "durable_followup_proposal_ready "
                            "action_id=%s attempt_id=%s",
                            action.action_id,
                            attempt.attempt_id,
                        )
                        if self._sender is not None:
                            if not is_allowed_whatsapp_target(
                                execution_context.buyer_phone,
                                self._allowed_jid,
                            ):
                                raise SupabaseError(
                                    "followup_recipient_not_allowlisted"
                                )
                            final_now = self._clock()
                            final_evidence = await self._load_chatwoot_evidence(
                                action=action,
                                now=final_now,
                            )
                            final_decision = (
                                await self._supabase.reevaluate_followup_action(
                                    action_id=action.action_id,
                                    worker_id=self._worker_id,
                                    lease_generation=action.lease_generation,
                                    now=final_now,
                                    chatwoot_evidence=final_evidence,
                                )
                            )
                            decision = final_decision
                            if final_decision.decision != "execute":
                                decisions.append(decision)
                                continue
                            if (
                                final_decision.reason_code
                                != "eligible_for_execution"
                                or final_decision.case_version
                                != attempt.expected_case_version
                                or final_decision.sequence_revision
                                != attempt.expected_sequence_revision
                            ):
                                raise SupabaseError(
                                    "final_followup_reevaluation_mismatch"
                                )
                            try:
                                started, request_start_cancelled = (
                                    await _await_with_cancellation_state(
                                        self._supabase.mark_followup_request_started(
                                            action_id=action.action_id,
                                            attempt_id=attempt.attempt_id,
                                            worker_id=self._worker_id,
                                            lease_generation=action.lease_generation,
                                            now=final_now,
                                        )
                                    )
                                )
                            except SupabaseCommittedResponseError:
                                deadline = (
                                    datetime.fromisoformat(final_now)
                                    + timedelta(minutes=15)
                                ).isoformat()
                                finalized = await _commit_outcome_despite_cancellation(
                                    self._supabase.finalize_followup_delivery_attempt(
                                        action_id=action.action_id,
                                        attempt_id=attempt.attempt_id,
                                        worker_id=self._worker_id,
                                        lease_generation=action.lease_generation,
                                        outcome="delivery_unknown",
                                        remote_message_id=None,
                                        accepted_message_id=None,
                                        reason_code="request_start_response_invalid",
                                        next_attempt_at=None,
                                        reconciliation_deadline=deadline,
                                        now=final_now,
                                    )
                                )
                                if finalized.status != "delivery_unknown":
                                    raise SupabaseError(
                                        "followup_unknown_finalization_mismatch"
                                    )
                                raise
                            try:
                                _validate_started_delivery_attempt(
                                    action,
                                    final_decision,
                                    attempt,
                                    started,
                                )
                            except Exception:
                                deadline = (
                                    datetime.fromisoformat(final_now)
                                    + timedelta(minutes=15)
                                ).isoformat()
                                finalized = await _commit_outcome_despite_cancellation(
                                    self._supabase.finalize_followup_delivery_attempt(
                                        action_id=action.action_id,
                                        attempt_id=attempt.attempt_id,
                                        worker_id=self._worker_id,
                                        lease_generation=action.lease_generation,
                                        outcome="delivery_unknown",
                                        remote_message_id=None,
                                        accepted_message_id=None,
                                        reason_code="request_start_response_invalid",
                                        next_attempt_at=None,
                                        reconciliation_deadline=deadline,
                                        now=final_now,
                                    )
                                )
                                if finalized.status != "delivery_unknown":
                                    raise SupabaseError(
                                        "followup_unknown_finalization_mismatch"
                                    )
                                raise
                            if request_start_cancelled:
                                deadline = (
                                    datetime.fromisoformat(final_now)
                                    + timedelta(minutes=15)
                                ).isoformat()
                                await _commit_outcome_despite_cancellation(
                                    self._supabase.finalize_followup_delivery_attempt(
                                        action_id=action.action_id,
                                        attempt_id=attempt.attempt_id,
                                        worker_id=self._worker_id,
                                        lease_generation=action.lease_generation,
                                        outcome="delivery_unknown",
                                        remote_message_id=None,
                                        accepted_message_id=None,
                                        reason_code=(
                                            "request_start_cancelled_after_commit"
                                        ),
                                        next_attempt_at=None,
                                        reconciliation_deadline=deadline,
                                        now=final_now,
                                    )
                                )
                                raise asyncio.CancelledError
                            try:
                                result = await self._sender.send_first_touch(
                                    phone=execution_context.buyer_phone or "",
                                    buyer_name=execution_context.buyer_name,
                                    buyer_email=execution_context.buyer_email,
                                    content=proposal.message,
                                    delivery_id=attempt.attempt_id,
                                )
                            except asyncio.CancelledError:
                                deadline = (
                                    datetime.fromisoformat(final_now)
                                    + timedelta(minutes=15)
                                ).isoformat()
                                finalized = await _await_despite_cancellation(
                                    self._supabase.finalize_followup_delivery_attempt(
                                        action_id=action.action_id,
                                        attempt_id=attempt.attempt_id,
                                        worker_id=self._worker_id,
                                        lease_generation=action.lease_generation,
                                        outcome="delivery_unknown",
                                        remote_message_id=None,
                                        accepted_message_id=None,
                                        reason_code=(
                                            "sender_cancelled_after_request_started"
                                        ),
                                        next_attempt_at=None,
                                        reconciliation_deadline=deadline,
                                        now=final_now,
                                    )
                                )
                                if finalized.status != "delivery_unknown":
                                    raise SupabaseError(
                                        "followup_unknown_finalization_mismatch"
                                    )
                                raise
                            except Exception:
                                deadline = (
                                    datetime.fromisoformat(final_now)
                                    + timedelta(minutes=15)
                                ).isoformat()
                                finalized = await _commit_outcome_despite_cancellation(
                                    self._supabase.finalize_followup_delivery_attempt(
                                        action_id=action.action_id,
                                        attempt_id=attempt.attempt_id,
                                        worker_id=self._worker_id,
                                        lease_generation=action.lease_generation,
                                        outcome="delivery_unknown",
                                        remote_message_id=None,
                                        accepted_message_id=None,
                                        reason_code=(
                                            "sender_exception_after_request_started"
                                        ),
                                        next_attempt_at=None,
                                        reconciliation_deadline=deadline,
                                        now=final_now,
                                    )
                                )
                                if finalized.status != "delivery_unknown":
                                    raise SupabaseError(
                                        "followup_unknown_finalization_mismatch"
                                    )
                                result = None
                            valid_sent = (
                                isinstance(result, FirstTouchResult)
                                and result.status == "sent"
                                and isinstance(result.conversation_id, int)
                                and not isinstance(result.conversation_id, bool)
                                and result.conversation_id > 0
                                and isinstance(result.message_id, int)
                                and not isinstance(result.message_id, bool)
                                and result.message_id > 0
                            )
                            if result is None:
                                pass
                            elif (
                                isinstance(result, FirstTouchResult)
                                and result.status == "blocked"
                            ):
                                finalized = await _commit_outcome_despite_cancellation(
                                    self._supabase.finalize_followup_delivery_attempt(
                                        action_id=action.action_id,
                                        attempt_id=attempt.attempt_id,
                                        worker_id=self._worker_id,
                                        lease_generation=action.lease_generation,
                                        outcome="rejected",
                                        remote_message_id=None,
                                        accepted_message_id=None,
                                        reason_code=(
                                            "sender_rejected_before_delivery"
                                        ),
                                        next_attempt_at=None,
                                        reconciliation_deadline=None,
                                        now=final_now,
                                    )
                                )
                                if finalized.status != "permanent_failed":
                                    raise SupabaseError(
                                        "followup_rejection_finalization_mismatch"
                                    )
                            elif not valid_sent:
                                deadline = (
                                    datetime.fromisoformat(final_now)
                                    + timedelta(minutes=15)
                                ).isoformat()
                                finalized = await _commit_outcome_despite_cancellation(
                                    self._supabase.finalize_followup_delivery_attempt(
                                        action_id=action.action_id,
                                        attempt_id=attempt.attempt_id,
                                        worker_id=self._worker_id,
                                        lease_generation=action.lease_generation,
                                        outcome="delivery_unknown",
                                        remote_message_id=None,
                                        accepted_message_id=None,
                                        reason_code="sender_result_inconclusive",
                                        next_attempt_at=None,
                                        reconciliation_deadline=deadline,
                                        now=final_now,
                                    )
                                )
                                if finalized.status != "delivery_unknown":
                                    raise SupabaseError(
                                        "followup_unknown_finalization_mismatch"
                                    )
                            else:
                                try:
                                    finalized = (
                                        await _commit_outcome_despite_cancellation(
                                            self._supabase.record_and_finalize_followup_acceptance(
                                                action_id=action.action_id,
                                                attempt_id=attempt.attempt_id,
                                                worker_id=self._worker_id,
                                                lease_generation=(
                                                    action.lease_generation
                                                ),
                                                external_conversation_id=str(
                                                    result.conversation_id
                                                ),
                                                remote_message_id=str(
                                                    result.message_id
                                                ),
                                                message_content=proposal.message,
                                                now=final_now,
                                            )
                                        )
                                    )
                                    if finalized.status != "accepted_by_chatwoot":
                                        raise SupabaseError(
                                            "followup_acceptance_finalization_mismatch"
                                        )
                                except SupabaseError:
                                    # The message was already delivered by the
                                    # sender, but persisting the canonical
                                    # acceptance failed (e.g. HTTP 400 from a
                                    # finalize invariant). Never strand the
                                    # attempt at request_started: resolve it to a
                                    # durable, reconcilable delivery_unknown so
                                    # reconciliation owns the canonical binding
                                    # instead of a blind resend.
                                    deadline = (
                                        datetime.fromisoformat(final_now)
                                        + timedelta(minutes=15)
                                    ).isoformat()
                                    unknown = (
                                        await _commit_outcome_despite_cancellation(
                                            self._supabase.finalize_followup_delivery_attempt(
                                                action_id=action.action_id,
                                                attempt_id=attempt.attempt_id,
                                                worker_id=self._worker_id,
                                                lease_generation=(
                                                    action.lease_generation
                                                ),
                                                outcome="delivery_unknown",
                                                remote_message_id=str(
                                                    result.message_id
                                                ),
                                                accepted_message_id=None,
                                                reason_code=(
                                                    "acceptance_finalization_failed"
                                                ),
                                                next_attempt_at=None,
                                                reconciliation_deadline=deadline,
                                                now=final_now,
                                            )
                                        )
                                    )
                                    if unknown.status != "delivery_unknown":
                                        raise SupabaseError(
                                            "followup_unknown_finalization_mismatch"
                                        )
                    else:
                        raise SupabaseError("invalid_followup_message_proposal")
            decisions.append(decision)
            logger.info(
                "durable_action_reevaluated action_id=%s decision=%s reason=%s",
                action.action_id,
                decision.decision,
                decision.reason_code,
            )
        return decisions


class ResolutionWorker:
    """Consume ingress events and, in durable mode, dispatch due actions."""

    def __init__(
        self,
        *,
        supabase: SupabaseClient,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 10,
        recovery_agent: RecoveryAgentClient | None = None,
        message_sender: MessageSender | None = None,
        allowed_jid: str | None = None,
        chatwoot_account_id: int | None = None,
        chatwoot_inbox_id: int | None = None,
        policy_key: str | None = None,
        policy_version: int | None = None,
    ) -> None:
        self._supabase = supabase
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._recovery_agent = recovery_agent
        self._message_sender = message_sender
        self._allowed_jid = allowed_jid
        self._chatwoot_account_id = chatwoot_account_id
        self._chatwoot_inbox_id = chatwoot_inbox_id
        self._policy_key = policy_key
        self._policy_version = policy_version
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background poll loop."""
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self, *, timeout: float = 10.0) -> None:
        """Signal the loop to stop and wait for it to finish."""
        self._stopped.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._process_batch()
            except SupabaseError:
                # Supabase is unavailable — wait and retry.
                pass
            except Exception:
                # Unexpected error — log without PII and keep running.
                logger.exception("resolution_worker_unexpected_error")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self._poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def _process_batch(self) -> None:
        events = await self._supabase.fetch_pending_events(
            limit=self._batch_size
        )
        for event in events:
            if self._stopped.is_set():
                break
            await self._process_one(event)

    async def _process_one(self, event: dict[str, Any]) -> None:
        event_id = event.get("id")
        if not isinstance(event_id, str):
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            await self._supabase.update_event_status(
                event_id=event_id, status="failed", error="invalid_payload"
            )
            return
        try:
            report = await resolve_event(
                webhook_event_id=event_id,
                payload=payload,
                supabase=self._supabase,
                policy_key=self._policy_key,
                policy_version=self._policy_version,
                allowed_jid=self._allowed_jid,
                chatwoot_account_id=self._chatwoot_account_id,
                chatwoot_inbox_id=self._chatwoot_inbox_id,
            )
        except ResolutionError:
            # resolve_event already marked the event as 'failed'.
            return
        except SupabaseError:
            # Supabase failed mid-resolution — mark as failed for retry.
            try:
                await self._supabase.update_event_status(
                    event_id=event_id,
                    status="failed",
                    error="supabase_mid_resolution_error",
                )
            except SupabaseError:
                pass
            return

        # Durable planning ends this ingestion stage. A separate dispatcher
        # will claim the due action, re-evaluate, and only then invoke Hermes.
        if self._policy_key is not None and self._policy_version is not None:
            logger.info(
                "cart_recovery_planned event_id=%s",
                report.event_id,
            )
            return

        # Legacy immediate path retained only when no durable policy is set.
        if self._recovery_agent is None:
            logger.warning(
                "recovery_agent_not_configured event_id=%s",
                report.event_id,
            )
            return

        if self._recovery_agent is not None:
            situation_report = report.to_dict()
            try:
                proposal = await self._recovery_agent.request_proposal(
                    event_id=report.event_id,
                    situation_report=situation_report,
                )
            except Exception:
                # Agent call failed — the event is already 'processed'.
                # The proposal can be retried later.
                logger.exception(
                    "recovery_agent_request_failed event_id=%s",
                    report.event_id,
                )
                return

            if proposal is None:
                logger.warning(
                    "recovery_proposal_unavailable event_id=%s",
                    report.event_id,
                )
                return

            logger.info(
                "recovery_proposal_received event_id=%s action=%s",
                report.event_id,
                proposal.get("action"),
            )

            action = proposal.get("action")
            if action != "send_first_touch":
                logger.info(
                    "recovery_proposal_no_send event_id=%s action=%s",
                    report.event_id,
                    action,
                )
                return

            required_decision = required_recovery_decision(situation_report)
            if (
                required_decision != {
                    "action": "send_first_touch",
                    "reason_code": "first_touch",
                }
                or proposal.get("reason_code") != "first_touch"
            ):
                logger.error(
                    "first_touch_decision_guard_blocked "
                    "event_id=%s required_action=%s",
                    report.event_id,
                    required_decision["action"],
                )
                return

            if self._message_sender is None:
                logger.warning(
                    "first_touch_sender_not_configured event_id=%s",
                    report.event_id,
                )
                return

            if not report.phone_available or report.buyer_phone is None:
                logger.warning(
                    "first_touch_phone_unavailable event_id=%s",
                    report.event_id,
                )
                return

            if not is_allowed_whatsapp_target(
                report.buyer_phone,
                self._allowed_jid,
            ):
                logger.error(
                    "first_touch_target_not_allowed event_id=%s",
                    report.event_id,
                )
                return

            message = proposal.get("message")
            if not isinstance(message, str):
                logger.error(
                    "send_first_touch_without_message event_id=%s",
                    report.event_id,
                )
                return
            try:
                send_result = await self._message_sender.send_first_touch(
                    phone=report.buyer_phone,
                    buyer_name=report.buyer_name,
                    buyer_email=report.buyer_email,
                    content=message,
                    delivery_id=report.event_id,
                )
            except Exception:
                logger.exception(
                    "first_touch_send_failed event_id=%s",
                    report.event_id,
                )
                return
            if send_result.status == "sent":
                logger.info(
                    "first_touch_sent event_id=%s conversation=%s",
                    report.event_id,
                    send_result.conversation_id,
                )
            else:
                logger.warning(
                    "first_touch_%s event_id=%s reason=%s",
                    send_result.status,
                    report.event_id,
                    send_result.reason,
                )
