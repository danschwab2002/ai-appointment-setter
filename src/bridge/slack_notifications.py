"""Bridge-side construction of closed operational Slack commands."""

from __future__ import annotations

from datetime import datetime
from dataclasses import asdict
import hashlib
import json
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
