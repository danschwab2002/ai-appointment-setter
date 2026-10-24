"""El carrito con la ruta en motor entra por la admision (la T2.4, commit 4).

Contrato: ``docs/contracts/flow-engine-v1.md``, secciones 7.1 y 8. Plan de la
T2.4, commit 4, y decisiones D2 y D9.

Fixtures:
- El carrito: ``tests/fixtures/hotmart_cart_abandonment_rejected_v1.json``
  (capturado del panel de Hotmart el 2026-09-21), con el producto y la oferta
  de ATT1, como ``test_resolution_pilot_plan_rejection``.
- El pago rechazado: la forma inline de ``test_resolution_pilot_plan_rejection``
  (no hay un PURCHASE_CANCELED capturado; deuda previa).
- Las filas de la admision y de las rutas: ``tests/fixtures/flow_engine/
  bridge_rpc_rows.json``, que sale de la base real (PGlite).

Lo que factura (a): con el flag prendido y la ruta del carrito en legado, el
cuerpo de ``plan_lancemos_pilot_cart_recovery`` es byte a byte el mismo que con
el flag apagado, y no hay ninguna llamada a la admision.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_resolution_pilot_plan_rejection import (
    BOUNDARY,
    CONFIG,
    CONTACT_ID,
    EVENT_ID,
    _cart,
    _failure,
)

from bridge.flow_engine_runtime import FORCE_LEGACY_AFTER_FAILURES, FlowEngineRuntime
from bridge.hotmart import EVENT_CART_ABANDONMENT, EVENT_PURCHASE_CANCELED
from bridge.resolution import FLOW_ENGINE_ADMISSION_PENDING, ResolutionError, resolve_event
from bridge.supabase import SupabaseClient
from bridge.worker import ResolutionWorker

ROOT = Path(__file__).resolve().parents[1]
ROWS = json.loads((ROOT / "tests" / "fixtures" / "flow_engine" / "bridge_rpc_rows.json").read_text())
NATIVE_ROWS = json.loads((Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "flow_engine"
                          / "native_bridge_rpc_rows.json").read_text())
PLAN_ROW = {
    "recovery_case_id": "00000000-0000-4000-8000-0000000000a1",
    "followup_sequence_id": "00000000-0000-4000-8000-0000000000a2",
    "scheduled_action_id": "00000000-0000-4000-8000-0000000000a3",
    "created": True,
}
INSTANCE = {"tenant_ref": CONFIG.tenant_ref, "ally_ref": CONFIG.ally_ref}
POLICY_KEY = "att1-carrito-tres-toques"


def _routes(executor: str | None) -> dict[str, Any]:
    payload = copy.deepcopy(ROWS["reads"]["routes"])
    if executor is None:
        payload["routes"] = []
    else:
        payload["routes"][0]["executor"] = executor
        payload["routes"][0]["legacy_scope_key"] = BOUNDARY.scope_key
    return payload


class _Base:
    """PostgREST falso: identidad nueva, y lo que se le diga a cada RPC."""

    def __init__(
        self,
        *,
        routes: dict[str, Any] | None = None,
        admission: list[httpx.Response] | None = None,
        routes_status: int = 200,
    ) -> None:
        self.routes = routes if routes is not None else _routes("legado")
        self.routes_status = routes_status
        self.admission = list(admission or [])
        self.calls: list[tuple[str, Any]] = []
        self.status_patches: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if request.method == "GET" and path == "/rest/v1/contact_points":
            return httpx.Response(200, json=[], request=request)
        if request.method == "GET" and path in {
            "/rest/v1/conversations", "/rest/v1/recovery_cases", "/rest/v1/channel_identities",
        }:
            return httpx.Response(200, json=[], request=request)
        if request.method == "POST" and path == "/rest/v1/contacts":
            return httpx.Response(201, json=[{"id": CONTACT_ID}], request=request)
        if request.method == "POST" and path == "/rest/v1/contact_points":
            return httpx.Response(201, request=request)
        if request.method == "PATCH" and path == "/rest/v1/webhook_events":
            self.status_patches.append(body)
            return httpx.Response(204, request=request)
        if request.method == "POST" and path.startswith("/rest/v1/rpc/"):
            name = path.removeprefix("/rest/v1/rpc/")
            self.calls.append((name, body))
            if name == "read_flow_engine_routes_v1":
                return httpx.Response(self.routes_status, json=self.routes, request=request)
            if name == "read_flow_engine_native_flows_v1":
                # ATT1 hoy: ningun flujo nativo (la base real, PGlite).
                return httpx.Response(200, json=NATIVE_ROWS["native_flows_empty"], request=request)
            if name == "admit_flow_engine_hotmart_v1":
                return self.admission.pop(0)
            if name.startswith("plan_"):
                return httpx.Response(200, json=[PLAN_ROW], request=request)
        raise AssertionError(f"unexpected request {request.method} {path}")

    def client(self) -> SupabaseClient:
        return SupabaseClient(
            base_url="https://supabase.example.test",
            service_role_key="test-key",
            transport=httpx.MockTransport(self.handler),
        )

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def body_of(self, name: str) -> Any:
        bodies = [body for called, body in self.calls if called == name]
        assert len(bodies) == 1, (name, self.names())
        return bodies[0]


def _admission(outcome: str, **changes: Any) -> httpx.Response:
    return httpx.Response(200, json=[{**ROWS["admission"][outcome], **changes}])


def _runtime(base: _Base, client: SupabaseClient) -> FlowEngineRuntime:
    runtime = FlowEngineRuntime(supabase=client, instance=INSTANCE)
    asyncio.run(runtime.routes.refresh_if_due())
    return runtime


def _resolve(base: _Base, *, flow_engine: bool, client: SupabaseClient | None = None,
             runtime: FlowEngineRuntime | None = None, payload: dict[str, Any] | None = None) -> Any:
    client = client or base.client()
    if flow_engine and runtime is None:
        runtime = _runtime(base, client)
    return asyncio.run(resolve_event(
        webhook_event_id=EVENT_ID,
        payload=payload or _cart(CONFIG.offer_code),
        supabase=client,
        policy_key=POLICY_KEY,
        policy_version=3,
        allowed_jid=None,
        chatwoot_account_id=CONFIG.chatwoot_account_id,
        chatwoot_inbox_id=CONFIG.chatwoot_inbox_id,
        pilot_boundary=BOUNDARY,
        commercial_ally_config=CONFIG,
        event_type=EVENT_CART_ABANDONMENT,
        flow_engine=runtime if flow_engine else None,
    ))


# ------------------------------------------------------------- lo que factura


@pytest.mark.parametrize("routes", [_routes("legado"), _routes(None)])
def test_a_with_the_route_in_legacy_the_call_is_exactly_the_one_of_today(routes: dict[str, Any]) -> None:
    off = _Base()
    _resolve(off, flow_engine=False)
    on = _Base(routes=routes)
    _resolve(on, flow_engine=True)

    assert "admit_flow_engine_hotmart_v1" not in on.names()
    assert off.body_of("plan_lancemos_pilot_cart_recovery") == on.body_of("plan_lancemos_pilot_cart_recovery")
    assert [n for n in on.names() if n != "read_flow_engine_routes_v1"] == off.names()
    assert on.status_patches == off.status_patches
    # Apagado no lee nada del motor.
    assert "read_flow_engine_routes_v1" not in off.names()


# ---------------------------------------------------------------- con el motor


def test_b_the_route_in_motor_admits_with_the_mapping_of_the_contract() -> None:
    off = _Base()
    _resolve(off, flow_engine=False)
    today = off.body_of("plan_lancemos_pilot_cart_recovery")
    base = _Base(routes=_routes("motor"), admission=[_admission("motor_arranco")])
    report = _resolve(base, flow_engine=True)

    assert not any(name.startswith("plan_") for name in base.names())
    assert base.body_of("admit_flow_engine_hotmart_v1") == {
        "p_webhook_event_id": today["p_webhook_event_id"],
        "p_contact_id": today["p_contact_id"],
        "p_external_product_id": today["p_external_product_id"],
        "p_product_name": today["p_product_name"],
        "p_offer_code": today["p_offer_code"],
        "p_abandoned_at": today["p_abandoned_at"],
        "p_chatwoot_account_id": today["p_chatwoot_account_id"],
        "p_chatwoot_inbox_id": today["p_chatwoot_inbox_id"],
        "p_destination_phone": today["p_external_user_id"],
        "p_legacy_scope_key": today["p_scope_key"],
        "p_legacy_scope_version": today["p_scope_version"],
        "p_legacy_policy_key": today["p_policy_key"],
        "p_legacy_policy_version": today["p_policy_version"],
        "p_force_legacy": False,
    }
    assert report.contact_id == CONTACT_ID
    assert base.status_patches[-1]["processing_status"] == "processed"


@pytest.mark.parametrize("outcome", ["motor_arranco", "legado", "legado_por_divergencia"])
def test_c_a_planned_outcome_leaves_the_event_processed(outcome: str) -> None:
    base = _Base(routes=_routes("motor"), admission=[_admission(outcome)])
    _resolve(base, flow_engine=True)
    assert [patch["processing_status"] for patch in base.status_patches] == ["processed"]


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        ("legado_rechazo", "pilot_scope_rejected:pilot_runtime_not_armed"),
        ("motor_no_arranca", "flow_engine_no_start:no_entra:permiso"),
    ],
)
def test_c_a_not_planned_outcome_leaves_the_event_failed_with_its_reason(outcome: str, error: str) -> None:
    base = _Base(routes=_routes("motor"), admission=[_admission(outcome)])
    with pytest.raises(ResolutionError, match=f"^{error}$"):
        _resolve(base, flow_engine=True)
    assert base.status_patches == [
        {"processing_status": "failed", "processed_at": None, "processing_error": error}
    ]


def test_d_an_admission_without_answer_leaves_the_event_received_and_the_fourth_forces_the_legacy() -> None:
    timeout = httpx.Response(504, text="gateway timeout")
    base = _Base(
        routes=_routes("motor"),
        admission=[timeout] * FORCE_LEGACY_AFTER_FAILURES + [_admission("legado_por_divergencia")],
    )
    client = base.client()
    runtime = _runtime(base, client)
    for _ in range(FORCE_LEGACY_AFTER_FAILURES):
        with pytest.raises(ResolutionError, match=f"^{FLOW_ENGINE_ADMISSION_PENDING}$"):
            _resolve(base, flow_engine=True, client=client, runtime=runtime)
    assert base.status_patches == []
    forced = [body["p_force_legacy"] for name, body in base.calls if name == "admit_flow_engine_hotmart_v1"]
    assert forced == [False, False, False]

    _resolve(base, flow_engine=True, client=client, runtime=runtime)
    forced = [body["p_force_legacy"] for name, body in base.calls if name == "admit_flow_engine_hotmart_v1"]
    assert forced == [False, False, False, True]
    assert [patch["processing_status"] for patch in base.status_patches] == ["processed"]
    assert len(runtime.admission_failures) == 0


def test_d_an_answer_clears_the_count() -> None:
    timeout = httpx.Response(504, text="gateway timeout")
    base = _Base(routes=_routes("motor"),
                 admission=[timeout, _admission("motor_arranco"), timeout, timeout, _admission("motor_arranco")])
    client = base.client()
    runtime = _runtime(base, client)
    with pytest.raises(ResolutionError):
        _resolve(base, flow_engine=True, client=client, runtime=runtime)
    _resolve(base, flow_engine=True, client=client, runtime=runtime)
    for _ in range(2):
        with pytest.raises(ResolutionError):
            _resolve(base, flow_engine=True, client=client, runtime=runtime)
    _resolve(base, flow_engine=True, client=client, runtime=runtime)
    forced = [body["p_force_legacy"] for name, body in base.calls if name == "admit_flow_engine_hotmart_v1"]
    assert forced == [False] * 5


def test_d_an_error_of_the_function_marks_the_event_failed_as_today() -> None:
    base = _Base(routes=_routes("motor"),
                 admission=[httpx.Response(500, json={"code": "XX000", "message": "internal"})])
    with pytest.raises(ResolutionError, match="^create_recovery_case_failed$"):
        _resolve(base, flow_engine=True)
    assert base.status_patches[-1]["processing_error"] == "create_recovery_case_failed"


def test_e_a_failed_read_of_the_routes_goes_through_the_wrapper() -> None:
    off = _Base()
    _resolve(off, flow_engine=False)
    base = _Base(routes=_routes("motor"), routes_status=503)
    client = base.client()
    runtime = _runtime(base, client)
    _resolve(base, flow_engine=True, client=client, runtime=runtime)

    assert runtime.routes.state == "failing"
    assert "admit_flow_engine_hotmart_v1" not in base.names()
    assert base.body_of("plan_lancemos_pilot_cart_recovery") == off.body_of("plan_lancemos_pilot_cart_recovery")


def test_the_cache_reads_the_routes_once_a_minute() -> None:
    base = _Base(routes=_routes("motor"))
    now = [1000.0]
    runtime = FlowEngineRuntime(supabase=base.client(), instance=INSTANCE, clock=lambda: now[0])
    assert runtime.routes.state == "pending"
    assert runtime.routes.executor(BOUNDARY.scope_key, "carrito") is None
    asyncio.run(runtime.routes.refresh_if_due())
    now[0] += 59
    asyncio.run(runtime.routes.refresh_if_due())
    assert base.names() == ["read_flow_engine_routes_v1"]
    now[0] += 1
    asyncio.run(runtime.routes.refresh_if_due())
    assert base.names() == ["read_flow_engine_routes_v1"] * 2
    assert runtime.routes.executor(BOUNDARY.scope_key, "carrito") == "motor"
    assert runtime.routes.executor("otro-scope", "carrito") is None
    assert runtime.routes.count("motor") == 1 and runtime.routes.count("legado") == 0
    assert json.loads(json.dumps(base.calls[0][1])) == {"p_instance": INSTANCE}


def test_the_failure_counter_keeps_at_most_the_limit() -> None:
    runtime = FlowEngineRuntime(supabase=_Base().client(), instance=INSTANCE)
    failures = runtime.admission_failures
    for index in range(1005):
        failures.record_failure(f"event-{index}")
    assert len(failures) == 1000
    assert not failures.should_force_legacy("event-0")


# ------------------------------------------------------------------ el worker


def _worker(base: _Base, runtime: FlowEngineRuntime | None, client: SupabaseClient) -> ResolutionWorker:
    return ResolutionWorker(
        supabase=client,
        chatwoot_account_id=CONFIG.chatwoot_account_id,
        chatwoot_inbox_id=CONFIG.chatwoot_inbox_id,
        policy_key=POLICY_KEY,
        policy_version=3,
        pilot_boundary=BOUNDARY,
        commercial_ally_config=CONFIG,
        payment_failure_enabled=True,
        flow_engine=runtime,
    )


def test_f_a_payment_failure_of_the_same_scope_never_goes_through_the_admission() -> None:
    base = _Base(routes=_routes("motor"), admission=[_admission("motor_arranco")])
    client = base.client()
    runtime = _runtime(base, client)
    worker = _worker(base, runtime, client)
    asyncio.run(worker._process_one(
        {"id": EVENT_ID, "payload": _failure(CONFIG.offer_code), "event_type": EVENT_PURCHASE_CANCELED}
    ))
    assert "admit_flow_engine_hotmart_v1" not in base.names()
    assert "plan_portable_payment_failure_recovery" in base.names()
    # El control: el carrito del mismo worker si entra por la admision.
    asyncio.run(worker._process_one(
        {"id": EVENT_ID, "payload": _cart(CONFIG.offer_code), "event_type": EVENT_CART_ABANDONMENT}
    ))
    assert base.names().count("admit_flow_engine_hotmart_v1") == 1


def test_the_batch_refreshes_the_routes_only_with_the_flag() -> None:
    def fetch(base: _Base) -> None:
        original = base.handler

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/rest/v1/webhook_events":
                return httpx.Response(200, json=[], request=request)
            return original(request)

        base.handler = handler  # type: ignore[method-assign]

    on = _Base(routes=_routes("legado"))
    fetch(on)
    client = on.client()
    asyncio.run(_worker(on, FlowEngineRuntime(supabase=client, instance=INSTANCE), client)._process_batch())
    # flow-engine-v2 (E-065): con el flag, tambien los flujos nativos, a lo sumo
    # cada 60 s. Sin ninguno, ni la cola de la compra ni nada por evento.
    assert on.names() == ["read_flow_engine_routes_v1", "read_flow_engine_native_flows_v1"]
    off = _Base()
    fetch(off)
    client = off.client()
    asyncio.run(_worker(off, None, client)._process_batch())
    assert off.names() == []


def test_a_read_that_fails_after_a_good_one_goes_back_to_the_wrapper() -> None:
    base = _Base(routes=_routes("motor"))
    now = [0.0]
    runtime = FlowEngineRuntime(supabase=base.client(), instance=INSTANCE, clock=lambda: now[0])
    asyncio.run(runtime.routes.refresh_if_due())
    assert runtime.routes.executor(BOUNDARY.scope_key, "carrito") == "motor"
    base.routes_status = 503
    now[0] += 60
    asyncio.run(runtime.routes.refresh_if_due())
    assert runtime.routes.state == "failing"
    assert runtime.routes.executor(BOUNDARY.scope_key, "carrito") is None
    assert runtime.routes.count("motor") == 0


def test_routes_without_a_legacy_scope_are_counted() -> None:
    payload = _routes("legado")
    payload["routes"].append({**payload["routes"][0], "flow_key": "descuento", "legacy_kind": "descuento",
                              "legacy_scope_key": None})
    base = _Base(routes=payload)
    runtime = _runtime(base, base.client())
    assert runtime.routes.count("legado") == 2
    assert runtime.routes.executor(BOUNDARY.scope_key, "carrito") == "legado"
