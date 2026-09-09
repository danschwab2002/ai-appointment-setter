from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from slack_correlation.catalog import NotificationCommand
from slack_correlation.producer import (
    ConnectorAdmissionUnknown,
    ConnectorSemanticConflict,
    SlackConnectorProducer,
)


def _command() -> NotificationCommand:
    from datetime import UTC, datetime

    return NotificationCommand(
        event_id="11111111-1111-4111-8111-111111111111",
        event_code="HND-001",
        dedupe_key="1" * 64,
        occurred_at=datetime(2026, 9, 7, 22, 0, tzinfo=UTC),
        subject_ref="C-11111111",
        reason_code="explicit_human_request",
    )


def test_producer_posts_closed_command_with_bearer_and_no_routing_fields() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["expected_tenant"] = request.headers.get("x-expected-tenant-ref")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            202,
            json={
                "status": "admitted",
                "tenant_ref": "johanna",
                "notification_id": "11111111-1111-4111-8111-111111111111",
                "delivery_state": "pending",
            },
        )

    producer = SlackConnectorProducer(
        base_url="https://connector.example.invalid",
        bearer_token="j" * 32,
        expected_tenant_ref="johanna",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(producer.admit(_command()))
    asyncio.run(producer.aclose())

    assert result.status == "admitted"
    assert captured["authorization"] == "Bearer " + "j" * 32
    assert captured["expected_tenant"] == "johanna"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert "tenant_ref" not in payload
    assert "channel" not in payload
    assert "text" not in payload
    assert "blocks" not in payload


def test_producer_reports_conflict_and_ambiguous_transport_separately() -> None:
    conflict = SlackConnectorProducer(
        base_url="https://connector.example.invalid",
        bearer_token="j" * 32,
        expected_tenant_ref="johanna",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(409, json={"detail": "semantic_conflict"})
        ),
    )
    with pytest.raises(ConnectorSemanticConflict):
        asyncio.run(conflict.admit(_command()))
    asyncio.run(conflict.aclose())

    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout")

    uncertain = SlackConnectorProducer(
        base_url="https://connector.example.invalid",
        bearer_token="j" * 32,
        expected_tenant_ref="johanna",
        transport=httpx.MockTransport(timeout),
    )
    with pytest.raises(ConnectorAdmissionUnknown):
        asyncio.run(uncertain.admit(_command()))
    asyncio.run(uncertain.aclose())


def test_producer_never_sends_bearer_to_plaintext_or_userinfo_url() -> None:
    for value in (
        "http://connector.example.invalid",
        "https://user@connector.example.invalid",
        "https://connector.example.invalid/path?redirect=evil",
    ):
        with pytest.raises(ValueError, match="invalid_connector_base_url"):
            SlackConnectorProducer(
                base_url=value,
                bearer_token="j" * 32,
                expected_tenant_ref="johanna",
            )

    loopback = SlackConnectorProducer(
        base_url="http://127.0.0.1:8765",
        bearer_token="j" * 32,
        expected_tenant_ref="johanna",
    )
    asyncio.run(loopback.aclose())


def test_producer_normalizes_aware_timestamps_to_utc_z() -> None:
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    captured: dict[str, object] = {}
    command = replace(
        _command(),
        occurred_at=datetime(
            2026,
            9,
            7,
            17,
            0,
            tzinfo=timezone(-timedelta(hours=5)),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            202,
            json={
                "status": "admitted",
                "tenant_ref": "johanna",
                "notification_id": command.event_id,
                "delivery_state": "pending",
            },
        )

    producer = SlackConnectorProducer(
        base_url="https://connector.example.invalid",
        bearer_token="j" * 32,
        expected_tenant_ref="johanna",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(producer.admit(command))
    asyncio.run(producer.aclose())

    assert captured["occurred_at"] == "2026-09-07T22:00:00Z"


def test_producer_rejects_a_receipt_attested_for_another_tenant() -> None:
    producer = SlackConnectorProducer(
        base_url="https://connector.example.invalid",
        bearer_token="j" * 32,
        expected_tenant_ref="johanna",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                202,
                json={
                    "status": "admitted",
                    "tenant_ref": "att1",
                    "notification_id": _command().event_id,
                    "delivery_state": "pending",
                },
            )
        ),
    )

    with pytest.raises(ConnectorSemanticConflict, match="tenant_mismatch"):
        asyncio.run(producer.admit(_command()))
    asyncio.run(producer.aclose())
