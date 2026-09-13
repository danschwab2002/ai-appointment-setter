"""Build PII-minimized native Slack surfaces for correlation review."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from uuid import UUID

from slack_correlation.case_copy import CommercialCaseCopy, commercial_case_copy, operator_task

_OUTCOME_LABELS = {
    "unmatched": "Sin coincidencia",
    "ambiguous": "Coincidencia ambigua",
    "conflict": "Conflicto de identidad",
}


class InvalidSlackCorrelationCase(ValueError):
    """Raised when a case cannot be projected safely to Slack."""


def _commercial_copy(case: dict[str, object]) -> CommercialCaseCopy:
    try:
        return commercial_case_copy(case.get("event_type"))
    except ValueError as exc:
        raise InvalidSlackCorrelationCase("invalid_event_type") from exc


_MASKED_EMAIL = re.compile(
    r"^[A-Za-z0-9._+\-]*\*{3,}[A-Za-z0-9._+\-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$"
)
_MASKED_PHONE = re.compile(r"^\*{4,}[0-9]{4}$")
_PRIVATE_EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$")
_PRIVATE_PHONE = re.compile(r"^[0-9]{4,32}$")


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


def _safe_private(value: object, *, kind: str) -> str | None:
    if value is None:
        return None
    pattern = _PRIVATE_EMAIL if kind == "email" else _PRIVATE_PHONE
    if (
        not isinstance(value, str)
        or pattern.fullmatch(value) is None
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in value for character in "`<>&")
    ):
        raise InvalidSlackCorrelationCase(f"invalid_private_{kind}")
    return value


def _private_identity(case: dict[str, object]) -> tuple[str | None, str | None]:
    identity = case.get("identity")
    if not isinstance(identity, dict) or set(identity) != {"email", "phone"}:
        raise InvalidSlackCorrelationCase("invalid_private_identity")
    return (
        _safe_private(identity.get("email"), kind="email"),
        _safe_private(identity.get("phone"), kind="phone"),
    )


def _review_identity(case: dict[str, object]) -> tuple[str | None, str | None]:
    identity = case.get("identity")
    if isinstance(identity, dict) and set(identity) == {"email", "phone"}:
        return _private_identity(case)
    return _masked_identity(case)


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


def _safe_timestamp(value: object, *, reason: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise InvalidSlackCorrelationCase(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidSlackCorrelationCase(reason) from None
    if parsed.tzinfo is None or any(
        ord(character) < 32
        or ord(character) == 127
        or character in "`<>&"
        for character in value
    ):
        raise InvalidSlackCorrelationCase(reason)
    return value


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
    copy = _commercial_copy(case)
    title = copy.title(outcome)
    explanation = copy.problem(outcome)
    identity_lines = [
        value
        for value in (
            f"{copy.identity_email_label}: `{email}`" if email is not None else None,
            f"{copy.identity_phone_label}: `{phone}`" if phone is not None else None,
        )
        if value is not None
    ]
    identity_text = "\n".join(identity_lines) or "El evento no incluye datos de identidad comparables."
    possible_label = "Persona posible" if candidate_count == 1 else "Personas posibles"
    return {
        "text": title,
        "metadata": {
            "event_type": "operator_correlation_case",
            "event_payload": {"case_id": case_id},
        },
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": title,
                },
            },
            _section(
                f"{copy.situation}\n\n*Cuál es el problema:* {explanation}\n"
                f"*Qué tenés que hacer:* {operator_task(copy, outcome)}\n"
                f"*{possible_label}: {candidate_count}*"
            ),
            _section(identity_text),
            _section(f"*Mientras esté pendiente:* {copy.impact}"),
            *(
                [
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "review_operator_correlation",
                                "text": {"type": "plain_text", "text": copy.button},
                                "style": "primary",
                                "value": case_id,
                            }
                        ],
                    }
                ]
                if outcome != "unmatched"
                else []
            ),
        ],
    }


def build_review_modal(
    case: dict[str, object], *, review_token: str
) -> dict[str, Any]:
    """Ask a commercial operator one plain-language ownership question."""
    _case_id(case)
    try:
        canonical_review_token = str(UUID(review_token))
    except (TypeError, ValueError) as exc:
        raise InvalidSlackCorrelationCase("invalid_review_token") from exc
    outcome = case.get("outcome")
    if not isinstance(outcome, str) or outcome not in _OUTCOME_LABELS:
        raise InvalidSlackCorrelationCase("invalid_outcome")
    copy = _commercial_copy(case)
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

    observed_email, observed_phone = _review_identity(case)
    observed_lines = [
        f"*{copy.observed_label}*",
        copy.situation,
        f"Email: `{observed_email}`" if observed_email is not None else "Email: no disponible",
        f"Teléfono: `{observed_phone}`" if observed_phone is not None else "Teléfono: no disponible",
    ]
    blocks: list[dict[str, object]] = [
        _section("\n".join(observed_lines)),
        _section(
            f"*Qué tenés que resolver:* {copy.task}\n"
            "Elegí una persona sólo si pudiste comprobarlo con información adicional. "
            "No elijas por intuición ni porque parezca la opción más probable."
        ),
    ]
    options: list[dict[str, object]] = []
    multiple = candidate_count != 1
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
        if candidate.get("lifecycle_state") != "waiting_for_purchase":
            raise InvalidSlackCorrelationCase("ineligible_candidate")
        if set(candidate).intersection({"normalized_email", "normalized_phone"}):
            raise InvalidSlackCorrelationCase("invalid_candidate_identity")
        try:
            if "email" in candidate or "phone" in candidate:
                if "masked_email" in candidate or "masked_phone" in candidate:
                    raise InvalidSlackCorrelationCase("invalid_candidate_identity")
                email = _safe_private(candidate.get("email"), kind="email")
                phone = _safe_private(candidate.get("phone"), kind="phone")
            else:
                email = _safe_masked(candidate.get("masked_email"), kind="email")
                phone = _safe_masked(candidate.get("masked_phone"), kind="phone")
        except InvalidSlackCorrelationCase:
            raise InvalidSlackCorrelationCase("invalid_candidate_identity") from None
        matched_by = candidate.get("matched_by")
        if (
            not isinstance(matched_by, list)
            or not matched_by
            or any(
                not isinstance(value, str) or value not in {"email", "phone"}
                for value in matched_by
            )
            or len(set(matched_by)) != len(matched_by)
        ):
            raise InvalidSlackCorrelationCase("invalid_candidate_match")
        submitted_at = _safe_timestamp(
            candidate.get("submitted_at"), reason="invalid_candidate_submitted_at"
        )
        matched_labels = [
            "email" if value == "email" else "teléfono" for value in matched_by
        ]
        label = f"Persona {index}" if multiple else "Persona encontrada"
        candidate_lines = [f"*{label}*"]
        if email is not None:
            marker = "✅" if "email" in matched_by else "⚠️"
            status = "Coincide" if "email" in matched_by else "No coincide"
            candidate_lines.append(f"Email: `{email}` {marker} {status}")
        if phone is not None:
            marker = "✅" if "phone" in matched_by else "⚠️"
            status = "Coincide" if "phone" in matched_by else "No coincide"
            candidate_lines.append(f"Teléfono: `{phone}` {marker} {status}")
        candidate_lines.extend(
            (
                f"Coincide por: {', '.join(matched_labels)}",
                f"Registro recibido: `{submitted_at}`",
            )
        )
        blocks.append(_section("\n".join(candidate_lines)))
        options.append(
            {
                "text": {
                    "type": "plain_text",
                    "text": f"Es la persona {index}" if multiple else "Sí, es esta persona",
                },
                "value": candidate_id,
            }
        )
    options.extend(
        (
            {
                "text": {
                    "type": "plain_text",
                    "text": "Revisé los datos: no corresponde a ninguna",
                },
                "value": "close_without_match",
            },
            {
                "text": {"type": "plain_text", "text": "No puedo determinarlo"},
                "value": "leave_pending",
            },
        )
    )
    if candidate_count == 0:
        question = copy.no_candidate_question
    elif candidate_count == 1:
        question = copy.singular_question
    else:
        question = copy.multiple_question
    decision_element: dict[str, object] = {
        "type": "radio_buttons" if len(options) <= 10 else "static_select",
        "action_id": "selected_decision",
        "options": options,
    }
    if len(options) > 10:
        decision_element["placeholder"] = {
            "type": "plain_text",
            "text": "Seleccionar una opción",
        }
    blocks.append(
        {
            "type": "input",
            "block_id": "decision",
            "label": {"type": "plain_text", "text": question},
            "element": decision_element,
        }
    )
    return {
        "type": "modal",
        "callback_id": "select_operator_correlation_resolution",
        "private_metadata": json.dumps(
            {"review_token": canonical_review_token}, separators=(",", ":")
        ),
        "title": {"type": "plain_text", "text": copy.modal_title},
        "submit": {"type": "plain_text", "text": "Continuar"},
        "close": {"type": "plain_text", "text": "Cancelar"},
        "blocks": blocks,
    }


def build_verification_modal(*, review_token: str, candidate_id: str) -> dict[str, Any]:
    """Ask how an already selected person was independently confirmed."""
    metadata = {
        "review_token": str(UUID(review_token)),
        "candidate_id": str(UUID(candidate_id)),
    }
    return {
        "type": "modal",
        "callback_id": "prepare_operator_correlation_resolution",
        "private_metadata": json.dumps(metadata, separators=(",", ":")),
        "title": {"type": "plain_text", "text": "Confirmar persona"},
        "submit": {"type": "plain_text", "text": "Continuar"},
        "close": {"type": "plain_text", "text": "Volver"},
        "blocks": [
            _section("Elegiste asociar este caso. *¿Cómo lo confirmaste?*"),
            {
                "type": "input",
                "block_id": "verification",
                "label": {"type": "plain_text", "text": "Comprobación"},
                "element": {
                    "type": "static_select",
                    "action_id": "verification_basis",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Seleccionar cómo lo comprobaste",
                    },
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": label},
                            "value": value,
                        }
                        for value, label in (
                            (
                                "external_transaction_reference",
                                "Revisé el evento o la transacción",
                            ),
                            (
                                "operator_source_record",
                                "Revisé el registro del cliente",
                            ),
                            ("customer_confirmation", "El cliente lo confirmó"),
                        )
                    ],
                },
            },
        ],
    }


def build_confirmation_modal(
    *, review_token: str, action: str, verification_basis: str
) -> dict[str, Any]:
    if action == "resolve_with_candidate":
        evidence_text = {
            "external_transaction_reference": "El evento o la transacción fueron verificados.",
            "operator_source_record": "El registro del cliente fue verificado.",
            "customer_confirmation": "El cliente confirmó que el caso le corresponde.",
        }.get(verification_basis)
        if evidence_text is None:
            raise ValueError("invalid_verification_basis")
        title = "Confirmar asociación"
        consequence = "Vas a asociar este caso con la persona seleccionada."
    elif (
        action == "close_without_match"
        and verification_basis == "no_valid_candidate_after_review"
    ):
        title = "Confirmar cierre"
        consequence = (
            "Vas a cerrar este caso sin asociarlo a ninguna persona.\n"
            "Confirmá únicamente si revisaste todas las opciones."
        )
        evidence_text = ""
    else:
        raise ValueError("invalid_confirmation")
    return {
        "type": "modal",
        "callback_id": "confirm_operator_correlation_resolution",
        "private_metadata": json.dumps(
            {"review_token": str(UUID(review_token))}, separators=(",", ":")
        ),
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": title},
        "close": {"type": "plain_text", "text": "Volver"},
        "blocks": [
            _section("\n".join(part for part in (consequence, evidence_text) if part)),
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
        "title": {"type": "plain_text", "text": "Guardando decisión"},
        "close": {"type": "plain_text", "text": "Cerrar"},
        "blocks": [
            _section(
                "Estamos comprobando tu decisión. "
                "Esta pantalla se actualizará al terminar."
            )
        ],
    }


def build_success_modal() -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "operator_correlation_resolution_complete",
        "title": {"type": "plain_text", "text": "Decisión guardada"},
        "close": {"type": "plain_text", "text": "Cerrar"},
        "blocks": [_section("El caso fue actualizado y ya podés cerrar esta ventana.")],
    }


def build_safe_error_modal() -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "operator_correlation_resolution_failed",
        "title": {"type": "plain_text", "text": "No pudimos guardar"},
        "close": {"type": "plain_text", "text": "Cerrar"},
        "blocks": [
            _section(
                "El caso sigue pendiente. Volvé a abrirlo desde el mensaje "
                "antes de intentar otra acción."
            )
        ],
    }


def build_terminal_message(
    *, case_id: str, outcome: str, actor_id: str, applied_at: str
) -> dict[str, Any]:
    canonical_case_id = str(UUID(case_id))
    if outcome == "linked_candidate":
        title = "Caso asociado"
        detail = "El caso quedó asociado a la persona seleccionada."
    elif outcome == "closed_without_match":
        title = "Caso cerrado sin asociación"
        detail = "El caso quedó cerrado sin asociarse a ninguna persona."
    else:
        raise InvalidSlackCorrelationCase("invalid_terminal_outcome")
    if re.fullmatch(r"[UW][A-Z0-9]{8,20}", actor_id) is None:
        raise InvalidSlackCorrelationCase("invalid_actor_id")
    applied_at = _safe_timestamp(applied_at, reason="invalid_applied_at")
    return {
        "text": title,
        "metadata": {
            "event_type": "operator_correlation_case",
            "event_payload": {"case_id": canonical_case_id, "state": outcome},
        },
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": title}},
            _section(
                f"{detail}\nRevisado por: <@{actor_id}>\nFecha: `{applied_at}`"
            ),
        ],
    }
