"""MOT-001, el aviso de una divergencia del motor de flujos (la T2.4, commit 7).

Contrato: ``docs/contracts/flow-engine-v1.md``, seccion 8. Decisiones D8 (una
sola via: la lectura con notified_at) y D10 (sin conector, sin_slack).

La base es el cliente real sobre un PostgREST falso que contesta las filas de
``tests/fixtures/flow_engine/bridge_rpc_rows.json`` (la base real, PGlite); el
conector es un productor falso que registra lo que admite.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from bridge.flow_engine_runtime import (
    DIVERGENCE_EVENT_CODE,
    DivergenceNotifier,
    FlowEngineRuntime,
    divergence_command,
)
from bridge.supabase import FlowEngineDivergence, SupabaseClient
from slack_correlation.catalog import EVENT_TEMPLATES, NotificationCommand, render_message
from slack_correlation.producer import AdmissionReceipt, ConnectorAdmissionUnknown

ROOT = Path(__file__).resolve().parents[1]
ROWS = json.loads((ROOT / "tests" / "fixtures" / "flow_engine" / "bridge_rpc_rows.json").read_text())
INSTANCE = {"tenant_ref": "att1", "ally_ref": "att1"}
NOW = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)


class _Base:
    def __init__(self, *, divergences: dict[str, Any] | None = None, read_status: int = 200) -> None:
        self.divergences = divergences if divergences is not None else copy.deepcopy(ROWS["reads"]["divergences"])
        self.read_status = read_status
        self.marks: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = request.url.path.removeprefix("/rest/v1/rpc/")
        if name == "read_flow_engine_divergences_v1":
            self.reads.append(body)
            return httpx.Response(self.read_status, json=self.divergences)
        if name == "mark_flow_engine_divergence_notified_v1":
            self.marks.append(body)
            already = any(mark["p_id"] == body["p_id"] for mark in self.marks[:-1])
            if not already:
                self.divergences = {
                    "unnotified": max(0, self.divergences["unnotified"] - 1),
                    "divergences": [d for d in self.divergences["divergences"] if d["id"] != body["p_id"]],
                }
            return httpx.Response(200, json=not already)
        raise AssertionError(f"unexpected {name}")

    def client(self) -> SupabaseClient:
        return SupabaseClient(base_url="https://supabase.example.test", service_role_key="k",
                              transport=httpx.MockTransport(self.handler))


class _Producer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.commands: list[NotificationCommand] = []

    async def admit(self, command: NotificationCommand) -> AdmissionReceipt:
        if self.fail:
            raise ConnectorAdmissionUnknown("connector_admission_unknown")
        self.commands.append(command)
        return AdmissionReceipt(status="admitted", notification_id=command.event_id,
                                delivery_state="pending", tenant_ref="att1")


def _notifier(base: _Base, producer: _Producer | None) -> tuple[DivergenceNotifier, FlowEngineRuntime]:
    client = base.client()
    runtime = FlowEngineRuntime(supabase=client, instance=INSTANCE)
    return DivergenceNotifier(supabase=client, runtime=runtime, producer=producer, clock=lambda: NOW), runtime


def test_a_divergence_not_notified_gives_one_admit_and_one_mark() -> None:
    base = _Base()
    producer = _Producer()
    notifier, runtime = _notifier(base, producer)
    [row] = ROWS["reads"]["divergences"]["divergences"]

    asyncio.run(notifier.run_once())

    [command] = producer.commands
    assert command.event_code == "MOT-001"
    assert command.event_id == row["id"]
    # La base ya trae la clase adelante: no se duplica.
    assert row["reason"].startswith(f"{row['kind']}:")
    assert command.reason_code == row["reason"]
    assert command.count == row["count"]
    assert command.state == "pending"
    assert base.reads == [{"p_instance": INSTANCE, "p_only_unnotified": True, "p_limit": 20}]
    assert base.marks == [{"p_id": row["id"], "p_notified_at": NOW.isoformat()}]
    assert runtime.divergences_unnotified == 0
    assert runtime.alerts == "slack"
    # La vuelta siguiente no la avisa otra vez.
    asyncio.run(notifier.run_once())
    assert len(producer.commands) == 1


def test_with_slack_down_there_is_no_mark_and_the_next_round_retries() -> None:
    base = _Base()
    producer = _Producer(fail=True)
    notifier, runtime = _notifier(base, producer)

    asyncio.run(notifier.run_once())
    assert base.marks == []
    assert runtime.divergences_unnotified == 1

    producer.fail = False
    asyncio.run(notifier.run_once())
    assert len(producer.commands) == 1
    assert len(base.marks) == 1
    assert runtime.divergences_unnotified == 0


def test_without_a_producer_there_is_no_admit_but_the_count_is_read() -> None:
    base = _Base()
    notifier, runtime = _notifier(base, None)

    asyncio.run(notifier.run_once())

    assert base.marks == []
    assert runtime.divergences_unnotified == 1
    assert runtime.alerts == "sin_slack"
    assert runtime.readiness()["flow_engine_alerts"] == "sin_slack"
    assert runtime.readiness()["flow_engine_divergences_unnotified"] == "1"


def test_a_failed_read_publishes_unknown() -> None:
    base = _Base(read_status=503)
    producer = _Producer()
    notifier, runtime = _notifier(base, producer)
    runtime.divergences_unnotified = 4

    asyncio.run(notifier.run_once())

    assert producer.commands == []
    assert runtime.readiness()["flow_engine_divergences_unnotified"] == "unknown"


def test_the_command_validates_and_carries_no_personal_data() -> None:
    [row] = ROWS["reads"]["divergences"]["divergences"]
    divergence = FlowEngineDivergence(
        divergence_id=row["id"], kind=row["kind"], reason=row["reason"], flow_key=row["flow_key"],
        product_ref=row["product_ref"], version=row["version"], count=row["count"],
        first_at=row["first_at"], last_at=row["last_at"],
    )
    command = divergence_command(divergence)

    assert command.dedupe_key == divergence_command(divergence).dedupe_key
    assert len(command.dedupe_key) == 64
    assert command.subject_ref is None
    assert command.occurred_at.tzinfo is not None
    rendered = json.dumps(render_message(command, tenant_label="ATT1"), ensure_ascii=False)
    assert "El motor no pudo y lo planificó el legado" in rendered
    assert "transitorio:forzado_al_legado" in rendered


def test_a_long_reason_is_cut_to_eighty() -> None:
    divergence = FlowEngineDivergence(
        divergence_id="00000000-0000-4000-8000-000000000005", kind="admision_al_legado",
        reason="admision_al_legado:" + "x" * 61, flow_key="carrito", product_ref="att1", version=None,
        count=3, first_at="2026-10-22T15:00:00.000000Z", last_at="2026-10-22T15:00:00.000000Z",
    )
    command = divergence_command(divergence)
    assert command.reason_code is not None and len(command.reason_code) == 80


def test_the_catalog_knows_mot_001() -> None:
    assert DIVERGENCE_EVENT_CODE in EVENT_TEMPLATES
    assert EVENT_TEMPLATES["MOT-001"].severity == "p2"
    # MOT-002 (flow-engine-v2): el rechazo del motor, el codigo 58.
    assert len(EVENT_TEMPLATES) == 58


def test_create_app_wires_the_notifier_only_with_the_flag(tmp_path: Path) -> None:
    from test_flow_engine_ready import _ReadyAuthority, _app

    on = _app(tmp_path, _ReadyAuthority())
    assert isinstance(on.state.flow_engine_divergence_notifier, DivergenceNotifier)
    assert on.state.flow_engine.alerts == "sin_slack"
    (tmp_path / "off").mkdir()
    off = _app(tmp_path / "off", _ReadyAuthority(), flow_engine_enabled=False)
    assert off.state.flow_engine_divergence_notifier is None
    assert off.state.flow_engine is None


def test_with_the_connector_the_notifier_uses_its_producer(tmp_path: Path) -> None:
    from test_cart_discount_first_touch import _Chatwoot
    from test_flow_engine_bridge_settings import engine_settings
    from test_flow_engine_ready import _ReadyAuthority

    from bridge.app import create_app
    from bridge.slack_runtime import SlackBridgeRuntime

    class _Closable(_Producer):
        async def aclose(self) -> None:
            return None

    producer = _Closable()
    app = create_app(
        engine_settings(tmp_path),
        supabase_client=_ReadyAuthority(),  # type: ignore[arg-type]
        chatwoot_client=_Chatwoot().client(),
        slack_runtime=SlackBridgeRuntime(producer=producer),
    )
    assert app.state.flow_engine.alerts == "slack"
    assert app.state.flow_engine_divergence_notifier._producer is producer  # type: ignore[attr-defined]


class _Conflicting(_Producer):
    """El conector ya admitio ese id con otro contenido (otra count)."""

    async def admit(self, command: NotificationCommand) -> AdmissionReceipt:
        from slack_correlation.producer import ConnectorSemanticConflict

        self.commands.append(command)
        raise ConnectorSemanticConflict("connector_semantic_conflict")


def test_a_semantic_conflict_is_an_alert_already_admitted_and_is_marked() -> None:
    # La vuelta anterior lo admitio pero la respuesta o la marca no llego, y la
    # fila sumo count: el reintento ya no es identico. Sin marcarla, volveria
    # en cada vuelta y la pagina de 20 se trabaria con ella.
    base = _Base()
    notifier, runtime = _notifier(base, _Conflicting())

    asyncio.run(notifier.run_once())

    assert len(base.marks) == 1
    assert runtime.divergences_unnotified == 0


def test_a_mark_that_changed_nothing_does_not_lower_the_count() -> None:
    base = _Base()
    producer = _Producer()
    original = base.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("mark_flow_engine_divergence_notified_v1"):
            base.marks.append(json.loads(request.content))
            return httpx.Response(200, json=False)
        return original(request)

    base.handler = handler  # type: ignore[method-assign]
    notifier, runtime = _notifier(base, producer)
    asyncio.run(notifier.run_once())
    assert len(base.marks) == 1
    assert runtime.divergences_unnotified == 1


def test_the_dedupe_key_is_the_one_of_the_plan() -> None:
    import hashlib

    [row] = ROWS["reads"]["divergences"]["divergences"]
    divergence = FlowEngineDivergence(
        divergence_id=row["id"], kind=row["kind"], reason=row["reason"], flow_key=row["flow_key"],
        product_ref=row["product_ref"], version=row["version"], count=row["count"],
        first_at=row["first_at"], last_at=row["last_at"],
    )
    expected = hashlib.sha256(b"flow-engine-divergence-v1\0" + row["id"].encode()).hexdigest()
    assert divergence_command(divergence).dedupe_key == expected


def test_a_reason_without_its_kind_gets_it() -> None:
    divergence = FlowEngineDivergence(
        divergence_id="00000000-0000-4000-8000-000000000005", kind="producto", reason="sin_binding",
        flow_key="carrito", product_ref="att1", version=1, count=1,
        first_at="2026-10-22T15:00:00.000000Z", last_at="2026-10-22T15:00:00.000000Z",
    )
    assert divergence_command(divergence).reason_code == "producto:sin_binding"


def test_the_loop_reads_at_start_and_stops() -> None:
    base = _Base()
    producer = _Producer()
    client = base.client()
    runtime = FlowEngineRuntime(supabase=client, instance=INSTANCE)
    notifier = DivergenceNotifier(supabase=client, runtime=runtime, producer=producer,
                                  poll_interval_seconds=3600, clock=lambda: NOW)

    async def run() -> None:
        await notifier.start()
        for _ in range(100):
            if base.marks:
                break
            await asyncio.sleep(0.01)
        await notifier.stop(timeout=1)

    asyncio.run(run())
    assert len(base.reads) == 1
    assert len(base.marks) == 1


def test_create_app_wires_the_flag_into_the_workers(tmp_path: Path) -> None:
    from test_flow_engine_ready import _ReadyAuthority, _app

    on = _app(tmp_path, _ReadyAuthority())
    assert on.state.durable_dispatcher._flow_engine_enabled is True  # type: ignore[attr-defined]
    assert on.state.resolution_worker._flow_engine is on.state.flow_engine  # type: ignore[attr-defined]
    (tmp_path / "off").mkdir()
    off = _app(tmp_path / "off", _ReadyAuthority(), flow_engine_enabled=False, worker_enabled=True)
    assert off.state.durable_dispatcher._flow_engine_enabled is False  # type: ignore[attr-defined]
    assert off.state.resolution_worker._flow_engine is None  # type: ignore[attr-defined]


def test_the_lifespan_starts_and_stops_the_notifier(tmp_path: Path) -> None:
    from test_flow_engine_ready import _ReadyAuthority, _app

    app = _app(tmp_path, _ReadyAuthority())
    notifier = app.state.flow_engine_divergence_notifier
    calls: list[str] = []

    async def start() -> None:
        calls.append("start")

    async def stop(**_: object) -> None:
        calls.append("stop")

    async def noop(**_: object) -> None:
        return None

    for worker in (app.state.resolution_worker, app.state.durable_dispatcher,
                   app.state.recovery_coupon_worker):
        if worker is not None:
            worker.start = noop  # type: ignore[method-assign]
            worker.stop = noop  # type: ignore[method-assign]
    notifier.start = start  # type: ignore[method-assign]
    notifier.stop = stop  # type: ignore[method-assign]

    async def run() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(run())
    assert calls == ["start", "stop"]
