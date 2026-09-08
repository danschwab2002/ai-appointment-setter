from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import sqlite3

import pytest

from slack_correlation.catalog import EVENT_TEMPLATES, NotificationCommand, render_message
from slack_correlation.store import NotificationStore
from slack_correlation.client import (
    SlackMessageReference,
    SlackProtocolError,
    SlackRejectedError,
)
from slack_correlation.worker import NotificationWorker


def _command(**overrides: object) -> NotificationCommand:
    values: dict[str, object] = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "event_code": "HND-001",
        "dedupe_key": "1" * 64,
        "occurred_at": datetime(2026, 9, 7, 22, 0, tzinfo=UTC),
        "subject_ref": "C-11111111",
        "reason_code": "explicit_human_request",
        "component": None,
        "state": "paused",
        "count": None,
        "deadline_at": None,
    }
    values.update(overrides)
    return NotificationCommand(**values)  # type: ignore[arg-type]


def test_catalog_has_exactly_the_49_approved_event_codes() -> None:
    assert len(EVENT_TEMPLATES) == 49
    assert set(EVENT_TEMPLATES) == {
        *(f"HND-{number:03d}" for number in range(1, 12)),
        *(f"COR-{number:03d}" for number in range(1, 10)),
        *(f"MSG-{number:03d}" for number in range(1, 8)),
        *(f"SYS-{number:03d}" for number in range(1, 10)),
        *(f"SEC-{number:03d}" for number in range(1, 6)),
        *(f"OPS-{number:03d}" for number in range(1, 7)),
        "DIG-001",
        "DIG-002",
    }


def test_catalog_uses_the_four_approved_severity_classes() -> None:
    assert EVENT_TEMPLATES["SEC-001"].severity == "p1"
    assert EVENT_TEMPLATES["HND-001"].severity == "p2"
    assert EVENT_TEMPLATES["COR-004"].severity == "p3"
    assert EVENT_TEMPLATES["MSG-006"].severity == "p4"


def test_renderer_uses_only_server_owned_template_and_machine_fields() -> None:
    rendered = render_message(_command(), tenant_label="Johanna")

    assert rendered["text"] == "[p2] Nueva derivación · Johanna · C-11111111"
    assert rendered["metadata"] == {
        "event_type": "supportmagician_operational_event",
        "event_payload": {
            "event_id": "11111111-1111-4111-8111-111111111111",
            "event_code": "HND-001",
        },
    }
    assert "explicit_human_request" in repr(rendered)
    assert "paused" in repr(rendered)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject_ref", "buyer@example.com"),
        ("subject_ref", "593991234567"),
        ("reason_code", "+593991234567"),
        ("component", "customer said hello"),
        ("state", "raw message body"),
    ],
)
def test_command_rejects_values_that_can_carry_free_text_or_pii(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=f"invalid_{field}"):
        _command(**{field: value})


def test_command_requires_a_sha256_dedupe_key() -> None:
    with pytest.raises(ValueError, match="invalid_dedupe_key"):
        _command(dedupe_key="phone:593991234567")


def test_store_admits_once_replays_exactly_and_rejects_semantic_conflict(
    tmp_path,
) -> None:
    store = NotificationStore(tmp_path / "slack.sqlite3")
    store.initialize()
    original = _command()

    admitted = store.admit(tenant_ref="johanna", command=original)
    duplicate = store.admit(tenant_ref="johanna", command=original)
    conflicting = store.admit(
        tenant_ref="johanna",
        command=_command(state="closed"),
    )

    assert admitted.outcome == "admitted"
    assert admitted.state == "pending"
    assert duplicate.outcome == "duplicate"
    assert duplicate.notification_id == admitted.notification_id
    assert conflicting.outcome == "semantic_conflict"
    assert store.count() == 1


def test_store_rejects_an_unknown_on_disk_schema_version(tmp_path) -> None:
    path = tmp_path / "slack.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(RuntimeError, match="unsupported_store_version"):
        NotificationStore(path).initialize()


def test_recovery_requeues_pre_request_claim_and_never_retries_started_request(
    tmp_path,
) -> None:
    store = NotificationStore(tmp_path / "slack.sqlite3")
    store.initialize()
    store.admit(tenant_ref="johanna", command=_command())
    first_claim = store.claim_next(worker_id="worker-a")
    assert first_claim is not None
    store.recover_incomplete()

    reclaimed = store.claim_next(worker_id="worker-b")
    assert reclaimed is not None
    assert reclaimed.notification_id == first_claim.notification_id
    assert reclaimed.generation == first_claim.generation + 1
    store.mark_request_started(reclaimed)
    store.recover_incomplete()

    recovered = store.get(
        tenant_ref="johanna",
        notification_id=first_claim.notification_id,
    )
    assert recovered is not None
    assert recovered.state == "delivery_unknown"
    assert recovered.failure_code == "process_interrupted_after_request_start"
    assert store.claim_next(worker_id="worker-c") is None


