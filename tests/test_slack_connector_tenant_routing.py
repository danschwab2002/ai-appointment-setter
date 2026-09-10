from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from slack_correlation.app import SlackConnectorSettings, create_app
from slack_correlation.catalog import NotificationCommand
from slack_correlation.client import SlackMessageReference
from slack_correlation.store import NotificationStore
from slack_correlation.worker import NotificationWorker


JOHANNA_CHANNEL = "C0C0YEACVT2"
ATT1_CHANNEL = "C0ATT1TEST01"


def _command(*, event_id: str, dedupe_key: str) -> NotificationCommand:
    return NotificationCommand(
        event_id=event_id,
        event_code="HND-001",
        dedupe_key=dedupe_key,
        occurred_at=datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
        subject_ref=f"C-{event_id.upper()}",
        reason_code="explicit_human_request",
        state="paused",
    )


class _AcceptedSlack:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def verify_auth(self, *, expected_team_id: str) -> None:
        assert expected_team_id == "T12345678"

    async def post_message(
        self, *, channel_id: str, message: dict[str, object]
    ) -> SlackMessageReference:
        self.messages.append((channel_id, message))
        return SlackMessageReference(
            channel_id=channel_id,
            message_ts=f"1789000000.{len(self.messages):06d}",
        )


def test_settings_reject_channel_reuse_between_tenants() -> None:
    with pytest.raises(ValueError, match="tenant_channel_collision"):
        create_app(
            SlackConnectorSettings(
                ingress_enabled=True,
                team_id="T12345678",
                tenant_tokens={"johanna": "j" * 32, "att1": "a" * 32},
                tenant_channels={
                    "johanna": JOHANNA_CHANNEL,
                    "att1": JOHANNA_CHANNEL,
                },
            )
        )


