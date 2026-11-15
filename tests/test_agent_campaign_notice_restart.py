"""El aviso de una campana que no se sirve, despues de un reinicio del bridge (E-572).

Log real de Maribel, arranque del bridge del 2026-11-14 17:49:40Z (leido por la
coordinadora del programa etapa-1, solo lectura, con `docker logs`):

    agent_campaign_not_served key=prueba-interna-2026-11-09 version=1 reason=chequeo_no_calza
    agent_campaign_notice_failed error=ConnectorSemanticConflict

y en el conector de Slack, el mismo segundo:

    POST /internal/v1/notifications HTTP/1.1" 409 Conflict

La causa: ``approval_notice`` arma ``event_id`` y ``dedupe_key`` con la campana y el
motivo (estables), pero ``occurred_at`` es la hora del arranque. El conector guarda el
sha del cuerpo entero (``NotificationStore.admit``), asi que el mismo aviso con otra
hora es ``semantic_conflict`` -> 409 -> ``ConnectorSemanticConflict``. El recuerdo de
"ya avisado" (``_announced``) vive en memoria y se pierde en cada reinicio.

Aca el conector es el de verdad (``NotificationStore`` en SQLite) detras del
``SlackConnectorProducer`` real; el transporte traduce el resultado del store a los
mismos codigos que ``slack_correlation.app.admit_notification`` (202 / 200 / 409).
El aviso es el mismo que arma ``bridge.app._announce_campaign_not_served``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from test_agent_campaigns import _context, _Db, _draft, _live, _row

from bridge import flow_approval
from slack_correlation.catalog import NotificationCommand
from slack_correlation.producer import ConnectorSemanticConflict, SlackConnectorProducer
from slack_correlation.app import _parse_command
from slack_correlation.store import NotificationStore

KEY = "prueba-interna-2026-11-09"
TENANT = "maribel"
# La hora del primer aviso no se conoce (el conector la guarda); cualquiera distinta da lo mismo.
FIRST_BOOT = datetime(2026, 11, 12, 15, 3, 11, tzinfo=UTC)
SECOND_BOOT = datetime(2026, 11, 14, 17, 49, 40, tzinfo=UTC)


def _notice(reason: str, at: datetime) -> NotificationCommand:
    # Igual que bridge.app._announce_campaign_not_served.
    return flow_approval.approval_notice(
        event_code=flow_approval.NOTICE_PAUSED,
        what=f"campana_no_servida_{reason}"[:60],
        flow_key=KEY,
        user_id="sistema",
        key=f"{KEY}:{reason}",
        occurred_at=at,
    )


def _connector(store: NotificationStore, *, down: bool = False, status: int | None = None,
               tenant: str = TENANT) -> SlackConnectorProducer:
    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            raise httpx.ReadTimeout("el conector no contesta")
        if status is not None:
            return httpx.Response(status, json={"detail": "tenant_attestation_failed"})
        command = _parse_command(request.content)  # el mismo parser del conector
        result = store.admit(tenant_ref=TENANT, command=command)
        code = {"admitted": 202, "duplicate": 200, "semantic_conflict": 409}[result.outcome]
        return httpx.Response(code, json={"status": result.outcome, "tenant_ref": tenant,
                                          "notification_id": result.notification_id,
                                          "delivery_state": result.state})

    return SlackConnectorProducer(base_url="https://connector.example.invalid", bearer_token="m" * 32,
                                  expected_tenant_ref=TENANT, transport=httpx.MockTransport(handler))


def _boot(tmp_path: Path, producer: SlackConnectorProducer, at: datetime, *, check_sha: str = "b" * 64) -> None:
    """Un arranque del bridge: un catalogo vivo nuevo (``_announced`` vacio) y su primera lectura."""
    boot_dir = tmp_path / f"arranque-{at:%Y%m%dT%H%M%S}"
    boot_dir.mkdir()
    context = _context(boot_dir)

    async def notify(key: str, reason: str) -> None:
        assert key == KEY
        await producer.admit(_notice(reason, at))

    db = _Db([_row(context, draft=_draft(clave=KEY), check_sha=check_sha)])
    assert asyncio.run(_live(context, db, notify=notify).refresh()) is True


@pytest.fixture
def store(tmp_path: Path) -> NotificationStore:
    store = NotificationStore(tmp_path / "connector.sqlite3")
    store.initialize()
    return store


def test_the_same_notice_after_a_restart_is_a_semantic_conflict_for_the_connector(store: NotificationStore) -> None:
    # La causa, sin el bridge: mismo id, otra hora.
    first, second = _notice("chequeo_no_calza", FIRST_BOOT), _notice("chequeo_no_calza", SECOND_BOOT)
    assert (first.event_id, first.dedupe_key) == (second.event_id, second.dedupe_key)
    assert store.admit(tenant_ref=TENANT, command=first).outcome == "admitted"
    assert store.admit(tenant_ref=TENANT, command=second).outcome == "semantic_conflict"
    producer = _connector(store)
    with pytest.raises(ConnectorSemanticConflict):
        asyncio.run(producer.admit(second))


def test_after_a_restart_the_409_counts_as_already_announced_and_does_not_log_an_error(
        tmp_path: Path, store: NotificationStore, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bridge.agent_campaigns")
    producer = _connector(store)
    _boot(tmp_path, producer, FIRST_BOOT)
    assert store.count() == 1
    caplog.clear()

    _boot(tmp_path, producer, SECOND_BOOT)
    text = caplog.text
    assert f"agent_campaign_not_served key={KEY} version=1 reason=chequeo_no_calza" in text
    assert "agent_campaign_notice_failed" not in text
    already = [r.getMessage() for r in caplog.records if "agent_campaign_notice_already_admitted" in r.getMessage()]
    assert already == [f"agent_campaign_notice_already_admitted key={KEY} reason=chequeo_no_calza"]
    # La verificacion de las subidas cuenta toda linea con «error» (comun/errores-log.sh).
    assert all("error" not in line.lower() for line in already)
    assert [r.levelno for r in caplog.records if "already_admitted" in r.getMessage()] == [logging.INFO]
    # El conector no publica dos veces.
    assert store.count() == 1


def test_a_connector_that_does_not_answer_is_still_a_failed_notice(
        tmp_path: Path, store: NotificationStore, caplog: pytest.LogCaptureFixture) -> None:
    # Lo que no es un 409 sigue como hoy: ConnectorAdmissionUnknown es un aviso que no se sabe si salio.
    caplog.set_level(logging.WARNING, logger="bridge.agent_campaigns")
    _boot(tmp_path, _connector(store, down=True), SECOND_BOOT)
    assert "agent_campaign_notice_failed error=ConnectorAdmissionUnknown" in caplog.text
    assert store.count() == 0


def test_the_first_boot_still_announces_once(tmp_path: Path, store: NotificationStore,
                                             caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bridge.agent_campaigns")
    _boot(tmp_path, _connector(store), FIRST_BOOT)
    assert store.count() == 1
    assert "agent_campaign_notice_already_admitted" not in caplog.text
    assert "agent_campaign_notice_failed" not in caplog.text


def test_a_rejected_notice_is_still_a_failed_notice(tmp_path: Path, store: NotificationStore,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bridge.agent_campaigns")
    _boot(tmp_path, _connector(store, status=403), SECOND_BOOT)
    assert "agent_campaign_notice_failed error=ConnectorRejected" in caplog.text
    assert "agent_campaign_notice_already_admitted" not in caplog.text


def test_a_tenant_that_does_not_match_is_not_taken_as_already_announced(
        tmp_path: Path, store: NotificationStore, caplog: pytest.LogCaptureFixture) -> None:
    # El productor levanta ConnectorSemanticConflict("connector_tenant_mismatch") con un 202
    # de otro tenant: eso no es «ya avisado», sigue como falla.
    caplog.set_level(logging.INFO, logger="bridge.agent_campaigns")
    _boot(tmp_path, _connector(store, tenant="att1"), SECOND_BOOT)
    assert "agent_campaign_notice_failed error=ConnectorSemanticConflict" in caplog.text
    assert "agent_campaign_notice_already_admitted" not in caplog.text


def test_another_reason_after_a_restart_is_a_new_notice(tmp_path: Path, store: NotificationStore,
                                                        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bridge.agent_campaigns")
    producer = _connector(store)
    _boot(tmp_path, producer, FIRST_BOOT)
    caplog.clear()
    # Mismo draft, pero el contenido no se puede servir por otro motivo: otro id, aviso nuevo.
    first = _notice("chequeo_no_calza", FIRST_BOOT)
    other = _notice("contenido_invalido", SECOND_BOOT)
    assert first.event_id != other.event_id
    asyncio.run(producer.admit(other))
    assert store.count() == 2
