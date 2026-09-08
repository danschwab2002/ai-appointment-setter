from __future__ import annotations

import asyncio
from pathlib import Path
import time

import httpx
from fastapi.testclient import TestClient
import pytest

from slack_correlation.app import (
    PayloadTooLarge,
    SlackConnectorSettings,
    _read_bounded_body,
    create_app,
)
from slack_correlation.client import SlackClient, SlackMessageReference, SlackProtocolError
from slack_correlation.store import NotificationStore


def test_inactive_connector_exposes_sanitized_health_and_readiness() -> None:
    app = create_app(SlackConnectorSettings())

    with TestClient(app) as client:
        health = client.get("/health")
        readiness = client.get("/ready")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert readiness.status_code == 200
    assert readiness.json() == {
        "status": "ok",
        "mode": "inactive",
        "ingress_enabled": False,
        "notifications_enabled": False,
        "interactions_enabled": False,
        "connectivity_check_enabled": False,
        "bot_token_configured": False,
        "signing_secret_configured": False,
        "team_configured": False,
        "channel_configured": False,
        "storage_ready": False,
        "tenant_count": 0,
        "worker_running": False,
    }


def test_public_api_documentation_surfaces_are_disabled() -> None:
    app = create_app(SlackConnectorSettings())

    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_settings_from_env_are_default_off(monkeypatch) -> None:
    for name in (
        "SLACK_NOTIFICATIONS_ENABLED",
        "SLACK_INTERACTIONS_ENABLED",
        "SLACK_CONNECTIVITY_CHECK_ENABLED",
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "SLACK_TEAM_ID",
        "SLACK_CHANNEL_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    assert SlackConnectorSettings.from_env() == SlackConnectorSettings()


def test_settings_reject_ambiguous_boolean_values(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_CONNECTIVITY_CHECK_ENABLED", "yes")

    with pytest.raises(
        ValueError, match="invalid_boolean:SLACK_CONNECTIVITY_CHECK_ENABLED"
    ):
        SlackConnectorSettings.from_env()


def test_connector_refuses_to_enable_unimplemented_interactions() -> None:
    with pytest.raises(ValueError, match="interactions_not_implemented"):
        create_app(SlackConnectorSettings(interactions_enabled=True))


def test_connectivity_check_requires_complete_exact_configuration() -> None:
    with pytest.raises(ValueError, match="connectivity_configuration_incomplete"):
        create_app(SlackConnectorSettings(connectivity_check_enabled=True))


@pytest.mark.parametrize(
    "overrides",
    [
        {"bot_token": "xoxp-user-token"},
        {"team_id": "C12345678"},
        {"channel_id": "T12345678"},
    ],
)
def test_connectivity_check_rejects_wrong_slack_identity_types(
    overrides: dict[str, str],
) -> None:
    values = {
        "connectivity_check_enabled": True,
        "bot_token": "xoxb-bot-token",
        "team_id": "T12345678",
        "channel_id": "C0C0YEACVT2",
        **overrides,
    }

    with pytest.raises(ValueError, match="invalid_slack_configuration"):
        create_app(SlackConnectorSettings(**values))


def test_connector_rejects_unsafe_internal_tokens_and_worker_poll_interval() -> None:
    with pytest.raises(ValueError, match="invalid_tenant_token_configuration"):
        create_app(
            SlackConnectorSettings(
                ingress_enabled=True,
                tenant_tokens={"johanna": "contains spaces" * 3, "att1": "a" * 32},
            )
        )
    with pytest.raises(ValueError, match="invalid_poll_interval"):
        create_app(SlackConnectorSettings(poll_interval_seconds=0))


def test_slack_client_auth_test_verifies_expected_workspace() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/auth.test"
        return httpx.Response(
            200,
            json={"ok": True, "team_id": "T12345678", "bot_id": "B12345678"},
        )

    client = SlackClient(
        bot_token="test-bot-token",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(client.verify_auth(expected_team_id="T12345678"))


def test_ready_verifies_slack_auth_without_sending_a_message() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={"ok": True, "team_id": "T12345678", "bot_id": "B12345678"},
        )

    settings = SlackConnectorSettings(
        connectivity_check_enabled=True,
        bot_token="xoxb-test-token",
        team_id="T12345678",
        channel_id="C0C0YEACVT2",
    )
    slack_client = SlackClient(
        bot_token="xoxb-test-token",
        transport=httpx.MockTransport(handler),
    )
    app = create_app(settings, slack_client=slack_client)

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "mode": "connectivity_verified",
        "ingress_enabled": False,
        "notifications_enabled": False,
        "interactions_enabled": False,
        "connectivity_check_enabled": True,
        "bot_token_configured": True,
        "signing_secret_configured": False,
        "team_configured": True,
        "channel_configured": True,
        "storage_ready": False,
        "tenant_count": 0,
        "worker_running": False,
    }
    assert requests == ["/api/auth.test"]


def test_ready_fails_closed_when_slack_auth_cannot_be_verified() -> None:
    class FailingSlackClient:
        async def verify_auth(self, *, expected_team_id: str) -> None:
            raise SlackProtocolError("slack_auth_rejected")

    settings = SlackConnectorSettings(
        connectivity_check_enabled=True,
        bot_token="xoxb-test-token",
        team_id="T12345678",
        channel_id="C0C0YEACVT2",
    )
    app = create_app(settings, slack_client=FailingSlackClient())

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["mode"] == "connectivity_failed"
    assert "slack_auth_rejected" not in response.text
    assert "xoxb" not in response.text
    assert "C0C0YEACVT2" not in response.text
    assert "T12345678" not in response.text


def test_authenticated_tenant_admission_is_durable_and_idempotent(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "slack.sqlite3")
    settings = SlackConnectorSettings(
        ingress_enabled=True,
        storage_path=str(tmp_path / "slack.sqlite3"),
        tenant_tokens={"johanna": "j" * 32, "att1": "a" * 32},
    )
    app = create_app(settings, store=store)
    payload = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "event_code": "HND-001",
        "dedupe_key": "1" * 64,
        "occurred_at": "2026-09-07T22:00:00Z",
        "subject_ref": "C-11111111",
        "reason_code": "explicit_human_request",
        "state": "paused",
    }

    with TestClient(app) as client:
        admitted = client.post(
            "/internal/v1/notifications",
            headers={"Authorization": f"Bearer {'j' * 32}"},
            json=payload,
        )
        duplicate = client.post(
            "/internal/v1/notifications",
            headers={"Authorization": f"Bearer {'j' * 32}"},
            json=payload,
        )

    assert admitted.status_code == 202
    assert admitted.json() == {
        "status": "admitted",
        "notification_id": payload["event_id"],
        "delivery_state": "pending",
    }
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert store.count() == 1


