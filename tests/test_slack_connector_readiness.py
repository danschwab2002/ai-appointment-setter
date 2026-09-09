from __future__ import annotations

import time

from fastapi.testclient import TestClient

from slack_correlation.app import SlackConnectorSettings, create_app
from slack_correlation.client import SlackMessageReference, SlackProtocolError
from slack_correlation.store import NotificationStore


class _SlackBecomesUncertain:
    async def verify_auth(self, *, expected_team_id: str) -> None:
        return None

    async def post_message(
        self, *, channel_id: str, message: dict[str, object]
    ) -> SlackMessageReference:
        raise SlackProtocolError("message_delivery_unknown")


class _SlackExpiresAfterStartup:
    def __init__(self) -> None:
        self.calls = 0

    async def verify_auth(self, *, expected_team_id: str) -> None:
        self.calls += 1
        if self.calls > 1:
            raise SlackProtocolError("slack_auth_failed")

    async def post_message(
        self, *, channel_id: str, message: dict[str, object]
    ) -> SlackMessageReference:
        raise AssertionError("no message should be sent")


class _SlackRecoversAfterStartup:
    def __init__(self) -> None:
        self.calls = 0

    async def verify_auth(self, *, expected_team_id: str) -> None:
        self.calls += 1
        if self.calls == 1:
            raise SlackProtocolError("slack_auth_failed")

    async def post_message(
        self, *, channel_id: str, message: dict[str, object]
    ) -> SlackMessageReference:
        raise AssertionError("no message should be sent")


def test_readiness_stays_halted_until_delivery_unknown_is_reconciled(tmp_path) -> None:
    now = [0.0]
    settings = SlackConnectorSettings(
        ingress_enabled=True,
        notifications_enabled=True,
        activation_mode="one_shot",
        activation_generation=1,
        tenant_tokens={"johanna": "j" * 32, "att1": "a" * 32},
        bot_token="xoxb-test",
        team_id="T00000000",
        channel_id="C0C0YEACVT2",
        storage_path=str(tmp_path / "connector.sqlite3"),
        poll_interval_seconds=1.0,
    )
    app = create_app(
        settings,
        slack_client=_SlackBecomesUncertain(),
        store=NotificationStore(settings.storage_path),
        monotonic_clock=lambda: now[0],
    )
    payload = {
        "event_id": "99999999-9999-4999-8999-999999999999",
        "event_code": "SYS-004",
        "dedupe_key": "9" * 64,
        "occurred_at": "2026-09-07T12:00:00Z",
        "component": "slack_api",
        "state": "degraded",
    }

    with TestClient(app) as client:
        admitted = client.post(
            "/internal/v1/notifications",
            headers={
                "Authorization": f"Bearer {'j' * 32}",
                "X-Expected-Tenant-Ref": "johanna",
            },
            json=payload,
        )
        assert admitted.status_code == 202
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = client.get(
                "/internal/v1/notifications/99999999-9999-4999-8999-999999999999",
                headers={
                    "Authorization": f"Bearer {'j' * 32}",
                    "X-Expected-Tenant-Ref": "johanna",
                },
            )
            if status.json()["delivery_state"] == "delivery_unknown":
                break
            time.sleep(0.05)

        ready = client.get("/ready")
        now[0] = 61.0
        recovered = client.get("/ready")

    assert ready.status_code == 503
    assert ready.json()["mode"] == "outbound_halted"
    assert ready.json()["worker_running"] is False
    assert recovered.status_code == 503
    assert recovered.json()["mode"] == "outbound_halted"
    assert recovered.json()["worker_running"] is False
    assert recovered.json()["ledger"]["delivery_unknown"] == 1


def test_readiness_rechecks_slack_auth_after_the_cache_window() -> None:
    now = [0.0]
    slack = _SlackExpiresAfterStartup()
    settings = SlackConnectorSettings(
        connectivity_check_enabled=True,
        bot_token="xoxb-test",
        team_id="T00000000",
        channel_id="C0C0YEACVT2",
    )
    app = create_app(
        settings,
        slack_client=slack,
        monotonic_clock=lambda: now[0],
    )

    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        now[0] = 61.0
        expired = client.get("/ready")

    assert expired.status_code == 503
    assert expired.json()["mode"] == "connectivity_failed"
    assert slack.calls == 2


def test_worker_starts_after_initial_slack_auth_recovers(tmp_path) -> None:
    now = [0.0]
    slack = _SlackRecoversAfterStartup()
    settings = SlackConnectorSettings(
        notifications_enabled=True,
        activation_mode="one_shot",
        activation_generation=1,
        bot_token="xoxb-test",
        team_id="T00000000",
        channel_id="C0C0YEACVT2",
        storage_path=str(tmp_path / "connector.sqlite3"),
    )
    app = create_app(
        settings,
        slack_client=slack,
        monotonic_clock=lambda: now[0],
    )

    with TestClient(app) as client:
        initial = client.get("/ready")
        now[0] = 61.0
        recovered = client.get("/ready")

    assert initial.status_code == 503
    assert initial.json()["mode"] == "connectivity_failed"
    assert initial.json()["worker_running"] is False
    assert recovered.status_code == 200
    assert recovered.json()["mode"] == "operational"
    assert recovered.json()["worker_running"] is True
    assert slack.calls == 2
