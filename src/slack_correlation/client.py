"""Minimal strict client for Slack correlation projections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

_MESSAGE_TS = re.compile(r"^[0-9]+\.[0-9]+$")


class SlackProtocolError(RuntimeError):
    """Raised when Slack transport or response identity is not trustworthy."""


@dataclass(frozen=True)
class SlackMessageReference:
    """Canonical external identity returned for one accepted Slack message."""

    channel_id: str
    message_ts: str


class SlackClient:
    """Post native Slack messages and validate their external identity."""

    def __init__(
        self,
        *,
        bot_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(bot_token, str) or not bot_token:
            raise ValueError("bot_token is required")
        self._bot_token = bot_token
        self._transport = transport

    async def verify_auth(self, *, expected_team_id: str) -> None:
        """Verify the token belongs to the configured Slack workspace."""

        if not isinstance(expected_team_id, str) or not expected_team_id:
            raise ValueError("expected_team_id is required")
        try:
            async with httpx.AsyncClient(
                base_url="https://slack.com/api",
                headers={"Authorization": f"Bearer {self._bot_token}"},
                transport=self._transport,
                timeout=15,
            ) as client:
                response = await client.post("/auth.test")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SlackProtocolError("slack_auth_unavailable") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise SlackProtocolError("invalid_json") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise SlackProtocolError("slack_auth_rejected")
        if (
            payload.get("team_id") != expected_team_id
            or not isinstance(payload.get("bot_id"), str)
            or not payload["bot_id"]
        ):
            raise SlackProtocolError("slack_auth_identity_mismatch")

    async def post_message(
        self,
        *,
        channel_id: str,
        message: dict[str, Any],
    ) -> SlackMessageReference:
        if (
            not isinstance(channel_id, str)
            or not channel_id
            or any(character.isspace() for character in channel_id)
        ):
            raise ValueError("invalid channel_id")
        if not isinstance(message, dict) or "channel" in message:
            raise ValueError("invalid message")
        body = {"channel": channel_id, **message}
        try:
            async with httpx.AsyncClient(
                base_url="https://slack.com/api",
                headers={
                    "Authorization": f"Bearer {self._bot_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                transport=self._transport,
                timeout=15,
            ) as client:
                response = await client.post("/chat.postMessage", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SlackProtocolError("message_delivery_unknown") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise SlackProtocolError("invalid_json") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise SlackProtocolError("slack_api_rejected_message")
        returned_channel = payload.get("channel")
        message_ts = payload.get("ts")
        if (
            not isinstance(returned_channel, str)
            or returned_channel != channel_id
            or not isinstance(message_ts, str)
            or _MESSAGE_TS.fullmatch(message_ts) is None
        ):
            raise SlackProtocolError("message_identity_mismatch")
        return SlackMessageReference(
            channel_id=returned_channel,
            message_ts=message_ts,
        )
