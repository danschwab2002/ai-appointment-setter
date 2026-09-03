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
import multiprocessing
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from bridge.chatwoot import (
    ChatwootAssignmentConflictError,
    ChatwootClient,
    ChatwootHandoffConflictError,
    ChatwootHandoffDeliveryUnknownError,
    ChatwootProtocolError,
)
from bridge.hotmart import (
    EVENT_CART_ABANDONMENT,
    EVENT_PURCHASE_APPROVED,
    parse_hotmart_purchase_payload,
)
from bridge.messaging import (
    FinalMetaEffect,
    FinalMetaEffectGate,
    FirstTouchResult,
    MessageSender,
    WhatsAppTemplateConfig,
    is_allowed_whatsapp_target,
)
from bridge.recovery_agent import (
    FollowupHandoffSuggestion,
    FollowupMessageProposal,
    RecoveryAgentClient,
    is_valid_followup_message_proposal,
    required_recovery_decision,
)
from bridge.resolution import ResolutionError, resolve_event
from bridge.supabase import (
    DeliveryAttempt,
    FollowupExecutionContext,
    PilotBoundaryConfig,
    ReevaluationDecision,
    ScheduledAction,
    SupabaseClient,
    SupabaseCommittedResponseError,
    SupabaseError,
    SupabasePermanentError,
)

logger = logging.getLogger(__name__)


def _precheckout_sender_process_entry(
    sender: MessageSender,
    kwargs: dict[str, Any],
    connection: Any,
) -> None:
    """Run the irreversible sender in a process the supervisor can terminate."""
    try:
        result = asyncio.run(sender.send_first_touch(**kwargs))
        connection.send(("ok", result))
    except BaseException as exc:
        connection.send(("error", type(exc).__name__))
    finally:
        connection.close()


def _terminate_process(process: Any) -> None:
    if not process.is_alive():
        process.join(timeout=0)
        return
    process.terminate()
    process.join(timeout=0.5)
    if process.is_alive():
        process.kill()
        process.join(timeout=0.5)


async def _send_precheckout_in_terminable_process(
    sender: MessageSender,
    kwargs: dict[str, Any],
) -> FirstTouchResult:
    """Return a sender result while making cancellation a hard process boundary."""
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_precheckout_sender_process_entry,
        args=(sender, kwargs, sending),
        daemon=True,
    )
    process.start()
    sending.close()
    try:
        while True:
            if receiving.poll():
                status, result = receiving.recv()
                process.join(timeout=0.5)
                if status == "ok":
                    return result
                logger.warning(
                    "precheckout_sender_process_failed error_type=%s", result
                )
                raise RuntimeError("precheckout_sender_process_failed")
            if not process.is_alive():
                process.join(timeout=0)
                if receiving.poll():
                    status, result = receiving.recv()
                    if status == "ok":
                        return result
                raise RuntimeError("precheckout_sender_process_exited")
            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        _terminate_process(process)
        raise
    finally:
        _terminate_process(process)
        receiving.close()


def _consume_detached_task_result(task: asyncio.Task[Any]) -> None:
    """Consume a detached task outcome without delaying a shutdown deadline."""
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


async def _await_with_hard_timeout(coro: Any, *, timeout: float) -> Any:
    """Bound an await without waiting for cancellation acknowledgement."""
    task = asyncio.create_task(coro)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_detached_task_result)
        raise TimeoutError
    return task.result()


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
    proposal: FollowupMessageProposal | FollowupHandoffSuggestion,
) -> None:
    """Revalidate agent output at the dispatcher trust boundary."""
    if isinstance(proposal, FollowupHandoffSuggestion):
        if is_valid_followup_message_proposal({
            "proposal": "suggest_handoff",
            "reason_code": proposal.reason_code,
        }):
            return
        raise SupabaseError("invalid_followup_message_proposal")
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
    expected_mode: str,
) -> None:
    """Accept only the exact reservation authorized for this outbound slice."""
    if (
        attempt.action_id != action.action_id
        or attempt.idempotency_key != action.idempotency_key
        or attempt.channel != "whatsapp"
        or attempt.mode != expected_mode
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
    expected_mode: str,
) -> None:
    """Require the request-start transition to preserve the complete fence."""
    if (
        started.attempt_id != reserved.attempt_id
        or started.action_id != action.action_id
        or started.idempotency_key != action.idempotency_key
        or started.attempt_number != reserved.attempt_number
        or started.channel != "whatsapp"
        or started.mode != expected_mode
        or started.phase != "request_started"
        or started.lease_generation != action.lease_generation
        or started.expected_case_version != decision.case_version
        or started.expected_sequence_revision != decision.sequence_revision
    ):
        raise SupabaseError("started_delivery_attempt_mismatch")