def test_accepted_notification_creates_stable_tenant_scoped_thread_root(
    tmp_path,
) -> None:
    store = NotificationStore(tmp_path / "slack.sqlite3")
    store.initialize()
    first = _command()
    store.admit(tenant_ref="johanna", command=first)
    first_claim = store.claim_next(worker_id="worker-a")
    assert first_claim is not None
    assert store.resolve_thread_ts(first_claim) is None
    store.mark_request_started(first_claim)
    store.finalize_accepted(
        first_claim,
        channel_id="C0C0YEACVT2",
        message_ts="1788800000.000001",
        thread_ts=None,
    )

    second = _command(
        event_id="22222222-2222-4222-8222-222222222222",
        event_code="HND-004",
        dedupe_key="2" * 64,
    )
    store.admit(tenant_ref="johanna", command=second)
    second_claim = store.claim_next(worker_id="worker-a")
    assert second_claim is not None
    assert store.resolve_thread_ts(second_claim) == "1788800000.000001"
    store.mark_request_started(second_claim)
    store.finalize_accepted(
        second_claim,
        channel_id="C0C0YEACVT2",
        message_ts="1788800001.000002",
        thread_ts="1788800000.000001",
    )

    stored = store.get(
        tenant_ref="johanna",
        notification_id=second.event_id,
    )
    assert stored is not None
    assert stored.state == "accepted"
    assert stored.thread_ts == "1788800000.000001"


def test_worker_never_retries_an_ambiguous_slack_request(tmp_path) -> None:
    class AmbiguousSlackClient:
        def __init__(self) -> None:
            self.calls = 0

        async def post_message(self, *, channel_id: str, message: dict) -> object:
            self.calls += 1
            raise SlackProtocolError("message_delivery_unknown")

    store = NotificationStore(tmp_path / "slack.sqlite3")
    store.initialize()
    command = _command()
    store.admit(tenant_ref="johanna", command=command)
    slack = AmbiguousSlackClient()
    worker = NotificationWorker(
        store=store,
        slack_client=slack,
        channel_id="C0C0YEACVT2",
        tenant_labels={"johanna": "Johanna", "att1": "ATT1"},
        worker_id="slack-worker-1",
    )

    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is False

    stored = store.get(tenant_ref="johanna", notification_id=command.event_id)
    assert stored is not None
    assert stored.state == "delivery_unknown"
    assert stored.failure_code == "slack_delivery_unknown"
    assert slack.calls == 1


def test_worker_posts_server_rendered_message_to_exact_configured_channel(
    tmp_path,
) -> None:
    class AcceptedSlackClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def post_message(
            self, *, channel_id: str, message: dict
        ) -> SlackMessageReference:
            self.calls.append((channel_id, message))
            return SlackMessageReference(
                channel_id=channel_id,
                message_ts="1788800000.000001",
            )

    store = NotificationStore(tmp_path / "slack.sqlite3")
    store.initialize()
    command = _command()
    store.admit(tenant_ref="johanna", command=command)
    slack = AcceptedSlackClient()
    worker = NotificationWorker(
        store=store,
        slack_client=slack,
        channel_id="C0C0YEACVT2",
        tenant_labels={"johanna": "Johanna", "att1": "ATT1"},
        worker_id="slack-worker-1",
    )

    assert asyncio.run(worker.run_once()) is True

    assert len(slack.calls) == 1
    channel_id, message = slack.calls[0]
    assert channel_id == "C0C0YEACVT2"
    assert message["text"] == "[p2] Nueva derivación · Johanna · C-11111111"
    assert "channel" not in message
    stored = store.get(tenant_ref="johanna", notification_id=command.event_id)
    assert stored is not None
    assert stored.state == "accepted"
    assert stored.message_ts == "1788800000.000001"


def test_worker_records_explicit_slack_rejection_without_retry(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    command = _command(
        event_id="55555555-5555-4555-8555-555555555555",
        dedupe_key="5" * 64,
    )
    store.admit(tenant_ref="att1", command=command)

    class RejectedSlack:
        def __init__(self) -> None:
            self.calls = 0

        async def post_message(self, *, channel_id, message, thread_ts=None):
            self.calls += 1
            raise SlackRejectedError("slack_api_rejected")

    client = RejectedSlack()
    worker = NotificationWorker(
        store=store,
        slack_client=client,
        channel_id="C0123456789",
        tenant_labels={"att1": "ATT1"},
        worker_id="worker-1",
    )

    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is False
    assert client.calls == 1
    stored = store.get(tenant_ref="att1", notification_id=command.event_id)
    assert stored is not None
    assert stored.state == "rejected"
    assert stored.failure_code == "slack_rejected"
