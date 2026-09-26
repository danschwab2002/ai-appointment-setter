"""Aviso a Slack de cada derivacion a humano (HND-001).

Dato real: tests/fixtures/supabase_human_handoff_requests_20260925.json, las 12
filas mas recientes de public.human_handoff_requests en el Supabase productivo
de Johanna, capturadas el 2026-09-25 (sin datos personales: id del pedido,
numero de conversacion de Chatwoot, motivos y fechas). Son las derivaciones que
existieron sin avisar a nadie. La forma de la fila que devuelve la RPC de claim
es la de esas columnas mas claim_token y lease_generation; el token y la
generacion de los tests son sinteticos porque la RPC se crea con esta misma
entrega (se reemplazan por una captura real cuando este aplicada).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import UUID, uuid5

from fastapi.testclient import TestClient
import httpx
import pytest

from bridge.app import Settings, create_app
from bridge.commercial_ally import JOHANNA_COMMERCIAL_ALLY
from bridge.slack_handoff_projection import (
    SlackHandoffNotificationClaim,
    SlackHandoffProjectionWorker,
)
from bridge.slack_notifications import SlackOperationalNotifier
from bridge.slack_runtime import SlackBridgeRuntime
from bridge.supabase import SupabaseClient, SupabaseError
from slack_correlation.catalog import NotificationCommand, render_message
from slack_correlation.producer import (
    AdmissionReceipt,
    ConnectorAdmissionUnknown,
    ConnectorRejected,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "supabase_human_handoff_requests_20260925.json"
)
CLAIM_TOKEN = "22222222-2222-4222-8222-222222222222"
NOTIFICATION_NAMESPACE = UUID("31f8cf87-488b-4e26-a395-d13270200459")


def _captured_rows() -> list[dict[str, object]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]


def _notify_kwargs(row: dict[str, object]) -> dict[str, object]:
    return {
        "handoff_request_id": row["handoff_request_id"],
        "external_conversation_id": row["external_conversation_id"],
        "primary_reason_code": row["primary_reason_code"],
        "detail_reason_code": row["detail_reason_code"],
        "occurred_at": datetime.fromisoformat(str(row["occurred_at"])),
    }


def _claim(
    row: dict[str, object], *, lease_generation: int = 1
) -> SlackHandoffNotificationClaim:
    return SlackHandoffNotificationClaim(
        handoff_request_id=str(row["handoff_request_id"]),
        external_conversation_id=int(row["external_conversation_id"]),  # type: ignore[arg-type]
        primary_reason_code=str(row["primary_reason_code"]),
        detail_reason_code=(
            str(row["detail_reason_code"])
            if row["detail_reason_code"] is not None
            else None
        ),
        occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
        claim_token=CLAIM_TOKEN,
        lease_generation=lease_generation,
    )


class _Producer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.commands: list[NotificationCommand] = []
        self.closed = False

    async def admit(self, command: NotificationCommand) -> AdmissionReceipt:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return AdmissionReceipt("admitted", command.event_id, "pending")

    async def aclose(self) -> None:
        self.closed = True


class _Store:
    def __init__(self, claims: list[SlackHandoffNotificationClaim]) -> None:
        self.claims = claims
        self.claim_calls: list[dict[str, object]] = []
        self.complete_calls: list[dict[str, object]] = []
        self.release_calls: list[dict[str, object]] = []

    async def claim_slack_handoff_notifications(
        self, **kwargs: object
    ) -> list[SlackHandoffNotificationClaim]:
        self.claim_calls.append(kwargs)
        claims, self.claims = self.claims, []
        return claims

    async def complete_slack_handoff_notification(self, **kwargs: object) -> None:
        self.complete_calls.append(kwargs)

    async def release_slack_handoff_notification(self, **kwargs: object) -> None:
        self.release_calls.append(kwargs)


def _worker(store: _Store, producer: _Producer) -> SlackHandoffProjectionWorker:
    return SlackHandoffProjectionWorker(
        store=store,
        producer=producer,
        worker_id="johanna-slack-correlation-worker-1:handoff",
    )


# --- el notificador: de la fila real a la tarjeta HND-001 ---


def test_every_captured_handoff_becomes_a_valid_hnd_001_card() -> None:
    rows = _captured_rows()
    assert len(rows) == 12
    producer = _Producer()
    notifier = SlackOperationalNotifier(producer=producer)

    for row in rows:
        command = asyncio.run(notifier.notify_new_handoff(**_notify_kwargs(row)))

        assert command.event_code == "HND-001"
        assert command.subject_ref == f"C-{str(row['handoff_request_id']).upper()}"
        assert command.component == (
            f"chatwoot.conversation.{row['external_conversation_id']}"
        )
        assert command.reason_code == (
            row["detail_reason_code"] or row["primary_reason_code"]
        )
        assert command.state == "pending"
        assert command.count is None
        assert command.recommendation is None
        assert command.event_id == str(
            uuid5(NOTIFICATION_NAMESPACE, f"handoff:{row['handoff_request_id']}")
        )
        # El conector la renderiza con su plantilla cerrada, y la tarjeta dice
        # que conversacion abrir en Chatwoot.
        message = render_message(command, tenant_label="Johanna")
        assert message["text"].startswith("[p2] Nueva derivaci")
        fields = message["blocks"][1]["fields"]
        assert any(
            f"chatwoot.conversation.{row['external_conversation_id']}" in field["text"]
            for field in fields
        )

    assert len(producer.commands) == len(rows)
    # Doce derivaciones son doce tarjetas distintas, aun cuando dos son de la
    # misma conversacion (la 173 y la 177 se derivaron dos veces cada una).
    assert len({command.event_id for command in producer.commands}) == len(rows)
    assert len({command.dedupe_key for command in producer.commands}) == len(rows)


def test_the_same_handoff_always_produces_the_same_card() -> None:
    row = _captured_rows()[0]
    producer = _Producer()
    notifier = SlackOperationalNotifier(producer=producer)

    first = asyncio.run(notifier.notify_new_handoff(**_notify_kwargs(row)))
    second = asyncio.run(notifier.notify_new_handoff(**_notify_kwargs(row)))

    assert first == second


def test_a_detail_reason_outside_the_catalog_alphabet_falls_back_to_the_primary() -> None:
    row = _captured_rows()[0]
    producer = _Producer()
    notifier = SlackOperationalNotifier(producer=producer)
    kwargs = _notify_kwargs(row)
    # Cabe en el CHECK de la base (100 chars) pero no en el catalogo (80).
    kwargs["detail_reason_code"] = "x" * 90

    command = asyncio.run(notifier.notify_new_handoff(**kwargs))

    assert command.reason_code == row["primary_reason_code"]
    assert producer.commands == [command]


@pytest.mark.parametrize(
    "override",
    [
        {"handoff_request_id": "not-a-uuid"},
        {"handoff_request_id": "19EC5E48-B5D8-45B7-ADF5-E9B07D0BA851"},
        {"external_conversation_id": 0},
        {"external_conversation_id": True},
        {"external_conversation_id": "177"},
        {"primary_reason_code": "Commercial Exception"},
    ],
)
def test_invalid_handoff_data_never_reaches_the_connector(
    override: dict[str, object],
) -> None:
    row = _captured_rows()[0]
    producer = _Producer()
    notifier = SlackOperationalNotifier(producer=producer)
    kwargs = _notify_kwargs(row)
    kwargs.update(override)

    with pytest.raises(ValueError):
        asyncio.run(notifier.notify_new_handoff(**kwargs))

    assert producer.commands == []


# --- el worker: claim, admision y cierre ---


def test_worker_admits_each_claim_and_completes_it_with_the_notification_id() -> None:
    rows = _captured_rows()[:3]
    store = _Store([_claim(row) for row in rows])
    producer = _Producer()
    worker = _worker(store, producer)

    assert asyncio.run(worker.run_once()) == 3

    assert store.claim_calls == [
        {
            "worker_id": "johanna-slack-correlation-worker-1:handoff",
            "limit": 1,
            "lease_seconds": 60,
        }
    ]
    assert [call["handoff_request_id"] for call in store.complete_calls] == [
        row["handoff_request_id"] for row in rows
    ]
    assert [call["notification_id"] for call in store.complete_calls] == [
        command.event_id for command in producer.commands
    ]
    assert all(
        call["claim_token"] == CLAIM_TOKEN and call["lease_generation"] == 1
        for call in store.complete_calls
    )
    assert store.release_calls == []


def test_worker_releases_a_transient_failure_as_retryable() -> None:
    rows = _captured_rows()[:2]
    store = _Store([_claim(row) for row in rows])
    producer = _Producer(
        error=ConnectorAdmissionUnknown("connector_admission_unknown")
    )
    worker = _worker(store, producer)

    assert asyncio.run(worker.run_once()) == 0

    assert [call["failure_code"] for call in store.release_calls] == [
        "connector_admission_unknown",
        "connector_admission_unknown",
    ]
    assert store.complete_calls == []


def test_a_rejected_card_does_not_stop_the_next_derivation() -> None:
    rows = _captured_rows()[:2]

    class _RejectsTheFirst(_Producer):
        async def admit(self, command: NotificationCommand) -> AdmissionReceipt:
            self.commands.append(command)
            if len(self.commands) == 1:
                raise ConnectorRejected("connector_admission_rejected")
            return AdmissionReceipt("admitted", command.event_id, "pending")

    store = _Store([_claim(row) for row in rows])
    producer = _RejectsTheFirst()
    worker = _worker(store, producer)

    assert asyncio.run(worker.run_once()) == 1

    assert [call["handoff_request_id"] for call in store.release_calls] == [
        rows[0]["handoff_request_id"]
    ]
    assert store.release_calls[0]["failure_code"] == "connector_rejected"
    assert [call["handoff_request_id"] for call in store.complete_calls] == [
        rows[1]["handoff_request_id"]
    ]
    # No existe un estado "frenado": el siguiente ciclo vuelve a reclamar.
    assert not hasattr(worker, "halted")
    store.claims = [_claim(rows[0], lease_generation=2)]
    assert asyncio.run(worker.run_once()) == 1


def test_an_unexpected_error_is_released_as_such_and_the_store_failure_propagates() -> None:
    row = _captured_rows()[0]
    store = _Store([_claim(row)])
    producer = _Producer(error=RuntimeError("boom"))
    worker = _worker(store, producer)

    assert asyncio.run(worker.run_once()) == 0
    assert store.release_calls[0]["failure_code"] == "connector_unexpected_error"

    class _ReleaseFails(_Store):
        async def release_slack_handoff_notification(self, **kwargs: object) -> None:
            raise RuntimeError("supabase down")

    failing_store = _ReleaseFails([_claim(row)])
    with pytest.raises(RuntimeError, match="supabase down"):
        asyncio.run(_worker(failing_store, producer).run_once())


@pytest.mark.parametrize(
    "override",
    [
        {"batch_size": 2},
        {"lease_seconds": 10},
        {"lease_seconds": 901},
        {"worker_id": ""},
        {"worker_id": "w" * 201},
        {"poll_interval_seconds": 0},
    ],
)
def test_worker_bounds_are_enforced(override: dict[str, object]) -> None:
    kwargs: dict[str, object] = {
        "store": _Store([]),
        "producer": _Producer(),
        "worker_id": "w",
    }
    kwargs.update(override)
    with pytest.raises(ValueError):
        SlackHandoffProjectionWorker(**kwargs)  # type: ignore[arg-type]


def test_worker_reports_healthy_only_while_polling() -> None:
    store = _Store([])
    worker = _worker(store, _Producer())

    async def scenario() -> tuple[bool, bool, bool]:
        before = worker.healthy
        await worker.start()
        deadline = time.monotonic() + 1
        while not store.claim_calls and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)
        during = worker.healthy
        await worker.stop()
        return before, during, worker.healthy

    assert asyncio.run(scenario()) == (False, True, False)


# --- el cliente Supabase: la RPC de claim y los cierres ---


def _client(handler) -> SupabaseClient:
    return SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )


def test_supabase_claim_parses_the_rpc_row_and_leases_exactly_one() -> None:
    row = _captured_rows()[0]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "handoff_request_id": row["handoff_request_id"],
                    "external_conversation_id": row["external_conversation_id"],
                    "primary_reason_code": row["primary_reason_code"],
                    "detail_reason_code": row["detail_reason_code"],
                    "occurred_at": row["occurred_at"],
                    "claim_token": CLAIM_TOKEN,
                    "lease_generation": 1,
                }
            ],
        )

    claims = asyncio.run(
        _client(handler).claim_slack_handoff_notifications(
            worker_id="johanna-slack-correlation-worker-1:handoff",
            limit=1,
            lease_seconds=60,
        )
    )

    assert claims == [_claim(row)]
    assert len(requests) == 1
    assert requests[0].url.path == "/rest/v1/rpc/claim_slack_handoff_notifications"
    assert json.loads(requests[0].content) == {
        "p_worker_id": "johanna-slack-correlation-worker-1:handoff",
        "p_limit": 1,
        "p_lease_seconds": 60,
    }


def test_supabase_claim_accepts_a_handoff_without_detail_reason() -> None:
    row = next(
        candidate
        for candidate in _captured_rows()
        if candidate["detail_reason_code"] is None
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "handoff_request_id": row["handoff_request_id"],
                    "external_conversation_id": row["external_conversation_id"],
                    "primary_reason_code": row["primary_reason_code"],
                    "detail_reason_code": None,
                    "occurred_at": row["occurred_at"],
                    "claim_token": CLAIM_TOKEN,
                    "lease_generation": 3,
                }
            ],
        )

    claims = asyncio.run(
        _client(handler).claim_slack_handoff_notifications(
            worker_id="w", limit=1, lease_seconds=60
        )
    )

    assert claims == [_claim(row, lease_generation=3)]
    assert claims[0].detail_reason_code is None


@pytest.mark.parametrize(
    "rows",
    [
        [{"handoff_request_id": "19ec5e48-b5d8-45b7-adf5-e9b07d0ba851"}],
        [
            {
                "handoff_request_id": "19ec5e48-b5d8-45b7-adf5-e9b07d0ba851",
                "external_conversation_id": 177,
                "primary_reason_code": "some_new_reason",
                "detail_reason_code": None,
                "occurred_at": "2026-09-25T23:30:24.065031+00:00",
                "claim_token": CLAIM_TOKEN,
                "lease_generation": 1,
            }
        ],
        [
            {
                "handoff_request_id": "19ec5e48-b5d8-45b7-adf5-e9b07d0ba851",
                "external_conversation_id": 177,
                "primary_reason_code": "commercial_exception",
                "detail_reason_code": None,
                "occurred_at": "2026-09-25 23:30:24",
                "claim_token": CLAIM_TOKEN,
                "lease_generation": 1,
            }
        ],
    ],
)
def test_supabase_claim_fails_closed_on_an_unexpected_row(rows: list[dict]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    with pytest.raises(SupabaseError):
        asyncio.run(
            _client(handler).claim_slack_handoff_notifications(
                worker_id="w", limit=1, lease_seconds=60
            )
        )


def test_supabase_claim_rejects_a_batch_bigger_than_one_and_a_bad_lease() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent")

    for kwargs in ({"limit": 2, "lease_seconds": 60}, {"limit": 1, "lease_seconds": 5}):
        with pytest.raises(ValueError):
            asyncio.run(
                _client(handler).claim_slack_handoff_notifications(
                    worker_id="w", **kwargs  # type: ignore[arg-type]
                )
            )


def test_supabase_complete_and_release_require_the_applied_row() -> None:
    row = _captured_rows()[0]
    notification_id = str(
        uuid5(NOTIFICATION_NAMESPACE, f"handoff:{row['handoff_request_id']}")
    )
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=[{"applied": True}])

    client = _client(handler)
    asyncio.run(
        client.complete_slack_handoff_notification(
            handoff_request_id=str(row["handoff_request_id"]),
            claim_token=CLAIM_TOKEN,
            lease_generation=1,
            notification_id=notification_id,
        )
    )
    asyncio.run(
        client.release_slack_handoff_notification(
            handoff_request_id=str(row["handoff_request_id"]),
            claim_token=CLAIM_TOKEN,
            lease_generation=1,
            failure_code="connector_admission_unknown",
        )
    )

    assert calls == [
        (
            "/rest/v1/rpc/complete_slack_handoff_notification",
            {
                "p_handoff_request_id": row["handoff_request_id"],
                "p_claim_token": CLAIM_TOKEN,
                "p_lease_generation": 1,
                "p_notification_id": notification_id,
            },
        ),
        (
            "/rest/v1/rpc/release_slack_handoff_notification",
            {
                "p_handoff_request_id": row["handoff_request_id"],
                "p_claim_token": CLAIM_TOKEN,
                "p_lease_generation": 1,
                "p_failure_code": "connector_admission_unknown",
            },
        ),
    ]

    # Un lease vencido (applied=false) no se da por cerrado en silencio.
    def stale(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"applied": False}])

    with pytest.raises(SupabaseError):
        asyncio.run(
            _client(stale).complete_slack_handoff_notification(
                handoff_request_id=str(row["handoff_request_id"]),
                claim_token=CLAIM_TOKEN,
                lease_generation=1,
                notification_id=notification_id,
            )
        )


# --- el cableado en el bridge ---


class _AppStore(_Store):
    def __init__(self) -> None:
        super().__init__([])
        self.handoff_effect_claims: list[dict[str, object]] = []

    async def resolve_commercial_ally_runtime_binding(self, expected):
        return expected

    async def claim_correlation_preresolution(self, **kwargs: object) -> None:
        return None

    async def claim_slack_correlation_notifications(
        self, **kwargs: object
    ) -> list[object]:
        return []

    async def claim_human_handoff_projection_effects(
        self, **kwargs: object
    ) -> list[object]:
        self.handoff_effect_claims.append(kwargs)
        return []

    async def get_human_handoff_projection_status(
        self, *args: object, **kwargs: object
    ) -> SimpleNamespace:
        # Lo que /ready le pide a la proyeccion de handoff que ya existe.
        return SimpleNamespace(
            pending_count=0,
            retryable_count=0,
            delivery_unknown_count=0,
            conflict_count=0,
            dead_letter_count=0,
        )


class _ControlClient:
    account_id = 1


def _app_settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "webhook_secret": "unused",
        "allowed_jid": "unused@s.whatsapp.net",
        "capture_dir": tmp_path,
        "max_age_seconds": 300,
        "commercial_ally_config": JOHANNA_COMMERCIAL_ALLY,
        "slack_connector_base_url": "https://connector.example.com",
        "slack_connector_bearer_token": "a" * 32,
        "slack_connector_projection_enabled": True,
        "correlation_preresolution_enabled": True,
        "correlation_preresolution_model_name": "resolver-model",
        "correlation_preresolution_worker_id": "correlation-ai-1",
        "hermes_api_base_url": "https://hermes.example.com",
        "hermes_api_key": "b" * 32,
        "slack_connector_worker_id": "johanna-slack-correlation-worker-1",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_app_wires_the_handoff_projection_only_when_both_flags_are_on(
    tmp_path: Path,
) -> None:
    store = _AppStore()
    app = create_app(
        _app_settings(tmp_path),
        supabase_client=store,  # type: ignore[arg-type]
        slack_runtime=SlackBridgeRuntime(producer=_Producer()),
    )
    assert app.state.slack_projection_worker is not None
    assert app.state.slack_handoff_projection_worker is None


def test_app_starts_the_handoff_projection_next_to_the_correlation_one(
    tmp_path: Path,
) -> None:
    store = _AppStore()
    app = create_app(
        _app_settings(
            tmp_path,
            agent_bot_id=1,
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            human_handoff_projection_enabled=True,
            handoff_projection_policy_key="lancemos-inbound-handoff",
            handoff_projection_policy_version=1,
            human_handoff_projection_worker_id="handoff-projection-test",
        ),
        chatwoot_client=_ControlClient(),  # type: ignore[arg-type]
        supabase_client=store,  # type: ignore[arg-type]
        slack_runtime=SlackBridgeRuntime(producer=_Producer()),
    )

    with TestClient(app) as client:
        deadline = time.monotonic() + 1
        while not store.claim_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        worker = app.state.slack_handoff_projection_worker
        assert worker is not None
        assert store.claim_calls[0] == {
            "worker_id": "johanna-slack-correlation-worker-1:handoff",
            "limit": 1,
            "lease_seconds": 60,
        }
        assert client.get("/ready").status_code == 200
        # No condiciona /ready: un 503 mata el proceso por el healthcheck, y
        # este worker falla de forma esperable hasta que la migracion exista.
        worker._healthy = False
        assert client.get("/ready").status_code == 200
    assert worker.healthy is False
