"""Bridge-side construction of closed operational Slack commands."""

from __future__ import annotations

from datetime import datetime
from dataclasses import asdict
import hashlib
import json
import re
from typing import Protocol
from uuid import UUID, uuid5

from slack_correlation.catalog import CorrelationRecommendation, NotificationCommand
from slack_correlation.case_copy import EVENT_OUTCOME_CODES
from slack_correlation.producer import AdmissionReceipt

_LEGACY_CORRELATION_CODES = {
    "unmatched": "COR-001",
    "ambiguous": "COR-002",
    "conflict": "COR-003",
}
_NOTIFICATION_NAMESPACE = UUID("31f8cf87-488b-4e26-a395-d13270200459")
_HANDOFF_EVENT_CODE = "HND-001"
# Mismo alfabeto que reason_code en el catalogo: si el motivo fino no entra,
# la tarjeta sale con el motivo primario en vez de perderse.
_MACHINE_REASON = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")


class NotificationProducer(Protocol):
    async def admit(self, command: NotificationCommand) -> AdmissionReceipt: ...


class SlackOperationalNotifier:
    def __init__(self, *, producer: NotificationProducer) -> None:
        self._producer = producer

    async def notify_unresolved_correlation(
        self,
        *,
        source_event_id: str,
        source_event_type: str,
        notification_contract_version: int = 2,
        outcome: str,
        reason_code: str,
        candidate_count: int,
        occurred_at: datetime,
        recommendation: CorrelationRecommendation | None = None,
    ) -> NotificationCommand:
        specific_event_code = EVENT_OUTCOME_CODES.get((source_event_type, outcome))
        if specific_event_code is None:
            raise ValueError("correlation_not_notifiable")
        legacy_event_code = _LEGACY_CORRELATION_CODES[outcome]
        if (
            isinstance(notification_contract_version, bool)
            or notification_contract_version not in {1, 2, 3}
        ):
            raise ValueError("invalid_notification_contract_version")
        if notification_contract_version == 3 and (
            outcome == "unmatched" or recommendation is None
        ):
            raise ValueError("correlation_not_notifiable")
        if notification_contract_version != 3 and recommendation is not None:
            raise ValueError("invalid_notification_contract_version")
        event_code = (
            legacy_event_code
            if notification_contract_version == 1
            else specific_event_code
        )
        try:
            source_id = UUID(source_event_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invalid_source_event_id") from exc
        canonical_source_id = str(source_id)
        if canonical_source_id != source_event_id:
            raise ValueError("invalid_source_event_id")
        notification_id = str(
            uuid5(
                _NOTIFICATION_NAMESPACE,
                f"correlation:{canonical_source_id}:{legacy_event_code}",
            )
        )
        semantic_parts = [
            "correlation-v1" if notification_contract_version in {1, 2} else "correlation-v3",
            canonical_source_id,
            legacy_event_code,
            reason_code,
            str(candidate_count),
        ]
        if recommendation is not None:
            semantic_parts.append(
                json.dumps(
                    asdict(recommendation),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        semantic_material = "\x1f".join(semantic_parts)
        command = NotificationCommand(
            event_id=notification_id,
            event_code=event_code,
            dedupe_key=hashlib.sha256(semantic_material.encode("utf-8")).hexdigest(),
            occurred_at=occurred_at,
            subject_ref=f"C-{canonical_source_id.upper()}",
            reason_code=reason_code,
            state="pending",
            count=candidate_count,
            recommendation=recommendation,
        )
        await self._producer.admit(command)
        return command

    async def notify_new_handoff(
        self,
        *,
        handoff_request_id: str,
        external_conversation_id: int,
        primary_reason_code: str,
        detail_reason_code: str | None,
        occurred_at: datetime,
    ) -> NotificationCommand:
        """HND-001: una derivacion nueva, con la conversacion de Chatwoot a abrir.

        La tarjeta no lleva nada de la persona. El caso es el id del pedido de
        derivacion y el puntero operativo es el numero de conversacion de
        Chatwoot, que es lo unico que el equipo necesita para abrirla. Dos
        derivaciones de la misma conversacion son dos tarjetas: si alguien la
        atendio y volvio a caer, hay que volver a mirarla.
        """
        try:
            source_id = UUID(handoff_request_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invalid_handoff_request_id") from exc
        canonical_request_id = str(source_id)
        if canonical_request_id != handoff_request_id:
            raise ValueError("invalid_handoff_request_id")
        if (
            isinstance(external_conversation_id, bool)
            or not isinstance(external_conversation_id, int)
            or external_conversation_id <= 0
        ):
            raise ValueError("invalid_external_conversation_id")
        if (
            not isinstance(primary_reason_code, str)
            or _MACHINE_REASON.fullmatch(primary_reason_code) is None
        ):
            raise ValueError("invalid_primary_reason_code")
        reason_code = primary_reason_code
        if (
            isinstance(detail_reason_code, str)
            and _MACHINE_REASON.fullmatch(detail_reason_code) is not None
        ):
            reason_code = detail_reason_code
        notification_id = str(
            uuid5(_NOTIFICATION_NAMESPACE, f"handoff:{canonical_request_id}")
        )
        semantic_material = "\x1f".join(
            [
                "handoff-v1",
                canonical_request_id,
                str(external_conversation_id),
                reason_code,
            ]
        )
        command = NotificationCommand(
            event_id=notification_id,
            event_code=_HANDOFF_EVENT_CODE,
            dedupe_key=hashlib.sha256(semantic_material.encode("utf-8")).hexdigest(),
            occurred_at=occurred_at,
            subject_ref=f"C-{canonical_request_id.upper()}",
            reason_code=reason_code,
            component=f"chatwoot.conversation.{external_conversation_id}",
            state="pending",
        )
        await self._producer.admit(command)
        return command
