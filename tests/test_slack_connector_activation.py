from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from slack_correlation.app import SlackConnectorSettings, create_app
from slack_correlation.catalog import NotificationCommand
from slack_correlation.client import SlackMessageReference
from slack_correlation.store import NotificationStore
from slack_correlation.worker import NotificationWorker


def _command(event_id: str, dedupe: str) -> NotificationCommand:
    return NotificationCommand(
        event_id=event_id,
        event_code="HND-001",
        dedupe_key=dedupe,
        occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
        subject_ref=None,
        reason_code="explicit_human_request",
        component=None,
        state="paused",
        count=None,
        deadline_at=None,
    )


class AcceptedSlack:
    def __init__(self) -> None:
        self.calls = 0

    async def post_message(self, *, channel_id: str, message: dict) -> SlackMessageReference:
        self.calls += 1
        return SlackMessageReference(channel_id, f"1788800000.{self.calls:06d}")


def _worker(store: NotificationStore, slack: AcceptedSlack) -> NotificationWorker:
    return NotificationWorker(
        store=store,
        slack_client=slack,
        tenant_channels={"johanna": "C0C0YEACVT2"},
        tenant_labels={"johanna": "Johanna"},
        worker_id="worker-1",
    )


def test_one_shot_generation_sends_exactly_one_across_worker_restart(tmp_path) -> None:
    path = tmp_path / "connector.sqlite3"
    store = NotificationStore(path)
    store.initialize()
    store.configure_activation(mode="one_shot", generation=1)
    store.admit(tenant_ref="johanna", command=_command("11111111-1111-4111-8111-111111111111", "1" * 64))
    store.admit(tenant_ref="johanna", command=_command("22222222-2222-4222-8222-222222222222", "2" * 64))
    slack = AcceptedSlack()

    assert asyncio.run(_worker(store, slack).run_once()) is True
    restarted = NotificationStore(path)
    restarted.initialize()
    restarted.configure_activation(mode="one_shot", generation=1)
    assert asyncio.run(_worker(restarted, slack).run_once()) is False

    assert slack.calls == 1
    assert restarted.state_inventory() == {
        "pending": 1,
        "claimed": 0,
        "request_started": 0,
        "delivery_unknown": 0,
    }
    assert restarted.activation_status()["consumed"] == 1


def test_continuous_activation_requires_operator_verified_prior_one_shot(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    store.configure_activation(mode="one_shot", generation=4)

    with pytest.raises(RuntimeError, match="activation_verification_required"):
        store.configure_activation(mode="continuous", generation=5)

    store.admit(
        tenant_ref="johanna",
        command=_command("11111111-1111-4111-8111-111111111111", "1" * 64),
    )
    slack = AcceptedSlack()
    assert asyncio.run(_worker(store, slack).run_once()) is True
    store.mark_activation_verified(generation=4, operator_id="operator")
    store.configure_activation(mode="continuous", generation=5)
    assert store.activation_status() == {
        "mode": "continuous",
        "generation": 5,
        "budget": None,
        "consumed": 0,
        "verified": False,
    }


def test_activation_generation_is_monotonic_and_configuration_is_immutable(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    store.configure_activation(mode="one_shot", generation=2)

    with pytest.raises(RuntimeError, match="activation_generation_regression"):
        store.configure_activation(mode="one_shot", generation=1)
    with pytest.raises(RuntimeError, match="activation_generation_conflict"):
        store.configure_activation(mode="inactive", generation=2)


def test_operator_verification_endpoint_enables_next_continuous_generation(tmp_path) -> None:
    path = tmp_path / "connector.sqlite3"
    store = NotificationStore(path)
    store.initialize()
    store.configure_activation(mode="one_shot", generation=7)
    store.admit(
        tenant_ref="johanna",
        command=_command("11111111-1111-4111-8111-111111111111", "1" * 64),
    )
    assert asyncio.run(_worker(store, AcceptedSlack()).run_once()) is True
    app = create_app(
        SlackConnectorSettings(
            storage_path=str(path),
            operator_bearer_token="o" * 32,
            tenant_channels={"johanna": "C0C0YEACVT2"},
        ),
        store=store,
    )

    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/operator/verify-activation",
            headers={"Authorization": "Bearer " + "o" * 32},
            json={"generation": 7},
        )

    assert response.status_code == 200
    store.configure_activation(mode="continuous", generation=8)
    assert store.activation_status()["mode"] == "continuous"


def test_new_activation_generation_is_blocked_by_delivery_unknown(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    store.configure_activation(mode="one_shot", generation=1)
    store.admit(
        tenant_ref="johanna",
        command=_command("11111111-1111-4111-8111-111111111111", "1" * 64),
    )
    claim = store.claim_next(worker_id="worker-1")
    assert claim is not None
    store.mark_request_started(claim)
    store.finalize_delivery_unknown(claim, failure_code="slack_delivery_unknown")

    with pytest.raises(RuntimeError, match="unresolved_delivery_blocks_activation"):
        store.configure_activation(mode="one_shot", generation=2)


def test_outbound_refuses_implicit_activation_defaults(tmp_path) -> None:
    settings = SlackConnectorSettings(
        notifications_enabled=True,
        bot_token="xoxb-test",
        team_id="T00000000",
        tenant_channels={"johanna": "C0C0YEACVT2"},
        storage_path=str(tmp_path / "connector.sqlite3"),
        activation_mode="inactive",
        activation_generation=0,
    )

    with pytest.raises(ValueError, match="explicit_activation_required"):
        create_app(settings)


def test_restart_halts_before_later_delivery_when_unknown_exists(tmp_path) -> None:
    path = tmp_path / "connector.sqlite3"
    store = NotificationStore(path)
    store.initialize()
    store.configure_activation(mode="one_shot", generation=1)
    store.admit(
        tenant_ref="johanna",
        command=_command("00000000-0000-4000-8000-000000000000", "0" * 64),
    )
    assert asyncio.run(_worker(store, AcceptedSlack()).run_once()) is True
    store.mark_activation_verified(generation=1, operator_id="operator")
    store.configure_activation(mode="continuous", generation=2)
    store.admit(
        tenant_ref="johanna",
        command=_command("11111111-1111-4111-8111-111111111111", "1" * 64),
    )
    store.admit(
        tenant_ref="johanna",
        command=_command("22222222-2222-4222-8222-222222222222", "2" * 64),
    )
    first = store.claim_next(worker_id="worker-before-restart")
    assert first is not None
    store.mark_request_started(first)
    store.finalize_delivery_unknown(first, failure_code="slack_delivery_unknown")

    restarted = NotificationStore(path)
    restarted.initialize()
    slack = AcceptedSlack()
    worker = _worker(restarted, slack)

    assert asyncio.run(worker.run_once()) is False
    assert worker.healthy is False
    assert slack.calls == 0
    assert restarted.state_inventory() == {
        "pending": 1,
        "claimed": 0,
        "request_started": 0,
        "delivery_unknown": 1,
    }

    restarted.reconcile_delivery_unknown(
        tenant_ref="johanna",
        notification_id=first.notification_id,
        decision="confirm_not_delivered",
        operator_id="operator",
        channel_id="C0C0YEACVT2",
    )
    worker.resume_after_verified_connectivity()

    assert worker.healthy is True
    assert asyncio.run(worker.run_once()) is True
    assert slack.calls == 1


def test_connectivity_cycle_cannot_clear_a_non_connectivity_halt(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    worker = _worker(store, AcceptedSlack())

    worker.halt("slack_rejected")
    worker.halt()
    worker.resume_after_verified_connectivity()

    assert worker.healthy is False
