"""Bridge-side construction of closed operational Slack commands."""

from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Protocol
from uuid import UUID, uuid5

from slack_correlation.catalog import NotificationCommand
from slack_correlation.producer import AdmissionReceipt

_CORRELATION_CODES = {
    "unmatched": "COR-001",
    "ambiguous": "COR-002",
    "conflict": "COR-003",
}
_NOTIFICATION_NAMESPACE = UUID("31f8cf87-488b-4e26-a395-d13270200459")


class NotificationProducer(Protocol):
    async def admit(self, command: NotificationCommand) -> AdmissionReceipt: ...


class SlackOperationalNotifier:
    def __init__(self, *, producer: NotificationProducer) -> None:
        self._producer = producer

    async def notify_unresolved_correlation(
        self,
        *,
        source_event_id: str,
        outcome: str,
        reason_code: str,
        candidate_count: int,
        occurred_at: datetime,
    ) -> NotificationCommand:
        event_code = _CORRELATION_CODES.get(outcome)
        if event_code is None:
            raise ValueError("correlation_not_notifiable")
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
                f"correlation:{canonical_source_id}:{event_code}",
            )
        )
        semantic_material = "\x1f".join(
            (
                "correlation-v1",
                canonical_source_id,
                event_code,
                reason_code,
                str(candidate_count),
            )
        )
        command = NotificationCommand(
            event_id=notification_id,
            event_code=event_code,
            dedupe_key=hashlib.sha256(semantic_material.encode("utf-8")).hexdigest(),
            occurred_at=occurred_at,
            subject_ref=f"C-{canonical_source_id.upper()}",
            reason_code=reason_code,
            state="pending",
            count=candidate_count,
        )
        await self._producer.admit(command)
        return command
