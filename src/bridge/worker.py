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
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from bridge.cart_first_touch import (
    CART_ABANDONMENT_TRIGGER,
    CART_DISCOUNT_VARIABLES,
    CartDiscountConfig,
    CartDiscountDefer,
    CartDiscountOutcome,
    CartDiscountReady,
    CartDiscountStop,
    compose_cart_discount_first_touch,
)
from bridge.cart_followups import (
    CART_FOLLOWUP_STEPS,
    CART_LAST_TOUCH_STEP,
    CART_LAST_TOUCH_VARIABLES,
    CART_SECOND_TOUCH_VARIABLES,
    CartFollowupConfig,
    CartFollowupOutcome,
    CartFollowupReady,
    compose_cart_last_touch,
    compose_cart_second_touch,
)
from bridge.checkout_issuance import generate_issuance_ulid
from bridge.approved_templates import (
    REASON_MISMATCH as APPROVED_TEMPLATE_MISMATCH,
    REASON_PARAMETERS_MISSING as APPROVED_TEMPLATE_PARAMETERS_MISSING,
    REASON_UNAVAILABLE as APPROVED_TEMPLATE_UNAVAILABLE,
    ApprovedTemplateError,
    parse_approved_template,
)
from bridge.chatwoot import (
    ChatwootAssignmentConflictError,
    ChatwootClient,
    ChatwootHandoffConflictError,
    ChatwootHandoffDeliveryUnknownError,
    ChatwootProtocolError,
)
from bridge.commercial_ally import CommercialAllyConfig
from bridge.hotmart import (
    EVENT_CART_ABANDONMENT,
    EVENT_PURCHASE_APPROVED,
    EVENT_PURCHASE_BILLET_PRINTED,
    EVENT_PURCHASE_CANCELED,
    HotmartPurchaseData,
    parse_hotmart_purchase_payload,
)
from bridge.lead_first_name import (
    resolve_greeting_name,
    template_greeting_name_is_safe,
)
from bridge.messaging import (
    FIRST_TOUCH_RECIPIENT_MISMATCH,
    FIRST_TOUCH_TEMPLATE_NOT_CONFIGURED,
    PAYMENT_SLIP_TRIGGER,
    PRECHECKOUT_INTENT_TRIGGER,
    FinalMetaEffect,
    FinalMetaEffectGate,
    FirstTouchRecipient,
    FirstTouchResult,
    MessageSender,
    WhatsAppTemplateConfig,
    first_touch_recipient_matches,
    is_allowed_whatsapp_target,
)
from bridge.payment_failure_followups import (
    PAYMENT_FAILURE_FOLLOWUP_STEP,
    PAYMENT_FAILURE_FOLLOWUP_VARIABLES,
    PaymentFailureFollowupConfig,
    compose_payment_failure_followup,
)
from bridge.payment_failure_variant import (
    PAYMENT_FAILURE_TRIGGER,
    PaymentFailureFirstTouchReady,
    PaymentFailurePlanningConfig,
    PaymentFailureVariantsConfig,
    compose_payment_failure_first_touch,
)
from bridge.payment_slip import payment_slip_facts
from bridge.payment_slip_first_touch import (
    PaymentSlipDefer,
    PaymentSlipFirstTouchConfig,
    PaymentSlipPlanningConfig,
    PaymentSlipReady,
    compose_payment_slip_first_touch,
    payment_slip_ingestion_reason,
)
from bridge.flow_engine_runtime import FlowEngineRuntime
from bridge.native_flows import (
    NATIVE_ANCHOR,
    NATIVE_NEEDS_DIRECT_TEMPLATE,
    NATIVE_NEEDS_ENGINE,
    NATIVE_CART_TRIGGER,
    NATIVE_PURCHASE_RETRY, NATIVE_PURCHASE_STARTED, NATIVE_PURCHASE_TRIGGER,
    NATIVE_QUEUE_PAGE,
    NATIVE_STEP_CONTEXT_UNAVAILABLE,
    NATIVE_STEP_VARIABLES,
    admit_native_purchase,
    apply_native_tail, cart_product_ref, native_step_categories,
    purchase_product_ref,
)
from bridge.product_label import ProductLabeler
from bridge.purchase_agent_pause import PurchaseAgentPause
from bridge.followup_steps import FOLLOWING_STEP_TRIGGERS
from bridge.payment_slip_reminders import PAYMENT_SLIP_STEPS
from bridge.phones import equivalent_whatsapp_phones, whatsapp_phone_region
from bridge.product_routes import HotmartUnrouted, ProductRoutes
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
    FlowEngineRunTerminated,
    FlowEngineStepContext,
    FollowupExecutionContext,
    PilotBoundaryConfig,
    PilotRequestStartRejectedError,
    ReevaluationDecision,
    ScheduledAction,
    SupabaseClient,
    SupabaseCommittedResponseError,
    SupabaseError,
    SupabasePermanentError,
)

logger = logging.getLogger(__name__)


def _first_touch_template_name(
    template: WhatsAppTemplateConfig,
    *,
    anchor_type: str,
) -> str | None:
    """The approved template the anchor of this first contact sends.

    ``None`` only for the first contact after the form (``precheckout_intent``)
    without its own template: the dispatcher closes the attempt instead of
    sending the cart template.
    """
    return template.first_touch_name_for(trigger_kind=anchor_type)


APPROVED_TEMPLATE_DIRECT_UNSUPPORTED_ACTION = (
    "approved_template_direct_unsupported_action"
)
# flow-engine-v1, seccion 8: una accion del motor se reconoce por su politica
# (la admision la pone en flujo:<flow_id>), sin preguntarle a la base.
FLOW_ENGINE_POLICY_PREFIX = "flujo:"
# Una version cuyas variables o boton el bridge no sabe armar (decision D5 de
# la T2.4): cierra sin mandar y sin reintento.
FLOW_ENGINE_STEP_UNSUPPORTED = "flow_engine_step_unsupported"
NATIVE_QUEUE_REFRESH_SECONDS = 60.0
_FLOW_ENGINE_CART_BUTTON = {"link": "checkout", "cupon": "personal"}
# Lo que el bridge arma a mano para cada paso del carrito: las variables, en
# orden, y el boton (cart_first_touch.py y cart_followups.py).
FLOW_ENGINE_CART_STEPS: dict[str, tuple[tuple[str, ...], dict[str, str] | None]] = {
    "first_contact": (CART_DISCOUNT_VARIABLES, _FLOW_ENGINE_CART_BUTTON),
    "cart_second_touch": (CART_SECOND_TOUCH_VARIABLES, None),
    CART_LAST_TOUCH_STEP: (CART_LAST_TOUCH_VARIABLES, _FLOW_ENGINE_CART_BUTTON),
}


def _flow_engine_step_supported(
    action: ScheduledAction, step: FlowEngineStepContext
) -> bool:
    """Whether the bridge knows how to build the send of this engine step."""
    shape = FLOW_ENGINE_CART_STEPS.get(action.step_key)
    if shape is None or step.step_key != action.step_key:
        return False
    if _flow_trigger(action) != CART_ABANDONMENT_TRIGGER:
        return False
    variables, button = shape
    return step.template_variables == variables and step.template_button == button


def _flow_trigger(action: ScheduledAction) -> str:
    """The flow an action belongs to.

    A first contact is anchored to its trigger (``payment_slip``,
    ``precheckout_intent``, ...), so the anchor says it. A following step
    (``no_reply_review``) is anchored to the accepted message of the step
    before it: its flow comes from its step key
    (``followup_steps.FOLLOWING_STEP_TRIGGERS``). A step key nobody
    declared keeps the anchor, which is what every following step did before.
    """
    if action.action_type == "no_reply_review":
        trigger = FOLLOWING_STEP_TRIGGERS.get(action.step_key)
        if trigger is not None:
            return trigger
    return action.anchor_type


@dataclass(frozen=True)
class _ApprovedTemplateComposition:
    """The direct-mode proposal, or the reason the attempt closes without one.

    ``retryable`` is False when the reason cannot change on its own before the
    action expires: the action type, the variables the runtime declares, and
    the values of the case (an empty name, a value Meta refuses). Those close
    the action as ``permanent_failed`` on the first attempt instead of being
    claimed again and re-reading the catalog. A catalog problem (unavailable,
    paused, not matching) stays retryable: Chatwoot or Meta can fix it.
    """

    proposal: FollowupMessageProposal | None = None
    greeting_name: str | None = None
    failure_reason: str | None = None
    retryable: bool = True
    # A1 of the cart with the personal coupon (cart_first_touch): how long a
    # deferred attempt waits, and the template outside the manifest with its
    # checkout button and the issued link it carries.
    retry_after: timedelta | None = None
    template_name: str | None = None
    template_params: dict[str, object] | None = None
    issuance_id: str | None = None


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


def _is_authorized_followup_recipient(
    *,
    execution_context: FollowupExecutionContext,
    allowed_jid: str | None,
    commercial_ally_config: CommercialAllyConfig | None,
    portable_recipient_enabled: bool = False,
) -> bool:
    """Validate the recipient through the legacy or portable tenant boundary.

    The portable branch accepts any offer of the binding (the default one plus
    ``additional_offer_codes``), the same set that admission and the pilot
    boundary already accept. Comparing only the default offer let a cart from
    another landing of the instance be planned and then die at dispatch.
    """
    if is_allowed_whatsapp_target(execution_context.buyer_phone, allowed_jid):
        return True
    return (
        portable_recipient_enabled
        and commercial_ally_config is not None
        and execution_context.buyer_phone is not None
        and execution_context.product_name == commercial_ally_config.product_name
        and execution_context.offer_code
        in commercial_ally_config.accepted_offer_codes
    )


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


