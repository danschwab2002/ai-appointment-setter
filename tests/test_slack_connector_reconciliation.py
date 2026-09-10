from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from slack_correlation.app import SlackConnectorSettings, create_app
from slack_correlation.catalog import NotificationCommand
from slack_correlation.store import NotificationStore


def _unknown(store: NotificationStore, *, event_id: str = "11111111-1111-4111-8111-111111111111") -> None:
    command = NotificationCommand(
        event_id=event_id,
        event_code="HND-001",
        dedupe_key="1" * 64,
        occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
        subject_ref="C-11111111",
        reason_code="explicit_human_request",
        component=None,
        state="paused",
        count=None,
        deadline_at=None,
    )
    store.admit(tenant_ref="johanna", command=command)
    claim = store.claim_next(worker_id="worker-1")
    assert claim is not None
    store.mark_request_started(claim)
    store.finalize_delivery_unknown(claim, failure_code="slack_delivery_unknown")


def _settings(path: str) -> SlackConnectorSettings:
    return SlackConnectorSettings(
        storage_path=path,
        operator_bearer_token="o" * 32,
        tenant_channels={"johanna": "C0C0YEACVT2"},
    )


def test_operator_confirm_delivered_binds_fixed_channel_and_audits(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    _unknown(store)
    app = create_app(_settings(str(tmp_path / "connector.sqlite3")), store=store)

    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/operator/reconcile-delivery",
            headers={"Authorization": "Bearer " + "o" * 32},
            json={
                "decision": "confirm_delivered",
                "tenant_ref": "johanna",
                "notification_id": "11111111-1111-4111-8111-111111111111",
                "message_ts": "1788800000.000001",
                "thread_ts": None,
            },
        )

    assert response.status_code == 200
    stored = store.get(tenant_ref="johanna", notification_id="11111111-1111-4111-8111-111111111111")
    assert stored is not None
    assert stored.state == "accepted"
    assert stored.channel_id == "C0C0YEACVT2"
    assert stored.message_ts == "1788800000.000001"
    assert store.reconciliation_audit_count() == 1


def test_operator_confirm_not_delivered_safely_requeues_and_restores_budget(tmp_path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    store.configure_activation(mode="one_shot", generation=1)
    _unknown(store)
    assert store.activation_status()["consumed"] == 1
    app = create_app(_settings(str(tmp_path / "connector.sqlite3")), store=store)

    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/operator/reconcile-delivery",
            headers={"Authorization": "Bearer " + "o" * 32},
            json={
                "decision": "confirm_not_delivered",
                "tenant_ref": "johanna",
                "notification_id": "11111111-1111-4111-8111-111111111111",
            },
        )

    assert response.status_code == 200
    assert response.json()["delivery_state"] == "pending"
    assert store.activation_status()["consumed"] == 0
    assert store.reconciliation_audit_count() == 1


def test_producer_bearer_cannot_reconcile_and_payload_surface_is_closed(tmp_path) -> None:
    path = tmp_path / "connector.sqlite3"
    store = NotificationStore(path)
    store.initialize()
    _unknown(store)
    settings = SlackConnectorSettings(
        storage_path=str(path),
        operator_bearer_token="o" * 32,
        tenant_tokens={"johanna": "j" * 32, "att1": "a" * 32},
        tenant_channels={"johanna": "C0C0YEACVT2"},
    )
    app = create_app(settings, store=store)

    forbidden = {
        "decision": "confirm_delivered",
        "tenant_ref": "johanna",
        "notification_id": "11111111-1111-4111-8111-111111111111",
        "message_ts": "1788800000.000001",
        "channel": "CATTACKER",
        "text": "attacker selected text",
    }
    with TestClient(app) as client:
        ordinary = client.post(
            "/internal/v1/operator/reconcile-delivery",
            headers={"Authorization": "Bearer " + "j" * 32},
            json=forbidden,
        )
        operator = client.post(
            "/internal/v1/operator/reconcile-delivery",
            headers={"Authorization": "Bearer " + "o" * 32},
            json=forbidden,
        )

    assert ordinary.status_code == 401
    assert operator.status_code == 400
    assert store.reconciliation_audit_count() == 0