def test_unrouted_tenant_is_not_authenticated_or_admitted(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    app = create_app(
        SlackConnectorSettings(
            ingress_enabled=True,
            team_id="T12345678",
            storage_path=str(tmp_path / "connector.sqlite3"),
            tenant_tokens={"johanna": "j" * 32},
            tenant_channels={"johanna": JOHANNA_CHANNEL},
        ),
        store=store,
    )
    payload = {
        "event_id": "22222222-2222-4222-8222-222222222222",
        "event_code": "HND-001",
        "dedupe_key": "2" * 64,
        "occurred_at": "2026-09-09T12:00:00Z",
        "subject_ref": "C-22222222-2222-4222-8222-222222222222",
        "reason_code": "explicit_human_request",
        "state": "paused",
    }

    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/notifications",
            headers={
                "Authorization": f"Bearer {'a' * 32}",
                "X-Expected-Tenant-Ref": "att1",
            },
            json=payload,
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert store.count() == 0


def test_worker_routes_each_notification_to_its_bound_tenant_channel(
    tmp_path: Path,
) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    store.configure_activation(mode="one_shot", generation=1)
    store.admit(
        tenant_ref="johanna",
        channel_id=JOHANNA_CHANNEL,
        command=_command(
            event_id="11111111-1111-4111-8111-111111111111",
            dedupe_key="1" * 64,
        ),
    )
    slack = _AcceptedSlack()
    worker = NotificationWorker(
        store=store,
        slack_client=slack,
        tenant_channels={"johanna": JOHANNA_CHANNEL, "att1": ATT1_CHANNEL},
        tenant_labels={"johanna": "Johanna", "att1": "ATT1"},
        worker_id="worker-1",
    )

    assert asyncio.run(worker.run_once()) is True
    assert slack.messages[0][0] == JOHANNA_CHANNEL


def test_exact_replay_cannot_change_the_server_owned_channel(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    command = _command(
        event_id="11111111-1111-4111-8111-111111111111",
        dedupe_key="1" * 64,
    )

    admitted = store.admit(
        tenant_ref="johanna",
        channel_id=JOHANNA_CHANNEL,
        command=command,
    )
    replay = store.admit(
        tenant_ref="johanna",
        channel_id=ATT1_CHANNEL,
        command=command,
    )

    assert admitted.outcome == "admitted"
    assert replay.outcome == "semantic_conflict"


def test_reserved_johanna_channel_cannot_be_routed_to_another_tenant() -> None:
    with pytest.raises(ValueError, match="reserved_johanna_channel"):
        create_app(
            SlackConnectorSettings(
                tenant_channels={"att1": JOHANNA_CHANNEL},
            )
        )


def test_johanna_route_must_use_reserved_channel() -> None:
    with pytest.raises(ValueError, match="reserved_johanna_channel"):
        create_app(
            SlackConnectorSettings(
                tenant_channels={"johanna": ATT1_CHANNEL},
            )
        )


def test_ingress_allows_exact_johanna_only_token_and_route_subset(tmp_path: Path) -> None:
    app = create_app(
        SlackConnectorSettings(
            ingress_enabled=True,
            storage_path=str(tmp_path / "connector.sqlite3"),
            team_id="T12345678",
            tenant_tokens={"johanna": "j" * 32},
            tenant_channels={"johanna": JOHANNA_CHANNEL},
        )
    )
    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200


def test_ingress_requires_exact_team_binding() -> None:
    with pytest.raises(ValueError, match="ingress_team_required"):
        create_app(SlackConnectorSettings(
            ingress_enabled=True,
            tenant_tokens={"johanna": "j" * 32},
            tenant_channels={"johanna": JOHANNA_CHANNEL},
        ))


def test_ingress_rejects_token_route_set_mismatch() -> None:
    with pytest.raises(ValueError, match="tenant_token_route_mismatch"):
        create_app(
            SlackConnectorSettings(
                ingress_enabled=True,
                team_id="T12345678",
                tenant_tokens={"johanna": "j" * 32, "att1": "a" * 32},
                tenant_channels={"johanna": JOHANNA_CHANNEL},
            )
        )


def test_notification_replay_cannot_change_server_owned_team(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    command = _command(
        event_id="11111111-1111-4111-8111-111111111111",
        dedupe_key="1" * 64,
    )
    admitted = store.admit(
        tenant_ref="johanna", team_id="T12345678", channel_id=JOHANNA_CHANNEL,
        command=command,
    )
    replay = store.admit(
        tenant_ref="johanna", team_id="T87654321", channel_id=JOHANNA_CHANNEL,
        command=command,
    )
    assert admitted.outcome == "admitted"
    assert replay.outcome == "semantic_conflict"


def test_worker_rejects_stored_team_mismatch_before_slack_request(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    store.admit(
        tenant_ref="johanna", team_id="T12345678", channel_id=JOHANNA_CHANNEL,
        command=_command(
            event_id="11111111-1111-4111-8111-111111111111", dedupe_key="1" * 64,
        ),
    )
    slack = _AcceptedSlack()
    worker = NotificationWorker(
        store=store, slack_client=slack,
        tenant_channels={"johanna": JOHANNA_CHANNEL}, tenant_labels={"johanna": "Johanna"},
        worker_id="worker-1", team_id="T87654321",
    )
    with pytest.raises(RuntimeError, match="tenant_team_binding_mismatch"):
        asyncio.run(worker.run_once())
    assert slack.messages == []


def test_worker_rejects_unbound_team_before_slack_request(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    store.admit(
        tenant_ref="johanna", channel_id=JOHANNA_CHANNEL,
        command=_command(
            event_id="11111111-1111-4111-8111-111111111111", dedupe_key="1" * 64,
        ),
    )
    slack = _AcceptedSlack()
    worker = NotificationWorker(
        store=store, slack_client=slack,
        tenant_channels={"johanna": JOHANNA_CHANNEL}, tenant_labels={"johanna": "Johanna"},
        worker_id="worker-1", team_id="T12345678",
    )
    with pytest.raises(RuntimeError, match="tenant_team_binding_mismatch"):
        asyncio.run(worker.run_once())
    assert slack.messages == []