class OptOutProjectionWorker:
    """Durably project authoritative opt-outs into Chatwoot."""

    def __init__(
        self,
        *,
        supabase: SupabaseClient,
        chatwoot: ChatwootClient,
        worker_id: str,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 10,
        lease_duration: str = "1 minute",
        max_attempts: int = 5,
    ) -> None:
        self._supabase = supabase
        self._chatwoot = chatwoot
        self._worker_id = worker_id
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self, *, timeout: float = 10.0) -> None:
        self._stopped.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.run_once()
            except SupabaseError:
                logger.exception("chatwoot_opt_out_projection_supabase_error")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self._poll_interval
                )
            except TimeoutError:
                pass

    async def run_once(self) -> int:
        now = datetime.now(UTC).isoformat()
        claims = await self._supabase.claim_chatwoot_opt_out_projections(
            worker_id=self._worker_id,
            now=now,
            lease_duration=self._lease_duration,
            batch_size=self._batch_size,
        )
        for claim in claims:
            applied = False
            error_code: str | None = None
            try:
                await self._chatwoot.apply_opt_out_macro(
                    conversation_id=claim.chatwoot_conversation_id,
                    expected_account_id=claim.chatwoot_account_id,
                    expected_inbox_id=claim.chatwoot_inbox_id,
                    expected_jid=f"{claim.external_user_id}@s.whatsapp.net",
                )
                applied = True
            except (httpx.HTTPError, ChatwootProtocolError) as exc:
                error_code = f"chatwoot_{type(exc).__name__}"
            status = await self._supabase.finalize_chatwoot_opt_out_projection(
                opt_out_event_id=claim.opt_out_event_id,
                worker_id=self._worker_id,
                lease_generation=claim.lease_generation,
                applied=applied,
                error_code=error_code,
                max_attempts=self._max_attempts,
                now=datetime.now(UTC).isoformat(),
            )
            logger.info(
                "chatwoot_opt_out_projection_finalized event_id=%s status=%s",
                claim.opt_out_event_id,
                status,
            )
        return len(claims)


