"""Build PII-minimized native Slack surfaces for correlation review."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

_OUTCOME_LABELS = {
    "unmatched": "Sin coincidencia",
    "ambiguous": "Coincidencia ambigua",
    "conflict": "Conflicto de identidad",
}


class InvalidSlackCorrelationCase(ValueError):
    """Raised when a case cannot be projected safely to Slack."""


def _case_id(case: dict[str, object]) -> str:
    value = case.get("case_id")
    if not isinstance(value, str):
        raise InvalidSlackCorrelationCase("invalid_case_id")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise InvalidSlackCorrelationCase("invalid_case_id") from exc


def _masked_identity(case: dict[str, object]) -> tuple[str | None, str | None]:
    identity = case.get("identity")
    if not isinstance(identity, dict):
        raise InvalidSlackCorrelationCase("invalid_identity")
    if "normalized_email" in identity or "normalized_phone" in identity:
        raise InvalidSlackCorrelationCase("raw_identity_forbidden")
    email = identity.get("masked_email")
    phone = identity.get("masked_phone")
    if email is not None and (
        not isinstance(email, str)
        or "***" not in email.partition("@")[0]
        or not email.partition("@")[1]
        or not email.partition("@")[2]
    ):
        raise InvalidSlackCorrelationCase("invalid_masked_email")
    if phone is not None and (
        not isinstance(phone, str)
        or len(phone) < 4
        or not phone[-4:].isdigit()
        or any(character != "*" for character in phone[:-4])
    ):
        raise InvalidSlackCorrelationCase("invalid_masked_phone")
    return email, phone


def _section(text: str) -> dict[str, object]:
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }


def build_pending_message(
    case: dict[str, object], *, review_due_at: str
) -> dict[str, Any]:
    """Build one native Slack root message for an unresolved case."""
    case_id = _case_id(case)
    outcome = case.get("outcome")
    if not isinstance(outcome, str) or outcome not in _OUTCOME_LABELS:
        raise InvalidSlackCorrelationCase("invalid_outcome")
    candidate_count = case.get("candidate_count")
    if (
        not isinstance(candidate_count, int)
        or isinstance(candidate_count, bool)
        or candidate_count < 0
    ):
        raise InvalidSlackCorrelationCase("invalid_candidate_count")
    if case.get("automation_blocked") is not True:
        raise InvalidSlackCorrelationCase("automation_not_blocked")
    if not isinstance(review_due_at, str) or not review_due_at:
        raise InvalidSlackCorrelationCase("invalid_review_due_at")
    email, phone = _masked_identity(case)
    short_id = f"C-{case_id.split('-', 1)[0]}"
    identity_lines = [
        value
        for value in (
            f"Email observado: `{email}`" if email is not None else None,
            f"Teléfono observado: `{phone}`" if phone is not None else None,
        )
        if value is not None
    ]
    identity_text = "\n".join(identity_lines) or "Sin identidad proyectable"
    return {
        "text": f"Correlación pendiente · Caso {short_id}",
        "metadata": {
            "event_type": "operator_correlation_case",
            "event_payload": {"case_id": case_id},
        },
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "⚠️ Correlación pendiente"},
            },
            _section(
                f"*Caso:* `{short_id}`\n"
                f"*Resultado:* {_OUTCOME_LABELS[outcome]}\n"
                f"*Candidatos:* {candidate_count}"
            ),
            _section(identity_text),
            _section(
                f"*Estado:* Pendiente\n*Revisar antes de:* `{review_due_at}`\n"
                "La automatización permanece bloqueada."
            ),
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "review_operator_correlation",
                        "text": {"type": "plain_text", "text": "Revisar caso"},
                        "style": "primary",
                        "value": case_id,
                    }
                ],
            },
        ],
    }


def build_review_modal(
    case: dict[str, object], *, review_token: str
) -> dict[str, Any]:
    """Build the native Slack modal for one current candidate snapshot."""
    case_id = _case_id(case)
    try:
        canonical_review_token = str(UUID(review_token))
    except (TypeError, ValueError) as exc:
        raise InvalidSlackCorrelationCase("invalid_review_token") from exc
    outcome = case.get("outcome")
    if not isinstance(outcome, str) or outcome not in _OUTCOME_LABELS:
        raise InvalidSlackCorrelationCase("invalid_outcome")
    candidate_count = case.get("candidate_count")
    candidates = case.get("candidates")
    if (
        not isinstance(candidate_count, int)
        or isinstance(candidate_count, bool)
        or not isinstance(candidates, list)
        or len(candidates) != candidate_count
        or len(candidates) > 20
    ):
        raise InvalidSlackCorrelationCase("incomplete_candidate_snapshot")

    options: list[dict[str, object]] = []
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            raise InvalidSlackCorrelationCase("invalid_candidate")
        candidate_id = candidate.get("purchase_intent_id")
        if not isinstance(candidate_id, str):
            raise InvalidSlackCorrelationCase("invalid_candidate_id")
        try:
            candidate_id = str(UUID(candidate_id))
        except ValueError as exc:
            raise InvalidSlackCorrelationCase("invalid_candidate_id") from exc
        lifecycle = candidate.get("lifecycle_state")
        if lifecycle != "waiting_for_purchase":
            raise InvalidSlackCorrelationCase("ineligible_candidate")
        email = candidate.get("masked_email")
        phone = candidate.get("masked_phone")
        if email is not None and not isinstance(email, str):
            raise InvalidSlackCorrelationCase("invalid_candidate_identity")
        if phone is not None and (
            not isinstance(phone, str)
            or len(phone) < 4
            or not phone[-4:].isdigit()
            or any(character != "*" for character in phone[:-4])
        ):
            raise InvalidSlackCorrelationCase("invalid_candidate_identity")
        identity = " · ".join(
            value for value in (email, phone) if isinstance(value, str)
        ) or "sin identidad visible"
        options.append(
            {
                "text": {
                    "type": "plain_text",
                    "text": f"Candidato {index} · {identity}"[:75],
                },
                "value": candidate_id,
            }
        )
    options.append(
        {
            "text": {
                "type": "plain_text",
                "text": "Ningún candidato corresponde",
            },
            "value": "close_without_match",
        }
    )
    short_id = f"C-{case_id.split('-', 1)[0]}"
    return {
        "type": "modal",
        "callback_id": "prepare_operator_correlation_resolution",
        "private_metadata": json.dumps(
            {"review_token": canonical_review_token},
            separators=(",", ":"),
        ),
        "title": {"type": "plain_text", "text": "Revisar correlación"},
        "submit": {"type": "plain_text", "text": "Preparar resolución"},
        "close": {"type": "plain_text", "text": "Cancelar"},
        "blocks": [
            _section(
                f"*Caso:* `{short_id}`\n"
                f"*Resultado determinístico:* {_OUTCOME_LABELS[outcome]}\n"
                "La automatización permanece bloqueada."
            ),
            {
                "type": "input",
                "block_id": "resolution",
                "label": {"type": "plain_text", "text": "Resolución"},
                "element": {
                    "type": "radio_buttons",
                    "action_id": "selected_resolution",
                    "options": options,
                },
            },
            {
                "type": "input",
                "block_id": "verification",
                "label": {
                    "type": "plain_text",
                    "text": "Evidencia utilizada",
                },
                "element": {
                    "type": "static_select",
                    "action_id": "verification_basis",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Seleccionar evidencia",
                    },
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": label},
                            "value": value,
                        }
                        for value, label in (
                            (
                                "external_transaction_reference",
                                "Referencia externa de transacción",
                            ),
                            ("operator_source_record", "Registro fuente verificado"),
                            ("customer_confirmation", "Confirmación del cliente"),
                            (
                                "no_valid_candidate_after_review",
                                "Ningún candidato válido tras revisar",
                            ),
                        )
                    ],
                },
            },
        ],
    }
