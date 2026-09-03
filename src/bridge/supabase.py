"""Minimal Supabase REST client for webhook event persistence and identity resolution.

Uses the PostgREST API exposed by every Supabase project — no SDK dependency,
just httpx, matching the project's existing conventions.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from bridge.commercial_ally import CommercialAllyConfig


class SupabaseError(RuntimeError):
    """Raised when a Supabase REST call fails."""


class SupabasePermanentError(SupabaseError):
    """Raised when retrying an unchanged request cannot succeed."""


class SupabaseCommittedResponseError(SupabaseError):
    """Raised when a successful mutating RPC returns an invalid committed row."""


class OperatorCorrelationResolutionError(SupabaseError):
    """Known fail-closed domain rejection from a resolution RPC."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


_OPERATOR_CORRELATION_RESOLUTION_ERRORS = frozenset(
    {
        ("22023", "invalid_operator_correlation_resolution"),
        ("P0002", "operator_correlation_case_not_found"),
        ("55000", "operator_correlation_stale_evidence"),
        ("55000", "operator_correlation_command_expired"),
        ("23505", "operator_correlation_already_resolved"),
        ("23505", "operator_correlation_idempotency_conflict"),
    }
)


def _raise_operator_correlation_resolution_error(
    response: httpx.Response, *, operation: str
) -> None:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        code = payload.get("code")
        message = payload.get("message")
        if (
            isinstance(code, str)
            and isinstance(message, str)
            and (code, message) in _OPERATOR_CORRELATION_RESOLUTION_ERRORS
        ):
            raise OperatorCorrelationResolutionError(message)
    raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")


# ── Data classes for resolution results ──────────────────────────────


@dataclass(frozen=True)
class InsertResult:
    """Outcome of an insert_webhook_event call."""

    inserted: bool


@dataclass(frozen=True)
class PurchaseAdmissionResult:
    """Durable semantic-admission outcome for a purchase webhook."""

    outcome: str
    webhook_event_id: str


@dataclass(frozen=True)
class CartAbandonmentAdmissionResult:
    """Durable semantic-admission outcome for a cart-abandonment webhook."""

    outcome: str
    webhook_event_id: str


@dataclass(frozen=True)
class PaymentFailureAdmissionResult:
    """Durable Johanna payment-failure review admission."""

    outcome: str
    payment_failure_case_id: str
    correlation_outcome: str
    case_status: str


@dataclass(frozen=True)
class PortablePaymentFailureAdmissionResult:
    """Portable payment-failure event admission outcome."""

    outcome: str
    webhook_event_id: str


@dataclass(frozen=True)
class PrecheckoutAdmissionResult:
    """Atomic admission outcome for one provisional form submission."""

    outcome: str
    submission_id: str
    purchase_intent_id: str


@dataclass(frozen=True)
class CommercialAllyDiscountPolicy:
    """Exact published discount policy resolved for one trigger."""

    policy_key: str
    policy_version: int
    trigger_kind: str
    discount_kind: str
    discount_value: Decimal
    currency: str | None
    coupon_reference: str
    offer_valid_for_seconds: int | None
    offer_expiration_mode: str
    presentation_stage: str
    template_key: str
    copy_version: str
    requires_inbound_reply_after_initial_template: bool
    coupon_delivery_mode: str
    urgency_copy_allowed: bool
    channel_provider: str
    delivery_mode: str
    template_language: str
    template_category: str | None
    coupon_template_component: str | None
    coupon_template_parameter_index: int | None
    valid_from: str
    valid_until: str | None
    release_requires_exact_trigger_set: bool

    def __post_init__(self) -> None:
        if not isinstance(self.policy_key, str) or not self.policy_key.strip():
            raise ValueError("invalid policy_key")
        if (
            isinstance(self.policy_version, bool)
            or not isinstance(self.policy_version, int)
            or self.policy_version <= 0
        ):
            raise ValueError("invalid policy_version")
        if self.trigger_kind not in {
            "payment_failure",
            "confirmed_cart_abandonment",
            "precheckout_without_purchase_signal",
        }:
            raise ValueError("invalid trigger_kind")
        if self.discount_kind not in {"percentage", "fixed_amount"}:
            raise ValueError("invalid discount_kind")
        if not isinstance(self.discount_value, Decimal):
            raise ValueError("invalid discount_value")
        if not self.discount_value.is_finite() or self.discount_value <= 0:
            raise ValueError("invalid discount_value")
        if self.discount_kind == "percentage":
            if self.discount_value > 100 or self.currency is not None:
                raise ValueError("invalid percentage discount")
        elif (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isalpha()
            or not self.currency.isupper()
        ):
            raise ValueError("invalid fixed discount currency")
        for field_name, value in (
            ("coupon_reference", self.coupon_reference),
            ("template_key", self.template_key),
            ("copy_version", self.copy_version),
            ("template_language", self.template_language),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"invalid {field_name}")
        for field_name, value in (
            ("template_category", self.template_category),
            ("coupon_template_component", self.coupon_template_component),
        ):
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"invalid {field_name}")
        if self.offer_expiration_mode == "indefinite":
            if self.offer_valid_for_seconds is not None:
                raise ValueError("invalid indefinite offer duration")
        elif self.offer_expiration_mode == "finite":
            if (
                isinstance(self.offer_valid_for_seconds, bool)
                or not isinstance(self.offer_valid_for_seconds, int)
                or self.offer_valid_for_seconds <= 0
            ):
                raise ValueError("invalid finite offer duration")
        else:
            raise ValueError("invalid offer_expiration_mode")
        if self.presentation_stage not in {"first_touch", "later_step"}:
            raise ValueError("invalid presentation_stage")
        if self.coupon_delivery_mode not in {"literal", "meta_template_variable"}:
            raise ValueError("invalid coupon_delivery_mode")
        if self.channel_provider not in {"evolution", "waba"}:
            raise ValueError("invalid channel_provider")
        if self.delivery_mode not in {"freeform", "approved_template"}:
            raise ValueError("invalid delivery_mode")
        for value in (
            self.requires_inbound_reply_after_initial_template,
            self.urgency_copy_allowed,
            self.release_requires_exact_trigger_set,
        ):
            if not isinstance(value, bool):
                raise ValueError("invalid boolean policy field")
        valid_from = self._parse_timestamp(self.valid_from, "valid_from")
        valid_until = (
            None
            if self.valid_until is None
            else self._parse_timestamp(self.valid_until, "valid_until")
        )
        if valid_until is not None and valid_until <= valid_from:
            raise ValueError("invalid policy availability window")
        if self.requires_inbound_reply_after_initial_template and (
            self.presentation_stage != "later_step"
        ):
            raise ValueError("invalid reply-gated presentation stage")
        if self.coupon_delivery_mode == "literal":
            if (
                self.coupon_template_component is not None
                or self.coupon_template_parameter_index is not None
            ):
                raise ValueError("invalid literal coupon transport")
        elif (
            self.channel_provider != "waba"
            or self.delivery_mode != "approved_template"
            or self.coupon_template_component not in {"body", "button"}
            or isinstance(self.coupon_template_parameter_index, bool)
            or not isinstance(self.coupon_template_parameter_index, int)
            or self.coupon_template_parameter_index <= 0
        ):
            raise ValueError("invalid Meta coupon transport")
        if self.release_requires_exact_trigger_set and (
            self.offer_expiration_mode != "indefinite"
            or not self.requires_inbound_reply_after_initial_template
            or self.coupon_delivery_mode != "meta_template_variable"
            or self.urgency_copy_allowed
            or self.channel_provider != "waba"
            or self.delivery_mode != "approved_template"
            or self.presentation_stage != "later_step"
            or self.template_category != "marketing"
        ):
            raise ValueError("invalid strict discount release semantics")

    @staticmethod
    def _parse_timestamp(value: object, field_name: str) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"invalid {field_name}")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid {field_name}") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"invalid {field_name}")
        return parsed


@dataclass(frozen=True)
class PrecheckoutFirstTouchStart:
    outcome: str
    command_id: str
    command_status: str
    target_phone: str
    buyer_name: str
    chatwoot_conversation_id: int
    template_name: str
    template_language: str
    template_category: str
    copy_version: str


@dataclass(frozen=True)
class PrecheckoutFirstTouchFinish:
    command_id: str
    command_status: str


@dataclass(frozen=True)
class JohannaAbandonmentOneShotStart:
    outcome: str
    command_id: str
    command_status: str
    target_phone: str
    buyer_name: str
    buyer_email: str
    product_name: str
    template_name: str
    template_language: str
    template_category: str
    copy_version: str


@dataclass(frozen=True)
class JohannaAbandonmentOneShotFinish:
    command_id: str
    command_status: str


@dataclass(frozen=True)
class InboundCommercialCaseAdmissionResult:
    """Canonical draft-only admission outcome for one Chatwoot conversation."""

    outcome: str
    commercial_case_id: str
    contact_id: str
    channel_identity_id: str
    conversation_id: str
    automation_status: str


@dataclass(frozen=True)
class CartRecoveryPlan:
    """Durable case, sequence, and next action created by PostgreSQL."""

    recovery_case_id: str
    followup_sequence_id: str
    scheduled_action_id: str
    created: bool


@dataclass(frozen=True)
class PilotBoundaryConfig:
    """Non-secret identity of one published pilot scope."""

    scope_key: str
    scope_version: int
    tenant_key: str
    channel_provider: str
    channel_account_ref: str


@dataclass(frozen=True)
class PilotRuntimeStatus:
    configured: bool
    runtime_state: str | None
    runtime_generation: int | None
    reason_code: str


@dataclass(frozen=True)
class PurchaseCorrelationResult:
    """Authoritative outcome of applying one approved purchase."""

    outcome: str
    recovery_case_id: str | None
    matched_by: str | None


@dataclass(frozen=True)
class PurchaseIntentCorrelationResult:
    """Durable identity outcome for one Hotmart event and purchase intent."""

    outcome: str
    purchase_intent_id: str | None
    matched_by: str | None
    candidate_count: int
    manual_handoff_required: bool


@dataclass(frozen=True)
class HotmartAbandonmentReevaluationResult:
    """Terminal or replayed result of one durable abandonment timer."""

    reevaluation_id: str
    status: str
    outcome: str
    completed_at: str
    replayed: bool


@dataclass(frozen=True)
class PrecheckoutDelayedOneShotCommand:
    """Exact sender projection for one reserved delayed precheckout command."""

    command_id: str
    command_status: str
    target_phone: str
    buyer_name: str | None
    buyer_email: str | None
    product_name: str | None
    template_name: str
    template_language: str
    template_category: str
    copy_version: str
    send_authorized: bool
    authorization_reason: str | None


@dataclass(frozen=True)
class InboundOptOutResult:
    """Authoritative outcome of one canonical inbound opt-out."""

    outcome: str
    opt_out_event_id: str
    contact_id: str | None
    affected_cases: int
    affected_actions: int
    affected_attempts: int


@dataclass(frozen=True)
class OptOutProjectionClaim:
    opt_out_event_id: str
    chatwoot_account_id: int
    chatwoot_inbox_id: int
    chatwoot_conversation_id: int
    external_user_id: str
    lease_generation: int


@dataclass(frozen=True)
class HumanHandoffProjectionClaim:
    effect_id: str
    handoff_request_id: str
    effect_kind: str
    current_effect_status: str
    attempt_count: int
    lease_generation: int
    expected_team_id: int
    chatwoot_account_id: int
    chatwoot_inbox_id: int
    chatwoot_conversation_id: int
    external_user_id: str
    private_note_body: str
    idempotency_marker: str


@dataclass(frozen=True)
class HumanHandoffProjectionFinalization:
    effect_status: str
    handoff_status: str


@dataclass(frozen=True)
class HumanHandoffRequestResult:
    outcome: str
    handoff_request_id: str
    affected_actions: int
    affected_attempts: int


@dataclass(frozen=True)
class HumanHandoffProjectionStatus:
    pending_count: int
    retryable_count: int
    delivery_unknown_count: int
    conflict_count: int
    dead_letter_count: int


@dataclass(frozen=True)
class PrecheckoutDelayedFirstTouchReadiness:
    migration_tracking_complete: bool
    scope_configured: bool
    runtime_state: str | None
    runtime_generation: int | None
    timer_binding_enabled: bool
    timer_binding_generation: int | None
    first_touch_binding_enabled: bool
    due_count: int
    reserved_count: int
    request_started_count: int
    delivery_unknown_count: int
    reason_code: str


@dataclass(frozen=True)
class ScheduledAction:
    """An action atomically claimed by a dispatcher lease."""

    action_id: str
    recovery_case_id: str
    followup_sequence_id: str
    action_type: str
    status: str
    due_at: str
    expires_at: str
    expected_case_version: int
    policy_key: str
    policy_version: int
    step_key: str
    anchor_type: str
    anchor_subject_internal_id: str
    anchor_observed_at: str
    lease_owner: str
    lease_generation: int
    lease_expires_at: str
    idempotency_key: str


@dataclass(frozen=True)
class ChatwootAuthorityContext:
    """Fenced external identifiers required for a canonical Chatwoot read."""

    action_id: str
    action_type: str
    chatwoot_account_id: int | None
    external_conversation_id: int | None
    expected_inbox_id: int | None
    anchor_external_message_id: int | None


@dataclass(frozen=True)
class FollowupExecutionContext:
    """Minimal fenced commercial context used to prepare one proposal."""

    action_id: str
    action_type: str
    step_key: str
    recovery_case_id: str
    contact_id: str
    source_event_id: str
    buyer_name: str | None
    buyer_email: str | None
    buyer_phone: str | None
    product_name: str
    offer_code: str | None
    current_goal: str | None
    lead_stage: str


@dataclass(frozen=True)
class ReevaluationDecision:
    """Authoritative result for one leased action."""

    action_id: str
    decision: str
    reason_code: str
    case_version: int
    sequence_revision: int


@dataclass(frozen=True)
class DeliveryAttempt:
    """A fenced outbound attempt reserved before an external request."""

    attempt_id: str
    action_id: str
    idempotency_key: str
    attempt_number: int
    channel: str
    mode: str
    phase: str
    lease_generation: int
    expected_case_version: int
    expected_sequence_revision: int


@dataclass(frozen=True)
class DeliveryFinalization:
    """Durable action projection returned after finalizing an effect attempt."""

    action_id: str
    status: str
    terminal_reason: str | None


@dataclass(frozen=True)
class ContactMatch:
    """Result of looking up a contact by email or phone."""

    contact_id: str
    full_name: str | None
    email: str | None
    phone: str | None
    contact_permission: str
    lifecycle_status: str
    matched_by: str  # "email" | "phone"


