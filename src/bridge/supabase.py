"""Minimal Supabase REST client for webhook event persistence and identity resolution.

Uses the PostgREST API exposed by every Supabase project — no SDK dependency,
just httpx, matching the project's existing conventions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


class SupabaseError(RuntimeError):
    """Raised when a Supabase REST call fails."""


class SupabaseCommittedResponseError(SupabaseError):
    """Raised when a successful mutating RPC returns an invalid committed row."""


# ── Data classes for resolution results ──────────────────────────────


@dataclass(frozen=True)
class InsertResult:
    """Outcome of an insert_webhook_event call."""

    inserted: bool


@dataclass(frozen=True)
class CartRecoveryPlan:
    """Durable case, sequence, and next action created by PostgreSQL."""

    recovery_case_id: str
    followup_sequence_id: str
    scheduled_action_id: str
    created: bool


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


def _required_string(
    row: dict[str, Any],
    key: str,
    *,
    operation: str,
) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise SupabaseError(f"{operation}_invalid_row")
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
        raise SupabaseError(f"{operation}_invalid_row")
    return value


def _optional_string(
    row: dict[str, Any],
    key: str,
    *,
    operation: str,
) -> str | None:
    value = row.get(key)
    if value is not None and not isinstance(value, str):
        raise SupabaseError(f"{operation}_invalid_row")
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
        raise SupabaseError(f"{operation}_invalid_row")
    return value


def _required_int(
    row: dict[str, Any],
    key: str,
    *,
    operation: str,
) -> int:
    value = row.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise SupabaseError(f"{operation}_invalid_row")
    return value


def _optional_positive_int(
    row: dict[str, Any], key: str, *, operation: str
) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SupabaseError(f"{operation}_invalid_row")
    return value


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
        return InsertResult(inserted=True)

    async def fetch_pending_events(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch webhook events in 'received' status, oldest first."""
        response = await self._request(
            "GET",
            "/rest/v1/webhook_events",
            params={
                "select": "id,source,external_event_id,event_type,payload",
                "processing_status": "eq.received",
                "order": "received_at.asc",
                "limit": str(limit),
            },
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
            f"/rest/v1/webhook_events",
            params={"id": f"eq.{event_id}"},
            content=body,
            prefer="return=minimal",
        )
        if response.status_code not in (200, 204):
            raise SupabaseError(
                f"update_event_status_failed: HTTP {response.status_code}"
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
    ) -> CartRecoveryPlan:
        """Atomically create or reuse the durable cart-recovery plan."""
        body = json.dumps({
            "p_webhook_event_id": webhook_event_id,
            "p_contact_id": contact_id,
            "p_external_product_id": external_product_id,
            "p_product_name": product_name,
            "p_offer_code": offer_code,
            "p_policy_key": policy_key,
            "p_policy_version": policy_version,
            "p_abandoned_at": abandoned_at,
        }, ensure_ascii=False)
        response = await self._request(
            "POST",
            "/rest/v1/rpc/plan_cart_recovery",
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
            chatwoot_account_id=_optional_positive_int(
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
    ) -> DeliveryAttempt:
        """Persist the last authorization boundary immediately before HTTP."""
        operation = "mark_followup_request_started"
        response = await self._request(
            "POST",
            f"/rest/v1/rpc/{operation}",
            content=json.dumps({
                "p_action_id": action_id,
                "p_attempt_id": attempt_id,
                "p_worker_id": worker_id,
                "p_lease_generation": lease_generation,
                "p_now": now,
            }, ensure_ascii=False),
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
                "limit": "1",
            },
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"find_contact_by_email_failed: HTTP {response.status_code}"
            )
        rows = _response_rows(response, operation="find_contact_by_email")
        if not rows:
            return None
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
                "limit": "1",
            },
        )
        if response.status_code != 200:
            raise SupabaseError(
                f"find_contact_by_phone_failed: HTTP {response.status_code}"
            )
        rows = _response_rows(response, operation="find_contact_by_phone")
        if not rows:
            return None
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
