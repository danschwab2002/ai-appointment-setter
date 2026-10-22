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
    assert command.reason_code == f"{row['kind']}:{row['reason']}"
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
    assert len(EVENT_TEMPLATES) == 57


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