@dataclass(frozen=True)
class ConversationSummary:
    """Summary of an existing conversation for the agent."""

    conversation_id: str
    status: str
    automation_status: str
    human_takeover: bool
    last_message_direction: str | None
    last_inbound_at: str | None
    last_outbound_at: str | None
    paused_until: str | None


@dataclass(frozen=True)
class RecoveryCaseSummary:
    """Summary of an existing recovery case for the agent."""

    recovery_case_id: str
    status: str
    lead_stage: str
    current_goal: str | None
    product_name: str | None


@dataclass(frozen=True)
class ChannelIdentitySummary:
    """Summary of a channel identity for the agent."""

    channel_identity_id: str
    channel: str
    external_user_id: str | None
    identity_status: str


@dataclass
class SituationReport:
    """Structured report built by the bridge for the agent.

    Contains all the information the agent needs to decide how to proceed,
    without giving the agent direct access to Supabase.
    """

    event_id: str
    event_type: str
    source: str
    buyer_name: str | None
    buyer_email: str | None
    buyer_phone: str | None
    product_name: str | None
    offer_code: str | None
    checkout_country_iso: str | None
    contact_id: str | None
    contact_match: ContactMatch | None
    identity_resolution_status: str
    identity_resolution_strategy: str | None
    conversations: list[ConversationSummary] = field(default_factory=list)
    recovery_cases: list[RecoveryCaseSummary] = field(default_factory=list)
    channel_identities: list[ChannelIdentitySummary] = field(default_factory=list)
    authoritative_context_complete: bool = False
    any_conversation_human_takeover: bool = False
    has_active_conversation: bool = False
    has_open_recovery_case: bool = False
    phone_available: bool = False
    contact_blocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for the agent context."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source": self.source,
            "buyer_name": self.buyer_name,
            "buyer_email": self.buyer_email,
            "buyer_phone": self.buyer_phone,
            "product_name": self.product_name,
            "offer_code": self.offer_code,
            "checkout_country_iso": self.checkout_country_iso,
            "contact_id": self.contact_id,
            "contact_match": (
                {
                    "matched_by": self.contact_match.matched_by,
                    "full_name": self.contact_match.full_name,
                    "email": self.contact_match.email,
                    "phone": self.contact_match.phone,
                    "contact_permission": self.contact_match.contact_permission,
                    "lifecycle_status": self.contact_match.lifecycle_status,
                }
                if self.contact_match is not None
                else None
            ),
            "identity_resolution_status": self.identity_resolution_status,
            "identity_resolution_strategy": self.identity_resolution_strategy,
            "conversations": [
                {
                    "conversation_id": c.conversation_id,
                    "status": c.status,
                    "automation_status": c.automation_status,
                    "human_takeover": c.human_takeover,
                    "last_message_direction": c.last_message_direction,
                    "last_inbound_at": c.last_inbound_at,
                    "last_outbound_at": c.last_outbound_at,
                    "paused_until": c.paused_until,
                }
                for c in self.conversations
            ],
            "recovery_cases": [
                {
                    "recovery_case_id": rc.recovery_case_id,
                    "status": rc.status,
                    "lead_stage": rc.lead_stage,
                    "current_goal": rc.current_goal,
                    "product_name": rc.product_name,
                }
                for rc in self.recovery_cases
            ],
            "channel_identities": [
                {
                    "channel_identity_id": ci.channel_identity_id,
                    "channel": ci.channel,
                    "external_user_id": ci.external_user_id,
                    "identity_status": ci.identity_status,
                }
                for ci in self.channel_identities
            ],
            "authoritative_context_complete": (
                self.authoritative_context_complete
            ),
            "any_conversation_human_takeover": (
                self.any_conversation_human_takeover
            ),
            "has_active_conversation": self.has_active_conversation,
            "has_open_recovery_case": self.has_open_recovery_case,
            "phone_available": self.phone_available,
            "contact_blocked": self.contact_blocked,
        }


_CONTACT_PERMISSIONS = {
    "unknown", "allowed", "opted_out", "blocked", "restricted",
}
_LIFECYCLE_STATUSES = {
    "lead", "qualified_lead", "opportunity", "customer", "nurture",
    "unqualified", "closed_lost", "do_not_contact",
}
_CONVERSATION_STATUSES = {
    "active", "awaiting_agent", "awaiting_contact", "snoozed",
    "paused_human", "completed", "closed", "blocked",
}
_AUTOMATION_STATUSES = {
    "enabled", "draft_only", "paused", "disabled", "restricted", "error",
}
_RECOVERY_CASE_STATUSES = {
    "grace_period", "active", "paused", "won", "sequence_exhausted",
    "lost", "cancelled", "unreachable", "error",
}
_LEAD_STAGES = {
    "new", "discovery", "qualifying", "qualified", "solution_presented",
    "proposal_pending", "objection_handling", "booking_pending", "booked",
    "nurture", "won", "lost", "unqualified",
}
_CHANNELS = {"instagram", "whatsapp", "email", "sms", "other"}
_IDENTITY_STATUSES = {"active", "unreachable", "blocked", "unknown"}


