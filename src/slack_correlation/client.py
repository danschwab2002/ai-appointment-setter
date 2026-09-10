"""Minimal strict client for Slack correlation projections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

_MESSAGE_TS = re.compile(r"^[0-9]+\.[0-9]+$")


class SlackProtocolError(RuntimeError):
    """Raised when Slack transport or response identity is not provable."""


class SlackRejectedError(SlackProtocolError):
    """Raised when Slack explicitly rejects a message without applying it."""


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
        base_url: str = "https://slack.com/api",
    ) -> None:
        if not isinstance(bot_token, str) or not bot_token:
            raise ValueError("bot_token is required")
        self._bot_token = bot_token
        self._transport = transport
        self._base_url = _validate_base_url(base_url)

    async def verify_auth(self, *, expected_team_id: str) -> None:
        """Verify the token belongs to the configured Slack workspace."""

        if not isinstance(expected_team_id, str) or not expected_team_id:
            raise ValueError("expected_team_id is required")
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
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
                base_url=self._base_url,
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
        if not isinstance(payload, dict):
            raise SlackProtocolError("invalid_response_shape")
        if payload.get("ok") is not True:
            raise SlackRejectedError("slack_api_rejected_message")
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

    async def open_view(
        self, *, trigger_id: str, view: dict[str, Any], expected_team_id: str
    ) -> None:
        if not isinstance(trigger_id, str) or not trigger_id or not isinstance(view, dict):
            raise ValueError("invalid view request")
        callback_id = view.get("callback_id")
        if not isinstance(callback_id, str) or not callback_id:
            raise ValueError("invalid view")
        payload = await self._post_api("/views.open", {"trigger_id": trigger_id, "view": view})
        returned = payload.get("view")
        if (
            not isinstance(returned, dict)
            or returned.get("team_id") != expected_team_id
            or returned.get("callback_id") != callback_id
            or not isinstance(returned.get("id"), str)
            or not returned["id"]
        ):
            raise SlackProtocolError("view_identity_mismatch")

    async def update_message(
        self, *, channel_id: str, message_ts: str, message: dict[str, Any]
    ) -> SlackMessageReference:
        if not isinstance(channel_id, str) or not channel_id or not isinstance(message_ts, str) or _MESSAGE_TS.fullmatch(message_ts) is None:
            raise ValueError("invalid message identity")
        if not isinstance(message, dict) or "channel" in message or "ts" in message:
            raise ValueError("invalid message")
        payload = await self._post_api(
            "/chat.update", {"channel": channel_id, "ts": message_ts, **message}
        )
        if payload.get("channel") != channel_id or payload.get("ts") != message_ts:
            raise SlackProtocolError("message_identity_mismatch")
        return SlackMessageReference(channel_id=channel_id, message_ts=message_ts)

    async def update_view(
        self,
        *,
        view_id: str,
        view_hash: str | None,
        view: dict[str, Any],
        expected_team_id: str,
    ) -> None:
        if not isinstance(view_id, str) or not view_id or not isinstance(view, dict):
            raise ValueError("invalid view request")
        callback_id = view.get("callback_id")
        if not isinstance(callback_id, str) or not callback_id:
            raise ValueError("invalid view")
        body: dict[str, Any] = {"view_id": view_id, "view": view}
        if view_hash is not None:
            body["hash"] = view_hash
        payload = await self._post_api("/views.update", body)
        returned = payload.get("view")
        if (
            not isinstance(returned, dict)
            or returned.get("id") != view_id
            or returned.get("team_id") != expected_team_id
            or returned.get("callback_id") != callback_id
        ):
            raise SlackProtocolError("view_identity_mismatch")

    async def _post_api(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._bot_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                transport=self._transport,
                timeout=15,
            ) as client:
                response = await client.post(path, json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SlackProtocolError("slack_request_unknown") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise SlackProtocolError("invalid_json") from exc
        if not isinstance(payload, dict):
            raise SlackProtocolError("invalid_response_shape")
        if payload.get("ok") is not True:
            raise SlackRejectedError("slack_api_rejected")
        return payload


def _validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise ValueError("invalid_slack_base_url")
    parsed = urlsplit(base_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid_slack_base_url")
    production = (
        parsed.scheme == "https"
        and parsed.hostname == "slack.com"
        and parsed.port is None
        and parsed.path.rstrip("/") == "/api"
    )
    loopback_test = (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.port is not None
        and parsed.path.rstrip("/") == "/api"
    )
    if not production and not loopback_test:
        raise ValueError("invalid_slack_base_url")
    return base_url.rstrip("/")
