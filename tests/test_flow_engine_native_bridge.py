"""El bridge del motor nativo: la compra, la cola, MOT-002 y lo que no cambia (E-065).

Contrato: ``docs/contracts/flow-engine-v2.md``, secciones 4, 5, 8 y la 11 (lo
del PR 3). Encargo E-065 (el PR 3 del hito 4).

Datos:

* La compra es la captura de ATT1
  ``tests/fixtures/hotmart_cash_payment_approved_att1_20261012.json`` (base de
  ATT1, ``webhook_events``, 12/10, anonimizada adentro de la base): trae
  ``data.buyer.name``, que el parser no extraia.
* Las filas de las RPC nuevas son las de
  ``tests/fixtures/flow_engine/native_bridge_rpc_rows.json``, que escribe la base
  real (PGlite, ``validate_flow_engine_native_bridge.mjs --emit-bridge-fixtures``).
  El cliente real las lee sobre un PostgREST falso.
* La base del worker es un doble que anota las llamadas en orden: lo que se
  prueba aca es que y en que orden llama el bridge. Lo que hace la base con
  esas llamadas (la compra corta A2 y despues arranca la bienvenida) lo prueba
  la cadena de PGlite, seccion 3 de ``validate_flow_engine_native_bridge.mjs``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge.flow_engine_runtime import (
    START_REJECTION_EVENT_CODE,
    FlowEngineRuntime,
    StartRejectionNotifier,
    start_rejection_command,
)
from bridge.hotmart import EVENT_PURCHASE_APPROVED, parse_hotmart_purchase_payload
from bridge.instance_manifest import InstanceManifest
from bridge.supabase import (
    ContactMatch,
    FlowEngineAdmissionUnavailable,
    FlowEngineNativeAdmissionRow,
    FlowEngineNativeFlow,
    FlowEngineNativeQueue,
    FlowEngineNativeQueuedEvent,
    FlowEngineStartRejection,
    PurchaseCorrelationResult,
    SupabaseClient,
    SupabaseError,
)
from bridge.worker import ResolutionWorker
from slack_correlation.catalog import EVENT_TEMPLATES, NotificationCommand
from slack_correlation.producer import AdmissionReceipt, ConnectorAdmissionUnknown, ConnectorSemanticConflict

FIXTURES = Path(__file__).parent / "fixtures"
ROWS = json.loads((FIXTURES / "flow_engine" / "native_bridge_rpc_rows.json").read_text())
MANIFEST = InstanceManifest.from_toml_file(FIXTURES / "instances" / "att1" / "instancia.toml")
ALLY = MANIFEST.to_commercial_ally_config()
INSTANCE = {"tenant_ref": ALLY.tenant_ref, "ally_ref": ALLY.ally_ref}
CAPTURED = json.loads((FIXTURES / "hotmart_cash_payment_approved_att1_20261012.json").read_text())
PURCHASE = copy.deepcopy(CAPTURED["eventos"][1]["payload"])
PURCHASE["data"]["product"]["id"] = ALLY.hotmart_product_id
BUYER_PHONE = PURCHASE["data"]["buyer"]["checkout_phone"]
BUYER_EMAIL = PURCHASE["data"]["buyer"]["email"]
EVENT_ID = "11111111-2222-4333-8444-555555555555"
FLOW_ID = "00000000-0000-4000-8000-000000000001"
CONTACT_ID = "66666666-7777-4888-8999-000000000000"
NOW = datetime(2026, 10, 24, 16, 0, tzinfo=UTC)


def _native_flow(state: str = "cohorte", *, product: str | None = None, trigger: str = "compra") -> FlowEngineNativeFlow:
    [row] = ROWS["native_flows_cohort"]["flows"]
    return FlowEngineNativeFlow(
        flow_id=row["flow_id"], product_ref=product or ALLY.funnel_ref, flow_key=row["flow_key"],
        state=state, active_version=row["active_version"], trigger=trigger,
    )


class _Base:
    """La base del worker: anota cada llamada, en orden."""

    def __init__(
        self,
        *,
        native: tuple[FlowEngineNativeFlow, ...] = (),
        payload: dict[str, Any] | None = None,
        apply_outcome: str = "applied",
        contact: bool = True,
        admission: str = "ok",
        enqueue_error: bool = False,
    ) -> None:
        self.native = native
        self.payload = payload if payload is not None else copy.deepcopy(PURCHASE)
        self.pending = [{"id": EVENT_ID, "event_type": EVENT_PURCHASE_APPROVED, "payload": self.payload}]
        self.apply_outcome = apply_outcome
        self.contact = contact
        self.admission = admission
        self.enqueue_error = enqueue_error
        self.calls: list[str] = []
        self.flow_events: list[dict[str, Any]] = []
        self.finishes: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.queue: list[str] = []

    async def read_flow_engine_routes(self, instance: Any) -> tuple[()]:
        self.calls.append("routes")
        return ()

    async def read_flow_engine_native_flows(self, instance: Any) -> tuple[FlowEngineNativeFlow, ...]:
        self.calls.append("native_flows")
        return self.native

    async def read_flow_engine_native_event_queue(self, instance: Any, *, limit: int) -> FlowEngineNativeQueue:
        self.calls.append("queue")
        events = tuple(
            FlowEngineNativeQueuedEvent(webhook_event_id=event_id, attempts=1, payload=self.payload)
            for event_id in self.queue
        )
        return FlowEngineNativeQueue(pending=len(self.queue), events=events)

    async def fetch_pending_events(self, **_: Any) -> list[dict[str, Any]]:
        self.calls.append("fetch")
        pending, self.pending = self.pending, []
        return pending

    async def enqueue_flow_engine_native_event(self, instance: Any, *, webhook_event_id: str) -> bool:
        self.calls.append("enqueue")
        if self.enqueue_error:
            raise SupabaseError("enqueue_flow_engine_native_event_v1_failed: HTTP 503")
        if webhook_event_id not in self.queue:
            self.queue.append(webhook_event_id)
        return True

    async def apply_hotmart_purchase_approved(self, **kwargs: Any) -> PurchaseCorrelationResult:
        self.calls.append("apply")
        return PurchaseCorrelationResult(outcome=self.apply_outcome, recovery_case_id=None, matched_by=None)

    async def update_event_status(self, **kwargs: Any) -> None:
        self.calls.append(f"status:{kwargs['status']}")

    async def find_contact_by_email(self, email: str) -> ContactMatch | None:
        self.calls.append("find_email")
        if not self.contact:
            return None
        return ContactMatch(CONTACT_ID, "Persona Prueba 1", email, None, "unknown", "new", "email")

    async def find_contact_by_phones(self, phones: Any) -> ContactMatch | None:
        self.calls.append("find_phone")
        if not self.contact:
            return None
        return ContactMatch(CONTACT_ID, "Persona Prueba 1", None, BUYER_PHONE, "unknown", "new", "phone")

    async def create_contact(self, **kwargs: Any) -> str:
        self.calls.append("create_contact")
        self.created.append(kwargs)
        return CONTACT_ID

    async def create_contact_point(self, **kwargs: Any) -> None:
        self.calls.append(f"point:{kwargs['point_type']}")

    async def find_active_whatsapp_identities(self, **_: Any) -> list[Any]:
        self.calls.append("identities")
        return []

    async def admit_flow_engine_event(self, instance: Any, flow_event: dict[str, Any]) -> tuple[Any, ...]:
        self.calls.append("admit")
        self.flow_events.append(flow_event)
        if self.admission == "unavailable":
            raise FlowEngineAdmissionUnavailable("admit_flow_engine_event_v1_unavailable: HTTP 503")
        return (FlowEngineNativeAdmissionRow(FLOW_ID, "motor_arranco", None, "case-1", "action-1", True),)

    async def finish_flow_engine_native_event(self, *, webhook_event_id: str, done: bool, now: str) -> bool:
        self.calls.append(f"finish:{done}")
        self.finishes.append({"webhook_event_id": webhook_event_id, "done": done})
        if done and webhook_event_id in self.queue:
            self.queue.remove(webhook_event_id)
        return True


def _worker(base: _Base, *, engine: bool = True) -> tuple[ResolutionWorker, FlowEngineRuntime | None]:
    runtime = FlowEngineRuntime(supabase=base, instance=INSTANCE) if engine else None  # type: ignore[arg-type]
    worker = ResolutionWorker(
        supabase=base,  # type: ignore[arg-type]
        purchase_worker_enabled=True,
        commercial_ally_config=ALLY,
        chatwoot_account_id=ALLY.chatwoot_account_id,
        chatwoot_inbox_id=ALLY.chatwoot_inbox_id,
        flow_engine=runtime,
    )
    return worker, runtime


def _batch(worker: ResolutionWorker) -> None:
    asyncio.run(worker._process_batch())


# ------------------------------------------------- lo de hoy: cero llamadas


def test_without_the_flag_a_purchase_is_exactly_today() -> None:
    base = _Base(native=(_native_flow(),))
    worker, _ = _worker(base, engine=False)

    _batch(worker)

    assert base.calls == ["fetch", "apply"]


def test_without_native_flows_a_purchase_makes_no_new_call() -> None:
    # ATT1 hoy: el flag prendido y ningun flujo nativo. Por evento, cero
    # llamadas nuevas; lo unico nuevo es la lectura de los flujos nativos, a lo
    # sumo cada 60 s, como la de las rutas.
    base = _Base()
    worker, runtime = _worker(base)

    _batch(worker)

    assert base.calls == ["routes", "native_flows", "fetch", "apply"]
    assert runtime is not None and runtime.readiness()["flow_engine_native_on"] == "0"
    # La segunda vuelta, dentro de los 60 s: ni las lecturas.
    base.pending = [{"id": "other", "event_type": EVENT_PURCHASE_APPROVED, "payload": copy.deepcopy(PURCHASE)}]
    _batch(worker)
    assert base.calls[4:] == ["fetch", "apply"]


@pytest.mark.parametrize("state", ["pausado"])
def test_a_paused_native_flow_is_not_on(state: str) -> None:
    base = _Base(native=(_native_flow(state),))
    worker, _ = _worker(base)

    _batch(worker)

    assert "enqueue" not in base.calls and "admit" not in base.calls and "queue" not in base.calls


def test_a_purchase_of_another_product_does_not_go_to_the_native_admission() -> None:
    # Un bump o un upsell de otro producto (choque 7): por ID de producto,
    # nunca por nombre.
    payload = copy.deepcopy(PURCHASE)
    payload["data"]["product"]["id"] = ALLY.hotmart_product_id + 1
    base = _Base(native=(_native_flow(),), payload=payload)
    worker, _ = _worker(base)

    _batch(worker)

    assert "enqueue" not in base.calls and "admit" not in base.calls


def test_a_native_flow_of_another_trigger_or_product_does_not_call() -> None:
    base = _Base(native=(_native_flow(trigger="carrito"), _native_flow(product="otro-producto")))
    worker, _ = _worker(base)

    _batch(worker)

    assert "enqueue" not in base.calls and "admit" not in base.calls


# ------------------------------------------------------- con un flujo nativo


@pytest.mark.parametrize("state", ["cohorte", "prendido"])
def test_the_purchase_is_noted_applied_and_only_then_admitted(state: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    base = _Base(native=(_native_flow(state),))
    worker, _ = _worker(base)

    _batch(worker)

    sequence = base.calls[base.calls.index("fetch"):]
    # Primero se anota, despues se aplica (corta lo abierto) y recien despues
    # arranca el flujo nativo; al final se cierra en la cola.
    assert sequence[:2] == ["fetch", "enqueue"]
    assert sequence.index("enqueue") < sequence.index("apply") < sequence.index("admit")
    assert sequence[-1] == "finish:True"
    [event] = base.flow_events
    assert event == {
        "fuente": "hotmart",
        "tipo": "compra",
        "evento_id": EVENT_ID,
        "producto_ref": ALLY.funnel_ref,
        "oferta": PURCHASE["data"]["purchase"]["offer"]["code"],
        "contacto_id": CONTACT_ID,
        "momento": datetime.fromtimestamp(PURCHASE["creation_date"] / 1000, tz=UTC).isoformat(),
        "chatwoot": {"account_id": ALLY.chatwoot_account_id, "inbox_id": ALLY.chatwoot_inbox_id},
        "destino": BUYER_PHONE,
    }
    # El estado del evento lo deja la aplicacion: la admision no lo toca.
    assert not any(call.startswith("status:") for call in base.calls)
    # Los logs no llevan el telefono ni el mail.
    assert BUYER_PHONE not in caplog.text and BUYER_EMAIL not in caplog.text


def test_a_sale_without_a_recovery_case_also_fires() -> None:
    base = _Base(native=(_native_flow(),), apply_outcome="not_found")
    worker, _ = _worker(base)

    _batch(worker)

    assert "admit" in base.calls and base.finishes == [{"webhook_event_id": EVENT_ID, "done": True}]


def test_a_new_buyer_is_created_with_the_name_of_the_purchase() -> None:
    base = _Base(native=(_native_flow(),), contact=False)
    worker, _ = _worker(base)

    _batch(worker)

    [created] = base.created
    assert created["full_name"] == PURCHASE["data"]["buyer"]["name"]
    assert created["email"] == BUYER_EMAIL and created["phone"] == BUYER_PHONE
    assert "point:email" in base.calls and "point:phone" in base.calls


def test_the_purchase_parser_now_extracts_the_buyer_name() -> None:
    purchase = parse_hotmart_purchase_payload(PURCHASE)
    assert purchase is not None and purchase.buyer_name == PURCHASE["data"]["buyer"]["name"]
    # Sin el nombre, la correlacion sigue igual (no entra en ella).
    without = copy.deepcopy(PURCHASE)
    del without["data"]["buyer"]["name"]
    parsed = parse_hotmart_purchase_payload(without)
    assert parsed is not None and parsed.buyer_name is None
    assert parsed.transaction == purchase.transaction


def test_a_failed_admission_is_not_lost_the_queue_brings_it_back_without_applying_again() -> None:
    base = _Base(native=(_native_flow(),), admission="unavailable")
    worker, runtime = _worker(base)

    _batch(worker)
    assert base.calls.count("apply") == 1
    assert base.finishes == [{"webhook_event_id": EVENT_ID, "done": False}]
    assert base.queue == [EVENT_ID]

    # La vuelta siguiente: el evento ya no esta received (fetch no lo trae), la
    # cola si. Se admite sin volver a aplicar la compra.
    base.admission = "ok"
    runtime.native_flows._read_at = None  # type: ignore[union-attr]
    _batch(worker)
    # Dentro de los 60 s la cola no se vuelve a leer.
    assert base.calls.count("queue") == 1
    worker._native_queue_read_at = None  # type: ignore[attr-defined]
    _batch(worker)
    assert base.calls.count("apply") == 1
    assert base.calls.count("admit") == 2
    assert base.finishes[-1] == {"webhook_event_id": EVENT_ID, "done": True}
    assert base.queue == []
    assert runtime is not None and runtime.readiness()["flow_engine_native_queue_pending"] == "1"


def test_if_noting_the_purchase_fails_it_is_not_applied_and_stays_received() -> None:
    base = _Base(native=(_native_flow(),), enqueue_error=True)
    worker, _ = _worker(base)

    _batch(worker)

    assert "apply" not in base.calls and "admit" not in base.calls
    assert not any(call.startswith("status:") for call in base.calls)


def test_a_failed_native_read_keeps_the_last_good_one() -> None:
    base = _Base(native=(_native_flow(),))
    worker, runtime = _worker(base)
    _batch(worker)
    assert runtime is not None and runtime.native_flows.state == "ok"

    async def broken(instance: Any) -> Any:
        raise SupabaseError("read_flow_engine_native_flows_v1_failed: HTTP 503")

    base.read_flow_engine_native_flows = broken  # type: ignore[method-assign]
    runtime.native_flows._read_at = None
    asyncio.run(runtime.native_flows.refresh_if_due())
    assert runtime.native_flows.state == "failing"
    assert runtime.native_flows.wants(ALLY.funnel_ref, "compra")


# -------------------------------------------------------------------- MOT-002


class _Producer:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.fail = fail
        self.commands: list[NotificationCommand] = []

    async def admit(self, command: NotificationCommand) -> AdmissionReceipt:
        if self.fail is not None:
            raise self.fail
        self.commands.append(command)
        return AdmissionReceipt(status="admitted", notification_id=command.event_id,
                                delivery_state="pending", tenant_ref="att1")


class _PostgREST:
    """PostgREST falso con las filas de la base real (PGlite)."""

    def __init__(self, reads: dict[str, Any]) -> None:
        self.reads = reads
        self.bodies: list[tuple[str, dict[str, Any]]] = []
        self.marked = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        name = request.url.path.removeprefix("/rest/v1/rpc/")
        body = json.loads(request.content)
        self.bodies.append((name, body))
        if name == "read_flow_engine_start_rejections_v1":
            if self.marked:
                return httpx.Response(200, json=ROWS["rejections_empty"])
            return httpx.Response(200, json=self.reads[name])
        if name == "mark_flow_engine_start_rejection_notified_v1":
            first = not self.marked
            self.marked = True
            return httpx.Response(200, json=first)
        if name in self.reads:
            return httpx.Response(200, json=self.reads[name])
        raise AssertionError(f"unexpected {name}")

    def client(self) -> SupabaseClient:
        return SupabaseClient(base_url="https://supabase.example.test", service_role_key="k",
                              transport=httpx.MockTransport(self.handler))


def _rejection_notifier(producer: _Producer | None) -> tuple[StartRejectionNotifier, FlowEngineRuntime, _PostgREST]:
    rest = _PostgREST({"read_flow_engine_start_rejections_v1": ROWS["rejections_unnotified"]})
    client = rest.client()
    runtime = FlowEngineRuntime(supabase=client, instance=INSTANCE)
    return StartRejectionNotifier(supabase=client, runtime=runtime, producer=producer, clock=lambda: NOW), runtime, rest


def test_mot_002_tells_a_seeded_internal_error_once_without_personal_data() -> None:
    producer = _Producer()
    notifier, runtime, rest = _rejection_notifier(producer)
    [row] = ROWS["rejections_unnotified"]["rejections"]

    asyncio.run(notifier.run_once())

    [command] = producer.commands
    assert command.event_code == START_REJECTION_EVENT_CODE == "MOT-002"
    assert EVENT_TEMPLATES["MOT-002"].severity == "p2"
    assert command.reason_code == "error_interno:22012"
    assert command.component == "flow_engine:activacion"
    assert command.count == row["count"]
    assert str(uuid.UUID(command.event_id)) == command.event_id
    # Solo el flujo, la fase, el motivo y la cuenta: nada de la persona.
    assert command.subject_ref is None
    marks = [body for name, body in rest.bodies if name == "mark_flow_engine_start_rejection_notified_v1"]
    assert marks == [{
        "p_flow_id": row["flow_id"], "p_phase": "admision", "p_reason": "error_interno:22012",
        "p_notified_count": row["count"], "p_notified_at": NOW.isoformat(),
    }]
    assert runtime.start_rejections_unnotified == 0
    asyncio.run(notifier.run_once())
    assert len(producer.commands) == 1


def test_mot_002_is_the_same_notice_for_the_same_step_and_another_at_ten() -> None:
    [row] = ROWS["rejections_unnotified"]["rejections"]
    rejection = FlowEngineStartRejection(
        flow_id=row["flow_id"], flow_key=row["flow_key"], product_ref=row["product_ref"], version=row["version"],
        phase=row["phase"], reason=row["reason"], count=row["count"], step=row["step"],
        notified_count=row["notified_count"], first_at=row["first_at"], last_at=row["last_at"],
    )
    first = start_rejection_command(rejection)
    again = start_rejection_command(rejection)
    from dataclasses import replace

    ten = start_rejection_command(replace(rejection, count=10, step=10))
    assert first.event_id == again.event_id and first.dedupe_key == again.dedupe_key
    assert ten.event_id != first.event_id and ten.count == 10


def test_mot_002_with_the_connector_down_marks_nothing() -> None:
    notifier, runtime, rest = _rejection_notifier(_Producer(fail=ConnectorAdmissionUnknown("unknown")))

    asyncio.run(notifier.run_once())

    assert not any(name.startswith("mark_") for name, _ in rest.bodies)
    assert runtime.start_rejections_unnotified == 1


def test_mot_002_already_admitted_by_the_connector_is_marked() -> None:
    notifier, runtime, rest = _rejection_notifier(_Producer(fail=ConnectorSemanticConflict("conflict")))

    asyncio.run(notifier.run_once())

    assert any(name == "mark_flow_engine_start_rejection_notified_v1" for name, _ in rest.bodies)
    assert runtime.start_rejections_unnotified == 0


def test_mot_002_without_a_connector_only_counts() -> None:
    notifier, runtime, rest = _rejection_notifier(None)

    asyncio.run(notifier.run_once())

    assert [name for name, _ in rest.bodies] == ["read_flow_engine_start_rejections_v1"]
    assert runtime.readiness()["flow_engine_start_rejections_unnotified"] == "1"


# --------------------------------------------- el cliente lee lo que da la base


def test_the_client_reads_the_rows_of_the_base() -> None:
    rest = _PostgREST({
        "read_flow_engine_native_flows_v1": ROWS["native_flows_cohort"],
        "read_flow_engine_native_event_queue_v1": {
            **ROWS["queue_ready"],
            "events": [{**event, "payload": PURCHASE} for event in ROWS["queue_ready"]["events"]],
        },
        "flow_engine_native_step_tail_v1": ROWS["step_tail"],
    })
    client = rest.client()

    [flow] = asyncio.run(client.read_flow_engine_native_flows(INSTANCE))
    queue = asyncio.run(client.read_flow_engine_native_event_queue(INSTANCE, limit=5))
    tail = asyncio.run(client.get_flow_engine_native_step_tail("action-1"))

    assert (flow.state, flow.trigger, flow.flow_key) == ("cohorte", "compra", "activacion")
    assert queue.pending == 1 and queue.events[0].payload["event"] == "PURCHASE_APPROVED"
    assert tail is not None and tail.etiquetar == "cliente" and tail.producto == "Alimenta tu Tiroides"


def test_the_client_reads_empty_lists_without_native_flows() -> None:
    rest = _PostgREST({
        "read_flow_engine_native_flows_v1": ROWS["native_flows_empty"],
        "read_flow_engine_native_event_queue_v1": ROWS["queue_empty"],
    })
    client = rest.client()

    assert asyncio.run(client.read_flow_engine_native_flows(INSTANCE)) == ()
    assert asyncio.run(client.read_flow_engine_native_event_queue(INSTANCE, limit=5)).events == ()


def test_the_admission_client_separates_unanswered_from_permanent() -> None:
    def handler(status: int, body: Any) -> SupabaseClient:
        return SupabaseClient(base_url="https://supabase.example.test", service_role_key="k",
                              transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body)))

    with pytest.raises(FlowEngineAdmissionUnavailable):
        asyncio.run(handler(503, {}).admit_flow_engine_event(INSTANCE, {}))
    from bridge.supabase import SupabasePermanentError

    with pytest.raises(SupabasePermanentError):
        asyncio.run(handler(400, {"code": "22023", "message": "invalid_flow_engine_event"}).admit_flow_engine_event(
            INSTANCE, {}))
    rows = asyncio.run(handler(200, [{"flow_id": None, "outcome": "sin_flujo", "reason": None,
                                      "recovery_case_id": None, "followup_sequence_id": None,
                                      "scheduled_action_id": None, "created": None}]).admit_flow_engine_event(
        INSTANCE, {}))
    assert rows[0].outcome == "sin_flujo"
