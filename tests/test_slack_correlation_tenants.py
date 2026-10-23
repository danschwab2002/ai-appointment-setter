"""Los tenants del conector salen de la configuracion, no del codigo.

Hasta el 2026-10-17 el conector aceptaba solo `johanna` y `att1`
(`_ALLOWED_TENANTS` en `app.py` y cuatro listas fijas en `store.py`): sumar una
instancia exigia un cambio de codigo y una release. Ahora un tenant existe si
tiene canal en `SLACK_TENANT_CHANNELS_JSON` (y token, con el ingreso prendido).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from slack_correlation.app import (
    SlackConnectorSettings,
    _resolved_tenant_labels,
    create_app,
)
from slack_correlation.catalog import NotificationCommand
from slack_correlation.client import SlackMessageReference
from slack_correlation.store import NotificationStore
from slack_correlation.worker import NotificationWorker


JOHANNA_CHANNEL = "C0C0YEACVT2"
ATT1_CHANNEL = "C0ATT1TEST01"
NUEVA_CHANNEL = "C0NUEVATEST1"
NUEVA = "aliada-nueva"


def _command(*, event_id: str, dedupe_key: str) -> NotificationCommand:
    return NotificationCommand(
        event_id=event_id,
        event_code="HND-001",
        dedupe_key=dedupe_key,
        occurred_at=datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
        subject_ref=f"C-{event_id.upper()}",
        reason_code="explicit_human_request",
        state="paused",
    )


class _AcceptedSlack:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []

    async def verify_auth(self, *, expected_team_id: str) -> None:
        assert expected_team_id == "T12345678"

    async def post_message(
        self, *, channel_id: str, message: dict[str, object]
    ) -> SlackMessageReference:
        self.messages.append((channel_id, message))
        return SlackMessageReference(
            channel_id=channel_id,
            message_ts=f"1789000000.{len(self.messages):06d}",
        )


def _three_tenants(tmp_path: Path, **overrides: object) -> SlackConnectorSettings:
    values: dict[str, object] = {
        "ingress_enabled": True,
        "team_id": "T12345678",
        "storage_path": str(tmp_path / "connector.sqlite3"),
        "tenant_tokens": {"johanna": "j" * 32, "att1": "a" * 32, NUEVA: "n" * 32},
        "tenant_channels": {
            "johanna": JOHANNA_CHANNEL,
            "att1": ATT1_CHANNEL,
            NUEVA: NUEVA_CHANNEL,
        },
    }
    values.update(overrides)
    return SlackConnectorSettings(**values)  # type: ignore[arg-type]


def test_a_new_tenant_with_channel_and_token_is_admitted(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    app = create_app(_three_tenants(tmp_path), store=store)
    event_id = "33333333-3333-4333-8333-333333333333"
    payload = {
        "event_id": event_id,
        "event_code": "HND-001",
        "dedupe_key": "3" * 64,
        "occurred_at": "2026-10-17T12:00:00Z",
        "subject_ref": f"C-{event_id.upper()}",
        "reason_code": "explicit_human_request",
        "state": "paused",
    }

    with TestClient(app) as client:
        assert client.get("/ready").status_code == 200
        response = client.post(
            "/internal/v1/notifications",
            headers={
                "Authorization": f"Bearer {'n' * 32}",
                "X-Expected-Tenant-Ref": NUEVA,
            },
            json=payload,
        )

    assert response.status_code == 202
    stored = store.get(tenant_ref=NUEVA, notification_id=event_id)
    assert stored is not None
    assert store.get(tenant_ref="att1", notification_id=event_id) is None


def test_a_tenant_without_route_is_still_unauthorized(tmp_path: Path) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    app = create_app(
        _three_tenants(
            tmp_path,
            tenant_tokens={"johanna": "j" * 32},
            tenant_channels={"johanna": JOHANNA_CHANNEL},
        ),
        store=store,
    )

    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/notifications",
            headers={
                "Authorization": f"Bearer {'n' * 32}",
                "X-Expected-Tenant-Ref": NUEVA,
            },
            json={},
        )

    assert response.status_code == 401
    assert store.count() == 0


@pytest.mark.parametrize(
    "tenant",
    ["Aliada", "aliada nueva", "-aliada", "a" * 41, "", "aliada/../x"],
)
def test_a_malformed_tenant_ref_is_rejected(tenant: str) -> None:
    with pytest.raises(ValueError, match="invalid_tenant_channel_configuration"):
        create_app(SlackConnectorSettings(tenant_channels={tenant: NUEVA_CHANNEL}))


def test_a_new_tenant_alone_can_use_any_channel() -> None:
    # Un conector por instancia: el canal es de la configuracion, no del codigo.
    create_app(SlackConnectorSettings(tenant_channels={NUEVA: JOHANNA_CHANNEL}))


def test_johanna_and_a_new_tenant_still_cannot_share_a_channel() -> None:
    with pytest.raises(ValueError, match="tenant_channel_collision"):
        create_app(
            SlackConnectorSettings(
                tenant_channels={"johanna": JOHANNA_CHANNEL, NUEVA: JOHANNA_CHANNEL}
            )
        )


def test_two_new_tenants_cannot_share_a_channel() -> None:
    with pytest.raises(ValueError, match="tenant_channel_collision"):
        create_app(
            SlackConnectorSettings(
                tenant_channels={NUEVA: NUEVA_CHANNEL, "otra": NUEVA_CHANNEL}
            )
        )


def test_labels_default_to_the_legacy_ones_and_the_ref() -> None:
    settings = SlackConnectorSettings(
        tenant_channels={
            "johanna": JOHANNA_CHANNEL,
            "att1": ATT1_CHANNEL,
            NUEVA: NUEVA_CHANNEL,
        }
    )

    assert _resolved_tenant_labels(settings) == {
        "johanna": "Johanna",
        "att1": "ATT1",
        NUEVA: "ALIADA-NUEVA",
    }


def test_a_configured_label_wins() -> None:
    settings = SlackConnectorSettings(
        tenant_channels={"att1": ATT1_CHANNEL, NUEVA: NUEVA_CHANNEL},
        tenant_labels={NUEVA: "Aliada Nueva"},
    )
    create_app(settings)

    assert _resolved_tenant_labels(settings) == {"att1": "ATT1", NUEVA: "Aliada Nueva"}


@pytest.mark.parametrize(
    "labels",
    [
        {"otra": "Otra"},
        {NUEVA: ""},
        {NUEVA: "   "},
        {NUEVA: " Aliada"},
        {NUEVA: "x" * 41},
        {NUEVA: "Aliada\nNueva"},
    ],
)
def test_an_invalid_label_is_rejected(labels: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="invalid_tenant_label_configuration"):
        create_app(
            SlackConnectorSettings(
                tenant_channels={NUEVA: NUEVA_CHANNEL},
                tenant_labels=labels,
            )
        )


def test_labels_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SLACK_TENANT_CHANNELS_JSON", json.dumps({NUEVA: NUEVA_CHANNEL})
    )
    monkeypatch.setenv("SLACK_TENANT_LABELS_JSON", json.dumps({NUEVA: "Aliada Nueva"}))

    settings = SlackConnectorSettings.from_env()

    assert settings.tenant_labels == {NUEVA: "Aliada Nueva"}


def test_labels_environment_must_be_an_object_of_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_TENANT_LABELS_JSON", json.dumps({NUEVA: 1}))

    with pytest.raises(ValueError, match="invalid_json:SLACK_TENANT_LABELS_JSON"):
        SlackConnectorSettings.from_env()


def test_the_worker_routes_a_new_tenant_to_its_channel_with_its_label(
    tmp_path: Path,
) -> None:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    store.configure_activation(mode="one_shot", generation=1)
    store.admit(
        tenant_ref=NUEVA,
        channel_id=NUEVA_CHANNEL,
        command=_command(
            event_id="44444444-4444-4444-8444-444444444444",
            dedupe_key="4" * 64,
        ),
    )
    settings = SlackConnectorSettings(
        tenant_channels={"johanna": JOHANNA_CHANNEL, NUEVA: NUEVA_CHANNEL},
        tenant_labels={NUEVA: "Aliada Nueva"},
    )
    slack = _AcceptedSlack()
    worker = NotificationWorker(
        store=store,
        slack_client=slack,
        tenant_channels=dict(settings.tenant_channels),
        tenant_labels=_resolved_tenant_labels(settings),
        worker_id="worker-1",
    )

    assert asyncio.run(worker.run_once()) is True
    channel, message = slack.messages[0]
    assert channel == NUEVA_CHANNEL
    assert "Aliada Nueva" in json.dumps(message, ensure_ascii=False)


def test_a_backup_with_a_new_tenant_round_trips(tmp_path: Path) -> None:
    source = NotificationStore(tmp_path / "source.sqlite3")
    source.initialize()
    event_id = "55555555-5555-4555-8555-555555555555"
    source.admit(
        tenant_ref=NUEVA,
        channel_id=NUEVA_CHANNEL,
        command=_command(event_id=event_id, dedupe_key="5" * 64),
    )
    backup = tmp_path / "backup.sqlite3"
    source.backup_to(backup)
    destination = tmp_path / "restored.sqlite3"

    NotificationStore.restore_from(backup, destination)

    restored = NotificationStore(destination)
    restored.initialize()
    assert restored.get(tenant_ref=NUEVA, notification_id=event_id) is not None
