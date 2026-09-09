from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from slack_correlation.app import SlackConnectorSettings, create_app
from slack_correlation.catalog import NotificationCommand
from slack_correlation.store import NotificationCapacityError, NotificationStore


def _command(event_id: str, dedupe: str) -> NotificationCommand:
    return NotificationCommand(
        event_id=event_id,
        event_code="SYS-002",
        dedupe_key=dedupe,
        occurred_at=datetime(2026, 9, 7, 12, 0, tzinfo=UTC),
        component="slack_queue",
        state="backlogged",
        count=1,
    )


def test_capacity_rejects_new_work_but_preserves_exact_replay(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    first = _command("11111111-1111-4111-8111-111111111111", "1" * 64)
    second = _command("22222222-2222-4222-8222-222222222222", "2" * 64)

    assert store.admit(
        tenant_ref="johanna",
        command=first,
        max_nonterminal=1,
    ).outcome == "admitted"
    assert store.admit(
        tenant_ref="johanna",
        command=first,
        max_nonterminal=1,
    ).outcome == "duplicate"
    with pytest.raises(NotificationCapacityError):
        store.admit(
            tenant_ref="att1",
            command=second,
            max_nonterminal=1,
        )


def test_http_capacity_failure_is_retryable_and_does_not_mark_storage_down(
    tmp_path,
) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    settings = SlackConnectorSettings(
        ingress_enabled=True,
        tenant_tokens={"johanna": "j" * 32, "att1": "a" * 32},
        storage_path=str(tmp_path / "connector.sqlite3"),
        max_nonterminal_notifications=1,
    )
    app = create_app(settings, store=store)

    with TestClient(app) as client:
        first = client.post(
            "/internal/v1/notifications",
            headers={
                "Authorization": "Bearer " + "j" * 32,
                "X-Expected-Tenant-Ref": "johanna",
            },
            json={
                "event_id": "11111111-1111-4111-8111-111111111111",
                "event_code": "SYS-002",
                "dedupe_key": "1" * 64,
                "occurred_at": "2026-09-07T12:00:00Z",
                "component": "slack_queue",
                "state": "backlogged",
                "count": 1,
            },
        )
        full = client.post(
            "/internal/v1/notifications",
            headers={
                "Authorization": "Bearer " + "a" * 32,
                "X-Expected-Tenant-Ref": "att1",
            },
            json={
                "event_id": "22222222-2222-4222-8222-222222222222",
                "event_code": "SYS-002",
                "dedupe_key": "2" * 64,
                "occurred_at": "2026-09-07T12:00:01Z",
                "component": "slack_queue",
                "state": "backlogged",
                "count": 2,
            },
        )
        ready = client.get("/ready")

    assert first.status_code == 202
    assert full.status_code == 429
    assert full.json() == {"detail": "queue_capacity_exhausted"}
    assert full.headers["retry-after"] == "30"
    assert ready.status_code == 200


def test_delivery_unknown_consumes_durable_capacity(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    store.configure_activation(mode="one_shot", generation=1)
    first = _command("11111111-1111-4111-8111-111111111111", "1" * 64)
    second = _command("22222222-2222-4222-8222-222222222222", "2" * 64)
    store.admit(tenant_ref="johanna", command=first, max_nonterminal=1)
    claim = store.claim_next(worker_id="worker-1")
    assert claim is not None
    store.mark_request_started(claim)
    store.finalize_delivery_unknown(claim, failure_code="slack_delivery_unknown")

    with pytest.raises(NotificationCapacityError):
        store.admit(tenant_ref="att1", command=second, max_nonterminal=1)
