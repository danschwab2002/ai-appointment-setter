"""Validation for supervised operator correlation resolution commands."""

from __future__ import annotations

from typing import Any
from uuid import UUID

ACTIONS = frozenset({"resolve_with_candidate", "close_without_match"})
LINK_VERIFICATION_BASES = frozenset(
    {
        "external_transaction_reference",
        "operator_source_record",
        "customer_confirmation",
    }
)
CLOSE_VERIFICATION_BASES = frozenset({"no_valid_candidate_after_review"})
UNRESOLVED_OUTCOMES = frozenset({"unmatched", "ambiguous", "conflict"})
RESOLUTION_OUTCOMES = frozenset({"linked_candidate", "closed_without_match"})


class InvalidCorrelationResolution(ValueError):
    """Raised when a resolution request or durable response is unsafe."""


def _uuid(value: object, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise InvalidCorrelationResolution(f"invalid_{field}")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise InvalidCorrelationResolution(f"invalid_{field}") from exc


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidCorrelationResolution(f"invalid_{field}")
    return value


def validate_prepare_resolution(
    payload: dict[str, object],
) -> dict[str, str | None]:
    """Validate the model-supplied portion of a prepare request."""
    case_id = _uuid(payload.get("case_id"), "case_id")
    idempotency_key = _uuid(payload.get("idempotency_key"), "idempotency_key")
    action = _string(payload.get("action"), "action")
    candidate_id = _uuid(payload.get("candidate_id"), "candidate_id", nullable=True)
    verification_basis = _string(
        payload.get("verification_basis"), "verification_basis"
    )
    if action not in ACTIONS:
        raise InvalidCorrelationResolution("invalid_action")
    if action == "resolve_with_candidate":
        if candidate_id is None or verification_basis not in LINK_VERIFICATION_BASES:
            raise InvalidCorrelationResolution("invalid_resolution_combination")
    elif candidate_id is not None or verification_basis not in CLOSE_VERIFICATION_BASES:
        raise InvalidCorrelationResolution("invalid_resolution_combination")
    return {
        "case_id": case_id,
        "idempotency_key": idempotency_key,
        "action": action,
        "candidate_id": candidate_id,
        "verification_basis": verification_basis,
    }


def build_resolution_command(raw: dict[str, Any]) -> dict[str, object]:
    """Validate and minimize one prepared command returned by PostgreSQL."""
    action = _string(raw.get("action"), "action")
    candidate_id = _uuid(
        raw.get("selected_purchase_intent_id"),
        "selected_purchase_intent_id",
        nullable=True,
    )
    verification_basis = _string(
        raw.get("verification_basis"), "verification_basis"
    )
    validate_prepare_resolution(
        {
            "case_id": raw.get("webhook_event_id"),
            "idempotency_key": raw.get("idempotency_key"),
            "action": action,
            "candidate_id": candidate_id,
            "verification_basis": verification_basis,
        }
    )
    outcome = _string(raw.get("deterministic_outcome"), "deterministic_outcome")
    if outcome not in UNRESOLVED_OUTCOMES:
        raise InvalidCorrelationResolution("invalid_deterministic_outcome")
    candidate_count = raw.get("candidate_count")
    if (
        not isinstance(candidate_count, int)
        or isinstance(candidate_count, bool)
        or candidate_count < 0
    ):
        raise InvalidCorrelationResolution("invalid_candidate_count")
    if raw.get("requires_human_approval") is not True:
        raise InvalidCorrelationResolution("human_approval_not_required")
    if raw.get("automation_blocked") is not True:
        raise InvalidCorrelationResolution("automation_not_blocked")
    return {
        "command_id": _uuid(raw.get("command_id"), "command_id"),
        "idempotency_key": _uuid(raw.get("idempotency_key"), "idempotency_key"),
        "case_id": _uuid(raw.get("webhook_event_id"), "webhook_event_id"),
        "action": action,
        "candidate_id": candidate_id,
        "verification_basis": verification_basis,
        "deterministic_outcome": outcome,
        "deterministic_reason_code": _string(
            raw.get("deterministic_reason_code"), "deterministic_reason_code"
        ),
        "candidate_count": candidate_count,
        "expires_at": _string(raw.get("expires_at"), "expires_at"),
        "requires_human_approval": True,
        "automation_blocked": True,
    }


def validate_confirm_resolution(
    payload: dict[str, object],
) -> dict[str, str | None]:
    """Validate the immutable command identity echoed for confirmation."""
    command_id = _uuid(payload.get("command_id"), "command_id")
    expected_action = _string(payload.get("expected_action"), "expected_action")
    expected_candidate_id = _uuid(
        payload.get("expected_candidate_id"),
        "expected_candidate_id",
        nullable=True,
    )
    if expected_action not in ACTIONS:
        raise InvalidCorrelationResolution("invalid_expected_action")
    if (
        expected_action == "resolve_with_candidate"
        and expected_candidate_id is None
    ) or (
        expected_action == "close_without_match"
        and expected_candidate_id is not None
    ):
        raise InvalidCorrelationResolution("invalid_confirmation_combination")
    return {
        "command_id": command_id,
        "expected_action": expected_action,
        "expected_candidate_id": expected_candidate_id,
    }


def build_resolution_result(raw: dict[str, Any]) -> dict[str, object]:
    """Validate and minimize one applied manual resolution."""
    outcome = _string(raw.get("resolution_outcome"), "resolution_outcome")
    if outcome not in RESOLUTION_OUTCOMES:
        raise InvalidCorrelationResolution("invalid_resolution_outcome")
    effective_id = _uuid(
        raw.get("effective_purchase_intent_id"),
        "effective_purchase_intent_id",
        nullable=True,
    )
    if (outcome == "linked_candidate") != (effective_id is not None):
        raise InvalidCorrelationResolution("invalid_effective_purchase_intent")
    deterministic_outcome = _string(
        raw.get("deterministic_outcome"), "deterministic_outcome"
    )
    if deterministic_outcome not in UNRESOLVED_OUTCOMES:
        raise InvalidCorrelationResolution("invalid_deterministic_outcome")
    replayed = raw.get("replayed")
    if not isinstance(replayed, bool):
        raise InvalidCorrelationResolution("invalid_replayed")
    if raw.get("automation_blocked") is not True:
        raise InvalidCorrelationResolution("automation_not_blocked")
    return {
        "resolution_id": _uuid(raw.get("resolution_id"), "resolution_id"),
        "command_id": _uuid(raw.get("command_id"), "command_id"),
        "case_id": _uuid(raw.get("webhook_event_id"), "webhook_event_id"),
        "resolution_outcome": outcome,
        "effective_purchase_intent_id": effective_id,
        "deterministic_outcome": deterministic_outcome,
        "applied_at": _string(raw.get("applied_at"), "applied_at"),
        "replayed": replayed,
        "automation_blocked": True,
    }
