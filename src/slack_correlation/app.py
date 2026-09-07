"""Default-off central Slack operations connector."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
import re
from typing import Protocol

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from slack_correlation.client import SlackClient, SlackProtocolError


_TEAM_ID = re.compile(r"^T[A-Z0-9]{8,}$")
_CHANNEL_ID = re.compile(r"^C[A-Z0-9]{8,}$")


class SlackAuthVerifier(Protocol):
    async def verify_auth(self, *, expected_team_id: str) -> None: ...


@dataclass(frozen=True)
class SlackConnectorSettings:
    """Explicit runtime state for the central Slack connector."""

    notifications_enabled: bool = False
    interactions_enabled: bool = False
    connectivity_check_enabled: bool = False
    bot_token: str | None = None
    signing_secret: str | None = None
    team_id: str | None = None
    channel_id: str | None = None

    @classmethod
    def from_env(cls) -> "SlackConnectorSettings":
        return cls(
            notifications_enabled=_env_flag("SLACK_NOTIFICATIONS_ENABLED"),
            interactions_enabled=_env_flag("SLACK_INTERACTIONS_ENABLED"),
            connectivity_check_enabled=_env_flag(
                "SLACK_CONNECTIVITY_CHECK_ENABLED"
            ),
            bot_token=_env_value("SLACK_BOT_TOKEN"),
            signing_secret=_env_value("SLACK_SIGNING_SECRET"),
            team_id=_env_value("SLACK_TEAM_ID"),
            channel_id=_env_value("SLACK_CHANNEL_ID"),
        )


def _env_flag(name: str) -> bool:
    value = os.getenv(name, "false").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"invalid_boolean:{name}")
    return value == "true"


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def create_app(
    settings: SlackConnectorSettings,
    *,
    slack_client: SlackAuthVerifier | None = None,
) -> FastAPI:
    """Create an inert-by-default ASGI application."""

    if settings.notifications_enabled:
        raise ValueError("notifications_not_implemented")
    if settings.interactions_enabled:
        raise ValueError("interactions_not_implemented")
    if settings.connectivity_check_enabled:
        bot_token = settings.bot_token
        team_id = settings.team_id
        channel_id = settings.channel_id
        if not all((bot_token, team_id, channel_id)):
            raise ValueError("connectivity_configuration_incomplete")
        assert bot_token is not None
        assert team_id is not None
        assert channel_id is not None
        if (
            not bot_token.startswith("xoxb-")
            or _TEAM_ID.fullmatch(team_id) is None
            or _CHANNEL_ID.fullmatch(channel_id) is None
        ):
            raise ValueError("invalid_slack_configuration")

    state = {"connectivity_verified": False}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.connectivity_check_enabled:
            assert settings.bot_token is not None
            assert settings.team_id is not None
            checker = slack_client or SlackClient(bot_token=settings.bot_token)
            try:
                await checker.verify_auth(expected_team_id=settings.team_id)
            except SlackProtocolError:
                state["connectivity_verified"] = False
            else:
                state["connectivity_verified"] = True
        yield

    app = FastAPI(title="SupportMagician Slack Connector", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        mode = "inactive"
        status = "ok"
        status_code = 200
        if settings.connectivity_check_enabled and state["connectivity_verified"]:
            mode = "connectivity_verified"
        elif settings.connectivity_check_enabled:
            mode = "connectivity_failed"
            status = "not_ready"
            status_code = 503
        payload = {
            "status": status,
            "mode": mode,
            "notifications_enabled": settings.notifications_enabled,
            "interactions_enabled": settings.interactions_enabled,
            "connectivity_check_enabled": settings.connectivity_check_enabled,
            "bot_token_configured": settings.bot_token is not None,
            "signing_secret_configured": settings.signing_secret is not None,
            "team_configured": settings.team_id is not None,
            "channel_configured": settings.channel_id is not None,
        }
        return JSONResponse(status_code=status_code, content=payload)

    return app