def test_operational_app_delivers_admitted_notification_and_exposes_status(
    tmp_path: Path,
) -> None:
    class AcceptedSlackClient:
        def __init__(self) -> None:
            self.messages: list[tuple[str, dict]] = []

        async def verify_auth(self, *, expected_team_id: str) -> None:
            assert expected_team_id == "T12345678"

        async def post_message(
            self, *, channel_id: str, message: dict
        ) -> SlackMessageReference:
            self.messages.append((channel_id, message))
            return SlackMessageReference(channel_id, "1788800000.000001")

    token = "j" * 32
    slack = AcceptedSlackClient()
    settings = SlackConnectorSettings(
        ingress_enabled=True,
        notifications_enabled=True,
        bot_token="xoxb-synthetic",
        team_id="T12345678",
        channel_id="C0C0YEACVT2",
        storage_path=str(tmp_path / "slack.sqlite3"),
        tenant_tokens={"johanna": token, "att1": "a" * 32},
        poll_interval_seconds=1.0,
    )
    payload = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "event_code": "HND-001",
        "dedupe_key": "1" * 64,
        "occurred_at": "2026-09-07T22:00:00Z",
        "subject_ref": "C-11111111",
        "reason_code": "explicit_human_request",
        "state": "paused",
    }
    app = create_app(settings, slack_client=slack)

    with TestClient(app) as client:
        admitted = client.post(
            "/internal/v1/notifications",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        deadline = time.monotonic() + 2
        while True:
            status = client.get(
                f"/internal/v1/notifications/{payload['event_id']}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if status.json().get("delivery_state") == "accepted":
                break
            if time.monotonic() >= deadline:
                pytest.fail(f"notification did not complete: {status.json()}")
            time.sleep(0.02)

    assert admitted.status_code == 202
    assert status.status_code == 200
    assert status.json()["message_ts"] == "1788800000.000001"
    assert len(slack.messages) == 1
    assert slack.messages[0][0] == "C0C0YEACVT2"


def test_second_connector_instance_cannot_share_the_same_sqlite_volume(tmp_path: Path) -> None:
    settings = SlackConnectorSettings(
        ingress_enabled=True,
        storage_path=str(tmp_path / "connector.sqlite3"),
        tenant_tokens={"johanna": "j" * 32, "att1": "a" * 32},
    )
    first = create_app(settings)
    second = create_app(settings)

    with TestClient(first) as first_client:
        assert first_client.get("/ready").status_code == 200
        with TestClient(second) as second_client:
            response = second_client.get("/ready")

        assert response.status_code == 503
        assert response.json()["mode"] == "storage_unavailable"
        assert first_client.get("/ready").status_code == 200


def test_readiness_fails_when_the_store_stops_being_writable(tmp_path: Path) -> None:
    class FailingProbeStore(NotificationStore):
        def probe(self) -> None:
            raise OSError("synthetic storage failure")

    settings = SlackConnectorSettings(
        ingress_enabled=True,
        storage_path=str(tmp_path / "connector.sqlite3"),
        tenant_tokens={"johanna": "j" * 32, "att1": "a" * 32},
    )
    app = create_app(settings, store=FailingProbeStore(settings.storage_path))

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["mode"] == "storage_unavailable"


def test_admission_storage_failure_is_sanitized_and_fails_closed(tmp_path: Path) -> None:
    class FailingAdmissionStore(NotificationStore):
        def admit(self, **kwargs):
            raise OSError("/secret/internal/storage/path")

    settings = SlackConnectorSettings(
        ingress_enabled=True,
        storage_path=str(tmp_path / "connector.sqlite3"),
        tenant_tokens={"johanna": "j" * 32, "att1": "a" * 32},
    )
    app = create_app(settings, store=FailingAdmissionStore(settings.storage_path))
    payload = {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "event_code": "HND-001",
        "dedupe_key": "1" * 64,
        "occurred_at": "2026-09-07T22:00:00Z",
    }

    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/notifications",
            headers={"Authorization": "Bearer " + "j" * 32},
            json=payload,
        )
        exact_retry = client.post(
            "/internal/v1/notifications",
            headers={"Authorization": "Bearer " + "j" * 32},
            json=payload,
        )
        unauthorized = client.post(
            "/internal/v1/notifications",
            headers={"Authorization": "Bearer " + "x" * 32},
            json=payload,
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "storage_unavailable"}
    assert exact_retry.status_code == 503
    assert exact_retry.json() == {"detail": "storage_unavailable"}
    assert unauthorized.status_code == 401
    assert "/secret" not in response.text


def test_body_reader_stops_as_soon_as_the_limit_is_crossed() -> None:
    class ChunkedRequest:
        def __init__(self) -> None:
            self.consumed = 0

        async def stream(self):
            for chunk in (b"a" * 5000, b"b" * 5000):
                self.consumed += 1
                yield chunk
            raise AssertionError("reader consumed beyond the rejecting chunk")

    request = ChunkedRequest()
    with pytest.raises(PayloadTooLarge):
        asyncio.run(_read_bounded_body(request, limit=8192))
    assert request.consumed == 2
