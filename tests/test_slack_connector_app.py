from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient
import pytest

from slack_correlation.app import SlackConnectorSettings, create_app
from slack_correlation.client import SlackClient, SlackProtocolError


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
        "notifications_enabled": False,
        "interactions_enabled": False,
        "connectivity_check_enabled": False,
        "bot_token_configured": False,
        "signing_secret_configured": False,
        "team_configured": False,
        "channel_configured": False,
    }


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


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        (
            SlackConnectorSettings(notifications_enabled=True),
            "notifications_not_implemented",
        ),
        (
            SlackConnectorSettings(interactions_enabled=True),
            "interactions_not_implemented",
        ),
    ],
)
def test_connector_refuses_to_enable_unimplemented_effects(
    settings: SlackConnectorSettings,
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        create_app(settings)


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
        "notifications_enabled": False,
        "interactions_enabled": False,
        "connectivity_check_enabled": True,
        "bot_token_configured": True,
        "signing_secret_configured": False,
        "team_configured": True,
        "channel_configured": True,
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