def _response_rows(
    response: httpx.Response,
    *,
    operation: str,
) -> list[dict[str, Any]]:
    """Parse an authoritative PostgREST list response or fail closed."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise SupabaseError(f"{operation}_invalid_json") from exc
    if not isinstance(payload, list) or any(
        not isinstance(row, dict) for row in payload
    ):
        raise SupabaseError(f"{operation}_invalid_shape")
    return payload


_COMMITTED_HANDOFF_OPERATIONS = {
    "claim_human_handoff_projection_effects",
    "request_inbound_human_handoff",
    "request_human_handoff",
    "finalize_human_handoff_projection_effect",
}


def _invalid_row_error(operation: str) -> SupabaseError:
    error_type = (
        SupabaseCommittedResponseError
        if operation in _COMMITTED_HANDOFF_OPERATIONS
        else SupabaseError
    )
    return error_type(f"{operation}_invalid_row")


def _committed_response_rows(
    response: httpx.Response, *, operation: str
) -> list[dict[str, Any]]:
    try:
        return _response_rows(response, operation=operation)
    except SupabaseError as exc:
        raise SupabaseCommittedResponseError(
            f"{operation}_committed_response_invalid"
        ) from exc


def _required_string(
    row: dict[str, Any],
    key: str,
    *,
    operation: str,
) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise _invalid_row_error(operation)
    return value


def _required_enum(
    row: dict[str, Any],
    key: str,
    allowed: set[str],
    *,
    operation: str,
) -> str:
    value = _required_string(row, key, operation=operation)
    if value not in allowed:
        raise _invalid_row_error(operation)
    return value


def _optional_string(
    row: dict[str, Any],
    key: str,
    *,
    operation: str,
) -> str | None:
    value = row.get(key)
    if value is not None and not isinstance(value, str):
        raise _invalid_row_error(operation)
    return value


def _optional_enum(
    row: dict[str, Any],
    key: str,
    allowed: set[str],
    *,
    operation: str,
) -> str | None:
    value = _optional_string(row, key, operation=operation)
    if value is not None and value not in allowed:
        raise _invalid_row_error(operation)
    return value


def _required_int(
    row: dict[str, Any],
    key: str,
    *,
    operation: str,
) -> int:
    value = row.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _invalid_row_error(operation)
    return value


def _required_bool(
    row: dict[str, Any], key: str, *, operation: str
) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise _invalid_row_error(operation)
    return value


def _required_positive_int(
    row: dict[str, Any], key: str, *, operation: str
) -> int:
    value = _required_int(row, key, operation=operation)
    if value < 1:
        raise _invalid_row_error(operation)
    return value


def _required_nonnegative_int(
    row: dict[str, Any], key: str, *, operation: str
) -> int:
    value = _required_int(row, key, operation=operation)
    if value < 0:
        raise _invalid_row_error(operation)
    return value


def _required_uuid(
    row: dict[str, Any], key: str, *, operation: str
) -> str:
    value = _required_string(row, key, operation=operation)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise _invalid_row_error(operation) from exc
    return str(parsed)


def _optional_positive_int(
    row: dict[str, Any], key: str, *, operation: str
) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _invalid_row_error(operation)
    return value


def _optional_chatwoot_account_id(
    row: dict[str, Any], key: str, *, operation: str
) -> int | None:
    value = row.get(key)
    if isinstance(value, str) and value.startswith("chatwoot:"):
        value = value.removeprefix("chatwoot:")
    return _optional_positive_int({key: value}, key, operation=operation)


# ── Client ───────────────────────────────────────────────────────────


class SupabaseClient:
    """REST (PostgREST) client for Supabase — read and write side."""

    def __init__(
        self,
        *,
        base_url: str,
        service_role_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_role_key = service_role_key
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    def _headers(self, *, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._service_role_key,
            "Authorization": f"Bearer {self._service_role_key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        content: str | None = None,
        prefer: str | None = None,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers(prefer=prefer),
                transport=self._transport,
                timeout=self._timeout_seconds,
            ) as client:
                return await client.request(
                    method, path, params=params, content=content
                )
        except httpx.HTTPError as exc:
            raise SupabaseError(f"supabase_request_failed: {method} {path}") from exc

    async def resolve_commercial_ally_runtime_binding(
        self,
        expected: CommercialAllyConfig,
    ) -> CommercialAllyConfig:
        """Resolve and verify the exact active durable binding for this runtime."""

        operation = "commercial_ally_runtime_binding_resolve"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/resolve_commercial_ally_runtime_binding",
            content=json.dumps(
                {
                    "p_tenant_ref": expected.tenant_ref,
                    "p_funnel_ref": expected.funnel_ref,
                    "p_binding_version": expected.binding_version,
                }
            ),
        )
        rows = _response_rows(response, operation=operation)
        if response.status_code != 200 or len(rows) != 1:
            raise SupabaseError(f"{operation}_not_active")
        row = rows[0]
        supported = {item.name for item in fields(CommercialAllyConfig)}
        if any(name not in row for name in supported) or row.get("status") != "active":
            raise SupabaseError(f"{operation}_invalid_row")
        values = {name: row[name] for name in supported}
        try:
            price = values["product_price"]
            if isinstance(price, bool) or not isinstance(price, (str, int, float)):
                raise ValueError
            values["product_price"] = Decimal(str(price))
            resolved = CommercialAllyConfig(**values)
        except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
            raise SupabaseError(f"{operation}_invalid_row") from exc
        if resolved != expected:
            raise SupabaseError(f"{operation}_config_drift")
        return resolved

    async def resolve_commercial_ally_discount_policy(
        self,
        *,
        tenant_ref: str,
        funnel_ref: str,
        binding_version: int,
        trigger_kind: str,
        expected_policy_key: str,
        expected_policy_version: int,
    ) -> CommercialAllyDiscountPolicy:
        """Resolve one exact active, published discount policy."""

        operation = "discount_policy_resolve"
        if (
            type(binding_version) is not int
            or binding_version < 1
            or type(expected_policy_version) is not int
            or expected_policy_version < 1
            or any(
                not isinstance(value, str) or not value.strip()
                for value in (
                    tenant_ref,
                    funnel_ref,
                    trigger_kind,
                    expected_policy_key,
                )
            )
        ):
            raise SupabaseError(f"{operation}_invalid_request")
        response = await self._request(
            "POST",
            "/rest/v1/rpc/resolve_commercial_ally_discount_policy",
            content=json.dumps({
                "p_tenant_ref": tenant_ref,
                "p_funnel_ref": funnel_ref,
                "p_binding_version": binding_version,
                "p_trigger_kind": trigger_kind,
            }),
        )
        rows = _response_rows(response, operation=operation)
        if response.status_code != 200 or len(rows) != 1:
            raise SupabaseError(f"{operation}_not_published")
        row = rows[0]
        expected_fields = {item.name for item in fields(CommercialAllyDiscountPolicy)}
        if set(row) != expected_fields:
            raise SupabaseError(f"{operation}_invalid_row")
        values = dict(row)
        try:
            raw_discount = values["discount_value"]
            if isinstance(raw_discount, bool) or not isinstance(
                raw_discount, (str, int, float)
            ):
                raise ValueError
            values["discount_value"] = Decimal(str(raw_discount))
            policy = CommercialAllyDiscountPolicy(**values)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise SupabaseError(f"{operation}_invalid_row") from exc
        if (
            policy.policy_key != expected_policy_key
            or policy.policy_version != expected_policy_version
            or policy.trigger_kind != trigger_kind
        ):
            raise SupabaseError(f"{operation}_config_drift")
        return policy

    # ── Webhook event persistence ──────────────────────────────────

    async def insert_webhook_event(
        self,
        *,
        source: str,
        external_event_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> InsertResult:
        """Insert a webhook event, silently skipping duplicates."""
        body = json.dumps(
            {
                "source": source,
                "external_event_id": external_event_id,
                "event_type": event_type,
                "payload": payload,
                "processing_status": "received",
            },
            ensure_ascii=False,
        )
        response = await self._request(
            "POST",
            "/rest/v1/webhook_events",
            content=body,
            prefer="return=representation,resolution=ignore-duplicates",
        )
        if response.status_code == 409:
            return InsertResult(inserted=False)
        if response.status_code != 201:
            raise SupabaseError(
                f"webhook_event_insert_failed: HTTP {response.status_code}"
            )
        try:
            rows = response.json()
        except ValueError as exc:
            raise SupabaseError("webhook_event_insert_invalid_json") from exc
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) for row in rows
        ):
            raise SupabaseError("webhook_event_insert_invalid_shape")
        if len(rows) == 0:
            return InsertResult(inserted=False)
        if len(rows) != 1:
            raise SupabaseError("webhook_event_insert_invalid_cardinality")
        return InsertResult(inserted=True)

    async def admit_and_correlate_hotmart_purchase_approved(
        self,
        *,
        external_event_id: str,
        payload: dict[str, Any],
        normalized_email: str | None,
        normalized_phone: str | None,
    ) -> PurchaseAdmissionResult:
        """Atomically admit a purchase and correlate its canonical identity."""
        operation = "purchase_correlation_admission"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/admit_and_correlate_hotmart_purchase_approved",
            content=json.dumps(
                {
                    "p_external_event_id": external_event_id,
                    "p_payload": payload,
                    "p_normalized_email": normalized_email,
                    "p_normalized_phone": normalized_phone,
                },
                ensure_ascii=False,
            ),
        )
        rows = _response_rows(response, operation=operation)
        if response.status_code != 200 or len(rows) != 1:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        outcome = rows[0].get("outcome")
        webhook_event_id = rows[0].get("webhook_event_id")
        if outcome not in {"inserted", "duplicate", "semantic_conflict"}:
            raise SupabaseError(f"{operation}_invalid_outcome")
        if not isinstance(webhook_event_id, str) or not webhook_event_id:
            raise SupabaseError(f"{operation}_invalid_event_id")
        return PurchaseAdmissionResult(outcome, webhook_event_id)

    async def admit_portable_hotmart_purchase_approved(
        self,
        *,
        config: CommercialAllyConfig,
        external_event_id: str,
        payload: dict[str, Any],
        normalized_email: str | None,
        normalized_phone: str | None,
    ) -> PurchaseAdmissionResult:
        """Admit an approved purchase against an exact durable ally binding."""
        operation = "portable_purchase_stop_admission"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/admit_portable_hotmart_purchase_approved",
            content=json.dumps(
                {
                    "p_tenant_ref": config.tenant_ref,
                    "p_funnel_ref": config.funnel_ref,
                    "p_binding_version": config.binding_version,
                    "p_external_event_id": external_event_id,
                    "p_payload": payload,
                    "p_normalized_email": normalized_email,
                    "p_normalized_phone": normalized_phone,
                },
                ensure_ascii=False,
            ),
        )
        rows = _response_rows(response, operation=operation)
        if response.status_code != 200 or len(rows) != 1:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        outcome = rows[0].get("outcome")
        webhook_event_id = rows[0].get("webhook_event_id")
        if outcome not in {"inserted", "duplicate", "semantic_conflict"}:
            raise SupabaseError(f"{operation}_invalid_outcome")
        if not isinstance(webhook_event_id, str) or not webhook_event_id:
            raise SupabaseError(f"{operation}_invalid_event_id")
        return PurchaseAdmissionResult(outcome, webhook_event_id)

    async def admit_portable_hotmart_cart_abandonment(
        self,
        *,
        config: CommercialAllyConfig,
        external_event_id: str,
        payload: dict[str, Any],
        normalized_email: str | None,
        normalized_phone: str | None,
    ) -> CartAbandonmentAdmissionResult:
        """Admit a cart event against an exact durable ally binding."""
        operation = "portable_cart_abandonment_admission"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/admit_portable_hotmart_cart_abandonment",
            content=json.dumps(
                {
                    "p_tenant_ref": config.tenant_ref,
                    "p_funnel_ref": config.funnel_ref,
                    "p_binding_version": config.binding_version,
                    "p_external_event_id": external_event_id,
                    "p_payload": payload,
                    "p_normalized_email": normalized_email,
                    "p_normalized_phone": normalized_phone,
                },
                ensure_ascii=False,
            ),
        )
        rows = _response_rows(response, operation=operation)
        if response.status_code != 200 or len(rows) != 1:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        row = rows[0]
        if set(row) != {"outcome", "webhook_event_id"}:
            raise _invalid_row_error(operation)
        outcome = row.get("outcome")
        if outcome not in {"inserted", "duplicate", "semantic_conflict"}:
            raise SupabaseError(f"{operation}_invalid_outcome")
        webhook_event_id = _required_uuid(
            row, "webhook_event_id", operation=operation
        )
        return CartAbandonmentAdmissionResult(outcome, webhook_event_id)

    async def admit_portable_hotmart_payment_failure(
        self,
        *,
        config: CommercialAllyConfig,
        external_event_id: str,
        payload: dict[str, Any],
        normalized_email: str | None,
        normalized_phone: str | None,
    ) -> PortablePaymentFailureAdmissionResult:
        """Admit a payment failure against an exact durable ally binding."""
        operation = "portable_payment_failure_admission"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/admit_portable_hotmart_payment_failure",
            content=json.dumps(
                {
                    "p_tenant_ref": config.tenant_ref,
                    "p_funnel_ref": config.funnel_ref,
                    "p_binding_version": config.binding_version,
                    "p_external_event_id": external_event_id,
                    "p_payload": payload,
                    "p_normalized_email": normalized_email,
                    "p_normalized_phone": normalized_phone,
                },
                ensure_ascii=False,
            ),
        )
        rows = _response_rows(response, operation=operation)
        if response.status_code != 200 or len(rows) != 1:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        row = rows[0]
        if set(row) != {"outcome", "webhook_event_id"}:
            raise _invalid_row_error(operation)
        outcome = row.get("outcome")
        if outcome not in {"inserted", "duplicate", "semantic_conflict"}:
            raise SupabaseError(f"{operation}_invalid_outcome")
        webhook_event_id = _required_uuid(
            row, "webhook_event_id", operation=operation
        )
        return PortablePaymentFailureAdmissionResult(outcome, webhook_event_id)

    async def admit_precheckout_form_submission(
        self,
        *,
        external_submission_id: str,
        raw_payload: dict[str, object],
        canonical_payload: dict[str, object],
    ) -> PrecheckoutAdmissionResult:
        """Atomically admit an emulated submission and its purchase intent."""
        response = await self._request(
            "POST",
            "/rest/v1/rpc/admit_precheckout_form_submission",
            content=json.dumps(
                {
                    "p_external_submission_id": external_submission_id,
                    "p_raw_payload": raw_payload,
                    "p_canonical_payload": canonical_payload,
                },
                ensure_ascii=False,
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"precheckout_admission_failed: HTTP {response.status_code}"
            )
        rows = _response_rows(response, operation="precheckout_admission")
        if len(rows) != 1:
            raise SupabaseError("precheckout_admission_invalid_shape")
        outcome = rows[0].get("outcome")
        submission_id = rows[0].get("submission_id")
        purchase_intent_id = rows[0].get("purchase_intent_id")
        if outcome not in {"inserted", "duplicate", "semantic_conflict"}:
            raise SupabaseError("precheckout_admission_invalid_outcome")
        if not isinstance(submission_id, str) or not submission_id:
            raise SupabaseError("precheckout_admission_invalid_submission_id")
        if not isinstance(purchase_intent_id, str) or not purchase_intent_id:
            raise SupabaseError("precheckout_admission_invalid_purchase_intent_id")
        return PrecheckoutAdmissionResult(
            outcome=outcome,
            submission_id=submission_id,
            purchase_intent_id=purchase_intent_id,
        )

    async def admit_observed_lead_precheckout(
        self,
        *,
        external_submission_id: str,
        raw_payload: dict[str, object],
        canonical_payload: dict[str, object],
    ) -> PrecheckoutAdmissionResult:
        """Atomically admit an authenticated Lancemos lead intent."""
        response = await self._request(
            "POST",
            "/rest/v1/rpc/admit_observed_lead_precheckout",
            content=json.dumps(
                {
                    "p_external_submission_id": external_submission_id,
                    "p_raw_payload": raw_payload,
                    "p_canonical_payload": canonical_payload,
                },
                ensure_ascii=False,
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"observed_lead_precheckout_admission_failed: HTTP {response.status_code}"
            )
        rows = _response_rows(response, operation="observed_lead_precheckout_admission")
        if len(rows) != 1:
            raise SupabaseError("observed_lead_precheckout_admission_invalid_shape")
        outcome = rows[0].get("outcome")
        submission_id = rows[0].get("submission_id")
        purchase_intent_id = rows[0].get("purchase_intent_id")
        if outcome not in {"inserted", "duplicate", "semantic_conflict"}:
            raise SupabaseError("observed_lead_precheckout_admission_invalid_outcome")
        if not isinstance(submission_id, str) or not submission_id:
            raise SupabaseError("observed_lead_precheckout_admission_invalid_submission_id")
        if not isinstance(purchase_intent_id, str) or not purchase_intent_id:
            raise SupabaseError("observed_lead_precheckout_admission_invalid_purchase_intent_id")
        return PrecheckoutAdmissionResult(
            outcome=outcome,
            submission_id=submission_id,
            purchase_intent_id=purchase_intent_id,
        )

    async def admit_portable_observed_lead_precheckout(
        self,
        *,
        config: CommercialAllyConfig,
        external_submission_id: str,
        raw_payload: dict[str, object],
        canonical_payload: dict[str, object],
    ) -> PrecheckoutAdmissionResult:
        """Admit a lead against the runtime's exact durable binding identity."""
        operation = "portable_observed_lead_precheckout_admission"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/admit_portable_observed_lead_precheckout",
            content=json.dumps(
                {
                    "p_tenant_ref": config.tenant_ref,
                    "p_funnel_ref": config.funnel_ref,
                    "p_binding_version": config.binding_version,
                    "p_external_submission_id": external_submission_id,
                    "p_raw_payload": raw_payload,
                    "p_canonical_payload": canonical_payload,
                },
                ensure_ascii=False,
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        outcome = row.get("outcome")
        submission_id = row.get("submission_id")
        purchase_intent_id = row.get("purchase_intent_id")
        if outcome not in {"inserted", "duplicate", "semantic_conflict"}:
            raise SupabaseError(f"{operation}_invalid_outcome")
        if not isinstance(submission_id, str) or not submission_id:
            raise SupabaseError(f"{operation}_invalid_submission_id")
        if not isinstance(purchase_intent_id, str) or not purchase_intent_id:
            raise SupabaseError(f"{operation}_invalid_purchase_intent_id")
        return PrecheckoutAdmissionResult(
            outcome=outcome,
            submission_id=submission_id,
            purchase_intent_id=purchase_intent_id,
        )

    async def begin_precheckout_test_first_touch(
        self,
        *,
        command_key: str,
        purchase_intent_id: str,
        allowed_external_user_id: str,
        chatwoot_account_id: int,
        chatwoot_inbox_id: int,
    ) -> PrecheckoutFirstTouchStart:
        operation = "precheckout_first_touch_begin"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/begin_precheckout_test_first_touch",
            content=json.dumps(
                {
                    "p_command_key": command_key,
                    "p_purchase_intent_id": purchase_intent_id,
                    "p_allowed_external_user_id": allowed_external_user_id,
                    "p_chatwoot_account_id": chatwoot_account_id,
                    "p_chatwoot_inbox_id": chatwoot_inbox_id,
                }
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        required_text = (
            "command_id",
            "command_status",
            "target_phone",
            "buyer_name",
            "template_name",
            "template_language",
            "template_category",
            "copy_version",
        )
        if row.get("outcome") not in {"started", "replay"} or any(
            not isinstance(row.get(field), str) or not row[field]
            for field in required_text
        ):
            raise SupabaseError(f"{operation}_invalid_row")
        conversation_id = row.get("chatwoot_conversation_id")
        if (
            not isinstance(conversation_id, int)
            or isinstance(conversation_id, bool)
            or conversation_id < 1
        ):
            raise SupabaseError(f"{operation}_invalid_conversation_id")
        return PrecheckoutFirstTouchStart(
            outcome=row["outcome"],
            command_id=row["command_id"],
            command_status=row["command_status"],
            target_phone=row["target_phone"],
            buyer_name=row["buyer_name"],
            chatwoot_conversation_id=conversation_id,
            template_name=row["template_name"],
            template_language=row["template_language"],
            template_category=row["template_category"],
            copy_version=row["copy_version"],
        )

    async def finish_precheckout_test_first_touch(
        self,
        *,
        command_id: str,
        outcome: str,
        chatwoot_conversation_id: int | None,
        chatwoot_message_id: int | None,
        failure_code: str | None,
    ) -> PrecheckoutFirstTouchFinish:
        operation = "precheckout_first_touch_finish"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/finish_precheckout_test_first_touch",
            content=json.dumps(
                {
                    "p_command_id": command_id,
                    "p_outcome": outcome,
                    "p_chatwoot_conversation_id": chatwoot_conversation_id,
                    "p_chatwoot_message_id": chatwoot_message_id,
                    "p_failure_code": failure_code,
                }
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        result_id = rows[0].get("command_id")
        result_status = rows[0].get("command_status")
        if not isinstance(result_id, str) or not isinstance(result_status, str):
            raise SupabaseError(f"{operation}_invalid_row")
        return PrecheckoutFirstTouchFinish(result_id, result_status)

    async def begin_johanna_abandonment_one_shot(
        self,
        *,
        command_key: str,
        purchase_intent_id: str,
        allowed_external_user_id: str,
        chatwoot_account_id: int,
        chatwoot_inbox_id: int,
        scope_key: str,
        scope_version: int,
        expected_generation: int,
    ) -> JohannaAbandonmentOneShotStart:
        operation = "johanna_abandonment_one_shot_begin"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/begin_johanna_abandonment_one_shot",
            content=json.dumps(
                {
                    "p_command_key": command_key,
                    "p_purchase_intent_id": purchase_intent_id,
                    "p_allowed_external_user_id": allowed_external_user_id,
                    "p_chatwoot_account_id": chatwoot_account_id,
                    "p_chatwoot_inbox_id": chatwoot_inbox_id,
                    "p_scope_key": scope_key,
                    "p_scope_version": scope_version,
                    "p_expected_generation": expected_generation,
                }
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        required_text = (
            "command_id",
            "command_status",
            "target_phone",
            "buyer_name",
            "buyer_email",
            "product_name",
            "template_name",
            "template_language",
            "template_category",
            "copy_version",
        )
        if row.get("outcome") not in {"started", "replay"} or any(
            not isinstance(row.get(field), str) or not row[field]
            for field in required_text
        ):
            raise SupabaseError(f"{operation}_invalid_row")
        return JohannaAbandonmentOneShotStart(
            outcome=row["outcome"],
            command_id=row["command_id"],
            command_status=row["command_status"],
            target_phone=row["target_phone"],
            buyer_name=row["buyer_name"],
            buyer_email=row["buyer_email"],
            product_name=row["product_name"],
            template_name=row["template_name"],
            template_language=row["template_language"],
            template_category=row["template_category"],
            copy_version=row["copy_version"],
        )

    async def begin_johanna_abandonment_hotmart_auto(
        self,
        *,
        command_key: str,
        hotmart_webhook_event_id: str,
        purchase_intent_id: str,
        chatwoot_account_id: int,
        chatwoot_inbox_id: int,
        scope_key: str,
        scope_version: int,
        expected_generation: int,
    ) -> JohannaAbandonmentOneShotStart:
        operation = "johanna_abandonment_hotmart_auto_begin"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/begin_johanna_abandonment_hotmart_auto_v2",
            content=json.dumps(
                {
                    "p_command_key": command_key,
                    "p_hotmart_webhook_event_id": hotmart_webhook_event_id,
                    "p_purchase_intent_id": purchase_intent_id,
                    "p_chatwoot_account_id": chatwoot_account_id,
                    "p_chatwoot_inbox_id": chatwoot_inbox_id,
                    "p_scope_key": scope_key,
                    "p_scope_version": scope_version,
                    "p_expected_generation": expected_generation,
                }
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        required_text = (
            "command_id",
            "command_status",
            "target_phone",
            "buyer_name",
            "buyer_email",
            "product_name",
            "template_name",
            "template_language",
            "template_category",
            "copy_version",
        )
        if row.get("outcome") not in {
            "started",
            "replay",
            "budget_consumed",
        } or any(
            not isinstance(row.get(field), str) or not row[field]
            for field in required_text
        ):
            raise SupabaseError(f"{operation}_invalid_row")
        return JohannaAbandonmentOneShotStart(
            outcome=row["outcome"],
            command_id=row["command_id"],
            command_status=row["command_status"],
            target_phone=row["target_phone"],
            buyer_name=row["buyer_name"],
            buyer_email=row["buyer_email"],
            product_name=row["product_name"],
            template_name=row["template_name"],
            template_language=row["template_language"],
            template_category=row["template_category"],
            copy_version=row["copy_version"],
        )

    async def begin_johanna_payment_failure_hotmart_auto(
        self,
        *,
        command_key: str,
        payment_failure_case_id: str,
        chatwoot_account_id: int,
        chatwoot_inbox_id: int,
    ) -> JohannaAbandonmentOneShotStart:
        operation = "johanna_payment_failure_hotmart_auto_begin"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/begin_johanna_payment_failure_hotmart_auto",
            content=json.dumps(
                {
                    "p_command_key": command_key,
                    "p_payment_failure_case_id": payment_failure_case_id,
                    "p_chatwoot_account_id": chatwoot_account_id,
                    "p_chatwoot_inbox_id": chatwoot_inbox_id,
                }
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        required_text = (
            "command_id",
            "command_status",
            "target_phone",
            "buyer_name",
            "buyer_email",
            "product_name",
            "template_name",
            "template_language",
            "template_category",
            "copy_version",
        )
        if row.get("outcome") not in {
            "started",
            "replay",
            "budget_consumed",
        } or any(
            not isinstance(row.get(field), str) or not row[field]
            for field in required_text
        ):
            raise SupabaseError(f"{operation}_invalid_row")
        return JohannaAbandonmentOneShotStart(
            outcome=row["outcome"],
            command_id=row["command_id"],
            command_status=row["command_status"],
            target_phone=row["target_phone"],
            buyer_name=row["buyer_name"],
            buyer_email=row["buyer_email"],
            product_name=row["product_name"],
            template_name=row["template_name"],
            template_language=row["template_language"],
            template_category=row["template_category"],
            copy_version=row["copy_version"],
        )

    async def prepare_johanna_payment_failure_invalid_contact_retry(
        self,
        *,
        command_key: str,
        payment_failure_case_id: str,
        chatwoot_account_id: int,
        chatwoot_inbox_id: int,
    ) -> JohannaAbandonmentOneShotStart:
        operation = "johanna_payment_failure_invalid_contact_retry_prepare"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/prepare_johanna_payment_failure_invalid_contact_retry",
            content=json.dumps(
                {
                    "p_command_key": command_key,
                    "p_payment_failure_case_id": payment_failure_case_id,
                    "p_chatwoot_account_id": chatwoot_account_id,
                    "p_chatwoot_inbox_id": chatwoot_inbox_id,
                }
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        required_text = (
            "command_id",
            "command_status",
            "target_phone",
            "buyer_name",
            "buyer_email",
            "product_name",
            "template_name",
            "template_language",
            "template_category",
            "copy_version",
        )
        if row.get("outcome") not in {
            "retry_started",
            "not_retryable",
        } or any(
            not isinstance(row.get(field), str) or not row[field]
            for field in required_text
        ):
            raise SupabaseError(f"{operation}_invalid_row")
        return JohannaAbandonmentOneShotStart(
            outcome=row["outcome"],
            command_id=row["command_id"],
            command_status=row["command_status"],
            target_phone=row["target_phone"],
            buyer_name=row["buyer_name"],
            buyer_email=row["buyer_email"],
            product_name=row["product_name"],
            template_name=row["template_name"],
            template_language=row["template_language"],
            template_category=row["template_category"],
            copy_version=row["copy_version"],
        )

    async def finish_johanna_abandonment_one_shot(
        self,
        *,
        command_id: str,
        outcome: str,
        chatwoot_conversation_id: int | None,
        chatwoot_message_id: int | None,
        failure_code: str | None,
    ) -> JohannaAbandonmentOneShotFinish:
        operation = "johanna_abandonment_one_shot_finish"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/finish_johanna_abandonment_one_shot",
            content=json.dumps(
                {
                    "p_command_id": command_id,
                    "p_outcome": outcome,
                    "p_chatwoot_conversation_id": chatwoot_conversation_id,
                    "p_chatwoot_message_id": chatwoot_message_id,
                    "p_failure_code": failure_code,
                }
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        result_id = rows[0].get("command_id")
        result_status = rows[0].get("command_status")
        if not isinstance(result_id, str) or not isinstance(result_status, str):
            raise SupabaseError(f"{operation}_invalid_row")
        return JohannaAbandonmentOneShotFinish(result_id, result_status)

    async def admit_johanna_payment_failure(
        self,
        *,
        external_event_id: str,
        payload: dict[str, Any],
        normalized_email: str | None,
        normalized_phone: str | None,
    ) -> PaymentFailureAdmissionResult:
        operation = "johanna_payment_failure_admission"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/admit_johanna_payment_failure",
            content=json.dumps(
                {
                    "p_external_event_id": external_event_id,
                    "p_payload": payload,
                    "p_normalized_email": normalized_email,
                    "p_normalized_phone": normalized_phone,
                },
                ensure_ascii=False,
            ),
        )
        rows = _response_rows(response, operation=operation)
        if response.status_code != 200 or len(rows) != 1:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        row = rows[0]
        outcome = row.get("outcome")
        case_id = row.get("payment_failure_case_id")
        correlation_outcome = row.get("correlation_outcome")
        case_status = row.get("case_status")
        if outcome not in {"inserted", "duplicate", "semantic_conflict"}:
            raise SupabaseError(f"{operation}_invalid_outcome")
        if not isinstance(case_id, str) or not case_id:
            raise SupabaseError(f"{operation}_invalid_case_id")
        if correlation_outcome not in {
            "resolved",
            "unmatched",
            "ambiguous",
            "conflict",
        }:
            raise SupabaseError(f"{operation}_invalid_correlation")
        if case_status not in {
            "pending_human_review",
            "outbound_started",
            "outbound_accepted",
            "delivery_unknown",
        }:
            raise SupabaseError(f"{operation}_invalid_status")
        return PaymentFailureAdmissionResult(
            outcome,
            case_id,
            correlation_outcome,
            case_status,
        )

    async def admit_and_correlate_hotmart_cart_abandonment(
        self,
        *,
        external_event_id: str,
        payload: dict[str, Any],
        normalized_email: str | None,
        normalized_phone: str | None,
    ) -> CartAbandonmentAdmissionResult:
        """Atomically admit an abandonment and correlate canonical identity."""
        operation = "cart_abandonment_correlation_admission"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/admit_johanna_hotmart_cart_abandonment",
            content=json.dumps(
                {
                    "p_external_event_id": external_event_id,
                    "p_payload": payload,
                    "p_normalized_email": normalized_email,
                    "p_normalized_phone": normalized_phone,
                },
                ensure_ascii=False,
            ),
        )
        rows = _response_rows(response, operation=operation)
        if response.status_code != 200 or len(rows) != 1:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        row = rows[0]
        if set(row) != {"outcome", "webhook_event_id"}:
            raise SupabaseError(f"{operation}_invalid_row")
        outcome = row.get("outcome")
        if outcome not in {"inserted", "duplicate", "semantic_conflict"}:
            raise SupabaseError(f"{operation}_invalid_outcome")
        webhook_event_id = _required_uuid(row, "webhook_event_id", operation=operation)
        return CartAbandonmentAdmissionResult(outcome, webhook_event_id)

    async def correlate_hotmart_purchase_intent(
        self,
        *,
        webhook_event_id: str,
    ) -> PurchaseIntentCorrelationResult:
        """Correlate one exact durable Hotmart event without creating effects."""
        operation = "hotmart_purchase_intent_correlation"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/correlate_hotmart_purchase_intent",
            content=json.dumps({"p_webhook_event_id": webhook_event_id}),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        correlation_outcome = row.get("outcome")
        if correlation_outcome not in {
            "resolved",
            "unmatched",
            "ambiguous",
            "conflict",
        }:
            raise SupabaseError(f"{operation}_invalid_outcome")
        purchase_intent_id = _optional_string(
            row, "purchase_intent_id", operation=operation
        )
        matched_by = _optional_enum(
            row,
            "matched_by",
            {"email", "phone", "email_and_phone"},
            operation=operation,
        )
        candidate_count = _required_nonnegative_int(
            row, "candidate_count", operation=operation
        )
        manual_handoff_required = row.get("manual_handoff_required")
        if not isinstance(manual_handoff_required, bool):
            raise SupabaseError(f"{operation}_invalid_row")
        if correlation_outcome == "resolved":
            if (
                purchase_intent_id is None
                or matched_by is None
                or candidate_count != 1
                or manual_handoff_required
            ):
                raise SupabaseError(f"{operation}_invalid_row")
        elif (
            purchase_intent_id is not None
            or matched_by is not None
            or not manual_handoff_required
        ):
            raise SupabaseError(f"{operation}_invalid_row")
        return PurchaseIntentCorrelationResult(
            outcome=correlation_outcome,
            purchase_intent_id=purchase_intent_id,
            matched_by=matched_by,
            candidate_count=candidate_count,
            manual_handoff_required=manual_handoff_required,
        )

    async def list_unresolved_purchase_intent_correlations(
        self,
        *,
        tenant_ref: str,
        funnel_ref: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Read unresolved correlation evidence without creating any effect."""
        if isinstance(limit, bool) or limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        tenant = _required_string(
            {"tenant_ref": tenant_ref},
            "tenant_ref",
            operation="operator_unresolved_correlation_list",
        )
        funnel = _required_string(
            {"funnel_ref": funnel_ref},
            "funnel_ref",
            operation="operator_unresolved_correlation_list",
        )
        return await self._read_operator_correlation_rpc(
            operation="operator_unresolved_correlation_list",
            path="/rest/v1/rpc/list_operator_unresolved_correlations",
            payload={
                "p_tenant_ref": tenant,
                "p_funnel_ref": funnel,
                "p_limit": limit,
                "p_webhook_event_id": None,
            },
        )

    async def get_unresolved_purchase_intent_correlation(
        self,
        *,
        tenant_ref: str,
        funnel_ref: str,
        webhook_event_id: str,
    ) -> dict[str, Any] | None:
        """Read one exact unresolved correlation, or return no result."""
        expected_id = _required_uuid(
            {"webhook_event_id": webhook_event_id},
            "webhook_event_id",
            operation="unresolved_purchase_intent_correlation_get",
        )
        tenant = _required_string(
            {"tenant_ref": tenant_ref},
            "tenant_ref",
            operation="operator_unresolved_correlation_get",
        )
        funnel = _required_string(
            {"funnel_ref": funnel_ref},
            "funnel_ref",
            operation="operator_unresolved_correlation_get",
        )
        rows = await self._read_operator_correlation_rpc(
            operation="operator_unresolved_correlation_get",
            path="/rest/v1/rpc/get_operator_unresolved_correlation",
            payload={
                "p_tenant_ref": tenant,
                "p_funnel_ref": funnel,
                "p_webhook_event_id": expected_id,
            },
        )
        if len(rows) > 1:
            raise SupabaseError("unresolved_purchase_intent_correlation_get_ambiguous")
        return rows[0] if rows else None

    async def prepare_operator_correlation_resolution(
        self,
        *,
        tenant_ref: str,
        funnel_ref: str,
        actor_ref: str,
        idempotency_key: str,
        webhook_event_id: str,
        action: str,
        selected_purchase_intent_id: str | None,
        verification_basis: str,
    ) -> dict[str, Any]:
        """Prepare an expiring command without applying a resolution."""
        operation = "operator_correlation_resolution_prepare"
        tenant = _required_string(
            {"tenant_ref": tenant_ref}, "tenant_ref", operation=operation
        )
        funnel = _required_string(
            {"funnel_ref": funnel_ref}, "funnel_ref", operation=operation
        )
        actor = _required_string(
            {"actor_ref": actor_ref}, "actor_ref", operation=operation
        )
        expected_idempotency_key = _required_uuid(
            {"idempotency_key": idempotency_key},
            "idempotency_key",
            operation=operation,
        )
        event_id = _required_uuid(
            {"webhook_event_id": webhook_event_id},
            "webhook_event_id",
            operation=operation,
        )
        candidate_id = None
        if selected_purchase_intent_id is not None:
            candidate_id = _required_uuid(
                {"selected_purchase_intent_id": selected_purchase_intent_id},
                "selected_purchase_intent_id",
                operation=operation,
            )
        response = await self._request(
            "POST",
            "/rest/v1/rpc/prepare_operator_correlation_resolution",
            content=json.dumps(
                {
                    "p_tenant_ref": tenant,
                    "p_funnel_ref": funnel,
                    "p_actor_ref": actor,
                    "p_idempotency_key": expected_idempotency_key,
                    "p_webhook_event_id": event_id,
                    "p_action": action,
                    "p_selected_purchase_intent_id": candidate_id,
                    "p_verification_basis": verification_basis,
                }
            ),
        )
        if response.status_code != 200:
            _raise_operator_correlation_resolution_error(
                response, operation=operation
            )
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1 or not isinstance(rows[0].get("command_data"), dict):
            raise SupabaseError(f"{operation}_invalid_shape")
        return rows[0]["command_data"]

    async def confirm_operator_correlation_resolution(
        self,
        *,
        tenant_ref: str,
        funnel_ref: str,
        actor_ref: str,
        command_id: str,
        expected_action: str,
        expected_purchase_intent_id: str | None,
    ) -> dict[str, Any]:
        """Apply one immutable prepared command or replay its result."""
        operation = "operator_correlation_resolution_confirm"
        tenant = _required_string(
            {"tenant_ref": tenant_ref}, "tenant_ref", operation=operation
        )
        funnel = _required_string(
            {"funnel_ref": funnel_ref}, "funnel_ref", operation=operation
        )
        actor = _required_string(
            {"actor_ref": actor_ref}, "actor_ref", operation=operation
        )
        expected_command_id = _required_uuid(
            {"command_id": command_id}, "command_id", operation=operation
        )
        expected_intent_id = None
        if expected_purchase_intent_id is not None:
            expected_intent_id = _required_uuid(
                {"expected_purchase_intent_id": expected_purchase_intent_id},
                "expected_purchase_intent_id",
                operation=operation,
            )
        response = await self._request(
            "POST",
            "/rest/v1/rpc/confirm_operator_correlation_resolution",
            content=json.dumps(
                {
                    "p_tenant_ref": tenant,
                    "p_funnel_ref": funnel,
                    "p_actor_ref": actor,
                    "p_command_id": expected_command_id,
                    "p_expected_action": expected_action,
                    "p_expected_purchase_intent_id": expected_intent_id,
                }
            ),
        )
        if response.status_code != 200:
            _raise_operator_correlation_resolution_error(
                response, operation=operation
            )
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1 or not isinstance(rows[0].get("resolution_data"), dict):
            raise SupabaseError(f"{operation}_invalid_shape")
        return rows[0]["resolution_data"]

    async def _read_operator_correlation_rpc(
        self,
        *,
        operation: str,
        path: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        response = await self._request(
            "POST",
            path,
            content=json.dumps(payload),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        result: list[dict[str, Any]] = []
        for row in rows:
            case_data = row.get("case_data")
            if not isinstance(case_data, dict):
                raise SupabaseError(f"{operation}_invalid_shape")
            result.append(case_data)
        return result

    async def list_due_hotmart_abandonment_reevaluations(
        self,
        *,
        now: str,
        batch_size: int,
        include_precheckout: bool = False,
    ) -> list[str]:
        """List due timer IDs without exposing purchase-intent PII."""
        operation = "hotmart_abandonment_reevaluation_due_list"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/list_due_hotmart_abandonment_reevaluations_v2",
            content=json.dumps(
                {
                    "p_now": now,
                    "p_batch_size": batch_size,
                    "p_include_precheckout": include_precheckout,
                }
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        ids: list[str] = []
        for row in rows:
            reevaluation_id = _required_uuid(
                row, "reevaluation_id", operation=operation
            )
            if reevaluation_id in ids:
                raise SupabaseError(f"{operation}_duplicate_id")
            ids.append(reevaluation_id)
        return ids

    async def reevaluate_hotmart_abandonment_timer(
        self,
        *,
        reevaluation_id: str,
        now: str,
    ) -> HotmartAbandonmentReevaluationResult:
        """Re-read one due intent and terminalize its timer idempotently."""
        operation = "hotmart_abandonment_timer_reevaluation"
        expected_id = _required_uuid(
            {"reevaluation_id": reevaluation_id},
            "reevaluation_id",
            operation=operation,
        )
        response = await self._request(
            "POST",
            "/rest/v1/rpc/reevaluate_hotmart_abandonment_timer",
            content=json.dumps(
                {"p_reevaluation_id": expected_id, "p_now": now}
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        result_id = _required_uuid(row, "reevaluation_id", operation=operation)
        status = _required_enum(
            row, "reevaluation_status", {"completed"}, operation=operation
        )
        outcome = _required_enum(
            row,
            "reevaluation_outcome",
            {
                "cancelled_purchased",
                "blocked_not_authorized",
                "blocked_contact_binding_missing",
                "cancelled_intent_changed",
                "superseded_by_provider_event",
                "blocked_contact",
                "blocked_identity",
                "blocked_handoff",
                "budget_consumed",
                "command_reserved",
            },
            operation=operation,
        )
        completed_at = _required_string(row, "completed_at", operation=operation)
        replayed = row.get("replayed")
        if result_id != expected_id or not isinstance(replayed, bool):
            raise SupabaseError(f"{operation}_invalid_row")
        return HotmartAbandonmentReevaluationResult(
            reevaluation_id=result_id,
            status=status,
            outcome=outcome,
            completed_at=completed_at,
            replayed=replayed,
        )

    async def get_precheckout_delayed_one_shot_command(
        self,
        *,
        reevaluation_id: str,
    ) -> PrecheckoutDelayedOneShotCommand:
        """Read the exact sender context for one reserved precheckout command."""
        operation = "precheckout_delayed_one_shot_command"
        expected_id = _required_uuid(
            {"reevaluation_id": reevaluation_id},
            "reevaluation_id",
            operation=operation,
        )
        response = await self._request(
            "POST",
            "/rest/v1/rpc/get_precheckout_delayed_one_shot_command",
            content=json.dumps({"p_reevaluation_id": expected_id}),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        send_authorized = row.get("send_authorized")
        if not isinstance(send_authorized, bool):
            raise SupabaseError(f"{operation}_invalid_row")
        authorization_reason = _optional_enum(
            row,
            "authorization_reason",
            {
                "command_terminal",
                "template_metadata_mismatch",
                "cancelled_purchased",
                "cancelled_intent_changed",
                "superseded_by_provider_event",
                "blocked_identity",
                "blocked_not_authorized",
                "blocked_contact_binding_missing",
                "blocked_contact",
                "blocked_handoff",
            },
            operation=operation,
        )
        buyer_name = _optional_string(row, "buyer_name", operation=operation)
        buyer_email = _optional_string(row, "buyer_email", operation=operation)
        product_name = _optional_string(row, "product_name", operation=operation)
        if send_authorized:
            if (
                authorization_reason is not None
                or not buyer_name
                or not buyer_email
                or not product_name
            ):
                raise SupabaseError(f"{operation}_invalid_authorization")
        elif authorization_reason is None or any(
            value is not None for value in (buyer_name, buyer_email, product_name)
        ):
            raise SupabaseError(f"{operation}_invalid_authorization")
        return PrecheckoutDelayedOneShotCommand(
            command_id=_required_uuid(row, "command_id", operation=operation),
            command_status=_required_enum(
                row,
                "command_status",
                {"request_started", "accepted_by_chatwoot", "delivery_unknown"},
                operation=operation,
            ),
            target_phone=_required_string(row, "target_phone", operation=operation),
            buyer_name=buyer_name,
            buyer_email=buyer_email,
            product_name=product_name,
            template_name=_required_string(row, "template_name", operation=operation),
            template_language=_required_string(
                row, "template_language", operation=operation
            ),
            template_category=_required_string(
                row, "template_category", operation=operation
            ),
            copy_version=_required_string(row, "copy_version", operation=operation),
            send_authorized=send_authorized,
            authorization_reason=authorization_reason,
        )


    async def admit_inbound_commercial_case(
        self,
        *,
        scope_key: str,
        scope_version: int,
        external_conversation_id: int,
        external_user_id: str,
    ) -> InboundCommercialCaseAdmissionResult:
        """Create or replay one canonical draft-only inbound commercial case."""
        operation = "inbound_commercial_case_admission"
        response = await self._request(
            "POST",
            "/rest/v1/rpc/admit_inbound_commercial_case_v2",
            content=json.dumps(
                {
                    "p_scope_key": scope_key,
                    "p_scope_version": scope_version,
                    "p_external_conversation_id": external_conversation_id,
                    "p_external_user_id": external_user_id,
                },
                ensure_ascii=False,
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"inbound_commercial_case_admission_failed: HTTP {response.status_code}"
            )
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError("inbound_commercial_case_admission_invalid_shape")
        row = rows[0]
        outcome = row.get("outcome")
        if outcome not in {
            "created",
            "already_exists",
            "evidence_conflict",
            "blocked",
        }:
            raise SupabaseError("inbound_commercial_case_admission_invalid_outcome")
        automation_status = row.get("automation_status")
        if automation_status not in {"draft_only", "disabled"}:
            raise SupabaseError("inbound_commercial_case_admission_not_draft_only")
        if outcome == "blocked" and automation_status != "disabled":
            raise SupabaseError("inbound_commercial_case_admission_blocked_state_invalid")
        if outcome != "blocked" and automation_status != "draft_only":
            raise SupabaseError("inbound_commercial_case_admission_replyable_state_invalid")
        return InboundCommercialCaseAdmissionResult(
            outcome=outcome,
            commercial_case_id=_required_string(
                row, "commercial_case_id", operation=operation
            ),
            contact_id=_required_string(row, "contact_id", operation=operation),
            channel_identity_id=_required_string(
                row, "channel_identity_id", operation=operation
            ),
            conversation_id=_required_string(
                row, "conversation_id", operation=operation
            ),
            automation_status=automation_status,
        )

    async def fetch_pending_events(
        self,
        *,
        limit: int = 10,
        excluded_event_types: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        """Fetch webhook events in 'received' status, oldest first."""
        params = {
            "select": "id,source,external_event_id,event_type,payload",
            "processing_status": "eq.received",
            "order": "received_at.asc,id.asc",
            "limit": str(limit),
        }
        if excluded_event_types:
            params["event_type"] = (
                "not.in.(" + ",".join(excluded_event_types) + ")"
            )
        response = await self._request(
            "GET",
            "/rest/v1/webhook_events",
            params=params,
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"fetch_pending_events_failed: HTTP {response.status_code}"
            )
        try:
            rows = response.json()
        except ValueError as exc:
            raise SupabaseError("fetch_pending_events_invalid_json") from exc
        return rows if isinstance(rows, list) else []

    async def update_event_status(
        self,
        *,
        event_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Update the processing_status of a webhook event."""
        body_dict: dict[str, Any] = {
            "processing_status": status,
            "processed_at": "now()" if status == "processed" else None,
        }
        if error:
            body_dict["processing_error"] = error
        body = json.dumps(body_dict, ensure_ascii=False)
        response = await self._request(
            "PATCH",
            "/rest/v1/webhook_events",
            params={"id": f"eq.{event_id}"},
            content=body,
            prefer="return=minimal",
        )
        if response.status_code not in (200, 204):
            raise SupabaseError(
                f"update_event_status_failed: HTTP {response.status_code}"
            )

    async def get_pilot_runtime_status(
        self,
        *,
        pilot_boundary: PilotBoundaryConfig,
    ) -> PilotRuntimeStatus:
        operation = "pilot_runtime_status"
        body = {
            "p_scope_key": pilot_boundary.scope_key,
            "p_scope_version": pilot_boundary.scope_version,
            "p_tenant_key": pilot_boundary.tenant_key,
            "p_channel_provider": pilot_boundary.channel_provider,
            "p_channel_account_ref": pilot_boundary.channel_account_ref,
        }
        response = await self._request(
            "POST",
            "/rest/v1/rpc/get_lancemos_pilot_runtime_status",
            content=json.dumps(body, ensure_ascii=False),
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"pilot_runtime_status_failed: HTTP {response.status_code}"
            )
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError("pilot_runtime_status_invalid_shape")
        row = rows[0]
        try:
            configured = row.get("configured")
            if not isinstance(configured, bool):
                raise ValueError("configured must be boolean")
            runtime_state = row.get("runtime_state")
            if runtime_state is not None and runtime_state not in {
                "inactive", "armed", "paused", "closed",
            }:
                raise ValueError("runtime_state is invalid")
            generation = row.get("runtime_generation")
            if generation is not None and (
                not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation < 0
            ):
                raise ValueError("runtime_generation is invalid")
            reason_code = _required_string(
                row, "reason_code", operation=operation
            )
        except (TypeError, ValueError) as exc:
            raise SupabaseCommittedResponseError(operation) from exc
        return PilotRuntimeStatus(
            configured=configured,
            runtime_state=runtime_state,
            runtime_generation=generation,
            reason_code=reason_code,
        )

    async def plan_cart_recovery(
        self,
        *,
        webhook_event_id: str,
        contact_id: str,
        external_product_id: str,
        product_name: str,
        offer_code: str | None,
        policy_key: str,
        policy_version: int,
        abandoned_at: str,
        chatwoot_account_id: int | None = None,
        chatwoot_inbox_id: int | None = None,
        external_user_id: str | None = None,
        pilot_boundary: PilotBoundaryConfig | None = None,
    ) -> CartRecoveryPlan:
        """Atomically create or reuse the durable cart-recovery plan."""
        identity_values = (
            chatwoot_account_id,
            chatwoot_inbox_id,
            external_user_id,
        )
        if any(value is not None for value in identity_values) and not all(
            value is not None for value in identity_values
        ):
            raise SupabaseError("plan_cart_recovery_incomplete_identity")
        if pilot_boundary is not None and not all(
            value is not None for value in identity_values
        ):
            raise SupabaseError("pilot_plan_cart_recovery_requires_identity")
        if pilot_boundary is not None:
            rpc_name = "plan_lancemos_pilot_cart_recovery"
        elif all(value is not None for value in identity_values):
            rpc_name = "plan_cart_recovery_with_identity"
        else:
            rpc_name = "plan_cart_recovery"
        rpc_body: dict[str, object] = {
            "p_webhook_event_id": webhook_event_id,
            "p_contact_id": contact_id,
            "p_external_product_id": external_product_id,
            "p_product_name": product_name,
            "p_offer_code": offer_code,
            "p_policy_key": policy_key,
            "p_policy_version": policy_version,
            "p_abandoned_at": abandoned_at,
        }
        if rpc_name != "plan_cart_recovery":
            rpc_body.update({
                "p_chatwoot_account_id": chatwoot_account_id,
                "p_chatwoot_inbox_id": chatwoot_inbox_id,
                "p_external_user_id": external_user_id,
            })
        if pilot_boundary is not None:
            rpc_body.update({
                "p_scope_key": pilot_boundary.scope_key,
                "p_scope_version": pilot_boundary.scope_version,
            })
        body = json.dumps(rpc_body, ensure_ascii=False)
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{rpc_name}",
            content=body,
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"plan_cart_recovery_failed: HTTP {response.status_code}"
            )
        rows = _response_rows(response, operation="plan_cart_recovery")
        if len(rows) != 1:
            raise SupabaseError("plan_cart_recovery_invalid_shape")
        row = rows[0]
        created = row.get("created")
        if not isinstance(created, bool):
            raise SupabaseError("plan_cart_recovery_invalid_row")
        return CartRecoveryPlan(
            recovery_case_id=_required_string(
                row, "recovery_case_id", operation="plan_cart_recovery"
            ),
            followup_sequence_id=_required_string(
                row, "followup_sequence_id", operation="plan_cart_recovery"
            ),
            scheduled_action_id=_required_string(
                row, "scheduled_action_id", operation="plan_cart_recovery"
            ),
            created=created,
        )

    async def plan_payment_failure_recovery(
        self,
        *,
        webhook_event_id: str,
        contact_id: str,
        external_product_id: str,
        product_name: str,
        offer_code: str | None,
        policy_key: str,
        policy_version: int,
        abandoned_at: str,
        chatwoot_account_id: int | None = None,
        chatwoot_inbox_id: int | None = None,
        external_user_id: str | None = None,
        pilot_boundary: PilotBoundaryConfig | None = None,
    ) -> CartRecoveryPlan:
        """Atomically create the portable payment-failure recovery plan."""
        if (
            pilot_boundary is None
            or chatwoot_account_id is None
            or chatwoot_inbox_id is None
            or external_user_id is None
        ):
            raise SupabaseError(
                "portable_payment_failure_plan_requires_scoped_identity"
            )
        operation = "plan_portable_payment_failure_recovery"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_webhook_event_id": webhook_event_id,
                "p_contact_id": contact_id,
                "p_external_product_id": external_product_id,
                "p_product_name": product_name,
                "p_offer_code": offer_code,
                "p_policy_key": policy_key,
                "p_policy_version": policy_version,
                "p_failed_at": abandoned_at,
                "p_chatwoot_account_id": chatwoot_account_id,
                "p_chatwoot_inbox_id": chatwoot_inbox_id,
                "p_external_user_id": external_user_id,
                "p_scope_key": pilot_boundary.scope_key,
                "p_scope_version": pilot_boundary.scope_version,
            }, ensure_ascii=False),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        created = row.get("created")
        if not isinstance(created, bool):
            raise SupabaseError(f"{operation}_invalid_row")
        return CartRecoveryPlan(
            recovery_case_id=_required_string(
                row, "recovery_case_id", operation=operation
            ),
            followup_sequence_id=_required_string(
                row, "followup_sequence_id", operation=operation
            ),
            scheduled_action_id=_required_string(
                row, "scheduled_action_id", operation=operation
            ),
            created=created,
        )

    async def apply_hotmart_purchase_approved(
        self,
        *,
        webhook_event_id: str,
        buyer_email: str | None,
        buyer_phone: str | None,
        external_product_id: str,
        offer_code: str | None,
        transaction: str,
        approved_at: str,
    ) -> PurchaseCorrelationResult:
        """Atomically correlate a purchase and stop its recovery sequence."""
        body = json.dumps({
            "p_webhook_event_id": webhook_event_id,
            "p_buyer_email": buyer_email,
            "p_buyer_phone": buyer_phone,
            "p_external_product_id": external_product_id,
            "p_offer_code": offer_code,
            "p_transaction": transaction,
            "p_approved_at": approved_at,
        }, ensure_ascii=False)
        response = await self._request(
            "POST",
            "/rest/v1/rpc/apply_hotmart_purchase_approved",
            content=body,
        )
        operation = "apply_hotmart_purchase_approved"
        if response.status_code != 200:
            try:
                error_body = response.json()
            except ValueError:
                error_body = None
            sqlstate = (
                error_body.get("code")
                if isinstance(error_body, dict)
                else None
            )
            error_message = (
                error_body.get("message")
                if isinstance(error_body, dict)
                else None
            )
            permanent_contract_errors = {
                ("22023", "invalid_purchase_correlation_input"),
                ("22023", "webhook_event_not_purchase_approved"),
                ("22023", "purchase_event_invalid_approved_date"),
                ("22023", "purchase_rpc_payload_mismatch"),
                ("22023", "purchase_approved_at_in_future"),
            }
            error_type = (
                SupabasePermanentError
                if (sqlstate, error_message) in permanent_contract_errors
                else SupabaseError
            )
            raise error_type(
                f"{operation}_failed: HTTP {response.status_code}"
            )
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        return PurchaseCorrelationResult(
            outcome=_required_enum(
                row,
                "outcome",
                {"applied", "already_applied", "not_found", "ambiguous"},
                operation=operation,
            ),
            recovery_case_id=_optional_string(
                row, "recovery_case_id", operation=operation
            ),
            matched_by=_optional_enum(
                row,
                "matched_by",
                {"email", "phone", "email_and_phone"},
                operation=operation,
            ),
        )

    async def apply_chatwoot_inbound_opt_out(
        self,
        *,
        chatwoot_account_id: int,
        chatwoot_inbox_id: int,
        chatwoot_conversation_id: int,
        chatwoot_message_id: int,
        external_user_id: str,
        occurred_at: str,
        rule_key: str,
    ) -> InboundOptOutResult:
        """Persist and atomically apply one canonical inbound opt-out."""
        operation = "apply_chatwoot_inbound_opt_out"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps(
                {
                    "p_chatwoot_account_id": chatwoot_account_id,
                    "p_chatwoot_inbox_id": chatwoot_inbox_id,
                    "p_chatwoot_conversation_id": chatwoot_conversation_id,
                    "p_chatwoot_message_id": chatwoot_message_id,
                    "p_external_user_id": external_user_id,
                    "p_occurred_at": occurred_at,
                    "p_rule_key": rule_key,
                },
                ensure_ascii=False,
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        return InboundOptOutResult(
            outcome=_required_enum(
                row,
                "outcome",
                {
                    "applied",
                    "already_applied",
                    "recorded_unmatched",
                    "recorded_ambiguous",
                    "evidence_conflict",
                },
                operation=operation,
            ),
            opt_out_event_id=_required_string(
                row, "opt_out_event_id", operation=operation
            ),
            contact_id=_optional_string(
                row, "matched_contact_id", operation=operation
            ),
            affected_cases=_required_int(
                row, "affected_cases", operation=operation
            ),
            affected_actions=_required_int(
                row, "affected_actions", operation=operation
            ),
            affected_attempts=_required_int(
                row, "affected_attempts", operation=operation
            ),
        )

    async def has_chatwoot_opt_out_stop(
        self,
        *,
        chatwoot_account_id: int,
        chatwoot_inbox_id: int,
        chatwoot_conversation_id: int,
        external_user_id: str,
    ) -> bool:
        """Check conversation-local and contact-global durable stop facts."""
        operation = "has_chatwoot_opt_out_stop"
        if not external_user_id.isdigit():
            raise SupabaseError(f"{operation}_invalid_external_user_id")
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps(
                {
                    "p_chatwoot_account_id": chatwoot_account_id,
                    "p_chatwoot_inbox_id": chatwoot_inbox_id,
                    "p_chatwoot_conversation_id": chatwoot_conversation_id,
                    "p_external_user_id": external_user_id,
                }
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise SupabaseError(f"{operation}_invalid_json") from exc
        if not isinstance(result, bool):
            raise SupabaseError(f"{operation}_invalid_shape")
        return result

    async def reconcile_chatwoot_opt_out_stop(
        self,
        *,
        chatwoot_account_id: int,
        chatwoot_inbox_id: int,
        chatwoot_conversation_id: int,
        external_user_id: str,
    ) -> InboundOptOutResult:
        """Reconcile a durable pending stop against current identity state."""
        operation = "reconcile_chatwoot_opt_out_stop"
        if not external_user_id.isdigit():
            raise SupabaseError(f"{operation}_invalid_external_user_id")
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps(
                {
                    "p_chatwoot_account_id": chatwoot_account_id,
                    "p_chatwoot_inbox_id": chatwoot_inbox_id,
                    "p_chatwoot_conversation_id": chatwoot_conversation_id,
                    "p_external_user_id": external_user_id,
                }
            ),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        return InboundOptOutResult(
            outcome=_required_enum(
                row,
                "outcome",
                {
                    "applied",
                    "already_applied",
                    "recorded_unmatched",
                    "recorded_ambiguous",
                    "evidence_conflict",
                },
                operation=operation,
            ),
            opt_out_event_id=_required_string(
                row, "opt_out_event_id", operation=operation
            ),
            contact_id=_optional_string(
                row, "matched_contact_id", operation=operation
            ),
            affected_cases=_required_int(
                row, "affected_cases", operation=operation
            ),
            affected_actions=_required_int(
                row, "affected_actions", operation=operation
            ),
            affected_attempts=_required_int(
                row, "affected_attempts", operation=operation
            ),
        )

    async def claim_chatwoot_opt_out_projections(
        self,
        *,
        worker_id: str,
        now: str,
        lease_duration: str,
        batch_size: int,
    ) -> list[OptOutProjectionClaim]:
        operation = "claim_chatwoot_opt_out_projections"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_worker_id": worker_id,
                "p_now": now,
                "p_lease_duration": lease_duration,
                "p_batch_size": batch_size,
            }),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        return [
            OptOutProjectionClaim(
                opt_out_event_id=_required_string(
                    row, "opt_out_event_id", operation=operation
                ),
                chatwoot_account_id=_required_int(
                    row, "chatwoot_account_id", operation=operation
                ),
                chatwoot_inbox_id=_required_int(
                    row, "chatwoot_inbox_id", operation=operation
                ),
                chatwoot_conversation_id=_required_int(
                    row, "chatwoot_conversation_id", operation=operation
                ),
                external_user_id=_required_string(
                    row, "external_user_id", operation=operation
                ),
                lease_generation=_required_int(
                    row, "lease_generation", operation=operation
                ),
            )
            for row in rows
        ]

    async def finalize_chatwoot_opt_out_projection(
        self,
        *,
        opt_out_event_id: str,
        worker_id: str,
        lease_generation: int,
        applied: bool,
        error_code: str | None,
        max_attempts: int,
        now: str,
    ) -> str:
        operation = "finalize_chatwoot_opt_out_projection"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_opt_out_event_id": opt_out_event_id,
                "p_worker_id": worker_id,
                "p_lease_generation": lease_generation,
                "p_applied": applied,
                "p_error_code": error_code,
                "p_max_attempts": max_attempts,
                "p_now": now,
            }),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        return _required_enum(
            rows[0],
            "projection_status",
            {"pending", "applied", "retryable_failed", "dead_letter"},
            operation=operation,
        )

    async def claim_human_handoff_projection_effects(
        self,
        *,
        worker_id: str,
        now: str,
        lease_seconds: int,
        batch_size: int,
    ) -> list[HumanHandoffProjectionClaim]:
        operation = "claim_human_handoff_projection_effects"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_worker_id": worker_id,
                "p_now": now,
                "p_lease_seconds": lease_seconds,
                "p_limit": batch_size,
            }),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _committed_response_rows(response, operation=operation)
        return [
            HumanHandoffProjectionClaim(
                effect_id=_required_uuid(row, "effect_id", operation=operation),
                handoff_request_id=_required_uuid(
                    row, "handoff_request_id", operation=operation
                ),
                effect_kind=_required_enum(
                    row,
                    "effect_kind",
                    {"assignment", "private_note"},
                    operation=operation,
                ),
                current_effect_status=_required_enum(
                    row,
                    "current_effect_status",
                    {"pending", "retryable_failed", "delivery_unknown"},
                    operation=operation,
                ),
                attempt_count=_required_positive_int(
                    row, "attempt_count", operation=operation
                ),
                lease_generation=_required_positive_int(
                    row, "lease_generation", operation=operation
                ),
                expected_team_id=_required_positive_int(
                    row, "expected_team_id", operation=operation
                ),
                chatwoot_account_id=_required_positive_int(
                    row, "chatwoot_account_id", operation=operation
                ),
                chatwoot_inbox_id=_required_positive_int(
                    row, "chatwoot_inbox_id", operation=operation
                ),
                chatwoot_conversation_id=_required_positive_int(
                    row, "chatwoot_conversation_id", operation=operation
                ),
                external_user_id=_required_string(
                    row, "external_user_id", operation=operation
                ),
                private_note_body=_required_string(
                    row, "private_note_body", operation=operation
                ),
                idempotency_marker=_required_string(
                    row, "idempotency_marker", operation=operation
                ),
            )
            for row in rows
        ]

    async def request_human_handoff(
        self,
        *,
        recovery_case_id: str,
        command_key: str,
        reason_code: str,
        requested_by: str,
        projection_policy_key: str,
        projection_policy_version: int,
        source_action_id: str,
        source_attempt_id: str,
        worker_id: str,
        lease_generation: int,
        now: str,
    ) -> HumanHandoffRequestResult:
        operation = "request_human_handoff"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_recovery_case_id": recovery_case_id,
                "p_command_key": command_key,
                "p_reason_code": reason_code,
                "p_requested_by": requested_by,
                "p_projection_policy_key": projection_policy_key,
                "p_projection_policy_version": projection_policy_version,
                "p_source_action_id": source_action_id,
                "p_source_attempt_id": source_attempt_id,
                "p_worker_id": worker_id,
                "p_lease_generation": lease_generation,
                "p_now": now,
            }),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _committed_response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseCommittedResponseError(
                f"{operation}_committed_response_invalid"
            )
        row = rows[0]
        return HumanHandoffRequestResult(
            outcome=_required_enum(
                row,
                "outcome",
                {"requested", "already_requested", "evidence_appended"},
                operation=operation,
            ),
            handoff_request_id=_required_uuid(
                row, "handoff_request_id", operation=operation
            ),
            affected_actions=_required_nonnegative_int(
                row, "affected_actions", operation=operation
            ),
            affected_attempts=_required_nonnegative_int(
                row, "affected_attempts", operation=operation
            ),
        )

    async def request_inbound_human_handoff(
        self,
        *,
        commercial_case_id: str,
        command_key: str,
        reason_code: str,
        projection_policy_key: str,
        projection_policy_version: int,
        now: str,
    ) -> HumanHandoffRequestResult:
        """Atomically stop one inbound case and enqueue its handoff effects."""
        operation = "request_inbound_human_handoff"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_commercial_case_id": commercial_case_id,
                "p_command_key": command_key,
                "p_reason_code": reason_code,
                "p_projection_policy_key": projection_policy_key,
                "p_projection_policy_version": projection_policy_version,
                "p_now": now,
            }),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _committed_response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseCommittedResponseError(
                f"{operation}_committed_response_invalid"
            )
        row = rows[0]
        return HumanHandoffRequestResult(
            outcome=_required_enum(
                row,
                "outcome",
                {"requested", "already_requested"},
                operation=operation,
            ),
            handoff_request_id=_required_uuid(
                row, "handoff_request_id", operation=operation
            ),
            affected_actions=_required_nonnegative_int(
                row, "affected_actions", operation=operation
            ),
            affected_attempts=_required_nonnegative_int(
                row, "affected_attempts", operation=operation
            ),
        )

    async def finalize_human_handoff_projection_effect(
        self,
        *,
        effect_id: str,
        worker_id: str,
        lease_generation: int,
        outcome: str,
        error_code: str | None,
        retry_at: str | None,
        now: str,
    ) -> HumanHandoffProjectionFinalization:
        operation = "finalize_human_handoff_projection_effect"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_effect_id": effect_id,
                "p_worker_id": worker_id,
                "p_lease_generation": lease_generation,
                "p_outcome": outcome,
                "p_error_code": error_code,
                "p_retry_at": retry_at,
                "p_now": now,
            }),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _committed_response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseCommittedResponseError(
                f"{operation}_committed_response_invalid"
            )
        row = rows[0]
        result = HumanHandoffProjectionFinalization(
            effect_status=_required_enum(
                row,
                "effect_status",
                {
                    "applied",
                    "retryable_failed",
                    "delivery_unknown",
                    "conflict",
                    "dead_letter",
                },
                operation=operation,
            ),
            handoff_status=_required_enum(
                row,
                "handoff_status",
                {"projected", "projection_failed", "dead_letter"},
                operation=operation,
            ),
        )

        if result.effect_status != outcome:
            raise SupabaseCommittedResponseError(
                f"{operation}_committed_response_mismatch"
            )
        return result

    async def get_human_handoff_projection_status(
        self,
    ) -> HumanHandoffProjectionStatus:
        operation = "get_human_handoff_projection_status"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content="{}",
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        return HumanHandoffProjectionStatus(
            pending_count=_required_nonnegative_int(
                row, "pending_count", operation=operation
            ),
            retryable_count=_required_nonnegative_int(
                row, "retryable_count", operation=operation
            ),
            delivery_unknown_count=_required_nonnegative_int(
                row, "delivery_unknown_count", operation=operation
            ),
            conflict_count=_required_nonnegative_int(
                row, "conflict_count", operation=operation
            ),
            dead_letter_count=_required_nonnegative_int(
                row, "dead_letter_count", operation=operation
            ),
        )

    async def get_precheckout_delayed_first_touch_readiness(
        self,
    ) -> PrecheckoutDelayedFirstTouchReadiness:
        operation = "get_precheckout_delayed_first_touch_readiness"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content="{}",
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        migration_tracking_complete = _required_bool(
            row, "migration_tracking_complete", operation=operation
        )
        scope_configured = _required_bool(
            row, "scope_configured", operation=operation
        )
        timer_binding_enabled = _required_bool(
            row, "timer_binding_enabled", operation=operation
        )
        first_touch_binding_enabled = _required_bool(
            row, "first_touch_binding_enabled", operation=operation
        )
        runtime_state = row.get("runtime_state")
        if runtime_state is not None and runtime_state not in {
            "inactive", "armed", "paused", "closed",
        }:
            raise SupabaseCommittedResponseError(operation)
        runtime_generation = row.get("runtime_generation")
        timer_binding_generation = row.get("timer_binding_generation")
        for value in (runtime_generation, timer_binding_generation):
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise SupabaseCommittedResponseError(operation)
        reason_code = _required_string(row, "reason_code", operation=operation)
        if reason_code not in {
            "migration_tracking_incomplete",
            "precheckout_scope_not_configured",
            "precheckout_runtime_not_inactive",
            "timer_binding_disabled",
            "timer_binding_policy_mismatch",
            "first_touch_binding_disabled",
            "precheckout_first_touch_ready",
        }:
            raise SupabaseCommittedResponseError(operation)
        return PrecheckoutDelayedFirstTouchReadiness(
            migration_tracking_complete=migration_tracking_complete,
            scope_configured=scope_configured,
            runtime_state=runtime_state,
            runtime_generation=runtime_generation,
            timer_binding_enabled=timer_binding_enabled,
            timer_binding_generation=timer_binding_generation,
            first_touch_binding_enabled=first_touch_binding_enabled,
            due_count=_required_nonnegative_int(
                row, "due_count", operation=operation
            ),
            reserved_count=_required_nonnegative_int(
                row, "reserved_count", operation=operation
            ),
            request_started_count=_required_nonnegative_int(
                row, "request_started_count", operation=operation
            ),
            delivery_unknown_count=_required_nonnegative_int(
                row, "delivery_unknown_count", operation=operation
            ),
            reason_code=reason_code,
        )

    async def claim_due_followup_actions(
        self,
        *,
        worker_id: str,
        now: str,
        lease_duration: str,
        batch_size: int,
    ) -> list[ScheduledAction]:
        """Claim due actions; PostgreSQL remains the queue authority."""
        body = json.dumps({
            "p_worker_id": worker_id,
            "p_now": now,
            "p_lease_duration": lease_duration,
            "p_batch_size": batch_size,
        }, ensure_ascii=False)
        response = await self._request(
            "POST",
            "/rest/v1/rpc/claim_due_followup_actions",
            content=body,
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"claim_due_followup_actions_failed: HTTP {response.status_code}"
            )
        operation = "claim_due_followup_actions"
        rows = _response_rows(response, operation=operation)
        actions: list[ScheduledAction] = []
        for row in rows:
            actions.append(ScheduledAction(
                action_id=_required_string(row, "id", operation=operation),
                recovery_case_id=_required_string(
                    row, "recovery_case_id", operation=operation
                ),
                followup_sequence_id=_required_string(
                    row, "followup_sequence_id", operation=operation
                ),
                action_type=_required_enum(
                    row,
                    "action_type",
                    {"first_contact_review", "no_reply_review", "reconcile_delivery"},
                    operation=operation,
                ),
                status=_required_enum(
                    row,
                    "status",
                    {"pending", "deferred", "retryable_failed"},
                    operation=operation,
                ),
                due_at=_required_string(row, "due_at", operation=operation),
                expires_at=_required_string(row, "expires_at", operation=operation),
                expected_case_version=_required_int(
                    row, "expected_case_version", operation=operation
                ),
                policy_key=_required_string(row, "policy_key", operation=operation),
                policy_version=_required_int(
                    row, "policy_version", operation=operation
                ),
                step_key=_required_string(row, "step_key", operation=operation),
                anchor_type=_required_string(row, "anchor_type", operation=operation),
                anchor_subject_internal_id=_required_string(
                    row, "anchor_subject_internal_id", operation=operation
                ),
                anchor_observed_at=_required_string(
                    row, "anchor_observed_at", operation=operation
                ),
                lease_owner=_required_string(row, "lease_owner", operation=operation),
                lease_generation=_required_int(
                    row, "lease_generation", operation=operation
                ),
                lease_expires_at=_required_string(
                    row, "lease_expires_at", operation=operation
                ),
                idempotency_key=_required_string(
                    row, "idempotency_key", operation=operation
                ),
            ))
        return actions

    async def get_followup_execution_context(
        self,
        *,
        action_id: str,
        worker_id: str,
        lease_generation: int,
        now: str,
    ) -> FollowupExecutionContext:
        """Read the minimal fenced case context needed before agent reasoning."""
        operation = "get_followup_execution_context"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_action_id": action_id,
                "p_worker_id": worker_id,
                "p_lease_generation": lease_generation,
                "p_now": now,
            }, ensure_ascii=False),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        returned_action_id = _required_string(row, "action_id", operation=operation)
        if returned_action_id != action_id:
            raise SupabaseError(f"{operation}_action_mismatch")
        return FollowupExecutionContext(
            action_id=returned_action_id,
            action_type=_required_enum(
                row,
                "action_type",
                {"first_contact_review", "no_reply_review", "reconcile_delivery"},
                operation=operation,
            ),
            step_key=_required_string(row, "step_key", operation=operation),
            recovery_case_id=_required_string(
                row, "recovery_case_id", operation=operation
            ),
            contact_id=_required_string(row, "contact_id", operation=operation),
            source_event_id=_required_string(
                row, "source_event_id", operation=operation
            ),
            buyer_name=_optional_string(row, "buyer_name", operation=operation),
            buyer_email=_optional_string(row, "buyer_email", operation=operation),
            buyer_phone=_optional_string(row, "buyer_phone", operation=operation),
            product_name=_required_string(row, "product_name", operation=operation),
            offer_code=_optional_string(row, "offer_code", operation=operation),
            current_goal=_optional_string(row, "current_goal", operation=operation),
            lead_stage=_required_enum(
                row, "lead_stage", _LEAD_STAGES, operation=operation
            ),
        )

    async def get_followup_chatwoot_context(
        self,
        *,
        action_id: str,
        worker_id: str,
        lease_generation: int,
        now: str,
    ) -> ChatwootAuthorityContext:
        """Read fenced external identifiers needed for canonical Chatwoot checks."""
        operation = "get_followup_chatwoot_context"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_action_id": action_id,
                "p_worker_id": worker_id,
                "p_lease_generation": lease_generation,
                "p_now": now,
            }, ensure_ascii=False),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        returned_action_id = _required_string(row, "action_id", operation=operation)
        if returned_action_id != action_id:
            raise SupabaseError(f"{operation}_action_mismatch")
        return ChatwootAuthorityContext(
            action_id=returned_action_id,
            action_type=_required_enum(
                row, "action_type",
                {"first_contact_review", "no_reply_review", "reconcile_delivery"},
                operation=operation,
            ),
            chatwoot_account_id=_optional_chatwoot_account_id(
                row, "chatwoot_account_id", operation=operation
            ),
            external_conversation_id=_optional_positive_int(
                row, "external_conversation_id", operation=operation
            ),
            expected_inbox_id=_optional_positive_int(
                row, "expected_inbox_id", operation=operation
            ),
            anchor_external_message_id=_optional_positive_int(
                row, "anchor_external_message_id", operation=operation
            ),
        )

    async def reevaluate_followup_action(
        self,
        *,
        action_id: str,
        worker_id: str,
        lease_generation: int,
        now: str,
        chatwoot_evidence: dict[str, object] | None = None,
    ) -> ReevaluationDecision:
        """Apply deterministic guards and atomically persist non-execute results."""
        rpc_body: dict[str, object] = {
            "p_action_id": action_id,
            "p_worker_id": worker_id,
            "p_lease_generation": lease_generation,
            "p_now": now,
            "p_chatwoot_checked": chatwoot_evidence is not None,
        }
        if chatwoot_evidence is not None:
            rpc_body.update(chatwoot_evidence)
        body = json.dumps(rpc_body, ensure_ascii=False)
        response = await self._request(
            "POST",
            "/rest/v1/rpc/reevaluate_followup_action",
            content=body,
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"reevaluate_followup_action_failed: HTTP {response.status_code}"
            )
        operation = "reevaluate_followup_action"
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        returned_action_id = _required_string(
            row, "action_id", operation=operation
        )
        if returned_action_id != action_id:
            raise SupabaseError(f"{operation}_action_mismatch")
        return ReevaluationDecision(
            action_id=returned_action_id,
            decision=_required_enum(
                row,
                "decision",
                {"execute", "cancel", "pause", "expire", "escalate"},
                operation=operation,
            ),
            reason_code=_required_string(row, "reason_code", operation=operation),
            case_version=_required_int(row, "case_version", operation=operation),
            sequence_revision=_required_int(
                row, "sequence_revision", operation=operation
            ),
        )

    async def reserve_followup_delivery_attempt(
        self,
        *,
        action_id: str,
        worker_id: str,
        lease_generation: int,
        expected_case_version: int,
        expected_sequence_revision: int,
        channel: str,
        mode: str,
        now: str,
    ) -> DeliveryAttempt:
        """Reserve the durable effect ledger entry for an execute decision."""
        operation = "reserve_followup_delivery_attempt"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_action_id": action_id,
                "p_worker_id": worker_id,
                "p_lease_generation": lease_generation,
                "p_expected_case_version": expected_case_version,
                "p_expected_sequence_revision": expected_sequence_revision,
                "p_channel": channel,
                "p_mode": mode,
                "p_now": now,
            }, ensure_ascii=False),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        returned_action_id = _required_string(row, "action_id", operation=operation)
        if returned_action_id != action_id:
            raise SupabaseError(f"{operation}_action_mismatch")
        returned_generation = _required_int(
            row, "lease_generation", operation=operation
        )
        if returned_generation != lease_generation:
            raise SupabaseError(f"{operation}_lease_mismatch")
        returned_case_version = _required_int(
            row, "expected_case_version", operation=operation
        )
        returned_sequence_revision = _required_int(
            row, "expected_sequence_revision", operation=operation
        )
        if (
            returned_case_version != expected_case_version
            or returned_sequence_revision != expected_sequence_revision
        ):
            raise SupabaseError(f"{operation}_revision_mismatch")
        return DeliveryAttempt(
            attempt_id=_required_string(row, "id", operation=operation),
            action_id=returned_action_id,
            idempotency_key=_required_string(
                row, "idempotency_key", operation=operation
            ),
            attempt_number=_required_int(
                row, "attempt_number", operation=operation
            ),
            channel=_required_enum(
                row, "channel", {"whatsapp"}, operation=operation
            ),
            mode=_required_enum(
                row,
                "mode",
                {"freeform", "approved_template"},
                operation=operation,
            ),
            phase=_required_enum(
                row,
                "phase",
                {"reserved", "request_started"},
                operation=operation,
            ),
            lease_generation=returned_generation,
            expected_case_version=returned_case_version,
            expected_sequence_revision=returned_sequence_revision,
        )

    async def mark_followup_request_started(
        self,
        *,
        action_id: str,
        attempt_id: str,
        worker_id: str,
        lease_generation: int,
        now: str,
        pilot_boundary: PilotBoundaryConfig | None = None,
        anchor_type: str | None = None,
    ) -> DeliveryAttempt:
        """Persist the last authorization boundary immediately before HTTP."""
        if anchor_type == "payment_failure":
            if pilot_boundary is None:
                raise SupabaseError("payment_failure_pilot_boundary_required")
            operation = "mark_portable_payment_failure_request_started"
        elif pilot_boundary is not None:
            operation = "mark_lancemos_pilot_request_started"
        else:
            operation = "mark_followup_request_started"
        rpc_body: dict[str, object] = {
            "p_action_id": action_id,
            "p_attempt_id": attempt_id,
            "p_worker_id": worker_id,
            "p_lease_generation": lease_generation,
            "p_now": now,
        }
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps(rpc_body, ensure_ascii=False),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        try:
            rows = _response_rows(response, operation=operation)
            if len(rows) != 1:
                raise SupabaseError(f"{operation}_invalid_shape")
            row = rows[0]
            returned_attempt_id = _required_string(row, "id", operation=operation)
            returned_action_id = _required_string(row, "action_id", operation=operation)
            returned_generation = _required_int(
                row, "lease_generation", operation=operation
            )
            phase = _required_enum(
                row, "phase", {"request_started"}, operation=operation
            )
            if returned_attempt_id != attempt_id:
                raise SupabaseError(f"{operation}_attempt_mismatch")
            if returned_action_id != action_id:
                raise SupabaseError(f"{operation}_action_mismatch")
            if returned_generation != lease_generation:
                raise SupabaseError(f"{operation}_lease_mismatch")
            if pilot_boundary is not None:
                _required_string(row, "pilot_authorization_id", operation=operation)
                _required_int(row, "pilot_runtime_generation", operation=operation)
                if not isinstance(row.get("pilot_authorization_replayed"), bool):
                    raise SupabaseError(f"{operation}_invalid_row")
            return DeliveryAttempt(
                attempt_id=returned_attempt_id,
                action_id=returned_action_id,
                idempotency_key=_required_string(
                    row, "idempotency_key", operation=operation
                ),
                attempt_number=_required_int(
                    row, "attempt_number", operation=operation
                ),
                channel=_required_enum(
                    row, "channel", {"whatsapp"}, operation=operation
                ),
                mode=_required_enum(
                    row,
                    "mode",
                    {"freeform", "approved_template"},
                    operation=operation,
                ),
                phase=phase,
                lease_generation=returned_generation,
                expected_case_version=_required_int(
                    row, "expected_case_version", operation=operation
                ),
                expected_sequence_revision=_required_int(
                    row, "expected_sequence_revision", operation=operation
                ),
            )
        except SupabaseError as exc:
            raise SupabaseCommittedResponseError(str(exc)) from exc

    async def finalize_followup_delivery_attempt(
        self,
        *,
        action_id: str,
        attempt_id: str,
        worker_id: str,
        lease_generation: int,
        outcome: str,
        remote_message_id: str | None,
        accepted_message_id: str | None,
        reason_code: str,
        next_attempt_at: str | None,
        reconciliation_deadline: str | None,
        now: str,
    ) -> DeliveryFinalization:
        """Finalize one started non-accepted attempt through the fenced RPC."""
        operation = "finalize_followup_delivery_attempt"
        if outcome == "accepted_by_chatwoot":
            raise SupabaseError(f"{operation}_canonical_acceptance_required")
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_action_id": action_id,
                "p_attempt_id": attempt_id,
                "p_worker_id": worker_id,
                "p_lease_generation": lease_generation,
                "p_outcome": outcome,
                "p_remote_message_id": remote_message_id,
                "p_accepted_message_id": accepted_message_id,
                "p_reason_code": reason_code,
                "p_next_attempt_at": next_attempt_at,
                "p_reconciliation_deadline": reconciliation_deadline,
                "p_now": now,
            }, ensure_ascii=False),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        returned_action_id = _required_string(row, "id", operation=operation)
        if returned_action_id != action_id:
            raise SupabaseError(f"{operation}_action_mismatch")
        return DeliveryFinalization(
            action_id=returned_action_id,
            status=_required_enum(
                row,
                "status",
                {
                    "pending",
                    "deferred",
                    "retryable_failed",
                    "delivery_unknown",
                    "accepted_by_chatwoot",
                    "cancelled",
                    "skipped",
                    "expired",
                    "permanent_failed",
                    "superseded",
                },
                operation=operation,
            ),
            terminal_reason=_optional_string(
                row, "terminal_reason", operation=operation
            ),
        )

    async def record_and_finalize_followup_acceptance(
        self,
        *,
        action_id: str,
        attempt_id: str,
        worker_id: str,
        lease_generation: int,
        external_conversation_id: str,
        remote_message_id: str,
        message_content: str,
        now: str,
    ) -> DeliveryFinalization:
        """Atomically persist the canonical accepted message and finalize its attempt."""
        operation = "record_and_finalize_followup_acceptance"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_action_id": action_id,
                "p_attempt_id": attempt_id,
                "p_worker_id": worker_id,
                "p_lease_generation": lease_generation,
                "p_external_conversation_id": str(external_conversation_id),
                "p_remote_message_id": str(remote_message_id),
                "p_message_content": message_content,
                "p_now": now,
            }, ensure_ascii=False),
        )
        if response.status_code != 200:
            raise SupabaseError(f"{operation}_failed: HTTP {response.status_code}")
        rows = _response_rows(response, operation=operation)
        if len(rows) != 1:
            raise SupabaseError(f"{operation}_invalid_shape")
        row = rows[0]
        returned_action_id = _required_string(row, "id", operation=operation)
        if returned_action_id != action_id:
            raise SupabaseError(f"{operation}_action_mismatch")
        status = _required_enum(
            row,
            "status",
            {"accepted_by_chatwoot"},
            operation=operation,
        )
        return DeliveryFinalization(
            action_id=returned_action_id,
            status=status,
            terminal_reason=_optional_string(
                row, "terminal_reason", operation=operation
            ),
        )

    # ── Contact lookup ────────────────────────────────────────────

    async def find_contact_by_email(self, email: str) -> ContactMatch | None:
        """Find a contact via a normalised email in contact_points."""
        response = await self._request(
            "GET",
            "/rest/v1/contact_points",
            params={
                "select": (
                    "contact_id,"
                    "normalized_value,"
                    "type,"
                    "contacts!inner("
                    "id,full_name,email,phone,"
                    "contact_permission,lifecycle_status"
                    ")"
                ),
                "normalized_value": f"eq.{email}",
                "type": "eq.email",
                "limit": "2",
            },
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"find_contact_by_email_failed: HTTP {response.status_code}"
            )
        rows = _response_rows(response, operation="find_contact_by_email")
        if not rows:
            return None
        if len(rows) != 1:
            raise SupabaseError("find_contact_by_email_ambiguous")
        row = rows[0]
        contact = row.get("contacts")
        if not isinstance(contact, dict):
            raise SupabaseError("find_contact_by_email_invalid_row")
        return ContactMatch(
            contact_id=_required_string(
                contact, "id", operation="find_contact_by_email"
            ),
            full_name=_optional_string(
                contact, "full_name", operation="find_contact_by_email"
            ),
            email=_optional_string(
                contact, "email", operation="find_contact_by_email"
            ),
            phone=_optional_string(
                contact, "phone", operation="find_contact_by_email"
            ),
            contact_permission=_required_enum(
                contact,
                "contact_permission",
                _CONTACT_PERMISSIONS,
                operation="find_contact_by_email",
            ),
            lifecycle_status=_required_enum(
                contact,
                "lifecycle_status",
                _LIFECYCLE_STATUSES,
                operation="find_contact_by_email",
            ),
            matched_by="email",
        )

    async def find_contact_by_phone(self, phone: str) -> ContactMatch | None:
        """Find a contact via a normalised phone in contact_points."""
        response = await self._request(
            "GET",
            "/rest/v1/contact_points",
            params={
                "select": (
                    "contact_id,"
                    "normalized_value,"
                    "type,"
                    "contacts!inner("
                    "id,full_name,email,phone,"
                    "contact_permission,lifecycle_status"
                    ")"
                ),
                "normalized_value": f"eq.{phone}",
                "type": "eq.phone",
                "limit": "2",
            },
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"find_contact_by_phone_failed: HTTP {response.status_code}"
            )
        rows = _response_rows(response, operation="find_contact_by_phone")
        if not rows:
            return None
        if len(rows) != 1:
            raise SupabaseError("find_contact_by_phone_ambiguous")
        row = rows[0]
        contact = row.get("contacts")
        if not isinstance(contact, dict):
            raise SupabaseError("find_contact_by_phone_invalid_row")
        return ContactMatch(
            contact_id=_required_string(
                contact, "id", operation="find_contact_by_phone"
            ),
            full_name=_optional_string(
                contact, "full_name", operation="find_contact_by_phone"
            ),
            email=_optional_string(
                contact, "email", operation="find_contact_by_phone"
            ),
            phone=_optional_string(
                contact, "phone", operation="find_contact_by_phone"
            ),
            contact_permission=_required_enum(
                contact,
                "contact_permission",
                _CONTACT_PERMISSIONS,
                operation="find_contact_by_phone",
            ),
            lifecycle_status=_required_enum(
                contact,
                "lifecycle_status",
                _LIFECYCLE_STATUSES,
                operation="find_contact_by_phone",
            ),
            matched_by="phone",
        )

    # ── Contact creation ──────────────────────────────────────────

    async def create_contact(
        self,
        *,
        full_name: str | None,
        email: str | None,
        phone: str | None,
        country_iso: str | None,
    ) -> str:
        """Create a new contact and return its id."""
        body = json.dumps(
            {
                "full_name": full_name,
                "email": email,
                "phone": phone,
                "country_iso": country_iso,
                "lifecycle_status": "lead",
                "contact_permission": "unknown",
            },
            ensure_ascii=False,
        )
        response = await self._request(
            "POST",
            "/rest/v1/contacts",
            content=body,
            prefer="return=representation",
        )
        if response.status_code != 201:
            raise SupabaseError(
                f"create_contact_failed: HTTP {response.status_code}"
            )
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            raise SupabaseError("create_contact_no_id_returned")
        return rows[0]["id"]

    async def create_contact_point(
        self,
        *,
        contact_id: str,
        point_type: str,
        raw_value: str,
        normalized_value: str,
        source: str,
        source_event_id: str,
    ) -> None:
        """Create a contact_point for a contact."""
        body = json.dumps(
            {
                "contact_id": contact_id,
                "type": point_type,
                "raw_value": raw_value,
                "normalized_value": normalized_value,
                "source": source,
                "source_event_id": source_event_id,
            },
            ensure_ascii=False,
        )
        response = await self._request(
            "POST",
            "/rest/v1/contact_points",
            content=body,
            prefer="return=minimal,resolution=ignore-duplicates",
        )
        if response.status_code not in (200, 201, 409):
            raise SupabaseError(
                f"create_contact_point_failed: HTTP {response.status_code}"
            )

    # ── Conversation lookup ───────────────────────────────────────

    async def fetch_conversations(
        self, *, contact_id: str
    ) -> list[ConversationSummary]:
        """Fetch all conversations for a contact."""
        response = await self._request(
            "GET",
            "/rest/v1/conversations",
            params={
                "select": (
                    "id,status,automation_status,human_takeover,"
                    "last_message_direction,last_inbound_at,"
                    "last_outbound_at,paused_until"
                ),
                "contact_id": f"eq.{contact_id}",
                "order": "created_at.desc",
            },
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"fetch_conversations_failed: HTTP {response.status_code}"
            )
        operation = "fetch_conversations"
        rows = _response_rows(response, operation=operation)
        summaries: list[ConversationSummary] = []
        for row in rows:
            human_takeover = row.get("human_takeover")
            if not isinstance(human_takeover, bool):
                raise SupabaseError(f"{operation}_invalid_row")
            summaries.append(
                ConversationSummary(
                    conversation_id=_required_string(
                        row, "id", operation=operation
                    ),
                    status=_required_enum(
                        row, "status", _CONVERSATION_STATUSES,
                        operation=operation,
                    ),
                    automation_status=_required_enum(
                        row, "automation_status", _AUTOMATION_STATUSES,
                        operation=operation,
                    ),
                    human_takeover=human_takeover,
                    last_message_direction=_optional_enum(
                        row,
                        "last_message_direction",
                        {"inbound", "outbound"},
                        operation=operation,
                    ),
                    last_inbound_at=_optional_string(
                        row, "last_inbound_at", operation=operation
                    ),
                    last_outbound_at=_optional_string(
                        row, "last_outbound_at", operation=operation
                    ),
                    paused_until=_optional_string(
                        row, "paused_until", operation=operation
                    ),
                )
            )
        return summaries

    # ── Recovery case lookup ──────────────────────────────────────

    async def fetch_recovery_cases(
        self, *, contact_id: str
    ) -> list[RecoveryCaseSummary]:
        """Fetch all recovery cases for a contact."""
        response = await self._request(
            "GET",
            "/rest/v1/recovery_cases",
            params={
                "select": (
                    "id,status,lead_stage,current_goal,product_name"
                ),
                "contact_id": f"eq.{contact_id}",
                "order": "created_at.desc",
            },
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"fetch_recovery_cases_failed: HTTP {response.status_code}"
            )
        operation = "fetch_recovery_cases"
        rows = _response_rows(response, operation=operation)
        summaries: list[RecoveryCaseSummary] = []
        for row in rows:
            summaries.append(
                RecoveryCaseSummary(
                    recovery_case_id=_required_string(
                        row, "id", operation=operation
                    ),
                    status=_required_enum(
                        row, "status", _RECOVERY_CASE_STATUSES,
                        operation=operation,
                    ),
                    lead_stage=_required_enum(
                        row, "lead_stage", _LEAD_STAGES,
                        operation=operation,
                    ),
                    current_goal=_optional_string(
                        row, "current_goal", operation=operation
                    ),
                    product_name=_required_string(
                        row, "product_name", operation=operation
                    ),
                )
            )
        return summaries

    # ── Channel identity lookup ────────────────────────────────────

    async def fetch_channel_identities(
        self, *, contact_id: str
    ) -> list[ChannelIdentitySummary]:
        """Fetch all channel identities for a contact."""
        response = await self._request(
            "GET",
            "/rest/v1/channel_identities",
            params={
                "select": "id,channel,external_user_id,identity_status",
                "contact_id": f"eq.{contact_id}",
                "order": "created_at.desc",
            },
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"fetch_channel_identities_failed: HTTP {response.status_code}"
            )
        operation = "fetch_channel_identities"
        rows = _response_rows(response, operation=operation)
        summaries: list[ChannelIdentitySummary] = []
        for row in rows:
            summaries.append(
                ChannelIdentitySummary(
                    channel_identity_id=_required_string(
                        row, "id", operation=operation
                    ),
                    channel=_required_enum(
                        row, "channel", _CHANNELS, operation=operation
                    ),
                    external_user_id=_optional_string(
                        row, "external_user_id", operation=operation
                    ),
                    identity_status=_required_enum(
                        row, "identity_status", _IDENTITY_STATUSES,
                        operation=operation,
                    ),
                )
            )
        return summaries

    # ── Recovery case creation ────────────────────────────────────

    async def create_recovery_case(
        self,
        *,
        contact_id: str,
        abandonment_event_id: str,
        external_product_id: str | None,
        product_name: str | None,
        offer_code: str | None,
        grace_expires_at: str,
    ) -> str:
        """Create a recovery case and return its id."""
        body = json.dumps(
            {
                "contact_id": contact_id,
                "abandonment_event_id": abandonment_event_id,
                "source": "hotmart",
                "external_product_id": external_product_id,
                "product_name": product_name,
                "offer_code": offer_code,
                "status": "grace_period",
                "lead_stage": "new",
                "grace_expires_at": grace_expires_at,
            },
            ensure_ascii=False,
        )
        response = await self._request(
            "POST",
            "/rest/v1/recovery_cases",
            content=body,
            prefer="return=representation",
        )
        if response.status_code != 201:
            raise SupabaseError(
                f"create_recovery_case_failed: HTTP {response.status_code}"
            )
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            raise SupabaseError("create_recovery_case_no_id_returned")
        return rows[0]["id"]

    # ── Identity resolution attempt logging ────────────────────────

    async def log_resolution_attempt(
        self,
        *,
        recovery_case_id: str,
        channel: str,
        strategy: str,
        status: str,
        confidence: float | None = None,
    ) -> None:
        """Log an identity resolution attempt."""
        body_dict: dict[str, Any] = {
            "recovery_case_id": recovery_case_id,
            "channel": channel,
            "strategy": strategy,
            "status": status,
        }
        if confidence is not None:
            body_dict["confidence"] = confidence
        body = json.dumps(body_dict, ensure_ascii=False)
        response = await self._request(
            "POST",
            "/rest/v1/identity_resolution_attempts",
            content=body,
            prefer="return=minimal",
        )
        if response.status_code != 201:
            raise SupabaseError(
                f"log_resolution_attempt_failed: HTTP {response.status_code}"
            )