class HumanHandoffProjectionWorker:
    """Reconcile durable handoff effects into Chatwoot without external replies."""

    def __init__(
        self,
        *,
        supabase: SupabaseClient,
        chatwoot: ChatwootClient,
        worker_id: str,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 10,
        lease_seconds: int = 60,
        max_attempts: int = 8,
        finalization_timeout_seconds: float = 10.0,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._supabase = supabase
        self._chatwoot = chatwoot
        self._worker_id = worker_id
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._finalization_timeout = finalization_timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC).isoformat())
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self, *, timeout: float = 10.0) -> None:
        self._stopped.set()
        if self._task is None:
            return
        done, _ = await asyncio.wait({self._task}, timeout=timeout)
        if self._task not in done:
            self._task.cancel()
            done, _ = await asyncio.wait({self._task}, timeout=timeout)
            if self._task not in done:
                self._task.add_done_callback(_consume_detached_task_result)
                logger.warning(
                    "human_handoff_projection_stop_deadline_exceeded; "
                    "durable lease will expire for reconciliation"
                )
            elif self._task.cancelled():
                pass
            else:
                self._task.result()
        elif self._task.cancelled():
            pass
        else:
            self._task.result()
        self._task = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.run_once()
            except SupabaseError:
                logger.exception("human_handoff_projection_supabase_error")
            except Exception:
                logger.exception("human_handoff_projection_unexpected_error")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self._poll_interval
                )
            except TimeoutError:
                pass

    @staticmethod
    def _retry_at(now: str, *, attempt_count: int) -> str:
        retry_minutes = min(max(attempt_count, 1), 10)
        return (
            datetime.fromisoformat(now) + timedelta(minutes=retry_minutes)
        ).isoformat()

    async def run_once(self) -> int:
        claimed_at = self._clock()
        claims = await self._supabase.claim_human_handoff_projection_effects(
            worker_id=self._worker_id,
            now=claimed_at,
            lease_seconds=self._lease_seconds,
            batch_size=self._batch_size,
        )
        for claim in claims:
            outcome = "applied"
            error_code: str | None = None
            retry_at: str | None = None
            expected_jid = f"{claim.external_user_id}@s.whatsapp.net"
            try:
                if (
                    not claim.external_user_id.isdigit()
                    or not 7 <= len(claim.external_user_id) <= 15
                    or claim.external_user_id.startswith("0")
                ):
                    outcome = "dead_letter"
                    error_code = "invalid_external_user_id"
                elif claim.chatwoot_account_id != self._chatwoot.account_id:
                    outcome = "dead_letter"
                    error_code = "chatwoot_account_mismatch"
                elif claim.effect_kind == "assignment":
                    await self._chatwoot.ensure_handoff_assignment(
                        conversation_id=claim.chatwoot_conversation_id,
                        expected_inbox_id=claim.chatwoot_inbox_id,
                        expected_team_id=claim.expected_team_id,
                        expected_jid=expected_jid,
                    )
                elif claim.effect_kind == "private_note":
                    applied = await self._chatwoot.ensure_private_handoff_note(
                        conversation_id=claim.chatwoot_conversation_id,
                        expected_inbox_id=claim.chatwoot_inbox_id,
                        expected_jid=expected_jid,
                        note_body=claim.private_note_body,
                        idempotency_marker=claim.idempotency_marker,
                        create_if_missing=(
                            claim.current_effect_status != "delivery_unknown"
                        ),
                    )
                    if not applied:
                        if claim.attempt_count >= self._max_attempts:
                            outcome = "dead_letter"
                            error_code = "private_note_delivery_unknown_unresolved"
                        else:
                            outcome = "delivery_unknown"
                            error_code = "private_note_not_yet_visible"
                            retry_at = self._retry_at(
                                claimed_at, attempt_count=claim.attempt_count
                            )
                else:
                    outcome = "dead_letter"
                    error_code = "unsupported_handoff_effect"
            except ChatwootAssignmentConflictError:
                outcome = "conflict"
                error_code = "unexpected_chatwoot_team"
            except ChatwootHandoffConflictError as exc:
                outcome = "conflict"
                error_code = str(exc)
            except ChatwootHandoffDeliveryUnknownError:
                if claim.attempt_count >= self._max_attempts:
                    outcome = "dead_letter"
                    error_code = "private_note_delivery_unknown_unresolved"
                else:
                    outcome = "delivery_unknown"
                    error_code = "private_note_delivery_unknown"
                    retry_at = self._retry_at(
                        claimed_at, attempt_count=claim.attempt_count
                    )
            except (httpx.HTTPError, ChatwootProtocolError, ValueError) as exc:
                error_code = f"chatwoot_{type(exc).__name__}"
                if claim.attempt_count >= self._max_attempts:
                    outcome = "dead_letter"
                elif claim.current_effect_status == "delivery_unknown":
                    outcome = "delivery_unknown"
                    retry_at = self._retry_at(
                        claimed_at, attempt_count=claim.attempt_count
                    )
                else:
                    outcome = "retryable_failed"
                    retry_at = self._retry_at(
                        claimed_at, attempt_count=claim.attempt_count
                    )

            finalized = await _commit_outcome_despite_cancellation(
                _await_with_hard_timeout(
                    self._supabase.finalize_human_handoff_projection_effect(
                        effect_id=claim.effect_id,
                        worker_id=self._worker_id,
                        lease_generation=claim.lease_generation,
                        outcome=outcome,
                        error_code=error_code,
                        retry_at=retry_at,
                        now=self._clock(),
                    ),
                    timeout=self._finalization_timeout,
                )
            )
            logger.info(
                "human_handoff_projection_finalized effect_id=%s "
                "effect_kind=%s effect_status=%s handoff_status=%s",
                claim.effect_id,
                claim.effect_kind,
                finalized.effect_status,
                finalized.handoff_status,
            )
        return len(claims)


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
        pilot_boundary: PilotBoundaryConfig | None = None,
        human_handoff_admission_enabled: bool = False,
        handoff_projection_policy_key: str | None = None,
        handoff_projection_policy_version: int | None = None,
        final_meta_effect_gate: FinalMetaEffectGate | None = None,
        waba_template: WhatsAppTemplateConfig | None = None,
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
        self._pilot_boundary = pilot_boundary
        self._human_handoff_admission_enabled = human_handoff_admission_enabled
        self._handoff_projection_policy_key = handoff_projection_policy_key
        self._handoff_projection_policy_version = handoff_projection_policy_version
        self._final_meta_effect_gate = final_meta_effect_gate
        self._waba_template = waba_template
        self._delivery_mode = (
            "approved_template"
            if pilot_boundary is not None
            and pilot_boundary.channel_provider == "waba"
            else "freeform"
        )
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

    async def _finalize_pre_request_failure(
        self,
        *,
        action: ScheduledAction,
        attempt: DeliveryAttempt,
        reason_code: str,
    ) -> None:
        """Close a reserved attempt when no external request could have started."""
        failed_at = self._clock()
        retry_at = (
            datetime.fromisoformat(failed_at) + timedelta(minutes=1)
        ).isoformat()
        finalized = await _commit_outcome_despite_cancellation(
            self._supabase.finalize_followup_delivery_attempt(
                action_id=action.action_id,
                attempt_id=attempt.attempt_id,
                worker_id=self._worker_id,
                lease_generation=action.lease_generation,
                outcome="failed_before_request",
                remote_message_id=None,
                accepted_message_id=None,
                reason_code=reason_code,
                next_attempt_at=retry_at,
                reconciliation_deadline=None,
                now=failed_at,
            )
        )
        if finalized.status not in {
            "retryable_failed",
            "permanent_failed",
            "expired",
        }:
            raise SupabaseError("followup_pre_request_finalization_mismatch")

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
                    mode=self._delivery_mode,
                    now=now,
                )
                _validate_reserved_delivery_attempt(
                    action,
                    decision,
                    attempt,
                    self._delivery_mode,
                )
                logger.info(
                    "durable_delivery_attempt_reserved action_id=%s attempt_id=%s",
                    action.action_id,
                    attempt.attempt_id,
                )
                if self._recovery_agent is not None:
                    try:
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
                        proposal = (
                            await self._recovery_agent.request_followup_message(
                                attempt_id=attempt.attempt_id,
                                execution_context=execution_context,
                            )
                        )
                        if proposal is not None:
                            _validate_followup_message_proposal(proposal)
                    except asyncio.CancelledError:
                        await self._finalize_pre_request_failure(
                            action=action,
                            attempt=attempt,
                            reason_code="pre_request_cancelled",
                        )
                        raise
                    except Exception:
                        await self._finalize_pre_request_failure(
                            action=action,
                            attempt=attempt,
                            reason_code="pre_request_failed",
                        )
                        raise
                    if proposal is None:
                        logger.warning(
                            "durable_followup_proposal_unavailable "
                            "action_id=%s attempt_id=%s",
                            action.action_id,
                            attempt.attempt_id,
                        )
                        await self._finalize_pre_request_failure(
                            action=action,
                            attempt=attempt,
                            reason_code="agent_proposal_unavailable",
                        )
                    elif isinstance(proposal, FollowupHandoffSuggestion):
                        if (
                            not self._human_handoff_admission_enabled
                            or self._handoff_projection_policy_key is None
                            or self._handoff_projection_policy_version is None
                        ):
                            await self._finalize_pre_request_failure(
                                action=action,
                                attempt=attempt,
                                reason_code="handoff_admission_disabled",
                            )
                        else:
                            requested = await self._supabase.request_human_handoff(
                                recovery_case_id=action.recovery_case_id,
                                command_key=f"handoff:{attempt.attempt_id}",
                                reason_code=proposal.reason_code,
                                requested_by="agent",
                                projection_policy_key=(
                                    self._handoff_projection_policy_key
                                ),
                                projection_policy_version=(
                                    self._handoff_projection_policy_version
                                ),
                                source_action_id=action.action_id,
                                source_attempt_id=attempt.attempt_id,
                                worker_id=self._worker_id,
                                lease_generation=action.lease_generation,
                                now=self._clock(),
                            )
                            if requested.outcome not in {
                                "requested",
                                "already_requested",
                                "evidence_appended",
                            }:
                                raise SupabaseError(
                                    "human_handoff_request_outcome_mismatch"
                                )
                            logger.info(
                                "handoff_requested reason=%s",
                                proposal.reason_code,
                            )
                    elif isinstance(proposal, FollowupMessageProposal):
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
                                await self._finalize_pre_request_failure(
                                    action=action,
                                    attempt=attempt,
                                    reason_code="pre_request_failed",
                                )
                                raise SupabaseError(
                                    "followup_recipient_not_allowlisted"
                                )
                            assert execution_context.buyer_phone is not None
                            final_now = self._clock()
                            try:
                                final_evidence = (
                                    await self._load_chatwoot_evidence(
                                        action=action,
                                        now=final_now,
                                    )
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
                            except asyncio.CancelledError:
                                await self._finalize_pre_request_failure(
                                    action=action,
                                    attempt=attempt,
                                    reason_code="pre_request_cancelled",
                                )
                                raise
                            except Exception:
                                await self._finalize_pre_request_failure(
                                    action=action,
                                    attempt=attempt,
                                    reason_code="pre_request_failed",
                                )
                                raise
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
                            followup_conversation_id: int | None = None
                            if action.action_type == "no_reply_review":
                                if not isinstance(final_evidence, dict):
                                    raise SupabaseError(
                                        "followup_conversation_not_available"
                                    )
                                raw_conversation_id = final_evidence.get(
                                    "p_chatwoot_conversation_id"
                                )
                                if (
                                    not isinstance(raw_conversation_id, str)
                                    or not raw_conversation_id.isdigit()
                                    or int(raw_conversation_id) <= 0
                                ):
                                    raise SupabaseError(
                                        "followup_conversation_not_available"
                                    )
                                followup_conversation_id = int(raw_conversation_id)
                            if self._final_meta_effect_gate is not None:
                                action_kind = (
                                    "followup"
                                    if action.action_type == "no_reply_review"
                                    else "first_touch"
                                )
                                template_name = (
                                    self._waba_template.followup_name
                                    if action_kind == "followup"
                                    and self._waba_template is not None
                                    else (
                                        self._waba_template.first_touch_name
                                        if self._waba_template is not None
                                        else ""
                                    )
                                )
                                authorized = self._final_meta_effect_gate.authorize(
                                    FinalMetaEffect(
                                        delivery_id=attempt.attempt_id,
                                        action_kind=action_kind,
                                        mode=attempt.mode,
                                        target_phone=(
                                            "+"
                                            + execution_context.buyer_phone.lstrip("+")
                                        ),
                                        content=proposal.message,
                                        template_name=template_name or "",
                                        template_language=(
                                            self._waba_template.language
                                            if self._waba_template is not None
                                            else ""
                                        ),
                                    )
                                )
                                if not authorized:
                                    await self._finalize_pre_request_failure(
                                        action=action,
                                        attempt=attempt,
                                        reason_code="final_meta_gate_closed",
                                    )
                                    decisions.append(decision)
                                    continue
                            try:
                                started, request_start_cancelled = (
                                    await _await_with_cancellation_state(
                                        self._supabase.mark_followup_request_started(
                                            action_id=action.action_id,
                                            attempt_id=attempt.attempt_id,
                                            worker_id=self._worker_id,
                                            lease_generation=action.lease_generation,
                                            now=final_now,
                                            pilot_boundary=self._pilot_boundary,
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
                                    self._delivery_mode,
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
                                if followup_conversation_id is not None:
                                    result = await self._sender.send_followup(
                                        conversation_id=followup_conversation_id,
                                        phone=execution_context.buyer_phone or "",
                                        content=proposal.message,
                                        delivery_id=attempt.attempt_id,
                                    )
                                else:
                                    result = await self._sender.send_first_touch(
                                        phone=execution_context.buyer_phone or "",
                                        buyer_name=execution_context.buyer_name,
                                        buyer_email=execution_context.buyer_email,
                                        product_name=execution_context.product_name,
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
                                and (
                                    followup_conversation_id is None
                                    or result.conversation_id
                                    == followup_conversation_id
                                )
                            )
                            sender_conversation_mismatch = (
                                isinstance(result, FirstTouchResult)
                                and result.status == "sent"
                                and followup_conversation_id is not None
                                and result.conversation_id
                                != followup_conversation_id
                            )
                            remote_message_id = (
                                str(result.message_id)
                                if isinstance(result, FirstTouchResult)
                                and isinstance(result.message_id, int)
                                and not isinstance(result.message_id, bool)
                                and result.message_id > 0
                                else None
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
                                        remote_message_id=remote_message_id,
                                        accepted_message_id=None,
                                        reason_code=(
                                            "sender_conversation_mismatch"
                                            if sender_conversation_mismatch
                                            else "sender_result_inconclusive"
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


class HotmartAbandonmentTimerWorker:
    """Reevaluate durable timers and optionally dispatch delayed precheckout."""

    def __init__(
        self,
        *,
        supabase: SupabaseClient,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 10,
        clock: Callable[[], str] | None = None,
        message_sender: MessageSender | None = None,
        precheckout_sender_factory: Callable[[str], MessageSender] | None = None,
        precheckout_first_touch_enabled: bool = False,
        precheckout_outbound_enabled: bool = False,
        isolate_precheckout_sender_process: bool = True,
    ) -> None:
        if (
            precheckout_first_touch_enabled
            and message_sender is None
            and precheckout_sender_factory is None
        ):
            raise ValueError("precheckout first touch requires a message sender")
        self._supabase = supabase
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._clock = clock or (lambda: datetime.now(UTC).isoformat())
        self._message_sender = message_sender
        self._precheckout_sender_factory = precheckout_sender_factory
        self._precheckout_first_touch_enabled = precheckout_first_touch_enabled
        self._precheckout_outbound_enabled = precheckout_outbound_enabled
        self._isolate_precheckout_sender_process = isolate_precheckout_sender_process
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self, *, timeout: float = 10.0) -> None:
        await _await_despite_cancellation(self._stop_bounded(timeout=timeout))

    async def _stop_bounded(self, *, timeout: float) -> None:
        self._stopped.set()
        task = self._task
        if task is None:
            return
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=max(timeout, 1.0))
        if not done:
            logger.warning("hotmart_abandonment_timer_shutdown_timed_out")
            return
        self._task = None
        if task.cancelled():
            return
        task.result()

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.run_once()
            except SupabaseError:
                logger.warning("hotmart_abandonment_timer_due_list_failed")
            except Exception:
                logger.exception("hotmart_abandonment_timer_worker_unexpected_error")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self._poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> int:
        now = self._clock()
        reevaluation_ids = (
            await self._supabase.list_due_hotmart_abandonment_reevaluations(
                now=now,
                batch_size=self._batch_size,
                include_precheckout=self._precheckout_first_touch_enabled,
            )
        )
        processed = 0
        for reevaluation_id in reevaluation_ids:
            if self._stopped.is_set():
                break
            try:
                result = await self._supabase.reevaluate_hotmart_abandonment_timer(
                    reevaluation_id=reevaluation_id,
                    now=now,
                )
            except SupabaseError:
                logger.warning(
                    "hotmart_abandonment_timer_reevaluation_failed "
                    "reevaluation_id=%s",
                    reevaluation_id,
                )
                continue
            processed += 1
            if (
                result.outcome == "command_reserved"
                and self._precheckout_first_touch_enabled
                and self._precheckout_outbound_enabled
            ):
                await self._dispatch_precheckout_first_touch(
                    reevaluation_id=result.reevaluation_id
                )
            logger.info(
                "hotmart_abandonment_timer_reevaluated "
                "reevaluation_id=%s outcome=%s",
                result.reevaluation_id,
                result.outcome,
            )
        return processed

    async def _dispatch_precheckout_first_touch(
        self,
        *,
        reevaluation_id: str,
    ) -> None:
        try:
            command = await self._supabase.get_precheckout_delayed_one_shot_command(
                reevaluation_id=reevaluation_id
            )
        except SupabaseError:
            logger.warning(
                "precheckout_delayed_command_projection_failed "
                "reevaluation_id=%s",
                reevaluation_id,
            )
            return

        expected_metadata = (
            "johanna_interes_precheckout_01",
            "es_EC",
            "MARKETING",
            "johanna-precheckout-delayed-first-touch-v1",
        )
        actual_metadata = (
            command.template_name,
            command.template_language,
            command.template_category,
            command.copy_version,
        )
        if command.command_status != "request_started":
            return
        if actual_metadata != expected_metadata:
            await self._finalize_precheckout_unknown(
                command_id=command.command_id,
                conversation_id=None,
                message_id=None,
                failure_code="template_metadata_mismatch",
            )
            return
        if not command.send_authorized:
            await self._finalize_precheckout_unknown(
                command_id=command.command_id,
                conversation_id=None,
                message_id=None,
                failure_code=(
                    command.authorization_reason or "sender_authority_changed"
                ),
            )
            return

        sender = self._message_sender
        try:
            if self._precheckout_sender_factory is not None:
                sender = self._precheckout_sender_factory(command.target_phone)
        except Exception:
            await self._finalize_precheckout_unknown(
                command_id=command.command_id,
                conversation_id=None,
                message_id=None,
                failure_code="sender_factory_failed",
            )
            return
        if sender is None:
            await self._finalize_precheckout_unknown(
                command_id=command.command_id,
                conversation_id=None,
                message_id=None,
                failure_code="sender_unavailable",
            )
            return

        assert command.buyer_name is not None
        assert command.buyer_email is not None
        assert command.product_name is not None
        buyer_name = command.buyer_name.strip()
        content = (
            f"Hola, {buyer_name}. Te escribe el equipo de la Psic. Johanna. "
            "Vimos que completaste el formulario de Libre de Ansiedad. "
            "¿Quieres que te ayudemos a continuar? Si no deseas recibir "
            "más mensajes, responde “No más mensajes”."
        )
        sender_kwargs: dict[str, Any] = {
            "phone": command.target_phone,
            "buyer_name": buyer_name,
            "buyer_email": command.buyer_email,
            "product_name": command.product_name,
            "content": content,
            "delivery_id": command.command_id,
        }
        try:
            if self._isolate_precheckout_sender_process:
                result = await _send_precheckout_in_terminable_process(
                    sender, sender_kwargs
                )
            else:
                result = await sender.send_first_touch(**sender_kwargs)
        except asyncio.CancelledError:
            await self._finalize_precheckout_after_cancellation(
                command_id=command.command_id,
                conversation_id=None,
                message_id=None,
                failure_code="sender_cancelled",
            )
            raise
        except Exception:
            await self._finalize_precheckout_unknown(
                command_id=command.command_id,
                conversation_id=None,
                message_id=None,
                failure_code="sender_exception",
            )
            return

        if not isinstance(result, FirstTouchResult):
            await self._finalize_precheckout_unknown(
                command_id=command.command_id,
                conversation_id=None,
                message_id=None,
                failure_code="sender_invalid_result",
            )
            return

        if (
            result.status == "sent"
            and result.conversation_id is not None
            and result.message_id is not None
        ):
            try:
                await self._supabase.finish_johanna_abandonment_one_shot(
                    command_id=command.command_id,
                    outcome="accepted_by_chatwoot",
                    chatwoot_conversation_id=result.conversation_id,
                    chatwoot_message_id=result.message_id,
                    failure_code=None,
                )
            except asyncio.CancelledError:
                await self._finalize_precheckout_after_cancellation(
                    command_id=command.command_id,
                    conversation_id=result.conversation_id,
                    message_id=result.message_id,
                    failure_code="acceptance_finalization_cancelled",
                )
                raise
            except SupabaseError:
                await self._finalize_precheckout_unknown(
                    command_id=command.command_id,
                    conversation_id=result.conversation_id,
                    message_id=result.message_id,
                    failure_code="acceptance_finalization_failed",
                )
            return

        stable_failure = (
            result.reason
            if result.reason
            in {
                "chatwoot_http_error",
                "chatwoot_protocol_error",
                "invalid_phone",
                "target_not_allowed",
                "template_parameters_missing",
            }
            else "sender_failed"
        )
        try:
            await self._finalize_precheckout_unknown(
                command_id=command.command_id,
                conversation_id=result.conversation_id,
                message_id=result.message_id,
                failure_code=stable_failure,
            )
        except asyncio.CancelledError:
            await self._finalize_precheckout_after_cancellation(
                command_id=command.command_id,
                conversation_id=result.conversation_id,
                message_id=result.message_id,
                failure_code=stable_failure,
            )
            raise

    async def _finalize_precheckout_after_cancellation(
        self,
        *,
        command_id: str,
        conversation_id: int | None,
        message_id: int | None,
        failure_code: str,
    ) -> None:
        finalization = asyncio.create_task(
            self._finalize_precheckout_unknown(
                command_id=command_id,
                conversation_id=conversation_id,
                message_id=message_id,
                failure_code=failure_code,
            )
        )
        done, _ = await _await_despite_cancellation(
            asyncio.wait({finalization}, timeout=5.0)
        )
        if done:
            finalization.result()
            return
        finalization.cancel()
        logger.warning(
            "precheckout_delayed_cancellation_finalization_timed_out "
            "command_id=%s",
            command_id,
        )

    async def _finalize_precheckout_unknown(
        self,
        *,
        command_id: str,
        conversation_id: int | None,
        message_id: int | None,
        failure_code: str,
    ) -> None:
        try:
            await self._supabase.finish_johanna_abandonment_one_shot(
                command_id=command_id,
                outcome="delivery_unknown",
                chatwoot_conversation_id=conversation_id,
                chatwoot_message_id=message_id,
                failure_code=failure_code,
            )
        except SupabaseError:
            logger.warning(
                "precheckout_delayed_command_finalization_unknown "
                "command_id=%s",
                command_id,
            )


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
        purchase_worker_enabled: bool = False,
        pilot_boundary: PilotBoundaryConfig | None = None,
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
        self._purchase_worker_enabled = purchase_worker_enabled
        self._pilot_boundary = pilot_boundary
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
            limit=self._batch_size,
            excluded_event_types=(
                ()
                if self._purchase_worker_enabled
                else (EVENT_PURCHASE_APPROVED,)
            ),
        )
        for event in events:
            if self._stopped.is_set():
                break
            try:
                await self._process_one(event)
            except SupabaseError:
                # Keep the event retryable, but do not let one bad or
                # unavailable RPC starve later events in the same batch.
                continue

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
        event_type = event.get("event_type")
        if event_type == EVENT_PURCHASE_APPROVED:
            purchase = parse_hotmart_purchase_payload(payload)
            if purchase is None:
                await self._supabase.update_event_status(
                    event_id=event_id,
                    status="failed",
                    error="invalid_purchase_payload",
                )
                return
            try:
                result = await self._supabase.apply_hotmart_purchase_approved(
                    webhook_event_id=event_id,
                    buyer_email=purchase.buyer_email,
                    buyer_phone=purchase.buyer_phone,
                    external_product_id=str(purchase.product_id),
                    offer_code=purchase.offer_code,
                    transaction=purchase.transaction,
                    approved_at=datetime.fromtimestamp(
                        purchase.approved_date_ms / 1000,
                        tz=timezone.utc,
                    ).isoformat(),
                )
            except SupabasePermanentError:
                await self._supabase.update_event_status(
                    event_id=event_id,
                    status="failed",
                    error="purchase_rpc_permanent_failure",
                )
                return
            logger.info(
                "hotmart_purchase_processed event_id=%s outcome=%s",
                purchase.event_id,
                result.outcome,
            )
            return
        if event_type != EVENT_CART_ABANDONMENT:
            await self._supabase.update_event_status(
                event_id=event_id,
                status="failed",
                error="unsupported_persisted_event_type",
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
                pilot_boundary=self._pilot_boundary,
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
                    product_name=report.product_name,
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
