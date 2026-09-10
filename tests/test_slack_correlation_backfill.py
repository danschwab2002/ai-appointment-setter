from __future__ import annotations

from datetime import UTC, datetime
import json
import sqlite3

from fastapi.testclient import TestClient

from slack_correlation.app import SlackConnectorSettings, build_app, create_app
from slack_correlation.catalog import NotificationCommand
from slack_correlation.store import NotificationStore

TEAM = "T12345678"
CHANNEL = "C0C0YEACVT2"
CASE = "11111111-1111-4111-8111-111111111111"
TS = "1789000000.000001"


class SlackUpdateOnly:
    def __init__(self, *, fail: bool = False) -> None:
        self.updates: list[tuple[str, str, dict]] = []
        self.posts = 0
        self.fail = fail

    async def verify_auth(self, *, expected_team_id: str) -> None:
        assert expected_team_id == TEAM

    async def update_message(self, *, channel_id: str, message_ts: str, message: dict):
        self.updates.append((channel_id, message_ts, message))
        if self.fail:
            from slack_correlation.client import SlackProtocolError
            raise SlackProtocolError("update_unknown")


class Operator:
    async def get_case(self, case_id: str) -> dict:
        assert case_id == CASE
        return {
            "case_id": CASE,
            "outcome": "unmatched",
            "candidate_count": 0,
            "automation_blocked": True,
            "identity": {"masked_email": "a***z@example.com", "masked_phone": None},
            "candidates": [],
        }


class MultiCaseOperator:
    async def get_case(self, case_id: str) -> dict:
        return {
            "case_id": case_id,
            "outcome": "unmatched",
            "candidate_count": 0,
            "automation_blocked": True,
            "identity": {"masked_email": "a***z@example.com", "masked_phone": None},
            "candidates": [],
        }


def _store(tmp_path) -> NotificationStore:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    command = NotificationCommand(
        event_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        event_code="COR-001",
        dedupe_key="a" * 64,
        occurred_at=datetime(2026, 9, 9, 12, tzinfo=UTC),
        subject_ref=f"C-{CASE}",
        deadline_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
    )
    store.admit(tenant_ref="johanna", command=command, channel_id=CHANNEL)
    claim = store.claim_next(worker_id="worker-1")
    assert claim is not None
    store.mark_request_started(claim)
    store.finalize_accepted(claim, channel_id=CHANNEL, message_ts=TS, thread_ts=None, team_id=TEAM)
    return store


def _app(tmp_path, slack):
    store = _store(tmp_path)
    app = create_app(
        SlackConnectorSettings(
            correlation_backfill_enabled=True,
            bot_token="xoxb-synthetic",
            operator_bearer_token="o" * 32,
            tenant_channels={"johanna": CHANNEL},
            operator_backends={"johanna": {
                "base_url": "https://operator.example", "read_token": "r" * 32,
                "write_token": "w" * 32,
            }},
            team_id=TEAM,
            storage_path=str(tmp_path / "connector.sqlite3"),
        ),
        store=store,
        slack_client=slack,
        operator_clients={"johanna": Operator()},
    )
    return app, store


def test_backfill_endpoint_is_404_when_separate_flag_is_off(tmp_path) -> None:
    store = _store(tmp_path)
    app = create_app(
        SlackConnectorSettings(
            operator_bearer_token="o" * 32,
            tenant_channels={"johanna": CHANNEL}, team_id=TEAM,
            storage_path=str(tmp_path / "connector.sqlite3"),
        ),
        store=store, slack_client=SlackUpdateOnly(),
        operator_clients={"johanna": Operator()},
    )
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/operator/backfill-correlation",
            headers={"Authorization": "Bearer " + "o" * 32},
            json={"tenant_ref": "johanna"},
        )
    assert response.status_code == 404


