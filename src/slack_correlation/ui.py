"""Build PII-minimized native Slack surfaces for correlation review."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

_OUTCOME_LABELS = {
    "unmatched": "Sin coincidencia",
    "ambiguous": "Coincidencia ambigua",
    "conflict": "Conflicto de identidad",
}


class InvalidSlackCorrelationCase(ValueError):
    """Raised when a case cannot be projected safely to Slack."""


_MASKED_EMAIL = re.compile(
    r"^[A-Za-z0-9._+\-]*\*{3,}[A-Za-z0-9._+\-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$"
)
_MASKED_PHONE = re.compile(r"^\*{4,}[0-9]{4}$")


def _safe_masked(value: object, *, kind: str) -> str | None:
    if value is None:
        return None
    pattern = _MASKED_EMAIL if kind == "email" else _MASKED_PHONE
    if (
        not isinstance(value, str)
        or pattern.fullmatch(value) is None
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in value for character in "`<>&")
    ):
        raise InvalidSlackCorrelationCase(f"invalid_masked_{kind}")
    return value


def _case_id(case: dict[str, object]) -> str:
    value = case.get("case_id")
    if not isinstance(value, str):
        raise InvalidSlackCorrelationCase("invalid_case_id")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise InvalidSlackCorrelationCase("invalid_case_id") from exc


def _masked_identity(case: dict[str, object]) -> tuple[str | None, str | None]:
    if any(
        key in case
        for key in ("email", "phone", "normalized_email", "normalized_phone")
    ):
        raise InvalidSlackCorrelationCase("raw_identity_forbidden")
    if "masked_email" in case:
        _safe_masked(case["masked_email"], kind="email")
    if "masked_phone" in case:
        _safe_masked(case["masked_phone"], kind="phone")
    identity = case.get("identity")
    if not isinstance(identity, dict):
        raise InvalidSlackCorrelationCase("invalid_identity")
    if "normalized_email" in identity or "normalized_phone" in identity:
        raise InvalidSlackCorrelationCase("raw_identity_forbidden")
    email = _safe_masked(identity.get("masked_email"), kind="email")
    phone = _safe_masked(identity.get("masked_phone"), kind="phone")
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
        if "normalized_email" in candidate or "normalized_phone" in candidate:
            raise InvalidSlackCorrelationCase("invalid_candidate_identity")
        try:
            email = _safe_masked(candidate.get("masked_email"), kind="email")
            phone = _safe_masked(candidate.get("masked_phone"), kind="phone")
        except InvalidSlackCorrelationCase:
            raise InvalidSlackCorrelationCase("invalid_candidate_identity") from None
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


def build_confirmation_modal(*, review_token: str, action: str) -> dict[str, Any]:
    label = (
        "vincular el candidato seleccionado"
        if action == "resolve_with_candidate"
        else "cerrar sin coincidencia válida"
    )
    return {
        "type": "modal",
        "callback_id": "confirm_operator_correlation_resolution",
        "private_metadata": json.dumps(
            {"review_token": str(UUID(review_token))}, separators=(",", ":")
        ),
        "title": {"type": "plain_text", "text": "Confirmar resolución"},
        "submit": {"type": "plain_text", "text": "Confirmar resolución"},
        "close": {"type": "plain_text", "text": "Cancelar"},
        "blocks": [
            _section(
                f"Vas a *{label}*.\nLa automatización continuará bloqueada."
            ),
        ],
    }


def build_processing_modal(*, review_token: str, phase: str) -> dict[str, Any]:
    if phase not in {"prepare", "confirm"}:
        raise ValueError("invalid_processing_phase")
    return {
        "type": "modal",
        "callback_id": "operator_correlation_resolution_processing",
        "private_metadata": json.dumps(
            {"review_token": str(UUID(review_token))}, separators=(",", ":")
        ),
        "title": {"type": "plain_text", "text": "Procesando resolución"},
        "close": {"type": "plain_text", "text": "Cerrar"},
        "blocks": [
            _section(
                "Procesando… La decisión quedó admitida de forma segura. "
                "Este modal se actualizará al terminar."
            )
        ],
    }


def build_success_modal() -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "operator_correlation_resolution_complete",
        "title": {"type": "plain_text", "text": "Resolución aplicada"},
        "close": {"type": "plain_text", "text": "Cerrar"},
        "blocks": [_section("La resolución fue aplicada y el mensaje raíz se actualizó.")],
    }


def build_safe_error_modal() -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "operator_correlation_resolution_failed",
        "title": {"type": "plain_text", "text": "Resolución no aplicada"},
        "close": {"type": "plain_text", "text": "Cerrar"},
        "blocks": [
            _section(
                "No se pudo comprobar la resolución. El caso permanece bloqueado; "
                "vuelve a abrirlo desde el mensaje antes de intentar otra acción."
            )
        ],
    }


def build_terminal_message(
    *, case_id: str, outcome: str, actor_id: str, applied_at: str
) -> dict[str, Any]:
    UUID(case_id)
    title = (
        "Resuelto — candidato vinculado"
        if outcome == "linked_candidate"
        else "Cerrado — sin coincidencia válida"
    )
    short_id = f"C-{case_id.split('-', 1)[0]}"
    return {
        "text": f"{title} · Caso {short_id}",
        "metadata": {
            "event_type": "operator_correlation_case",
            "event_payload": {"case_id": case_id, "state": outcome},
        },
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": title}},
            _section(
                f"*Caso:* `{short_id}`\n*Actor Slack:* `{actor_id}`\n"
                f"*Aplicado:* `{applied_at}`\nLa automatización permanece bloqueada."
            ),
        ],
    }
