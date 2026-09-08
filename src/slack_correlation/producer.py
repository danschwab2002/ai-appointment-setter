"""Strict bridge-side client for the central Slack connector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from slack_correlation.catalog import NotificationCommand

_INTERNAL_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_TERMINAL_REJECTIONS = {400, 401, 404, 409, 413}


class ConnectorAdmissionUnknown(RuntimeError):
    """The bridge cannot prove whether the durable admission committed."""


class ConnectorSemanticConflict(RuntimeError):
    """An idempotency identifier was reused with different semantics."""


class ConnectorRejected(RuntimeError):
    """The connector explicitly rejected the command before admission."""


@dataclass(frozen=True)
class AdmissionReceipt:
    status: str
    notification_id: str
    delivery_state: str


class SlackConnectorProducer:
    """Submit typed commands; exact retries are safe after ambiguous admission."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized_url = _validate_base_url(base_url)
        if _INTERNAL_TOKEN.fullmatch(bearer_token) is None:
            raise ValueError("invalid_connector_bearer_token")
        self._token = bearer_token
        self._client = httpx.AsyncClient(
            base_url=normalized_url,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def admit(self, command: NotificationCommand) -> AdmissionReceipt:
        try:
            response = await self._client.post(
                "/internal/v1/notifications",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                json=_serialize_command(command),
            )
        except httpx.HTTPError as exc:
            raise ConnectorAdmissionUnknown("connector_admission_unknown") from exc

        if response.status_code == 409:
            raise ConnectorSemanticConflict("connector_semantic_conflict")
        if response.status_code in _TERMINAL_REJECTIONS:
            raise ConnectorRejected("connector_admission_rejected")
        if response.status_code not in {200, 202}:
            raise ConnectorAdmissionUnknown("connector_admission_unknown")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ConnectorAdmissionUnknown("connector_admission_unknown") from exc
        if not isinstance(payload, dict):
            raise ConnectorAdmissionUnknown("connector_admission_unknown")
        status = payload.get("status")
        notification_id = payload.get("notification_id")
        delivery_state = payload.get("delivery_state")
        if (
            not isinstance(status, str)
            or status not in {"admitted", "duplicate"}
            or not isinstance(notification_id, str)
            or notification_id != command.event_id
            or not isinstance(delivery_state, str)
        ):
            raise ConnectorAdmissionUnknown("connector_admission_unknown")
        return AdmissionReceipt(
            status=status,
            notification_id=notification_id,
            delivery_state=delivery_state,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if (
        parsed.scheme not in {"http", "https"}
        or (parsed.scheme == "http" and not loopback)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("invalid_connector_base_url")
    return value.rstrip("/")


def _serialize_command(command: NotificationCommand) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_id": command.event_id,
        "event_code": command.event_code,
        "dedupe_key": command.dedupe_key,
        "occurred_at": _utc_text(command.occurred_at),
    }
    for field_name in ("subject_ref", "reason_code", "component", "state", "count"):
        value = getattr(command, field_name)
        if value is not None:
            payload[field_name] = value
    if command.deadline_at is not None:
        payload["deadline_at"] = _utc_text(command.deadline_at)
    return payload


def _utc_text(value: Any) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
