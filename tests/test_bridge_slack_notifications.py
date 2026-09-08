from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from bridge.slack_notifications import SlackOperationalNotifier


class _CapturingProducer:
    def __init__(self) -> None:
        self.commands = []

    async def admit(self, command):
        self.commands.append(command)
        return object()


@pytest.mark.parametrize(
    ("outcome", "event_code"),
    [
        ("unmatched", "COR-001"),
        ("ambiguous", "COR-002"),
        ("conflict", "COR-003"),
    ],
)
def test_unresolved_correlation_maps_to_stable_closed_notification(
    outcome: str,
    event_code: str,
) -> None:
    producer = _CapturingProducer()
    notifier = SlackOperationalNotifier(producer=producer)
    occurred_at = datetime(2026, 9, 7, 22, 0, tzinfo=UTC)

    first = asyncio.run(
        notifier.notify_unresolved_correlation(
            source_event_id="93f3fc37-5f92-4966-af71-b37c9bbac45e",
            outcome=outcome,
            reason_code="multiple_candidates",
            candidate_count=2,
            occurred_at=occurred_at,
        )
    )
    second = asyncio.run(
        notifier.notify_unresolved_correlation(
            source_event_id="93f3fc37-5f92-4966-af71-b37c9bbac45e",
            outcome=outcome,
            reason_code="multiple_candidates",
            candidate_count=2,
            occurred_at=occurred_at,
        )
    )

    assert first.event_code == event_code
    assert second == first
    assert len(first.dedupe_key) == 64
    assert first.subject_ref == "C-93F3FC37-5F92-4966-AF71-B37C9BBAC45E"
    assert first.reason_code == "multiple_candidates"
    assert first.count == 2
    assert first.state == "pending"
    assert producer.commands == [first, first]


def test_notifier_rejects_resolved_or_malformed_correlation_without_calling_connector() -> None:
    producer = _CapturingProducer()
    notifier = SlackOperationalNotifier(producer=producer)

    with pytest.raises(ValueError, match="correlation_not_notifiable"):
        asyncio.run(
            notifier.notify_unresolved_correlation(
                source_event_id="93f3fc37-5f92-4966-af71-b37c9bbac45e",
                outcome="resolved",
                reason_code="exact_email",
                candidate_count=1,
                occurred_at=datetime(2026, 9, 7, 22, 0, tzinfo=UTC),
            )
        )

    assert producer.commands == []


def test_distinct_source_uuids_with_same_prefix_get_distinct_thread_keys() -> None:
    producer = _CapturingProducer()
    notifier = SlackOperationalNotifier(producer=producer)
    occurred_at = datetime(2026, 9, 7, 22, 0, tzinfo=UTC)

    first = asyncio.run(
        notifier.notify_unresolved_correlation(
            source_event_id="93f3fc37-5f92-4966-af71-b37c9bbac45e",
            outcome="unmatched",
            reason_code="no_candidate",
            candidate_count=0,
            occurred_at=occurred_at,
        )
    )
    second = asyncio.run(
        notifier.notify_unresolved_correlation(
            source_event_id="93f3fc37-0000-4000-8000-000000000000",
            outcome="unmatched",
            reason_code="no_candidate",
            candidate_count=0,
            occurred_at=occurred_at,
        )
    )

    assert first.subject_ref != second.subject_ref
