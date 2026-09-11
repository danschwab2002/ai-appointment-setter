"""Closed, PII-minimized operational message catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

_DEDUPE_KEY = re.compile(r"^[a-f0-9]{64}$")
_SUBJECT_REF = re.compile(r"^C-[A-Fa-f0-9-]{8,36}$")
_MACHINE_VALUE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")


@dataclass(frozen=True)
class EventTemplate:
    severity: str
    title: str


EVENT_TEMPLATES: dict[str, EventTemplate] = {
    "HND-001": EventTemplate("p2", "Nueva derivación"),
    "HND-002": EventTemplate("p2", "Derivación sin destino disponible"),
    "HND-003": EventTemplate("p2", "Proyección de derivación fallida"),
    "HND-004": EventTemplate("p4", "Derivación tomada"),
    "HND-005": EventTemplate("p2", "SLA de derivación próximo a vencer"),
    "HND-006": EventTemplate("p2", "Derivación vencida y escalada"),
    "HND-007": EventTemplate("p4", "Derivación resuelta"),
    "HND-008": EventTemplate("p4", "Derivación cambió mientras se atendía"),
    "HND-009": EventTemplate("p2", "Resultado incierto al proyectar la derivación"),
    "HND-010": EventTemplate("p2", "Conflicto al proyectar la derivación"),
    "HND-011": EventTemplate("p2", "Proyección enviada a dead letter"),
    "COR-001": EventTemplate("p2", "Sin coincidencia"),
    "COR-002": EventTemplate("p2", "Coincidencia ambigua"),
    "COR-003": EventTemplate("p2", "Evidencia en conflicto"),
    "COR-004": EventTemplate("p3", "Evidencia cambió"),
    "COR-005": EventTemplate("p2", "Revisión vencida"),
    "COR-006": EventTemplate("p2", "Correlación escalada"),
    "COR-007": EventTemplate("p4", "Candidato vinculado"),
    "COR-008": EventTemplate("p4", "Cerrada sin coincidencia"),
    "COR-009": EventTemplate("p2", "Proyección Slack incierta o dañada"),
    "MSG-001": EventTemplate("p2", "Resultado de envío incierto"),
    "MSG-002": EventTemplate("p2", "Envío falló definitivamente"),
    "MSG-003": EventTemplate("p2", "Retries agotados antes del request"),
    "MSG-004": EventTemplate("p2", "Template requerido no disponible"),
    "MSG-005": EventTemplate("p2", "Inbound admitido pero no procesable"),
    "MSG-006": EventTemplate("p4", "Respuesta automática bloqueada por una guarda"),
    "MSG-007": EventTemplate("p2", "Tarea manual prometida"),
    "SYS-001": EventTemplate("p2", "Servicio o worker no saludable"),
    "SYS-002": EventTemplate("p2", "Cola atrasada"),
    "SYS-003": EventTemplate("p2", "Reconciliación vencida"),
    "SYS-004": EventTemplate("p3", "Dependencia externa degradada"),
    "SYS-005": EventTemplate("p2", "Persistencia no disponible"),
    "SYS-006": EventTemplate("p2", "Propuestas del agente inválidas o no disponibles"),
    "SYS-007": EventTemplate("p2", "Automatización pausada automáticamente"),
    "SYS-008": EventTemplate("p4", "Recuperación confirmada"),
    "SYS-009": EventTemplate("p2", "Drift de configuración o versión"),
    "SEC-001": EventTemplate("p1", "Inconsistencia de tenant, cuenta, inbox o identidad"),
    "SEC-002": EventTemplate("p1", "Anomalía de autenticación o replay"),
    "SEC-003": EventTemplate("p1", "Acción prohibida bloqueada"),
    "SEC-004": EventTemplate("p2", "Credencial rechazada o integración revocada"),
    "SEC-005": EventTemplate("p1", "Posible exposición de datos sensibles"),
    "OPS-001": EventTemplate("p3", "Activación solicitada"),
    "OPS-002": EventTemplate("p4", "Capacidad activada"),
    "OPS-003": EventTemplate("p2", "Capacidad pausada o desactivada"),
    "OPS-004": EventTemplate("p2", "Deploy, migración o cambio de release fallido"),
    "OPS-005": EventTemplate("p4", "Cambio operativo verificado"),
    "OPS-006": EventTemplate("p3", "Revisión de política, copy o release requerida"),
    "DIG-001": EventTemplate("p4", "Resumen operativo diario"),
    "DIG-002": EventTemplate("p4", "Resumen de pendientes por turno"),
    "REV-001": EventTemplate("p4", "Reporte diario listo"),
}


@dataclass(frozen=True)
class NotificationCommand:
    event_id: str
    event_code: str
    dedupe_key: str
    occurred_at: datetime
    subject_ref: str | None = None
    reason_code: str | None = None
    component: str | None = None
    state: str | None = None
    count: int | None = None
    deadline_at: datetime | None = None
    review_ref: str | None = None

    def __post_init__(self) -> None:
        try:
            parsed_id = UUID(self.event_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invalid_event_id") from exc
        if str(parsed_id) != self.event_id:
            raise ValueError("invalid_event_id")
        if self.event_code not in EVENT_TEMPLATES:
            raise ValueError("invalid_event_code")
        if _DEDUPE_KEY.fullmatch(self.dedupe_key) is None:
            raise ValueError("invalid_dedupe_key")
        if self.occurred_at.tzinfo is None:
            raise ValueError("invalid_occurred_at")
        if self.subject_ref is not None and _SUBJECT_REF.fullmatch(self.subject_ref) is None:
            raise ValueError("invalid_subject_ref")
        for field_name in ("reason_code", "component", "state"):
            value = getattr(self, field_name)
            if value is not None and _MACHINE_VALUE.fullmatch(value) is None:
                raise ValueError(f"invalid_{field_name}")
        if self.count is not None and (
            isinstance(self.count, bool) or not 0 <= self.count <= 1_000_000
        ):
            raise ValueError("invalid_count")
        if self.deadline_at is not None and self.deadline_at.tzinfo is None:
            raise ValueError("invalid_deadline_at")
        if self.event_code == "REV-001":
            try:
                parsed_review_ref = UUID(self.review_ref or "")
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("invalid_review_ref") from exc
            if str(parsed_review_ref) != self.review_ref:
                raise ValueError("invalid_review_ref")
        elif self.review_ref is not None:
            raise ValueError("invalid_review_ref")


def render_message(
    command: NotificationCommand,
    *,
    tenant_label: str,
    thread_ts: str | None = None,
    review_base_url: str | None = None,
) -> dict[str, Any]:
    """Render one closed template; callers cannot supply Slack text or blocks."""

    template = EVENT_TEMPLATES[command.event_code]
    headline_parts = [f"[{template.severity}] {template.title}", tenant_label]
    if command.subject_ref is not None:
        headline_parts.append(command.subject_ref)
    headline = " · ".join(headline_parts)
    fields = [
        {"type": "mrkdwn", "text": f"*Código*\n`{command.event_code}`"},
        {
            "type": "mrkdwn",
            "text": f"*Ocurrió*\n{command.occurred_at.astimezone(UTC).isoformat().replace('+00:00', 'Z')}",
        },
    ]
    for label, value in (
        ("Motivo", command.reason_code),
        ("Componente", command.component),
        ("Estado", command.state),
        ("Cantidad", command.count),
        (
            "Plazo",
            command.deadline_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
            if command.deadline_at is not None
            else None,
        ),
    ):
        if value is not None:
            fields.append({"type": "mrkdwn", "text": f"*{label}*\n`{value}`"})
    message: dict[str, Any] = {
        "text": headline,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": headline}},
            {"type": "section", "fields": fields},
        ],
        "metadata": {
            "event_type": "supportmagician_operational_event",
            "event_payload": {
                "event_id": command.event_id,
                "event_code": command.event_code,
            },
        },
    }
    if command.event_code in {"COR-001", "COR-002", "COR-003"}:
        if command.subject_ref is None or not command.subject_ref.startswith("C-"):
            raise ValueError("correlation_case_id_required")
        try:
            case_id = str(UUID(command.subject_ref[2:]))
        except ValueError as exc:
            raise ValueError("correlation_case_id_required") from exc
        message["metadata"]["event_payload"]["case_id"] = case_id
        message["blocks"].append(
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
            }
        )
    if command.event_code == "REV-001":
        base_url = _validate_review_base_url(review_base_url)
        message["blocks"].append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "<"
                        f"{base_url}/daily-feedback/review/{command.review_ref}"
                        "|Abrir reporte>"
                    ),
                },
            }
        )
        message["unfurl_links"] = False
        message["unfurl_media"] = False
    if thread_ts is not None:
        message["thread_ts"] = thread_ts
    return message


def _validate_review_base_url(value: str | None) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid_review_base_url")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid_review_base_url")
    return value.rstrip("/")