def test_backfill_updates_one_existing_root_in_place_and_never_posts(tmp_path) -> None:
    slack = SlackUpdateOnly()
    app, store = _app(tmp_path, slack)
    with TestClient(app) as client:
        first = client.post(
            "/internal/v1/operator/backfill-correlation",
            headers={"Authorization": "Bearer " + "o" * 32},
            json={"tenant_ref": "johanna"},
        )
        second = client.post(
            "/internal/v1/operator/backfill-correlation",
            headers={"Authorization": "Bearer " + "o" * 32},
            json={"tenant_ref": "johanna"},
        )
    assert first.status_code == 200
    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "idle"
    assert slack.posts == 0
    assert len(slack.updates) == 1
    assert slack.updates[0][0:2] == (CHANNEL, TS)
    assert "review_operator_correlation" in repr(slack.updates[0][2])
    assert store.projection_inventory()["accepted"] == 1


def test_ambiguous_backfill_update_is_not_retried_and_degrades_readiness(tmp_path) -> None:
    slack = SlackUpdateOnly(fail=True)
    app, store = _app(tmp_path, slack)
    with TestClient(app) as client:
        first = client.post(
            "/internal/v1/operator/backfill-correlation",
            headers={"Authorization": "Bearer " + "o" * 32},
            json={"tenant_ref": "johanna"},
        )
        second = client.post(
            "/internal/v1/operator/backfill-correlation",
            headers={"Authorization": "Bearer " + "o" * 32},
            json={"tenant_ref": "johanna"},
        )
        ready = client.get("/ready")
    assert first.json()["status"] == "delivery_unknown"
    assert second.json()["status"] == "idle"
    assert len(slack.updates) == 1
    assert store.projection_inventory()["delivery_unknown"] == 1
    assert ready.status_code == 503
    assert ready.json()["projection_ledger"]["delivery_unknown"] == 1


def test_projection_claim_recovery_returns_pre_effect_work_to_pending(tmp_path) -> None:
    store = _store(tmp_path)
    binding = store.claim_projection(
        tenant_ref="johanna", team_id=TEAM, channel_id=CHANNEL,
    )
    assert binding is not None
    assert store.projection_inventory()["claimed"] == 1
    store.recover_incomplete()
    inventory = store.projection_inventory()
    assert inventory["pending"] == 1
    assert inventory["delivery_unknown"] == 0


def test_v2_accepted_correlation_is_queued_and_updated_in_place(tmp_path) -> None:
    store = _store(tmp_path)
    with sqlite3.connect(tmp_path / "connector.sqlite3") as connection:
        connection.execute("DROP TABLE correlation_projections")
        connection.execute("PRAGMA user_version = 2")
    store.initialize()
    assert store.projection_inventory()["pending"] == 1

    slack = SlackUpdateOnly()
    app = create_app(
        SlackConnectorSettings(
            correlation_backfill_enabled=True,
            bot_token="xoxb-synthetic",
            operator_bearer_token="o" * 32,
            tenant_channels={"johanna": CHANNEL},
            operator_backends={"johanna": {
                "base_url": "https://operator.example", "read_token": "r" * 32,
                "write_token": "w" * 32,
            }},
            team_id=TEAM,
            storage_path=str(tmp_path / "connector.sqlite3"),
        ),
        store=store,
        slack_client=slack,
        operator_clients={"johanna": Operator()},
    )
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/operator/backfill-correlation",
            headers={"Authorization": "Bearer " + "o" * 32},
            json={"tenant_ref": "johanna"},
        )
    assert response.json()["status"] == "accepted"
    assert slack.posts == 0
    assert [(channel, ts) for channel, ts, _message in slack.updates] == [(CHANNEL, TS)]


