"""Lifecycle-safe bridge ownership of the central Slack producer."""

from __future__ import annotations

from typing import Protocol

import httpx

from bridge.slack_notifications import NotificationProducer
from slack_correlation.producer import SlackConnectorProducer


class ClosableNotificationProducer(NotificationProducer, Protocol):
    async def aclose(self) -> None: ...


class SlackBridgeRuntime:
    """Own the producer shared by one bridge projection worker."""

    def __init__(self, *, producer: ClosableNotificationProducer) -> None:
        self.producer = producer

    async def aclose(self) -> None:
        await self.producer.aclose()


def create_slack_bridge_runtime(
    *,
    base_url: str | None,
    bearer_token: str | None,
    expected_tenant_ref: str | None = None,
    timeout_seconds: float = 5.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SlackBridgeRuntime | None:
    """Build the producer only when its two required settings are present."""
    if base_url is None and bearer_token is None:
        return None
    if base_url is None or bearer_token is None:
        raise ValueError("slack_connector_configuration_incomplete")
    return SlackBridgeRuntime(
        producer=SlackConnectorProducer(
            base_url=base_url,
            bearer_token=bearer_token,
            expected_tenant_ref=expected_tenant_ref,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
    )
