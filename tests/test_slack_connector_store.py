from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from slack_correlation.app import SlackConnectorSettings, _parse_command, create_app
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


def test_catalog_has_exactly_the_50_approved_event_codes() -> None:
    assert len(EVENT_TEMPLATES) == 50
    assert set(EVENT_TEMPLATES) == {
        *(f"HND-{number:03d}" for number in range(1, 12)),
        *(f"COR-{number:03d}" for number in range(1, 10)),
        *(f"MSG-{number:03d}" for number in range(1, 8)),
        *(f"SYS-{number:03d}" for number in range(1, 10)),
        *(f"SEC-{number:03d}" for number in range(1, 6)),
        *(f"OPS-{number:03d}" for number in range(1, 7)),
        "DIG-001",
        "DIG-002",
        "REV-001",
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


def test_only_pending_correlation_events_receive_native_review_control() -> None:
    correlation = render_message(
        _command(
            event_code="COR-001",
            subject_ref="C-11111111-1111-4111-8111-111111111111",
        ),
        tenant_label="Johanna",
    )
    ordinary = render_message(_command(), tenant_label="Johanna")

    assert correlation["metadata"]["event_payload"]["case_id"] == "11111111-1111-4111-8111-111111111111"
    assert correlation["blocks"][-1]["elements"][0]["action_id"] == "review_operator_correlation"
    assert ordinary["blocks"][-1]["type"] == "section"


def test_daily_review_renders_one_server_owned_https_link_without_unfurls() -> None:
    rendered = render_message(
        _command(
            event_code="REV-001",
            subject_ref=None,
            reason_code=None,
            state="ready",
            count=7,
            review_ref="22222222-2222-4222-8222-222222222222",
        ),
        tenant_label="Johanna",
        review_base_url="https://reviews.example.test",
    )

    text = repr(rendered)
    assert text.count("https://reviews.example.test/daily-feedback/review/22222222-2222-4222-8222-222222222222") == 1
    assert "Abrir reporte" in text
    assert rendered["unfurl_links"] is False
    assert rendered["unfurl_media"] is False
    assert "review_ref" not in rendered["metadata"]["event_payload"]


@pytest.mark.parametrize(
    "review_ref",
    [None, "not-a-uuid", "22222222-2222-4222-8222-222222222222?token=secret"],
)
def test_daily_review_requires_one_opaque_uuid_ref(review_ref: str | None) -> None:
    with pytest.raises(ValueError, match="invalid_review_ref"):
        _command(
            event_code="REV-001",
            subject_ref=None,
            review_ref=review_ref,
        )


def test_non_review_events_reject_review_refs() -> None:
    with pytest.raises(ValueError, match="invalid_review_ref"):
        _command(review_ref="22222222-2222-4222-8222-222222222222")


def test_ingress_parses_the_daily_review_reference() -> None:
    command = _command(
        event_code="REV-001",
        subject_ref=None,
        reason_code=None,
        review_ref="22222222-2222-4222-8222-222222222222",
    )
    payload = {
        "event_id": command.event_id,
        "event_code": command.event_code,
        "dedupe_key": command.dedupe_key,
        "occurred_at": "2026-09-07T22:00:00Z",
        "state": command.state,
        "review_ref": command.review_ref,
    }

    parsed = _parse_command(json.dumps(payload).encode())

    assert parsed.review_ref == command.review_ref


@pytest.mark.parametrize(
    "review_base_url",
    [
        None,
        "http://reviews.example.test",
        "https://user:pass@reviews.example.test",
        "https://reviews.example.test/path",
        "https://reviews.example.test?token=secret",
        "https://reviews.example.test#fragment",
    ],
)
def test_daily_review_rejects_missing_or_unsafe_review_base_url(
    review_base_url: str | None,
) -> None:
    with pytest.raises(ValueError, match="invalid_review_base_url"):
        render_message(
            _command(
                event_code="REV-001",
                subject_ref=None,
                review_ref="22222222-2222-4222-8222-222222222222",
            ),
            tenant_label="Johanna",
            review_base_url=review_base_url,
        )


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
        tenant_channels={"johanna": "C0C0YEACVT2"},
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
        tenant_channels={"johanna": "C0C0YEACVT2"},
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


def test_worker_renders_daily_review_with_tenant_bound_origin(tmp_path) -> None:
    class AcceptedSlackClient:
        def __init__(self) -> None:
            self.message: dict | None = None

        async def post_message(
            self, *, channel_id: str, message: dict
        ) -> SlackMessageReference:
            self.message = message
            return SlackMessageReference(
                channel_id=channel_id,
                message_ts="1788800000.000002",
            )

    store = NotificationStore(tmp_path / "slack.sqlite3")
    store.initialize()
    command = _command(
        event_code="REV-001",
        subject_ref=None,
        reason_code=None,
        state="ready",
        review_ref="22222222-2222-4222-8222-222222222222",
    )
    store.admit(tenant_ref="johanna", command=command)
    slack = AcceptedSlackClient()
    worker = NotificationWorker(
        store=store,
        slack_client=slack,
        tenant_channels={"johanna": "C0C0YEACVT2"},
        tenant_labels={"johanna": "Johanna"},
        tenant_review_base_urls={"johanna": "https://reviews.example.test"},
        worker_id="slack-worker-1",
    )

    assert asyncio.run(worker.run_once()) is True
    assert slack.message is not None
    assert "https://reviews.example.test/daily-feedback/review/22222222-2222-4222-8222-222222222222" in str(
        slack.message["blocks"]
    )


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
        tenant_channels={"att1": "C0123456789"},
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


@pytest.mark.parametrize("legacy_version", [2, 3])
def test_v2_v3_pending_notification_upgrades_and_sends_with_new_features_off(
    tmp_path, legacy_version: int,
) -> None:
    path = tmp_path / f"legacy-v{legacy_version}.sqlite3"
    store = NotificationStore(path)
    store.initialize()
    command = _command()
    store.admit(
        tenant_ref="johanna", channel_id="C0C0YEACVT2", command=command,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE notifications DROP COLUMN team_id")
        connection.execute(f"PRAGMA user_version = {legacy_version}")

    class AcceptedSlack:
        def __init__(self) -> None:
            self.calls = 0

        async def post_message(self, *, channel_id, message, thread_ts=None):
            self.calls += 1
            return SlackMessageReference(
                channel_id=channel_id, message_ts="1788800000.000001"
            )

        async def verify_auth(self, *, expected_team_id: str) -> None:
            assert expected_team_id == "T12345678"

    slack = AcceptedSlack()
    upgraded = NotificationStore(path)
    app = create_app(
        SlackConnectorSettings(
            notifications_enabled=True,
            bot_token="xoxb-synthetic",
            team_id="T12345678",
            tenant_channels={"johanna": "C0C0YEACVT2"},
            storage_path=str(path),
            worker_id="legacy-worker",
            activation_mode="one_shot",
            activation_generation=1,
        ),
        store=upgraded,
        slack_client=slack,
    )
    with TestClient(app):
        deadline = time.monotonic() + 2
        while slack.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
    assert slack.calls == 1
    stored = upgraded.get(tenant_ref="johanna", notification_id=command.event_id)
    assert stored is not None and stored.state == "accepted"


def test_legacy_team_bind_fails_closed_on_channel_mismatch(tmp_path) -> None:
    path = tmp_path / "legacy-mismatch.sqlite3"
    old = NotificationStore(path)
    old.initialize()
    old.admit(
        tenant_ref="johanna", channel_id="C0ATT1TEST01", command=_command(),
    )
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE notifications DROP COLUMN team_id")
        connection.execute("PRAGMA user_version = 3")

    class NoSlack:
        calls = 0

        async def verify_auth(self, *, expected_team_id: str) -> None:
            raise AssertionError("auth must not start after legacy binding failure")

        async def post_message(self, **_kwargs):
            self.calls += 1
            raise AssertionError("Slack effect must not start")

    slack = NoSlack()
    app = create_app(
        SlackConnectorSettings(
            notifications_enabled=True,
            bot_token="xoxb-synthetic",
            team_id="T12345678",
            tenant_channels={"johanna": "C0C0YEACVT2"},
            storage_path=str(path),
            activation_mode="one_shot",
            activation_generation=1,
        ),
        store=NotificationStore(path),
        slack_client=slack,
    )
    with TestClient(app) as client:
        ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json()["mode"] == "storage_unavailable"
    assert slack.calls == 0
