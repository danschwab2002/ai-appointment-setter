from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from slack_correlation.catalog import NotificationCommand
from slack_correlation.client import SlackMessageReference, SlackProtocolError
from slack_correlation.store import NotificationStore
from slack_correlation.worker import NotificationWorker


def test_provider_uncertainty_halts_the_queue_before_the_next_notification(
    tmp_path,
) -> None:
    class AmbiguousSlackClient:
        def __init__(self) -> None:
            self.calls = 0

        async def post_message(
            self, *, channel_id: str, message: dict[str, object]
        ) -> SlackMessageReference:
            self.calls += 1
            raise SlackProtocolError("message_delivery_unknown")

    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    first = NotificationCommand(
        event_id="11111111-1111-4111-8111-111111111111",
        event_code="HND-001",
        dedupe_key="1" * 64,
        occurred_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        subject_ref="C-11111111",
        reason_code="operator_requested",
    )
    second = NotificationCommand(
        event_id="22222222-2222-4222-8222-222222222222",
        event_code="HND-001",
        dedupe_key="2" * 64,
        occurred_at=datetime(2026, 9, 7, 12, 1, tzinfo=UTC),
        subject_ref="C-22222222",
        reason_code="operator_requested",
    )
    store.admit(tenant_ref="johanna", command=first)
    store.admit(tenant_ref="att1", command=second)
    slack = AmbiguousSlackClient()
    worker = NotificationWorker(
        store=store,
        slack_client=slack,
        tenant_channels={"johanna": "C0C0YEACVT2"},
        tenant_labels={"johanna": "Johanna", "att1": "ATT1"},
        worker_id="slack-worker-1",
    )

    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is False
    queued = store.get(tenant_ref="att1", notification_id=second.event_id)
    assert queued is not None
    assert queued.state == "pending"
    assert slack.calls == 1


def test_pre_request_persistence_failure_releases_claim_and_halts(tmp_path) -> None:
    class FailingMarkStore(NotificationStore):
        def mark_request_started(self, claim) -> None:
            raise OSError("synthetic persistence failure")

    class NoSlackCall:
        def __init__(self) -> None:
            self.calls = 0

        async def post_message(
            self, *, channel_id: str, message: dict[str, object]
        ) -> SlackMessageReference:
            self.calls += 1
            raise AssertionError("must not call Slack")

    store = FailingMarkStore(tmp_path / "connector.sqlite3")
    store.initialize()
    command = NotificationCommand(
        event_id="33333333-3333-4333-8333-333333333333",
        event_code="HND-001",
        dedupe_key="3" * 64,
        occurred_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        subject_ref="C-33333333",
        reason_code="operator_requested",
    )
    store.admit(tenant_ref="johanna", command=command)
    slack = NoSlackCall()
    worker = NotificationWorker(
        store=store,
        slack_client=slack,
        tenant_channels={"johanna": "C0C0YEACVT2"},
        tenant_labels={"johanna": "Johanna", "att1": "ATT1"},
        worker_id="slack-worker-1",
    )

    with pytest.raises(OSError, match="synthetic persistence failure"):
        asyncio.run(worker.run_once())

    stored = store.get(tenant_ref="johanna", notification_id=command.event_id)
    assert stored is not None
    assert stored.state == "pending"
    assert worker.healthy is False
    assert slack.calls == 0