def test_env_factory_enables_backfill_without_interactions(monkeypatch, tmp_path) -> None:
    path = tmp_path / "env-factory.sqlite3"
    NotificationStore(path).initialize()
    monkeypatch.setenv("SLACK_CORRELATION_BACKFILL_ENABLED", "true")
    monkeypatch.setenv("SLACK_INTERACTIONS_ENABLED", "false")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-synthetic")
    monkeypatch.setenv("SLACK_TEAM_ID", TEAM)
    monkeypatch.setenv("SLACK_TENANT_CHANNELS_JSON", json.dumps({"johanna": CHANNEL}))
    monkeypatch.setenv("SLACK_TENANT_OPERATOR_BACKENDS_JSON", json.dumps({
        "johanna": {
            "base_url": "https://operator.example",
            "read_token": "r" * 32,
            "write_token": "w" * 32,
        }
    }))
    monkeypatch.setenv("SLACK_OPERATOR_BEARER_TOKEN", "o" * 32)
    monkeypatch.setenv("SLACK_STORAGE_PATH", str(path))
    slack = SlackUpdateOnly()
    monkeypatch.setattr("slack_correlation.app.SlackClient", lambda **_kwargs: slack)
    monkeypatch.setattr(
        "slack_correlation.operator_client.OperatorBridgeClient",
        lambda **_kwargs: MultiCaseOperator(),
    )

    app = build_app()
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/operator/backfill-correlation",
            headers={"Authorization": "Bearer " + "o" * 32},
            json={"tenant_ref": "johanna"},
        )
        assert client.post("/slack/interactions").status_code == 404
    assert response.status_code == 200
    assert response.json() == {"status": "idle"}


def test_backfill_all_13_johanna_roots_across_restart_without_duplicates(tmp_path) -> None:
    path = tmp_path / "thirteen.sqlite3"
    store = NotificationStore(path)
    store.initialize()
    expected: list[tuple[str, str]] = []
    for index in range(13):
        case_id = f"{index + 1:08d}-1111-4111-8111-111111111111"
        event_id = f"{index + 1:08d}-2222-4222-8222-222222222222"
        message_ts = f"1789000000.{index + 1:06d}"
        command = NotificationCommand(
            event_id=event_id,
            event_code="COR-001",
            dedupe_key=f"{index + 1:064x}",
            occurred_at=datetime(2026, 9, 9, 12, tzinfo=UTC),
            subject_ref=f"C-{case_id}",
            deadline_at=datetime(2026, 9, 10, 12, tzinfo=UTC),
        )
        store.admit(
            tenant_ref="johanna", team_id=TEAM, command=command, channel_id=CHANNEL,
        )
        claim = store.claim_next(worker_id="fixture")
        assert claim is not None
        store.mark_request_started(claim)
        store.finalize_accepted(
            claim, channel_id=CHANNEL, message_ts=message_ts, thread_ts=None, team_id=TEAM,
        )
        expected.append((CHANNEL, message_ts))

    slack = SlackUpdateOnly()
    settings = SlackConnectorSettings(
        correlation_backfill_enabled=True,
        bot_token="xoxb-synthetic",
        operator_bearer_token="o" * 32,
        tenant_channels={"johanna": CHANNEL},
        operator_backends={"johanna": {
            "base_url": "https://operator.example", "read_token": "r" * 32,
            "write_token": "w" * 32,
        }},
        team_id=TEAM,
        storage_path=str(path),
    )
    headers = {"Authorization": "Bearer " + "o" * 32}
    with TestClient(create_app(
        settings, store=store, slack_client=slack,
        operator_clients={"johanna": MultiCaseOperator()},
    )) as client:
        for _ in range(5):
            assert client.post(
                "/internal/v1/operator/backfill-correlation", headers=headers,
                json={"tenant_ref": "johanna"},
            ).json()["status"] == "accepted"

    restarted = NotificationStore(path)
    with TestClient(create_app(
        settings, store=restarted, slack_client=slack,
        operator_clients={"johanna": MultiCaseOperator()},
    )) as client:
        statuses = [client.post(
            "/internal/v1/operator/backfill-correlation", headers=headers,
            json={"tenant_ref": "johanna"},
        ).json()["status"] for _ in range(9)]
    assert statuses == ["accepted"] * 8 + ["idle"]
    assert slack.posts == 0
    actual = [(channel, ts) for channel, ts, _message in slack.updates]
    assert actual == expected
    assert len(actual) == len(set(actual)) == 13
