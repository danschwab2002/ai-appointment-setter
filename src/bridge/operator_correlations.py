"""Read-only presentation helpers for unresolved deterministic correlations."""

from __future__ import annotations

from typing import Any
from uuid import UUID

UNRESOLVED_OUTCOMES = frozenset({"unmatched", "ambiguous", "conflict"})
REASON_EXPLANATIONS = {
    "scope_not_configured": (
        "El producto o la oferta del evento no tienen un alcance de correlación activo."
    ),
    "identity_not_found": (
        "Ni el email ni el teléfono encontraron una intención de compra elegible."
    ),
    "multiple_candidates": (
        "Las señales coinciden con más de una intención de compra elegible."
    ),
    "email_phone_conflict": (
        "El email y el teléfono apuntan a intenciones diferentes o una señal "
        "contradice a la otra."
    ),
}


class InvalidCorrelationEvidence(ValueError):
    """Raised when durable evidence cannot be presented safely."""


def mask_email(value: str | None) -> str | None:
    """Mask an email while retaining enough shape for operator comparison."""
    if value is None:
        return None
    local, separator, domain = value.partition("@")
    if not separator or not local or not domain:
        return None
    if len(local) <= 2:
        masked_local = "***"
    else:
        masked_local = f"{local[0]}***{local[-1]}"
    return f"{masked_local}@{domain}"


def mask_phone(value: str | None) -> str | None:
    """Mask a canonical phone while retaining only its final four digits."""
    if value is None:
        return None
    if len(value) < 4 or not value.isdigit():
        return None
    return f"{'*' * (len(value) - 4)}{value[-4:]}"


def _required_string(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise InvalidCorrelationEvidence(f"invalid_{field}")
    return value


def _required_uuid(row: dict[str, Any], field: str) -> str:
    value = _required_string(row, field)
    try:
        UUID(value)
    except ValueError as exc:
        raise InvalidCorrelationEvidence(f"invalid_{field}") from exc
    return value


def _masked_identity(value: object) -> dict[str, object]:
    if value is None:
        return {
            "email_present": False,
            "phone_present": False,
            "masked_email": None,
            "masked_phone": None,
        }
    if not isinstance(value, dict):
        raise InvalidCorrelationEvidence("invalid_identity")
    if "normalized_email" in value or "normalized_phone" in value:
        raise InvalidCorrelationEvidence("contains_raw_identity")
    masked_email = value.get("masked_email")
    masked_phone = value.get("masked_phone")
    if masked_email is not None and (
        not isinstance(masked_email, str)
        or "***" not in masked_email.partition("@")[0]
        or not masked_email.partition("@")[1]
        or not masked_email.partition("@")[2]
        or any(character.isspace() for character in masked_email)
    ):
        raise InvalidCorrelationEvidence("invalid_identity_email")
    if masked_phone is not None and (
        not isinstance(masked_phone, str)
        or len(masked_phone) < 4
        or not masked_phone[-4:].isdigit()
        or any(character != "*" for character in masked_phone[:-4])
    ):
        raise InvalidCorrelationEvidence("invalid_identity_phone")
    email_present = value.get("email_present", masked_email is not None)
    phone_present = value.get("phone_present", masked_phone is not None)
    if not isinstance(email_present, bool) or email_present != (masked_email is not None):
        raise InvalidCorrelationEvidence("invalid_identity_email_presence")
    if not isinstance(phone_present, bool) or phone_present != (masked_phone is not None):
        raise InvalidCorrelationEvidence("invalid_identity_phone_presence")
    return {
        "email_present": email_present,
        "phone_present": phone_present,
        "masked_email": masked_email,
        "masked_phone": masked_phone,
    }


def _build_scope(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidCorrelationEvidence("invalid_scope")
    return {
        field: _required_string(value, field)
        for field in ("tenant_ref", "funnel_ref", "product_ref", "offer_ref")
    }


def _build_candidates(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise InvalidCorrelationEvidence("invalid_candidates")
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw_candidate in value:
        if not isinstance(raw_candidate, dict):
            raise InvalidCorrelationEvidence("invalid_candidate")
        candidate_id = _required_uuid(raw_candidate, "purchase_intent_id")
        if candidate_id in seen:
            raise InvalidCorrelationEvidence("duplicate_candidate")
        seen.add(candidate_id)
        email_match = raw_candidate.get("email_match")
        phone_match = raw_candidate.get("phone_match")
        if not isinstance(email_match, bool) or not isinstance(phone_match, bool):
            raise InvalidCorrelationEvidence("invalid_candidate_match")
        if not email_match and not phone_match:
            raise InvalidCorrelationEvidence("invalid_candidate_match")
        masked = _masked_identity(raw_candidate)
        candidates.append(
            {
                "purchase_intent_id": candidate_id,
                "matched_by": [
                    signal
                    for signal, matched in (
                        ("email", email_match),
                        ("phone", phone_match),
                    )
                    if matched
                ],
                "submitted_at": _required_string(raw_candidate, "submitted_at"),
                "lifecycle_state": _required_string(
                    raw_candidate, "lifecycle_state"
                ),
                "masked_email": masked["masked_email"],
                "masked_phone": masked["masked_phone"],
            }
        )
    return candidates


def build_unresolved_correlation(
    raw: dict[str, Any], *, include_candidates: bool
) -> dict[str, object]:
    """Validate, explain and PII-minimize one durable unresolved correlation."""
    outcome = raw.get("outcome")
    if outcome not in UNRESOLVED_OUTCOMES or raw.get("manual_handoff_required") is not True:
        raise InvalidCorrelationEvidence("not_unresolved")
    reason_code = _required_string(raw, "reason_code")
    reason = REASON_EXPLANATIONS.get(reason_code)
    if reason is None:
        raise InvalidCorrelationEvidence("invalid_reason_code")
    candidate_count = raw.get("candidate_count")
    if (
        not isinstance(candidate_count, int)
        or isinstance(candidate_count, bool)
        or candidate_count < 0
    ):
        raise InvalidCorrelationEvidence("invalid_candidate_count")
    candidates = _build_candidates(raw.get("candidates", []))
    if len(candidates) != candidate_count:
        raise InvalidCorrelationEvidence("candidate_count_mismatch")
    if outcome == "unmatched" and candidate_count != 0:
        raise InvalidCorrelationEvidence("invalid_unmatched_candidates")
    if outcome == "ambiguous" and candidate_count <= 1:
        raise InvalidCorrelationEvidence("invalid_ambiguous_candidates")
    if outcome == "conflict" and candidate_count < 1:
        raise InvalidCorrelationEvidence("invalid_conflict_candidates")

    result: dict[str, object] = {
        "case_id": _required_uuid(raw, "webhook_event_id"),
        "event_type": _required_string(raw, "event_type"),
        "outcome": outcome,
        "reason_code": reason_code,
        "reason": reason,
        "candidate_count": candidate_count,
        "observed_at": _required_string(raw, "observed_at"),
        "automation_blocked": True,
        "scope": _build_scope(raw.get("scope")),
        "identity": _masked_identity(raw.get("identity")),
    }
    if include_candidates:
        result["candidates"] = candidates
    return result