async def _equivalent_conversation_jid(
    chatwoot: ChatwootClient,
    *,
    conversation_id: int,
    expected_inbox_id: int,
    external_user_id: str,
) -> str:
    """The JID Chatwoot holds for a conversation, among the forms of one phone.

    A durable row carries the identity as the base stores it (``52…`` when the
    landing form created the contact), while the Chatwoot conversation belongs
    to the ``wa_id`` (``521…``). With the portable inbound equivalence both are
    the same person, so a projection that validated the stored form exactly
    would never reach Chatwoot. The stored form is tried first and the other
    one only on an identity mismatch. The caller's own Chatwoot call validates
    the returned JID again: a conversation of neither form still fails closed.
    A number with a single form costs no extra request.
    """
    forms = [
        external_user_id,
        *(
            form
            for form in equivalent_whatsapp_phones(external_user_id)
            if form != external_user_id
        ),
    ]
    for form in forms[:-1]:
        jid = f"{form}@s.whatsapp.net"
        try:
            await chatwoot.validate_conversation_authority(
                conversation_id=conversation_id,
                expected_inbox_id=expected_inbox_id,
                expected_jid=jid,
            )
        except ChatwootProtocolError as exc:
            if str(exc) != "conversation_identity_mismatch":
                raise
            continue
        return jid
    return f"{forms[-1]}@s.whatsapp.net"


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
        whatsapp_equivalence_enabled: bool = False,
        run_lock: asyncio.Lock | None = None,
    ) -> None:
        if type(whatsapp_equivalence_enabled) is not bool:
            raise ValueError("invalid whatsapp equivalence flag")
        self._supabase = supabase
        self._chatwoot = chatwoot
        self._worker_id = worker_id
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        # E-107: el candado que comparte con la confirmacion de la baja del
        # entrante. Mientras el entrante registra una baja y manda la
        # confirmacion, la macro (que pone automation_paused) espera.
        self._run_lock = run_lock
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts
        self._whatsapp_equivalence_enabled = whatsapp_equivalence_enabled
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
        if self._run_lock is None:
            return await self._run_once()
        async with self._run_lock:
            return await self._run_once()

    async def _run_once(self) -> int:
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
                expected_jid = f"{claim.external_user_id}@s.whatsapp.net"
                if self._whatsapp_equivalence_enabled:
                    expected_jid = await _equivalent_conversation_jid(
                        self._chatwoot,
                        conversation_id=claim.chatwoot_conversation_id,
                        expected_inbox_id=claim.chatwoot_inbox_id,
                        external_user_id=claim.external_user_id,
                    )
                await self._chatwoot.apply_opt_out_macro(
                    conversation_id=claim.chatwoot_conversation_id,
                    expected_account_id=claim.chatwoot_account_id,
                    expected_inbox_id=claim.chatwoot_inbox_id,
                    expected_jid=expected_jid,
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
        whatsapp_equivalence_enabled: bool = False,
    ) -> None:
        if type(whatsapp_equivalence_enabled) is not bool:
            raise ValueError("invalid whatsapp equivalence flag")
        self._supabase = supabase
        self._chatwoot = chatwoot
        self._worker_id = worker_id
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._whatsapp_equivalence_enabled = whatsapp_equivalence_enabled
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

    async def _conversation_jid(self, claim: Any, stored_jid: str) -> str:
        """The JID to validate in Chatwoot: the stored one, or its other form."""
        if not self._whatsapp_equivalence_enabled:
            return stored_jid
        return await _equivalent_conversation_jid(
            self._chatwoot,
            conversation_id=claim.chatwoot_conversation_id,
            expected_inbox_id=claim.chatwoot_inbox_id,
            external_user_id=claim.external_user_id,
        )

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
                        expected_jid=await self._conversation_jid(
                            claim, expected_jid
                        ),
                    )
                elif claim.effect_kind == "private_note":
                    applied = await self._chatwoot.ensure_private_handoff_note(
                        conversation_id=claim.chatwoot_conversation_id,
                        expected_inbox_id=claim.chatwoot_inbox_id,
                        expected_jid=await self._conversation_jid(
                            claim, expected_jid
                        ),
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
        commercial_ally_config: CommercialAllyConfig | None = None,
        portable_recipient_enabled: bool = False,
        clock: Callable[[], str] | None = None,
        pilot_boundary: PilotBoundaryConfig | None = None,
        chatwoot_inbox_id: int | None = None,
        human_handoff_admission_enabled: bool = False,
        handoff_projection_policy_key: str | None = None,
        handoff_projection_policy_version: int | None = None,
        final_meta_effect_gate: FinalMetaEffectGate | None = None,
        waba_template: WhatsAppTemplateConfig | None = None,
        approved_template_direct: bool = False,
        lead_first_name_greeting_enabled: bool = False,
        whatsapp_equivalence_enabled: bool = False,
        cart_discount: CartDiscountConfig | None = None,
        cart_followups: CartFollowupConfig | None = None,
        payment_slip: PaymentSlipFirstTouchConfig | None = None,
        payment_failure_variants: PaymentFailureVariantsConfig | None = None,
        payment_failure_followup: PaymentFailureFollowupConfig | None = None,
        flow_engine_enabled: bool = False,
        native_client_labeler: ProductLabeler | None = None,
    ) -> None:
        if payment_failure_followup is not None and (
            not approved_template_direct or payment_failure_variants is None
        ):
            # B2 and Z are approved templates sent by the direct mode, and
            # which one goes out depends on the variant B1 recorded.
            raise ValueError(
                "payment failure followup requires the approved template direct "
                "mode and the payment failure variants"
            )
        if payment_failure_variants is not None and not approved_template_direct:
            # B1 is an approved template with its URL button, sent by the
            # direct mode with the final Meta gate; never a Hermes draft.
            raise ValueError(
                "payment failure variants require the approved template direct mode"
            )
        if cart_discount is not None and not approved_template_direct:
            # A1 with the personal coupon is an approved template sent by the
            # direct mode, with the final Meta gate; never a Hermes draft.
            raise ValueError("cart discount requires the approved template direct mode")
        if cart_followups is not None and not approved_template_direct:
            # A2 and Z are approved templates sent by the direct mode.
            raise ValueError("cart followups require the approved template direct mode")
        if payment_slip is not None and (
            not approved_template_direct
            or waba_template is None
            or waba_template.payment_slip_name is None
        ):
            # C1 is an approved template with its URL button, sent by the
            # direct mode: never a Hermes draft, never without its template.
            raise ValueError(
                "payment slip requires the approved template direct mode and its template"
            )
        if type(whatsapp_equivalence_enabled) is not bool:
            raise ValueError("invalid whatsapp equivalence flag")
        if whatsapp_equivalence_enabled and commercial_ally_config is None:
            # The equivalent forms of a phone (52/521, 54/549) are compared
            # only for the portable binding. Without it the dispatcher keeps
            # sending to the exact phone of the contact.
            raise ValueError("whatsapp equivalence requires the portable binding")
        if approved_template_direct:
            # The direct mode sends the approved body of the catalog and never
            # asks Hermes for a draft (docs/contracts/
            # approved-template-direct-dispatch-v1.md). It only exists for the
            # portable WABA binding, where the final Meta gate is mandatory.
            if commercial_ally_config is None:
                raise ValueError(
                    "approved template direct dispatch requires the portable binding"
                )
            if waba_template is None:
                raise ValueError(
                    "approved template direct dispatch requires the WABA templates"
                )
            if chatwoot is None or chatwoot_inbox_id is None:
                raise ValueError(
                    "approved template direct dispatch requires the Chatwoot inbox"
                )
            if sender is None:
                raise ValueError("approved template direct dispatch requires a sender")
            if recovery_agent is not None:
                raise ValueError("approved template direct dispatch does not call Hermes")
            if human_handoff_admission_enabled:
                raise ValueError(
                    "approved template direct dispatch has no handoff suggestion"
                )
        elif lead_first_name_greeting_enabled:
            raise ValueError(
                "dispatcher first-name greeting requires approved template direct "
                "dispatch"
            )
        if commercial_ally_config is not None:
            if not portable_recipient_enabled:
                raise ValueError("portable recipient capability is not enabled")
            if pilot_boundary is None:
                raise ValueError("portable recipient boundary is required")
            if pilot_boundary.tenant_key != commercial_ally_config.tenant_ref:
                raise ValueError("portable recipient tenant does not match manifest")
            if pilot_boundary.channel_provider != "waba":
                raise ValueError("portable recipient provider must be waba")
            if sender is not None and final_meta_effect_gate is None:
                raise ValueError(
                    "portable WABA outbound requires final Meta effect gate"
                )
            if chatwoot_account_id != commercial_ally_config.chatwoot_account_id:
                raise ValueError(
                    "portable recipient Chatwoot account does not match manifest"
                )
            if chatwoot_inbox_id != commercial_ally_config.chatwoot_inbox_id:
                raise ValueError(
                    "portable recipient Chatwoot inbox does not match manifest"
                )
            if (
                pilot_boundary.channel_account_ref
                != f"chatwoot-inbox:{commercial_ally_config.chatwoot_inbox_id}"
            ):
                raise ValueError(
                    "portable recipient channel account does not match manifest"
                )
        elif portable_recipient_enabled:
            raise ValueError("portable recipient capability requires a manifest")
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
        self._commercial_ally_config = commercial_ally_config
        self._portable_recipient_enabled = portable_recipient_enabled
        self._pilot_boundary = pilot_boundary
        self._human_handoff_admission_enabled = human_handoff_admission_enabled
        self._handoff_projection_policy_key = handoff_projection_policy_key
        self._handoff_projection_policy_version = handoff_projection_policy_version
        self._final_meta_effect_gate = final_meta_effect_gate
        self._waba_template = waba_template
        self._chatwoot_inbox_id = chatwoot_inbox_id
        self._approved_template_direct = approved_template_direct
        self._cart_discount = cart_discount
        self._cart_followups = cart_followups
        # flow-engine-v1, seccion 8: con el flag, una accion con politica
        # flujo:<flow_id> sale con la plantilla de su version fijada y arranca
        # por mark_flow_engine_request_started_v1. Sin el flag, o con otra
        # politica, ninguna llamada nueva.
        self._flow_engine_enabled = flow_engine_enabled
        # flow-engine-v2 (E-065): la etiqueta cliente del paso etiquetar, por
        # un macro que solo la suma. Sin macro configurado, solo la nota.
        self._native_client_labeler = native_client_labeler
        self._payment_slip = payment_slip
        self._payment_failure_variants = payment_failure_variants
        self._payment_failure_followup = payment_failure_followup
        self._lead_first_name_greeting_enabled = lead_first_name_greeting_enabled
        self._whatsapp_equivalence_enabled = whatsapp_equivalence_enabled
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
        retryable: bool = True,
        retry_after: timedelta | None = None,
    ) -> None:
        """Close a reserved attempt when no external request could have started.

        A retryable failure asks for another attempt in a minute; the base
        grants it while the action has retries left (``max_execution_retries``)
        and has not expired, and otherwise closes it. A non retryable one
        passes no next attempt, so the base closes the action as
        ``permanent_failed`` with ``reason_code`` right away.
        """
        failed_at = self._clock()
        retry_at = (
            (
                datetime.fromisoformat(failed_at)
                + (retry_after if retry_after is not None else timedelta(minutes=1))
            ).isoformat()
            if retryable
            else None
        )
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

    async def _authorize_recovery_issuance(
        self,
        *,
        action: ScheduledAction,
        attempt: DeliveryAttempt,
        issuance_id: str,
        now: str,
    ) -> bool:
        """Move the A1 link to ``request_started`` once the request started.

        It runs after ``mark_followup_request_started`` on purpose: a start
        the base rejects leaves the attempt reserved for the next lease, and a
        link already authorized would then come back as a replay and close A1
        without sending it. A refusal here means a stop arrived in the last
        seconds (an opt-out, a purchase): nothing is sent, and the attempt,
        already started, closes as ``delivery_unknown`` for reconciliation.
        """
        try:
            authorization = await self._supabase.authorize_recovery_checkout_issuance(
                issuance_id=issuance_id,
                action_id=action.action_id,
                worker_id=self._worker_id,
                lease_generation=action.lease_generation,
                now=now,
            )
            outcome = authorization.outcome
        except SupabaseError:
            outcome = "authorize_failed"
        if outcome == "request_started":
            return True
        logger.warning(
            "cart_discount_issuance_not_authorized action_id=%s attempt_id=%s outcome=%s",
            action.action_id,
            attempt.attempt_id,
            outcome,
        )
        finalized = await _commit_outcome_despite_cancellation(
            self._supabase.finalize_followup_delivery_attempt(
                action_id=action.action_id,
                attempt_id=attempt.attempt_id,
                worker_id=self._worker_id,
                lease_generation=action.lease_generation,
                outcome="delivery_unknown",
                remote_message_id=None,
                accepted_message_id=None,
                reason_code=f"recovery_checkout_{outcome}",
                next_attempt_at=None,
                reconciliation_deadline=(
                    datetime.fromisoformat(now) + timedelta(minutes=15)
                ).isoformat(),
                now=now,
            )
        )
        if finalized.status != "delivery_unknown":
            raise SupabaseError("followup_unknown_finalization_mismatch")
        return False

    async def _finalize_recovery_issuance(
        self,
        *,
        issuance_id: str,
        result: object,
    ) -> None:
        """Close the A1 link with what the send did; never breaks the attempt.

        A message Chatwoot accepted closes it ``accepted_by_chatwoot`` with its
        id; anything else, ``delivery_unknown``. The link carries its own sck,
        so a purchase still finds the case either way.
        """
        sent = (
            isinstance(result, FirstTouchResult)
            and result.status == "sent"
            and isinstance(result.message_id, int)
            and not isinstance(result.message_id, bool)
            and result.message_id > 0
        )
        try:
            await self._supabase.finalize_chatwoot_checkout_issuance_v2(
                issuance_id=issuance_id,
                status="accepted_by_chatwoot" if sent else "delivery_unknown",
                chatwoot_message_id=result.message_id if sent else None,  # type: ignore[union-attr]
                failure_code=None if sent else "first_touch_not_confirmed",
                now=self._clock(),
            )
        except SupabaseError as exc:
            logger.warning(
                "cart_discount_issuance_finalize_failed issuance_id=%s error=%s",
                issuance_id,
                type(exc).__name__,
            )

    async def _case_conversation_jids(
        self,
        *,
        action: ScheduledAction,
        now: str,
        buyer_phone: str | None,
    ) -> list[str | None]:
        """The JIDs the case's own conversation may carry in Chatwoot.

        A portable instance sends to every buyer of its offers, so the
        conversation of a following step (A2, Z, B2, C3) belongs to the case's
        contact, not to the one JID a legacy runtime allowlists. Measured in
        ATT1 on 2026-10-21: with the allowlisted JID every following step
        failed ``conversation_identity_mismatch`` and closed with
        ``chatwoot_authority_unavailable`` (17 of 17 A2). Chatwoot keeps the
        contact as the form the first touch went to (``521`` + 10 for Mexico,
        D13), so both equivalent forms are tried, the canonical one first.
        Without a phone the legacy check runs unchanged and fails closed.
        """
        if not self._portable_recipient_enabled:
            return [None]
        if buyer_phone is None:
            execution_context = await self._supabase.get_followup_execution_context(
                action_id=action.action_id,
                worker_id=self._worker_id,
                lease_generation=action.lease_generation,
                now=now,
            )
            _validate_followup_execution_context(action, execution_context)
            buyer_phone = execution_context.buyer_phone
        if self._whatsapp_equivalence_enabled:
            forms = equivalent_whatsapp_phones(buyer_phone)
        else:
            digits = "".join(ch for ch in buyer_phone or "" if ch.isdigit())
            forms = (digits,) if digits else ()
        if not forms:
            return [None]
        return [f"{form}@s.whatsapp.net" for form in forms]

    async def _load_chatwoot_evidence(
        self,
        *,
        action: ScheduledAction,
        now: str,
        buyer_phone: str | None = None,
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
                candidates = await self._case_conversation_jids(
                    action=action, now=now, buyer_phone=buyer_phone
                )
                for index, expected_jid in enumerate(candidates):
                    # A legacy runtime keeps the call it always made.
                    identity = (
                        {} if expected_jid is None else {"expected_jid": expected_jid}
                    )
                    try:
                        snapshot = (
                            await self._chatwoot.get_canonical_conversation_snapshot(
                                conversation_id=context.external_conversation_id,
                                expected_inbox_id=context.expected_inbox_id,
                                anchor_message_id=context.anchor_external_message_id,
                                anchor_observed_at_epoch=int(observed_at.timestamp()),
                                **identity,
                            )
                        )
                    except ChatwootProtocolError as exc:
                        if (
                            str(exc) == "conversation_identity_mismatch"
                            and index + 1 < len(candidates)
                        ):
                            continue
                        raise
                    break
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

    async def _flow_engine_step(
        self, action: ScheduledAction
    ) -> FlowEngineStepContext | None:
        """The send of an engine step, or None to go the way of today (D3).

        Only with the flag and a policy flujo:<flow_id>: any other action makes
        no new call. A failed call also goes the way of today: with the first
        version the template of the manifest is the right one, and with
        another the base refuses with flow_engine_needs_engine_image.
        """
        if not self._flow_engine_enabled or not action.policy_key.startswith(
            FLOW_ENGINE_POLICY_PREFIX
        ):
            return None
        try:
            return await self._supabase.get_flow_engine_step_context(action.action_id)
        except SupabaseError as exc:
            logger.warning(
                "flow_engine_step_context_unavailable action_id=%s error=%s",
                action.action_id,
                type(exc).__name__,
            )
            return None

    async def _compose_approved_template_proposal(
        self,
        *,
        action: ScheduledAction,
        execution_context: FollowupExecutionContext,
        attempt: DeliveryAttempt | None = None,
        step_template: FlowEngineStepContext | None = None,
    ) -> _ApprovedTemplateComposition:
        """Build the first contact from the approved template, without Hermes.

        The text is the approved body of the Chatwoot catalog filled with the
        same values the sender puts in ``processed_params`` (the greeting name
        when the flag is on), so the final Meta gate hashes what Meta will
        show. Every catalog or data problem returns a reason instead of
        raising: the caller closes the reserved attempt with it.

        ``step_template`` is the send of an engine step in its pinned version
        (flow-engine-v1, 7.3): its template and language replace those of the
        configuration; the price, the currency, the waits of the coupon and the
        category stay those of today.
        """
        trigger_kind = _flow_trigger(action)
        if action.anchor_type == NATIVE_ANCHOR:
            # flow-engine-v2: el paso de un flujo nativo sale solo con la
            # plantilla de su version fijada; nunca con la del manifiesto.
            return await self._compose_native_step_proposal(
                action=action,
                execution_context=execution_context,
                step_template=step_template,
            )
        cart_discount = self._cart_discount
        cart_followups = self._cart_followups
        if step_template is not None:
            if not _flow_engine_step_supported(action, step_template) or (
                cart_discount is None
                if action.step_key == "first_contact"
                else cart_followups is None
            ):
                logger.warning(
                    "flow_engine_step_unsupported action_id=%s step=%s version=%s",
                    action.action_id,
                    action.step_key,
                    step_template.version,
                )
                return _ApprovedTemplateComposition(
                    failure_reason=FLOW_ENGINE_STEP_UNSUPPORTED, retryable=False
                )
            if action.step_key == "first_contact":
                assert cart_discount is not None
                cart_discount = replace(
                    cart_discount,
                    template_name=step_template.template_name,
                    language=step_template.template_language,
                )
            else:
                assert cart_followups is not None
                cart_followups = replace(
                    cart_followups,
                    language=step_template.template_language,
                    **(
                        {"last_touch_template": step_template.template_name}
                        if action.step_key == CART_LAST_TOUCH_STEP
                        else {"second_touch_template": step_template.template_name}
                    ),
                )
        step_key: str | None = None
        if action.action_type != "first_contact_review":
            # The only following steps this mode sends are the reminders of a
            # payment slip (C2, C3) and, when the instance turns them on, A2
            # and Z of the cart and B2 or Z of a refused payment: every other
            # one closes as before.
            slip_step = (
                trigger_kind == PAYMENT_SLIP_TRIGGER
                and action.step_key in PAYMENT_SLIP_STEPS
            )
            cart_step = (
                cart_followups is not None
                and trigger_kind == CART_ABANDONMENT_TRIGGER
                and action.step_key in CART_FOLLOWUP_STEPS
            )
            refusal_step = (
                self._payment_failure_followup is not None
                and trigger_kind == PAYMENT_FAILURE_TRIGGER
                and action.step_key == PAYMENT_FAILURE_FOLLOWUP_STEP
            )
            if not (
                action.action_type == "no_reply_review"
                and (slip_step or cart_step or refusal_step)
            ):
                return _ApprovedTemplateComposition(
                    failure_reason=APPROVED_TEMPLATE_DIRECT_UNSUPPORTED_ACTION,
                    retryable=False,
                )
            step_key = action.step_key
        template = self._waba_template
        assert template is not None
        assert self._chatwoot is not None
        assert self._chatwoot_inbox_id is not None
        if step_key is not None and step_key in CART_FOLLOWUP_STEPS:
            assert cart_followups is not None
            template_name = cart_followups.template_for(step_key)
            declared = (
                CART_LAST_TOUCH_VARIABLES
                if step_key == CART_LAST_TOUCH_STEP
                else CART_SECOND_TOUCH_VARIABLES
            )
        elif step_key == PAYMENT_FAILURE_FOLLOWUP_STEP:
            # B2 or Z: the base picks which one when it is built; both take
            # the same variables.
            assert self._payment_failure_followup is not None
            template_name = self._payment_failure_followup.second_touch_template
            declared = PAYMENT_FAILURE_FOLLOWUP_VARIABLES
        elif step_key is not None:
            step_template = template.payment_slip_step(step_key)
            template_name = step_template.name if step_template is not None else None
            declared = (
                step_template.body_parameters if step_template is not None else None
            )
        else:
            template_name = _first_touch_template_name(
                template, anchor_type=trigger_kind
            )
            declared = template.declared_body_parameters(trigger_kind=trigger_kind)
        if template_name is None:
            return _ApprovedTemplateComposition(
                failure_reason=FIRST_TOUCH_TEMPLATE_NOT_CONFIGURED,
                retryable=False,
            )
        if declared is None:
            # Without the manifest declaring this template's variables the
            # bridge does not know what the approved body expects.
            logger.warning(
                "approved_template_parameters_undeclared action_id=%s trigger=%s",
                action.action_id,
                trigger_kind,
            )
            return _ApprovedTemplateComposition(
                failure_reason=APPROVED_TEMPLATE_MISMATCH,
                retryable=False,
            )
        buyer_name = execution_context.buyer_name
        product_name = execution_context.product_name
        if step_key is not None:
            # The values of the case the reminder declares: the store and the
            # due date are checked where they are built.
            case_values = {"nombre": buyer_name, "producto": product_name}
            values_missing = any(
                not (isinstance(case_values[item], str) and case_values[item].strip())
                for item in declared
                if item in case_values
            )
        else:
            values_missing = template.body_parameters_missing(
                trigger_kind=trigger_kind,
                buyer_name=buyer_name,
                product_name=product_name,
            )
        if values_missing:
            return _ApprovedTemplateComposition(
                failure_reason=APPROVED_TEMPLATE_PARAMETERS_MISSING,
                retryable=False,
            )
        greeting_name: str | None = None
        name_value = buyer_name
        name_source = "buyer_name"
        if (
            self._lead_first_name_greeting_enabled
            and isinstance(buyer_name, str)
            and buyer_name.strip()
        ):
            greeting = await resolve_greeting_name(buyer_name, store=self._supabase)
            # The sender strips the greeting before using it (messaging.
            # _template_name); stripping here keeps the rendered text and the
            # template variable identical.
            greeting_name = greeting.name.strip()
            name_value = greeting_name
            name_source = greeting.source
            logger.info(
                "durable_first_touch_greeting action_id=%s source=%s",
                action.action_id,
                greeting.source,
            )
        if "nombre" in declared and not template_greeting_name_is_safe(name_value):
            # The name is unverified input (the public form, Hotmart) and the
            # full name is the last resort of the greeting, or the value itself
            # without it: a URL, an email or a long text would go out inside
            # the approved template. It does not change on its own, so the
            # action closes on the first attempt, before the catalog and any
            # Chatwoot call. The log never carries the name.
            logger.warning(
                "approved_template_name_refused action_id=%s trigger=%s source=%s",
                action.action_id,
                trigger_kind,
                name_source,
            )
            return _ApprovedTemplateComposition(
                failure_reason=APPROVED_TEMPLATE_PARAMETERS_MISSING,
                retryable=False,
            )
        if step_key == PAYMENT_FAILURE_FOLLOWUP_STEP:
            return await self._compose_payment_failure_followup_proposal(
                action=action,
                attempt=attempt,
                name_value=name_value or "",
                product_name=product_name or "",
                buyer_phone=execution_context.buyer_phone,
                greeting_name=greeting_name,
            )
        if step_key is not None and step_key in CART_FOLLOWUP_STEPS:
            assert cart_followups is not None
            return await self._compose_cart_followup_proposal(
                config=cart_followups,
                action=action,
                attempt=attempt,
                step_key=step_key,
                name_value=name_value or "",
                product_name=product_name or "",
                buyer_phone=execution_context.buyer_phone,
                greeting_name=greeting_name,
            )
        if trigger_kind == PAYMENT_SLIP_TRIGGER:
            return await self._compose_payment_slip_proposal(
                action=action,
                declared=declared,
                name_value=name_value or "",
                product_name=product_name or "",
                greeting_name=greeting_name,
                step_key=step_key,
            )
        values = template.body_values(
            trigger_kind=trigger_kind,
            buyer_name=name_value,
            product_name=product_name,
        )
        assert values is not None
        try:
            inbox = await self._chatwoot.get_inbox(inbox_id=self._chatwoot_inbox_id)
        except (httpx.HTTPError, ChatwootProtocolError) as exc:
            logger.warning(
                "approved_template_catalog_unavailable action_id=%s error=%s",
                action.action_id,
                type(exc).__name__,
            )
            return _ApprovedTemplateComposition(
                failure_reason=APPROVED_TEMPLATE_UNAVAILABLE
            )
        discount: CartDiscountOutcome | None = None
        log_prefix = "cart_discount_first_touch"
        if (
            self._payment_failure_variants is not None
            and trigger_kind == PAYMENT_FAILURE_TRIGGER
            and attempt is not None
        ):
            # B1: the variant of the refusal, with its link and, when it has
            # one, its coupon. Without the send context it does not go out
            # with a guess: the attempt fails like any lookup of the base.
            log_prefix = "payment_failure_first_touch"
            try:
                discount = await compose_payment_failure_first_touch(
                    config=self._payment_failure_variants,
                    supabase=self._supabase,
                    inbox=inbox,
                    action=action,
                    attempt=attempt,
                    buyer_name=name_value or "",
                    product_name=product_name or "",
                    buyer_phone=execution_context.buyer_phone,
                    worker_id=self._worker_id,
                    now=self._clock(),
                    ulid_factory=generate_issuance_ulid,
                )
            except SupabasePermanentError as exc:
                logger.warning(
                    "payment_failure_first_touch_context_rejected action_id=%s reason=%s",
                    action.action_id,
                    exc.reason,
                )
                return _ApprovedTemplateComposition(
                    failure_reason=exc.reason or "payment_failure_send_context_rejected",
                    retryable=False,
                )
            except SupabaseError as exc:
                logger.warning(
                    "payment_failure_first_touch_context_unavailable action_id=%s error=%s",
                    action.action_id,
                    type(exc).__name__,
                )
                return _ApprovedTemplateComposition(
                    failure_reason="payment_failure_send_context_unavailable"
                )
        elif (
            cart_discount is not None
            and trigger_kind == CART_ABANDONMENT_TRIGGER
            and attempt is not None
        ):
            discount = await compose_cart_discount_first_touch(
                config=cart_discount,
                supabase=self._supabase,
                inbox=inbox,
                action=action,
                attempt=attempt,
                buyer_name=name_value or "",
                product_name=product_name or "",
                buyer_phone=execution_context.buyer_phone,
                worker_id=self._worker_id,
                now=self._clock(),
                ulid_factory=generate_issuance_ulid,
            )
        if discount is not None:
            assert attempt is not None
            if isinstance(discount, CartDiscountReady):
                if isinstance(discount, PaymentFailureFirstTouchReady):
                    await self._record_payment_failure_first_touch(
                        action=action, attempt=attempt, ready=discount
                    )
                logger.info(
                    "%s_ready action_id=%s attempt_id=%s",
                    log_prefix,
                    action.action_id,
                    attempt.attempt_id,
                )
                return _ApprovedTemplateComposition(
                    proposal=FollowupMessageProposal(
                        strategy=f"approved_template:{discount.template_name}",
                        message=discount.message,
                    ),
                    greeting_name=greeting_name,
                    template_name=discount.template_name,
                    template_params=discount.template_params,
                    issuance_id=discount.issuance_id,
                )
            if isinstance(discount, CartDiscountDefer):
                logger.info(
                    "%s_deferred action_id=%s attempt_number=%s retry_after_s=%s",
                    log_prefix,
                    action.action_id,
                    attempt.attempt_number,
                    int(discount.retry_after.total_seconds()),
                )
                return _ApprovedTemplateComposition(
                    failure_reason=discount.reason,
                    retry_after=discount.retry_after,
                )
            if isinstance(discount, CartDiscountStop):
                logger.warning(
                    "%s_stopped action_id=%s reason=%s",
                    log_prefix,
                    action.action_id,
                    discount.reason,
                )
                return _ApprovedTemplateComposition(
                    failure_reason=discount.reason,
                    retryable=discount.retryable,
                )
            # A1 and B1 are never lost because of the coupon or the link: the
            # template of today.
            logger.warning(
                "%s_fallback action_id=%s reason=%s",
                log_prefix,
                action.action_id,
                discount.reason,
            )
        try:
            approved = parse_approved_template(
                inbox,
                template_name=template_name,
                expected_language=template.language,
                expected_category=template.category_for(trigger_kind=trigger_kind),
                parameter_count=len(declared),
            )
            message = approved.render(values)
        except ApprovedTemplateError as exc:
            logger.warning(
                "approved_template_rejected action_id=%s template=%s "
                "reason=%s detail=%s",
                action.action_id,
                template_name,
                exc.reason,
                exc.detail,
            )
            # A value the render refuses comes from the case and does not
            # change; a catalog that does not close can.
            return _ApprovedTemplateComposition(
                failure_reason=exc.reason,
                retryable=exc.reason != APPROVED_TEMPLATE_PARAMETERS_MISSING,
            )
        return _ApprovedTemplateComposition(
            proposal=FollowupMessageProposal(
                strategy=f"approved_template:{template_name}",
                message=message,
            ),
            greeting_name=greeting_name,
        )

    async def _compose_native_step_proposal(
        self,
        *,
        action: ScheduledAction,
        execution_context: FollowupExecutionContext,
        step_template: FlowEngineStepContext | None,
    ) -> _ApprovedTemplateComposition:
        """The send of a native step (flow-engine-v2): its pinned template.

        A first contact with the variables the step declares, in its order,
        out of ``nombre`` and ``producto``, in a template whose category the
        permission of its pinned version accepts: utility with ``compra`` (rule 17), also marketing otherwise.
        The step declares no button; the template may carry fixed URL buttons
        (E-115) and quick replies (2.6.0), and nothing else. Without the engine flag it closes for good
        (``flow_engine_native_needs_engine``): it never goes out with the
        template of the manifest. A step context that did not answer is
        retried; a shape the bridge cannot build closes for good.
        """
        if not self._flow_engine_enabled:
            return _ApprovedTemplateComposition(
                failure_reason=NATIVE_NEEDS_ENGINE, retryable=False
            )
        if step_template is None:
            return _ApprovedTemplateComposition(
                failure_reason=NATIVE_STEP_CONTEXT_UNAVAILABLE
            )
        variables = step_template.template_variables
        if (
            action.action_type != "first_contact_review"
            or step_template.step_key != action.step_key
            or step_template.template_button is not None
            or not variables
            or len(set(variables)) != len(variables)
            or not set(variables) <= NATIVE_STEP_VARIABLES
        ):
            logger.warning(
                "flow_engine_step_unsupported action_id=%s step=%s version=%s",
                action.action_id,
                action.step_key,
                step_template.version,
            )
            return _ApprovedTemplateComposition(
                failure_reason=FLOW_ENGINE_STEP_UNSUPPORTED, retryable=False
            )
        buyer_name = execution_context.buyer_name
        product_name = execution_context.product_name
        greeting_name: str | None = None
        name_value = buyer_name
        if (
            "nombre" in variables
            and self._lead_first_name_greeting_enabled
            and isinstance(buyer_name, str)
            and buyer_name.strip()
        ):
            greeting = await resolve_greeting_name(buyer_name, store=self._supabase)
            greeting_name = greeting.name.strip()
            name_value = greeting_name
        source = {"nombre": name_value, "producto": product_name}
        if any(
            not isinstance(source[item], str) or not source[item].strip()  # type: ignore[union-attr]
            for item in variables
        ) or ("nombre" in variables and not template_greeting_name_is_safe(name_value)):
            # The values of the case (the name from Hotmart, unverified input,
            # and the product): they do not change on their own.
            return _ApprovedTemplateComposition(
                failure_reason=APPROVED_TEMPLATE_PARAMETERS_MISSING, retryable=False
            )
        ordered = {
            str(position): " ".join(str(source[item]).split())
            for position, item in enumerate(variables, start=1)
        }
        assert self._chatwoot is not None
        assert self._chatwoot_inbox_id is not None
        try:
            inbox = await self._chatwoot.get_inbox(inbox_id=self._chatwoot_inbox_id)
        except (httpx.HTTPError, ChatwootProtocolError) as exc:
            logger.warning(
                "approved_template_catalog_unavailable action_id=%s error=%s",
                action.action_id,
                type(exc).__name__,
            )
            return _ApprovedTemplateComposition(failure_reason=APPROVED_TEMPLATE_UNAVAILABLE)
        try:
            approved = parse_approved_template(
                inbox,
                template_name=step_template.template_name,
                expected_language=step_template.template_language,
                expected_category=native_step_categories(step_template.entry_permission),
                parameter_count=len(variables),
                parameterless_buttons_only=True,
            )
            message = approved.render(ordered)
            processed = approved.processed_params(ordered)
        except ApprovedTemplateError as exc:
            logger.warning(
                "approved_template_rejected action_id=%s template=%s reason=%s detail=%s",
                action.action_id,
                step_template.template_name,
                exc.reason,
                exc.detail,
            )
            return _ApprovedTemplateComposition(
                failure_reason=exc.reason,
                retryable=exc.reason != APPROVED_TEMPLATE_PARAMETERS_MISSING,
            )
        return _ApprovedTemplateComposition(
            proposal=FollowupMessageProposal(
                strategy=f"approved_template:{step_template.template_name}",
                message=message,
            ),
            greeting_name=greeting_name,
            template_name=step_template.template_name,
            template_params={
                "name": step_template.template_name,
                "category": approved.category,
                "language": step_template.template_language,
                "processed_params": processed,
            },
        )

    async def _record_payment_failure_first_touch(
        self,
        *,
        action: ScheduledAction,
        attempt: DeliveryAttempt,
        ready: PaymentFailureFirstTouchReady,
    ) -> None:
        """Record the variant of B1 and its coupon, under the lease, before the send.

        It never stops B1: without the record the follow-up goes out as B2,
        which carries its own coupon 2, so the worst case is B2 where Z was
        due. A base without the migration (20261019000700) answers with an
        error here and B1 goes out as before.
        """
        try:
            await self._supabase.record_payment_failure_first_touch(
                action_id=action.action_id,
                attempt_id=attempt.attempt_id,
                worker_id=self._worker_id,
                lease_generation=action.lease_generation,
                variant=ready.variant,
                coupon_code=ready.coupon_code,
                now=self._clock(),
            )
        except SupabaseError as exc:
            logger.warning(
                "payment_failure_first_touch_record_failed action_id=%s "
                "attempt_id=%s error=%s",
                action.action_id,
                attempt.attempt_id,
                type(exc).__name__,
            )

    async def _compose_payment_failure_followup_proposal(
        self,
        *,
        action: ScheduledAction,
        attempt: DeliveryAttempt | None,
        name_value: str,
        product_name: str,
        buyer_phone: str | None,
        greeting_name: str | None,
    ) -> _ApprovedTemplateComposition:
        """B2 or Z of a refused payment: coupon 2 and our link, or no message.

        Like Z of the cart, nothing falls back to another template. Only an
        unreadable catalog or context, a catalog that does not close, or
        coupon 2 still being created retries.
        """
        config = self._payment_failure_followup
        assert config is not None
        assert self._chatwoot is not None
        assert self._chatwoot_inbox_id is not None
        if attempt is None:
            # The follow-up issues a link for its attempt: without one it
            # cannot be built.
            return _ApprovedTemplateComposition(
                failure_reason="payment_failure_followup_attempt_missing",
                retryable=False,
            )
        try:
            inbox = await self._chatwoot.get_inbox(inbox_id=self._chatwoot_inbox_id)
        except (httpx.HTTPError, ChatwootProtocolError) as exc:
            logger.warning(
                "approved_template_catalog_unavailable action_id=%s error=%s",
                action.action_id,
                type(exc).__name__,
            )
            return _ApprovedTemplateComposition(
                failure_reason=APPROVED_TEMPLATE_UNAVAILABLE
            )
        try:
            outcome = await compose_payment_failure_followup(
                config=config,
                supabase=self._supabase,
                inbox=inbox,
                action=action,
                attempt=attempt,
                buyer_name=name_value,
                product_name=product_name,
                buyer_phone=buyer_phone,
                worker_id=self._worker_id,
                now=self._clock(),
                ulid_factory=generate_issuance_ulid,
            )
        except SupabasePermanentError as exc:
            logger.warning(
                "payment_failure_followup_context_rejected action_id=%s reason=%s",
                action.action_id,
                exc.reason,
            )
            return _ApprovedTemplateComposition(
                failure_reason=exc.reason or "payment_failure_followup_context_rejected",
                retryable=False,
            )
        except SupabaseError as exc:
            logger.warning(
                "payment_failure_followup_context_unavailable action_id=%s error=%s",
                action.action_id,
                type(exc).__name__,
            )
            return _ApprovedTemplateComposition(
                failure_reason="payment_failure_followup_context_unavailable"
            )
        if isinstance(outcome, CartFollowupReady):
            logger.info(
                "payment_failure_followup_ready action_id=%s template=%s",
                action.action_id,
                outcome.template_name,
            )
            return _ApprovedTemplateComposition(
                proposal=FollowupMessageProposal(
                    strategy=f"approved_template:{outcome.template_name}",
                    message=outcome.message,
                ),
                greeting_name=greeting_name,
                template_name=outcome.template_name,
                template_params=outcome.template_params,
                issuance_id=outcome.issuance_id,
            )
        if isinstance(outcome, CartDiscountDefer):
            logger.info(
                "payment_failure_followup_deferred action_id=%s retry_after_s=%s",
                action.action_id,
                int(outcome.retry_after.total_seconds()),
            )
            return _ApprovedTemplateComposition(
                failure_reason=outcome.reason,
                retry_after=outcome.retry_after,
            )
        logger.warning(
            "payment_failure_followup_stopped action_id=%s reason=%s",
            action.action_id,
            outcome.reason,
        )
        return _ApprovedTemplateComposition(
            failure_reason=outcome.reason,
            retryable=outcome.retryable,
        )

    async def _compose_cart_followup_proposal(
        self,
        *,
        config: CartFollowupConfig,
        action: ScheduledAction,
        attempt: DeliveryAttempt | None,
        step_key: str,
        name_value: str,
        product_name: str,
        buyer_phone: str | None,
        greeting_name: str | None,
    ) -> _ApprovedTemplateComposition:
        """A2 or Z of the cart: its approved template, and for Z coupon 2 and the link.

        Nothing falls back to another template: a step that cannot be built
        closes without a message. Only an unreadable catalog, a catalog that
        does not close, or coupon 2 still being created retries.
        """
        assert self._chatwoot is not None
        assert self._chatwoot_inbox_id is not None
        if step_key == CART_LAST_TOUCH_STEP and attempt is None:
            # Z issues a link for its attempt: without one it cannot be built.
            return _ApprovedTemplateComposition(
                failure_reason="cart_last_touch_attempt_missing", retryable=False
            )
        try:
            inbox = await self._chatwoot.get_inbox(inbox_id=self._chatwoot_inbox_id)
        except (httpx.HTTPError, ChatwootProtocolError) as exc:
            logger.warning(
                "approved_template_catalog_unavailable action_id=%s error=%s",
                action.action_id,
                type(exc).__name__,
            )
            return _ApprovedTemplateComposition(
                failure_reason=APPROVED_TEMPLATE_UNAVAILABLE
            )
        outcome: CartFollowupOutcome
        if step_key == CART_LAST_TOUCH_STEP:
            assert attempt is not None
            outcome = await compose_cart_last_touch(
                config=config,
                supabase=self._supabase,
                inbox=inbox,
                action=action,
                attempt=attempt,
                buyer_name=name_value,
                product_name=product_name,
                buyer_phone=buyer_phone,
                worker_id=self._worker_id,
                now=self._clock(),
                ulid_factory=generate_issuance_ulid,
            )
        else:
            outcome = compose_cart_second_touch(
                config=config,
                inbox=inbox,
                buyer_name=name_value,
                product_name=product_name,
            )
        if isinstance(outcome, CartFollowupReady):
            logger.info(
                "cart_followup_ready action_id=%s step=%s",
                action.action_id,
                step_key,
            )
            return _ApprovedTemplateComposition(
                proposal=FollowupMessageProposal(
                    strategy=f"approved_template:{outcome.template_name}",
                    message=outcome.message,
                ),
                greeting_name=greeting_name,
                template_name=outcome.template_name,
                template_params=outcome.template_params,
                issuance_id=outcome.issuance_id,
            )
        if isinstance(outcome, CartDiscountDefer):
            logger.info(
                "cart_followup_deferred action_id=%s step=%s retry_after_s=%s",
                action.action_id,
                step_key,
                int(outcome.retry_after.total_seconds()),
            )
            return _ApprovedTemplateComposition(
                failure_reason=outcome.reason,
                retry_after=outcome.retry_after,
            )
        logger.warning(
            "cart_followup_stopped action_id=%s step=%s reason=%s",
            action.action_id,
            step_key,
            outcome.reason,
        )
        return _ApprovedTemplateComposition(
            failure_reason=outcome.reason,
            retryable=outcome.retryable,
        )

    async def _compose_payment_slip_proposal(
        self,
        *,
        action: ScheduledAction,
        declared: tuple[str, ...],
        name_value: str,
        product_name: str,
        greeting_name: str | None,
        step_key: str | None = None,
    ) -> _ApprovedTemplateComposition:
        """C1, C2 or C3: the template of the slip with its store, due date and button.

        Every reason closes the attempt without a message. Only an unreadable
        catalog or slip context, or a catalog that does not close, retries. A
        reminder composed before its moment waits until then.
        """
        template = self._waba_template
        assert template is not None
        assert self._chatwoot is not None
        assert self._chatwoot_inbox_id is not None
        step = PAYMENT_SLIP_STEPS.get(step_key) if step_key is not None else None
        step_template = (
            template.payment_slip_step(step_key) if step_key is not None else None
        )
        if step_key is None:
            template_name = template.first_touch_name_for(
                trigger_kind=PAYMENT_SLIP_TRIGGER
            )
            category = template.category_for(trigger_kind=PAYMENT_SLIP_TRIGGER)
        elif step is not None and step_template is not None:
            template_name = step_template.name
            category = step_template.category
        else:
            template_name = None
            category = template.category
        if self._payment_slip is None or template_name is None:
            return _ApprovedTemplateComposition(
                failure_reason=FIRST_TOUCH_TEMPLATE_NOT_CONFIGURED,
                retryable=False,
            )
        try:
            inbox = await self._chatwoot.get_inbox(inbox_id=self._chatwoot_inbox_id)
        except (httpx.HTTPError, ChatwootProtocolError) as exc:
            logger.warning(
                "approved_template_catalog_unavailable action_id=%s error=%s",
                action.action_id,
                type(exc).__name__,
            )
            return _ApprovedTemplateComposition(
                failure_reason=APPROVED_TEMPLATE_UNAVAILABLE
            )
        slip = await compose_payment_slip_first_touch(
            config=self._payment_slip,
            supabase=self._supabase,
            inbox=inbox,
            action=action,
            template_name=template_name,
            language=template.language,
            category=category,
            parameters=declared,
            buyer_name=name_value,
            product_name=product_name,
            worker_id=self._worker_id,
            now=self._clock(),
            step=step,
        )
        if isinstance(slip, PaymentSlipDefer):
            logger.info(
                "payment_slip_step_deferred action_id=%s step=%s retry_after_s=%s",
                action.action_id,
                step_key,
                int(slip.retry_after.total_seconds()),
            )
            return _ApprovedTemplateComposition(
                failure_reason=slip.reason,
                retry_after=slip.retry_after,
            )
        if not isinstance(slip, PaymentSlipReady):
            # The log carries the code only: never the link, the reference or
            # the person.
            logger.warning(
                "payment_slip_first_touch_not_sent action_id=%s step=%s reason=%s "
                "retryable=%s",
                action.action_id,
                step_key or "first_contact",
                slip.reason,
                slip.retryable,
            )
            return _ApprovedTemplateComposition(
                failure_reason=slip.reason,
                retryable=slip.retryable,
            )
        logger.info(
            "payment_slip_first_touch_ready action_id=%s step=%s due_date_source=%s",
            action.action_id,
            step_key or "first_contact",
            slip.due_date_source,
        )
        return _ApprovedTemplateComposition(
            proposal=FollowupMessageProposal(
                strategy=f"approved_template:{slip.template_name}",
                message=slip.message,
            ),
            greeting_name=greeting_name,
            template_name=slip.template_name,
            template_params=slip.template_params,
        )

    async def dispatch_due(self, *, now: str) -> list[ReevaluationDecision]:
        """Claim and re-evaluate; return execute candidates without side effects."""
        actions = await self.claim_due(now=now)
        decisions: list[ReevaluationDecision] = []
        for action in actions:
            evidence = await self._load_chatwoot_evidence(action=action, now=now)
            # The anchor picks the reevaluation: the first contact after the
            # form has its own, which checks its stops (purchase, opt-out,
            # consent) before the shared guards.
            decision = await self._supabase.reevaluate_followup_action(
                action_id=action.action_id,
                worker_id=self._worker_id,
                lease_generation=action.lease_generation,
                now=now,
                chatwoot_evidence=evidence,
                anchor_type=action.anchor_type,
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
                if (
                    self._recovery_agent is not None or self._approved_template_direct
                ) and (
                    (
                        action.anchor_type == PRECHECKOUT_INTENT_TRIGGER
                        and (
                            self._waba_template is None
                            or self._waba_template.precheckout_name is None
                        )
                    )
                    or (
                        # The first contact after a slip: its own template in
                        # the direct mode, or nothing (never Hermes, never
                        # the cart template).
                        _flow_trigger(action) == PAYMENT_SLIP_TRIGGER
                        and self._payment_slip is None
                    )
                ):
                    # The first contact after the form only goes out with its
                    # own approved template. Without it the attempt closes
                    # here, before Hermes, the final gate and the sender: it
                    # never falls back to the cart template. It does not
                    # change by retrying.
                    logger.warning(
                        "durable_first_touch_template_not_configured "
                        "action_id=%s attempt_id=%s anchor=%s",
                        action.action_id,
                        attempt.attempt_id,
                        action.anchor_type,
                    )
                    await self._finalize_pre_request_failure(
                        action=action,
                        attempt=attempt,
                        reason_code=FIRST_TOUCH_TEMPLATE_NOT_CONFIGURED,
                        retryable=False,
                    )
                    decisions.append(decision)
                    continue
                if self._recovery_agent is not None or self._approved_template_direct:
                    direct_failure: str | None = None
                    direct_retryable = True
                    direct_retry_after: timedelta | None = None
                    direct_template_name: str | None = None
                    direct_template_params: dict[str, object] | None = None
                    direct_issuance_id: str | None = None
                    greeting_name: str | None = None
                    engine_step: FlowEngineStepContext | None = None
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
                        if self._approved_template_direct:
                            engine_step = await self._flow_engine_step(action)
                            composition = (
                                await self._compose_approved_template_proposal(
                                    action=action,
                                    execution_context=execution_context,
                                    attempt=attempt,
                                    step_template=engine_step,
                                )
                            )
                            proposal = composition.proposal
                            greeting_name = composition.greeting_name
                            direct_failure = composition.failure_reason
                            direct_retryable = composition.retryable
                            direct_retry_after = composition.retry_after
                            direct_template_name = composition.template_name
                            direct_template_params = composition.template_params
                            direct_issuance_id = composition.issuance_id
                        elif action.anchor_type == NATIVE_ANCHOR:
                            # flow-engine-v2: un paso nativo nunca va a
                            # Hermes. Asi nunca llega a request_human_handoff
                            # (que exige el binding del piloto que un caso
                            # nativo no tiene: handoff_pilot_scope_mismatch).
                            proposal = None
                            direct_failure = NATIVE_NEEDS_DIRECT_TEMPLATE
                            direct_retryable = False
                        else:
                            assert self._recovery_agent is not None
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
                    if direct_failure is not None:
                        logger.warning(
                            "durable_approved_template_not_sent "
                            "action_id=%s attempt_id=%s reason=%s",
                            action.action_id,
                            attempt.attempt_id,
                            direct_failure,
                        )
                        await self._finalize_pre_request_failure(
                            action=action,
                            attempt=attempt,
                            reason_code=direct_failure,
                            retryable=direct_retryable,
                            retry_after=direct_retry_after,
                        )
                    elif proposal is None:
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
                            if not _is_authorized_followup_recipient(
                                execution_context=execution_context,
                                allowed_jid=self._allowed_jid,
                                commercial_ally_config=(
                                    self._commercial_ally_config
                                ),
                                portable_recipient_enabled=(
                                    self._portable_recipient_enabled
                                ),
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
                            first_touch_recipient: FirstTouchRecipient | None = None
                            if (
                                self._whatsapp_equivalence_enabled
                                and action.action_type != "no_reply_review"
                            ):
                                # The Chatwoot contact this first touch goes
                                # through is resolved now, with reads only and
                                # before the final gate and the request start:
                                # the gate hashes the wa_id Meta will receive,
                                # not the phone as the contact stores it.
                                try:
                                    first_touch_recipient = (
                                        await self._sender.resolve_first_touch_recipient(
                                            phone=execution_context.buyer_phone,
                                        )
                                    )
                                except asyncio.CancelledError:
                                    await self._finalize_pre_request_failure(
                                        action=action,
                                        attempt=attempt,
                                        reason_code="pre_request_cancelled",
                                    )
                                    raise
                                except Exception as exc:
                                    # A ChatwootProtocolError carries a
                                    # fixed reason code (contact_blocked,
                                    # ambiguous_contact_match...), never data
                                    # of the person.
                                    logger.warning(
                                        "durable_first_touch_recipient_unresolved "
                                        "action_id=%s attempt_id=%s error=%s "
                                        "reason=%s",
                                        action.action_id,
                                        attempt.attempt_id,
                                        type(exc).__name__,
                                        (
                                            str(exc)
                                            if isinstance(exc, ChatwootProtocolError)
                                            else "unavailable"
                                        ),
                                    )
                                    await self._finalize_pre_request_failure(
                                        action=action,
                                        attempt=attempt,
                                        reason_code="pre_request_failed",
                                    )
                                    decisions.append(decision)
                                    continue
                                if not isinstance(
                                    first_touch_recipient, FirstTouchRecipient
                                ) or not first_touch_recipient_matches(
                                    first_touch_recipient,
                                    phone=execution_context.buyer_phone,
                                ):
                                    # Fail closed: Chatwoot would deliver to a
                                    # number that is not the consented one. It
                                    # does not change by retrying.
                                    logger.warning(
                                        "durable_first_touch_recipient_mismatch "
                                        "action_id=%s attempt_id=%s region=%s "
                                        "chatwoot_contact_id=%s",
                                        action.action_id,
                                        attempt.attempt_id,
                                        whatsapp_phone_region(
                                            execution_context.buyer_phone
                                        ),
                                        getattr(
                                            first_touch_recipient, "contact_id", None
                                        ),
                                    )
                                    await self._finalize_pre_request_failure(
                                        action=action,
                                        attempt=attempt,
                                        reason_code=FIRST_TOUCH_RECIPIENT_MISMATCH,
                                        retryable=False,
                                    )
                                    decisions.append(decision)
                                    continue
                            final_now = self._clock()
                            try:
                                final_evidence = (
                                    await self._load_chatwoot_evidence(
                                        action=action,
                                        now=final_now,
                                        buyer_phone=execution_context.buyer_phone,
                                    )
                                )
                                final_decision = (
                                    await self._supabase.reevaluate_followup_action(
                                        action_id=action.action_id,
                                        worker_id=self._worker_id,
                                        lease_generation=action.lease_generation,
                                        now=final_now,
                                        chatwoot_evidence=final_evidence,
                                        anchor_type=action.anchor_type,
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
                                        _first_touch_template_name(
                                            self._waba_template,
                                            anchor_type=action.anchor_type,
                                        )
                                        if self._waba_template is not None
                                        else ""
                                    )
                                )
                                if direct_template_name is not None:
                                    template_name = direct_template_name
                                authorized = self._final_meta_effect_gate.authorize(
                                    FinalMetaEffect(
                                        delivery_id=attempt.attempt_id,
                                        action_kind=action_kind,
                                        mode=attempt.mode,
                                        target_phone=(
                                            "+" + first_touch_recipient.wa_id
                                            if first_touch_recipient is not None
                                            else "+"
                                            + execution_context.buyer_phone.lstrip("+")
                                        ),
                                        content=proposal.message,
                                        template_name=template_name or "",
                                        # The language of the pinned version
                                        # only goes with its template: A1 that
                                        # falls back without its coupon sends
                                        # the template of the manifest.
                                        template_language=(
                                            engine_step.template_language
                                            if engine_step is not None
                                            and direct_template_name is not None
                                            else self._waba_template.language
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
                                        # An engine step starts by the engine
                                        # authorization (flow-engine-v1, 7.2).
                                        self._supabase.mark_flow_engine_request_started(
                                            action_id=action.action_id,
                                            attempt_id=attempt.attempt_id,
                                            worker_id=self._worker_id,
                                            lease_generation=action.lease_generation,
                                            now=final_now,
                                        )
                                        if engine_step is not None
                                        else self._supabase.mark_followup_request_started(
                                            action_id=action.action_id,
                                            attempt_id=attempt.attempt_id,
                                            worker_id=self._worker_id,
                                            lease_generation=action.lease_generation,
                                            now=final_now,
                                            pilot_boundary=self._pilot_boundary,
                                            # A reminder of a slip starts
                                            # through the RPC of the slip,
                                            # against its scope.
                                            anchor_type=_flow_trigger(action),
                                        )
                                    )
                                )
                            except FlowEngineRunTerminated as exc:
                                # The flow was turned off after the run
                                # started: the base already closed the attempt,
                                # the action, the sequence and the case, and
                                # committed. Nothing to finalize here.
                                logger.info(
                                    "flow_engine_run_terminated action_id=%s "
                                    "attempt_id=%s reason=%s",
                                    action.action_id,
                                    attempt.attempt_id,
                                    exc.reason,
                                )
                                decisions.append(decision)
                                continue
                            except PilotRequestStartRejectedError as exc:
                                # The base refused to start: a cap of the
                                # scope, a disarmed runtime, or a stop that
                                # came in after the final reevaluation. Nothing
                                # went out and no start was consumed. The
                                # attempt stays reserved on purpose: closing it
                                # without a retry would leave the action
                                # permanent_failed and its case open for good,
                                # while the next lease lets the reevaluation
                                # cancel the case with the stop, or the action
                                # expire with it. What must not happen is what
                                # used to: the error cut the whole batch and
                                # the reason was lost.
                                logger.warning(
                                    "durable_request_start_rejected "
                                    "action_id=%s attempt_id=%s anchor=%s reason=%s",
                                    action.action_id,
                                    attempt.attempt_id,
                                    action.anchor_type,
                                    exc.reason,
                                )
                                decisions.append(decision)
                                continue
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
                            if direct_issuance_id is not None and not (
                                await self._authorize_recovery_issuance(
                                    action=action,
                                    attempt=attempt,
                                    issuance_id=direct_issuance_id,
                                    now=final_now,
                                )
                            ):
                                decisions.append(decision)
                                continue
                            try:
                                if followup_conversation_id is not None:
                                    followup_kwargs: dict[str, Any] = {
                                        "conversation_id": followup_conversation_id,
                                        "phone": execution_context.buyer_phone or "",
                                        "content": proposal.message,
                                        "delivery_id": attempt.attempt_id,
                                    }
                                    # A slip reminder: the template the final
                                    # gate hashed, with its button.
                                    if direct_template_params is not None:
                                        followup_kwargs["template_params"] = (
                                            direct_template_params
                                        )
                                    result = await self._sender.send_followup(
                                        **followup_kwargs
                                    )
                                else:
                                    first_touch_kwargs: dict[str, Any] = {
                                        "phone": execution_context.buyer_phone or "",
                                        "buyer_name": execution_context.buyer_name,
                                        "buyer_email": execution_context.buyer_email,
                                        "product_name": execution_context.product_name,
                                        "content": proposal.message,
                                        "delivery_id": attempt.attempt_id,
                                        "trigger_kind": action.anchor_type,
                                    }
                                    # The same greeting the approved body was
                                    # rendered with: the content the final gate
                                    # hashed is the text Meta shows.
                                    if greeting_name is not None:
                                        first_touch_kwargs["greeting_name"] = (
                                            greeting_name
                                        )
                                    # The recipient the final gate recorded:
                                    # the sender does not search it again.
                                    if first_touch_recipient is not None:
                                        first_touch_kwargs["recipient"] = (
                                            first_touch_recipient
                                        )
                                    # A1 with the personal coupon: the template
                                    # the final gate hashed, with its button.
                                    if direct_template_params is not None:
                                        first_touch_kwargs["template_params"] = (
                                            direct_template_params
                                        )
                                    result = await self._sender.send_first_touch(
                                        **first_touch_kwargs
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
                            if direct_issuance_id is not None:
                                await self._finalize_recovery_issuance(
                                    issuance_id=direct_issuance_id,
                                    result=result,
                                )
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
                                    native_accepted = (
                                        action.anchor_type == NATIVE_ANCHOR
                                        and engine_step is not None
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
                                    native_accepted = False
                                if native_accepted:
                                    # flow-engine-v2: etiquetar despues de la
                                    # plantilla. Falla blanda: el envio ya
                                    # quedo aceptado.
                                    assert self._chatwoot is not None
                                    assert self._chatwoot_inbox_id is not None
                                    await apply_native_tail(
                                        self._supabase,
                                        action_id=action.action_id,
                                        conversation_id=result.conversation_id,
                                        inbox_id=self._chatwoot_inbox_id,
                                        labeler=self._native_client_labeler,
                                        notes=self._chatwoot,
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
        lead_first_name_greeting_enabled: bool = False,
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
        self._lead_first_name_greeting_enabled = lead_first_name_greeting_enabled
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
        greeting_name = buyer_name
        if self._lead_first_name_greeting_enabled:
            greeting = await resolve_greeting_name(buyer_name, store=self._supabase)
            greeting_name = greeting.name
            logger.info(
                "precheckout_first_touch_greeting source=%s", greeting.source
            )
        content = (
            f"Hola, {greeting_name}. Te escribe el equipo de la Psic. Johanna. "
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
        if self._lead_first_name_greeting_enabled:
            sender_kwargs["greeting_name"] = greeting_name
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
        commercial_ally_config: CommercialAllyConfig | None = None,
        payment_failure_enabled: bool = False,
        product_routes: ProductRoutes | None = None,
        payment_slip: PaymentSlipPlanningConfig | None = None,
        payment_failure_planning: PaymentFailurePlanningConfig | None = None,
        flow_engine: FlowEngineRuntime | None = None,
        purchase_agent_pause: PurchaseAgentPause | None = None,
    ) -> None:
        self._supabase = supabase
        self._poll_interval = poll_interval_seconds
        # E-100: con PURCHASE_AGENT_PAUSE_ENABLED, la compra aprobada pausa al
        # agente en la conversacion del comprador y la pasa al equipo. Sin el
        # flag, ninguna llamada nueva.
        self._purchase_agent_pause = purchase_agent_pause
        # flow-engine-v1, seccion 8: solo con FLOW_ENGINE_ENABLED. Con None,
        # el lote no lee nada del motor y el carrito se planifica como hoy.
        self._flow_engine = flow_engine
        # flow-engine-v2: la ultima lectura de la cola de la compra nativa.
        self._native_queue_read_at: float | None = None
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
        self._commercial_ally_config = commercial_ally_config
        self._payment_failure_enabled = payment_failure_enabled
        # La ficha de pago (C1): sin esto, una ficha queda failed con
        # unsupported_persisted_event_type, como hasta la 1.10.
        self._payment_slip = payment_slip
        # El pago rechazado (B1) con su propio scope y su politica (15 minutos
        # de gracia). Sin esto se planifica como hasta la 1.11, con los del
        # carrito.
        self._payment_failure_planning = payment_failure_planning
        # Con varios productos, solo el principal tiene flujos de recuperacion
        # (paso 2c-2): los de los demas llegan con el motor de flujos.
        self._product_routes = product_routes
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
        if self._flow_engine is not None:
            # A lo sumo cada 60 s; una falla deja la cache failing y el
            # carrito va por el envoltorio de hoy.
            await self._flow_engine.routes.refresh_if_due()
            # flow-engine-v2: los flujos nativos, tambien cada 60 s. Sin uno
            # prendido o en cohorte, el lote no hace ninguna llamada por evento.
            await self._flow_engine.native_flows.refresh_if_due()
            await self._retry_native_queue()
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

    def _native_purchase_product(self, payload: dict[str, Any]) -> str | None:
        """The product of a purchase with a native flow on, or None.

        None without the engine flag, without the portable runtime, or when no
        native flow of that product is on or in its cohort: then the purchase
        goes exactly as today, without any new call.
        """
        if self._flow_engine is None or self._commercial_ally_config is None:
            return None
        if not self._flow_engine.native_flows.any_on():
            return None
        product_ref = purchase_product_ref(
            payload,
            commercial_ally_config=self._commercial_ally_config,
            product_routes=self._product_routes,
        )
        if product_ref is None or not self._flow_engine.native_flows.wants(
            product_ref, NATIVE_PURCHASE_TRIGGER
        ):
            return None
        return product_ref

    def _native_cart_product(self, payload: dict[str, Any]) -> str | None:
        """The product of a cart with a native cart flow on or in its cohort, or None (E-161).

        None without the engine flag, without the portable runtime, or without
        such a flow: the cart goes exactly as today, without any new call.
        """
        if self._flow_engine is None or self._commercial_ally_config is None:
            return None
        if not self._flow_engine.native_flows.any_on():
            return None
        product_ref = cart_product_ref(
            payload,
            commercial_ally_config=self._commercial_ally_config,
            product_routes=self._product_routes,
        )
        if product_ref is None or not self._flow_engine.native_flows.wants(
            product_ref, NATIVE_CART_TRIGGER
        ):
            return None
        return product_ref

    async def _admit_native_purchase(
        self, event_id: str, payload: dict[str, Any], product_ref: str
    ) -> str:
        assert self._flow_engine is not None
        assert self._commercial_ally_config is not None
        return await admit_native_purchase(
            self._supabase,
            runtime=self._flow_engine,
            webhook_event_id=event_id,
            payload=payload,
            product_ref=product_ref,
            commercial_ally_config=self._commercial_ally_config,
            now=datetime.now(timezone.utc).isoformat(),
        )

    async def _close_native_purchase(self, event_id: str) -> None:
        try:
            await self._supabase.finish_flow_engine_native_event(
                webhook_event_id=event_id,
                done=True,
                now=datetime.now(timezone.utc).isoformat(),
            )
        except SupabaseError as exc:
            logger.warning(
                "flow_engine_native_queue_mark_failed event_id=%s error=%s",
                event_id,
                type(exc).__name__,
            )

    async def _retry_native_queue(self) -> None:
        """The applied purchases the native admission has not answered.

        Only with a native flow on: without one, no call. A purchase whose flow
        was turned off meanwhile is admitted anyway and the base answers
        sin_flujo; the queue closes it.
        """
        assert self._flow_engine is not None
        if self._commercial_ally_config is None or not self._flow_engine.native_flows.any_on():
            return
        # A lo sumo cada 60 s, como las otras lecturas del motor.
        now = time.monotonic()
        if (
            self._native_queue_read_at is not None
            and now - self._native_queue_read_at < NATIVE_QUEUE_REFRESH_SECONDS
        ):
            return
        self._native_queue_read_at = now
        try:
            queue = await self._supabase.read_flow_engine_native_event_queue(
                self._flow_engine.instance, limit=NATIVE_QUEUE_PAGE
            )
        except SupabaseError as exc:
            self._flow_engine.native_queue_pending = None
            logger.warning(
                "flow_engine_read_failed read=native_queue error=%s", type(exc).__name__
            )
            return
        self._flow_engine.native_queue_pending = queue.pending
        for queued in queue.events:
            if self._stopped.is_set():
                return
            product_ref = purchase_product_ref(
                queued.payload,
                commercial_ally_config=self._commercial_ally_config,
                product_routes=self._product_routes,
            )
            if product_ref is None:
                await self._close_native_purchase(queued.webhook_event_id)
                continue
            outcome = await self._admit_native_purchase(
                queued.webhook_event_id, queued.payload, product_ref
            )
            logger.info(
                "flow_engine_native_queue_retry event_id=%s attempts=%s outcome=%s",
                queued.webhook_event_id,
                queued.attempts,
                outcome,
            )
            # E-165: la compra que la primera admision no contesto no se pauso (no se sabia si el
            # flujo nativo la tomaba). Con la respuesta, se pausa si ningun flujo arranco.
            purchase = parse_hotmart_purchase_payload(queued.payload)
            if self._purchase_agent_pause is not None and purchase is not None:
                await self._pause_agent_for_purchase(
                    queued.webhook_event_id,
                    queued.payload,
                    purchase,
                    native_outcome=outcome,
                )

    async def _pause_agent_for_purchase(
        self,
        event_id: str,
        payload: dict[str, Any],
        purchase: HotmartPurchaseData,
        *,
        native_outcome: str | None,
    ) -> None:
        """E-100: after the purchase is applied, the agent stops answering the buyer.

        Only for a product of the instance. Since E-165 the pause is skipped
        only for the buyer a native purchase flow took (``native_outcome`` is
        ``admitida``: its welcome template goes to the same conversation, and a
        paused conversation stops proactive sends), and while the native
        admission has not answered (``reintentar``: the queue retry pauses if
        no flow started). A buyer outside the cohort, one the flow does not
        take, or one it can never take is paused as without the flow.
        ``None`` is a purchase without a native flow on. It never fails the
        purchase: ``pause_buyer`` logs and returns.
        """
        assert self._purchase_agent_pause is not None
        if native_outcome == NATIVE_PURCHASE_STARTED:
            logger.info(
                "purchase_agent_pause_skipped event_id=%s reason=native_flow_started",
                event_id,
            )
            return
        if native_outcome == NATIVE_PURCHASE_RETRY:
            logger.info(
                "purchase_agent_pause_skipped event_id=%s reason=native_admission_pending",
                event_id,
            )
            return
        if purchase_product_ref(
            payload,
            commercial_ally_config=self._commercial_ally_config,
            product_routes=self._product_routes,
        ) is None:
            logger.info(
                "purchase_agent_pause_skipped event_id=%s reason=other_product",
                event_id,
            )
            return
        await self._purchase_agent_pause.pause_buyer(
            webhook_event_id=event_id, purchase=purchase
        )

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
            native_product = self._native_purchase_product(payload)
            if native_product is not None:
                assert self._flow_engine is not None
                # flow-engine-v2, seccion 5: la compra se anota para la admision
                # nativa ANTES de aplicarla. La aplicacion la marca processed, y
                # despues el lote ya no la ve: si la admision no contesta, la
                # cola la trae de nuevo. Si la anotacion falla, el evento sigue
                # received y el lote lo reintenta entero.
                await self._supabase.enqueue_flow_engine_native_event(
                    self._flow_engine.instance, webhook_event_id=event_id
                )
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
                if native_product is not None:
                    await self._close_native_purchase(event_id)
                return
            logger.info(
                "hotmart_purchase_processed event_id=%s outcome=%s",
                purchase.event_id,
                result.outcome,
            )
            native_outcome: str | None = None
            if native_product is not None:
                # Primero la compra corto lo abierto (choque 2 del diseno);
                # recien ahora arranca el flujo nativo. Con not_found (una
                # venta sin caso del recuperador) tambien dispara.
                native_outcome = await self._admit_native_purchase(
                    event_id, payload, native_product
                )
            if self._purchase_agent_pause is not None:
                # E-165: despues de la admision nativa, para saber si un flujo
                # tomo al comprador; si no lo tomo, se pausa como sin flujo.
                await self._pause_agent_for_purchase(
                    event_id, payload, purchase, native_outcome=native_outcome
                )
            return
        slip_event = (
            self._payment_slip is not None
            and event_type == EVENT_PURCHASE_BILLET_PRINTED
        )
        if event_type != EVENT_CART_ABANDONMENT and not (
            self._payment_failure_enabled
            and event_type == EVENT_PURCHASE_CANCELED
        ) and not slip_event:
            await self._supabase.update_event_status(
                event_id=event_id,
                status="failed",
                error="unsupported_persisted_event_type",
            )
            return
        if self._product_routes is not None and len(self._product_routes) > 1:
            route = self._product_routes.for_hotmart(payload)
            if isinstance(route, HotmartUnrouted) or not route.principal:
                # El evento ya quedo admitido en el funnel de su producto (la
                # historia de la persona, la correlacion y los frenos lo ven).
                # Ese producto no tiene flujos todavia: nada se planifica y no
                # se crea el contacto. Queda ignored con el motivo, no failed.
                reason = (
                    route.reason
                    if isinstance(route, HotmartUnrouted)
                    else "product_without_recovery_flows"
                )
                await self._supabase.update_event_status(
                    event_id=event_id, status="ignored", error=reason
                )
                logger.info(
                    "hotmart_event_not_planned event_id=%s product=%s reason=%s",
                    event_id,
                    "-" if isinstance(route, HotmartUnrouted) else route.key,
                    reason,
                )
                return
        policy_key = self._policy_key
        policy_version = self._policy_version
        pilot_boundary = self._pilot_boundary
        if slip_event:
            assert self._payment_slip is not None
            # The filter by method and country: what the table of the manifest
            # does not enable stays in the base (admitted and correlated) and
            # nobody is written to.
            slip_reason = payment_slip_ingestion_reason(
                payment_slip_facts(payload), self._payment_slip.methods
            )
            if slip_reason is not None:
                await self._supabase.update_event_status(
                    event_id=event_id, status="ignored", error=slip_reason
                )
                logger.info(
                    "payment_slip_not_planned event_id=%s reason=%s",
                    event_id,
                    slip_reason,
                )
                return
            # Its own policy (one hour of grace, one day of life) and its own
            # scope (source event PURCHASE_BILLET_PRINTED).
            policy_key = self._payment_slip.policy_key
            policy_version = self._payment_slip.policy_version
            pilot_boundary = self._payment_slip.pilot_boundary
        elif (
            self._payment_failure_planning is not None
            and event_type == EVENT_PURCHASE_CANCELED
        ):
            policy_key = self._payment_failure_planning.policy_key
            policy_version = self._payment_failure_planning.policy_version
            pilot_boundary = self._payment_failure_planning.pilot_boundary
        try:
            resolver_kwargs: dict[str, Any] = {}
            if self._commercial_ally_config is not None:
                resolver_kwargs["commercial_ally_config"] = self._commercial_ally_config
            if self._flow_engine is not None and event_type == EVENT_CART_ABANDONMENT:
                # Solo el carrito: el pago rechazado y la ficha nunca pasan
                # por la admision del motor.
                resolver_kwargs["flow_engine"] = self._flow_engine
                # flow-engine-v2 2.7.0 (E-161): con un flujo nativo del carrito
                # de ese producto, el carrito va primero a la admision nativa.
                native_cart_product = self._native_cart_product(payload)
                if native_cart_product is not None:
                    resolver_kwargs["native_cart_product_ref"] = native_cart_product
            report = await resolve_event(
                webhook_event_id=event_id,
                payload=payload,
                supabase=self._supabase,
                policy_key=policy_key,
                policy_version=policy_version,
                allowed_jid=self._allowed_jid,
                chatwoot_account_id=self._chatwoot_account_id,
                chatwoot_inbox_id=self._chatwoot_inbox_id,
                pilot_boundary=pilot_boundary,
                event_type=event_type,
                **resolver_kwargs,
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
        if policy_key is not None and policy_version is not None:
            logger.info(
                "payment_slip_recovery_planned event_id=%s"
                if slip_event
                else "cart_recovery_planned event_id=%s",
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
