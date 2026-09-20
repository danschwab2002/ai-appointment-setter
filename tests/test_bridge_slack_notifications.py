from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
from uuid import UUID, uuid5

import pytest

from bridge.slack_notifications import SlackOperationalNotifier
from slack_correlation.catalog import (
    CorrelationRecommendation,
    CorrelationRecommendationEvidence,
    NotificationCommand,
)
from slack_correlation.store import NotificationStore


class _CapturingProducer:
    def __init__(self) -> None:
        self.commands = []

    async def admit(self, command):
        self.commands.append(command)
        return object()


def _recommendation() -> CorrelationRecommendation:
    return CorrelationRecommendation(
        recommendation_ref="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        candidate_id="22222222-2222-4222-8222-222222222222",
        candidate_label="Persona 2",
        evidence=(
            CorrelationRecommendationEvidence(
                kind="precheckout_time_proximity_minutes", value=4
            ),
        ),
        evidence_fingerprint="f" * 64,
        model_name="resolver-model",
        prompt_version="correlation-preresolution-v1",
    )


@pytest.mark.parametrize(
    ("source_event_type", "outcome", "event_code"),
    [
        ("PURCHASE_APPROVED", "unmatched", "COR-001"),
        ("PURCHASE_APPROVED", "ambiguous", "COR-002"),
        ("PURCHASE_APPROVED", "conflict", "COR-003"),
        ("PURCHASE_OUT_OF_SHOPPING_CART", "unmatched", "COR-010"),
        ("PURCHASE_OUT_OF_SHOPPING_CART", "ambiguous", "COR-011"),
        ("PURCHASE_OUT_OF_SHOPPING_CART", "conflict", "COR-012"),
        ("PURCHASE_CANCELED", "unmatched", "COR-013"),
        ("PURCHASE_CANCELED", "ambiguous", "COR-014"),
        ("PURCHASE_CANCELED", "conflict", "COR-015"),
    ],
)
def test_unresolved_correlation_maps_to_stable_closed_notification(
    source_event_type: str,
    outcome: str,
    event_code: str,
) -> None:
    producer = _CapturingProducer()
    notifier = SlackOperationalNotifier(producer=producer)
    occurred_at = datetime(2026, 9, 7, 22, 0, tzinfo=UTC)

    first = asyncio.run(
        notifier.notify_unresolved_correlation(
            source_event_id="93f3fc37-5f92-4966-af71-b37c9bbac45e",
            source_event_type=source_event_type,
            outcome=outcome,
            reason_code="multiple_candidates",
            candidate_count=2,
            occurred_at=occurred_at,
        )
    )
    second = asyncio.run(
        notifier.notify_unresolved_correlation(
            source_event_id="93f3fc37-5f92-4966-af71-b37c9bbac45e",
            source_event_type=source_event_type,
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


def test_old_admission_replays_exactly_after_new_worker_reclaims_v1_contract(tmp_path) -> None:
    producer = _CapturingProducer()
    source_event_id = "11111111-1111-4111-8111-111111111111"

    command = asyncio.run(
        SlackOperationalNotifier(producer=producer).notify_unresolved_correlation(
            source_event_id=source_event_id,
            source_event_type="PURCHASE_OUT_OF_SHOPPING_CART",
            notification_contract_version=1,
            outcome="conflict",
            reason_code="email_phone_conflict",
            candidate_count=2,
            occurred_at=datetime(2026, 9, 8, 12, tzinfo=UTC),
        )
    )

    legacy_code = "COR-003"
    namespace = UUID("31f8cf87-488b-4e26-a395-d13270200459")
    material = "\x1f".join(
        ("correlation-v1", source_event_id, legacy_code, "email_phone_conflict", "2")
    )
    assert command.event_id == str(
        uuid5(namespace, f"correlation:{source_event_id}:{legacy_code}")
    )
    assert command.dedupe_key == hashlib.sha256(material.encode("utf-8")).hexdigest()
    assert command.event_code == legacy_code

    old_command = NotificationCommand(
        event_id=command.event_id,
        event_code=legacy_code,
        dedupe_key=command.dedupe_key,
        occurred_at=command.occurred_at,
        subject_ref=command.subject_ref,
        reason_code=command.reason_code,
        state=command.state,
        count=command.count,
    )
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    first = store.admit(tenant_ref="johanna", command=old_command)
    reclaimed = store.admit(tenant_ref="johanna", command=command)

    assert first.outcome == "admitted"
    assert reclaimed.outcome == "duplicate"
    assert store.count() == 1


def test_contract_v3_requires_a_persisted_recommendation_and_carries_it_exactly() -> None:
    producer = _CapturingProducer()
    notifier = SlackOperationalNotifier(producer=producer)

    command = asyncio.run(
        notifier.notify_unresolved_correlation(
            source_event_id="93f3fc37-5f92-4966-af71-b37c9bbac45e",
            source_event_type="PURCHASE_APPROVED",
            notification_contract_version=3,
            outcome="conflict",
            reason_code="email_phone_conflict",
            candidate_count=2,
            occurred_at=datetime(2026, 9, 7, 22, 0, tzinfo=UTC),
            recommendation=_recommendation(),
        )
    )

    assert command.event_code == "COR-003"
    assert command.recommendation == _recommendation()
    assert producer.commands == [command]


@pytest.mark.parametrize("outcome", ["unmatched", "ambiguous", "conflict"])
def test_contract_v3_does_not_publish_without_a_safe_recommendation(outcome: str) -> None:
    producer = _CapturingProducer()
    notifier = SlackOperationalNotifier(producer=producer)

    with pytest.raises(ValueError, match="correlation_not_notifiable"):
        asyncio.run(
            notifier.notify_unresolved_correlation(
                source_event_id="93f3fc37-5f92-4966-af71-b37c9bbac45e",
                source_event_type="PURCHASE_APPROVED",
                notification_contract_version=3,
                outcome=outcome,
                reason_code="identity_not_found" if outcome == "unmatched" else "multiple_candidates",
                candidate_count=0 if outcome == "unmatched" else 2,
                occurred_at=datetime(2026, 9, 7, 22, 0, tzinfo=UTC),
            )
        )

    assert producer.commands == []


def test_notifier_rejects_resolved_or_malformed_correlation_without_calling_connector() -> None:
    producer = _CapturingProducer()
    notifier = SlackOperationalNotifier(producer=producer)

    with pytest.raises(ValueError, match="correlation_not_notifiable"):
        asyncio.run(
            notifier.notify_unresolved_correlation(
                source_event_id="93f3fc37-5f92-4966-af71-b37c9bbac45e",
                source_event_type="PURCHASE_APPROVED",
                outcome="resolved",
                reason_code="exact_email",
                candidate_count=1,
                occurred_at=datetime(2026, 9, 7, 22, 0, tzinfo=UTC),
            )
        )

    assert producer.commands == []


def test_notifier_rejects_unknown_business_event() -> None:
    producer = _CapturingProducer()
    notifier = SlackOperationalNotifier(producer=producer)

    with pytest.raises(ValueError, match="correlation_not_notifiable"):
        asyncio.run(
            notifier.notify_unresolved_correlation(
                source_event_id="93f3fc37-5f92-4966-af71-b37c9bbac45e",
                source_event_type="UNKNOWN_EVENT",
                outcome="ambiguous",
                reason_code="multiple_candidates",
                candidate_count=2,
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
            source_event_type="PURCHASE_APPROVED",
            outcome="unmatched",
            reason_code="no_candidate",
            candidate_count=0,
            occurred_at=occurred_at,
        )
    )
    second = asyncio.run(
        notifier.notify_unresolved_correlation(
            source_event_id="93f3fc37-0000-4000-8000-000000000000",
            source_event_type="PURCHASE_APPROVED",
            outcome="unmatched",
            reason_code="no_candidate",
            candidate_count=0,
            occurred_at=occurred_at,
        )
    )

    assert first.subject_ref != second.subject_ref
