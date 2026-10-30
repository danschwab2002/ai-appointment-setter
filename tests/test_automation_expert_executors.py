"""automation-expert-read-v1 1.3.0 (E-208, M16 P5 y P8): quien ejecuta cada flujo de negocio.

La base se simula con la salida real de las lecturas, capturada sobre la cadena
de migraciones en PGlite: los flujos de ATT1 (tests/fixtures/automation_expert/
rpc_flows_att1.json), los nativos (tests/fixtures/automation_expert_native/), y
las rutas, divergencias y rechazos del motor (tests/fixtures/flow_engine/
bridge_rpc_rows.json y native_bridge_rpc_rows.json, de
validate_flow_engine_bridge_reads_v1.mjs y validate_flow_engine_native_bridge.mjs
--emit-bridge-fixtures). El estado de un scope tiene la forma de
PilotRuntimeStatus (src/bridge/supabase.py), la que devuelven
get_portable_payment_slip_pilot_runtime_status y
get_portable_payment_failure_pilot_runtime_status. Cuando una prueba cambia un
valor de un fixture, lo dice.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from bridge import automation_expert_read as reader
from bridge.supabase import FlowEngineDivergence, FlowEngineDivergences, FlowEngineRoute, FlowEngineStartRejection
from bridge.supabase import FlowEngineStartRejections
from test_automation_expert_read_app import AUTH, FakeReader, _client

FIXTURES = Path(__file__).parent / "fixtures"
FLOWS_RAW = json.loads((FIXTURES / "automation_expert" / "rpc_flows_att1.json").read_text())["result"]
NATIVE_RAW = json.loads((FIXTURES / "automation_expert_native" / "rpc_native_flows.json").read_text())["value"]
ENGINE = json.loads((FIXTURES / "flow_engine" / "bridge_rpc_rows.json").read_text())["reads"]
NATIVE_ENGINE = json.loads((FIXTURES / "flow_engine" / "native_bridge_rpc_rows.json").read_text())

INSTANCE = reader.InstanceMark(
    tenant_ref="lancemos", ally_ref="att1", funnel_ref="att1", chatwoot_account_id=2, chatwoot_inbox_id=11,
    hotmart_product_id="5071808", inbound_scope_key="att1-inbound", agent_scope_ref="att1-agent-bot",
    time_zone="America/Mexico_City", product_version="1.8.0",
    recovery_scope=reader.ScopeRef("att1-recuperacion", 5), precheckout_scope=reader.ScopeRef("att1-primer-contacto", 2),
)
FICHA_TEMPLATES = {
    # Los nombres y los parametros del manifiesto de ATT1 para la ficha (instance_manifest.py, :108-115).
    "ficha": ("att1_ficha_efectivo_01", "es_MX", ("nombre", "tienda", "producto", "vencimiento")),
    "ficha_vence_hoy": ("att1_ficha_vence_hoy_01", "es_MX", ("nombre", "tienda", "producto")),
}
ARMED = {"configured": True, "runtime_state": "armed", "runtime_generation": 3, "reason_code": "ok"}


def _settings(**overrides: object) -> reader.FlowSettings:
    values: dict[str, object] = dict(
        manifest_flows={"carrito": True, "pago_fallido": True, "precheckout": False}, templates={},
        time_zone="America/Mexico_City", precheckout_enabled=False, pilot_enabled=True, recovery_enabled=True,
        payment_failure_enabled=True, coupon_enabled=False, coupon_template=None, coupon_code=None,
        coupon_product_name=None, coupon_min_age_seconds=86_400, coupon_max_age_seconds=259_200,
        coupon_send_hours=None, coupon_test_phone_set=False, reactivation_enabled=False,
        reactivation_template=None, reactivation_min_age_seconds=86_400, reactivation_max_age_seconds=2_592_000,
        stalled_monitor_enabled=False, resume_enabled=False, precheckout_test_phone_set=False,
        allowed_jid_set=False,
    )
    values.update(overrides)
    return reader.FlowSettings(**values)  # type: ignore[arg-type]


def _data(settings: reader.FlowSettings, *, natives: bool = True) -> dict[str, object]:
    data, _ = reader.build_flows(copy.deepcopy(FLOWS_RAW), instance=INSTANCE, settings=settings)
    if natives:
        data = {**data, "flows": [*data["flows"], *reader.build_native_flows(copy.deepcopy(NATIVE_RAW))]}
    return data


def _engine(**changes: object) -> dict[str, object]:
    engine = {"routes": copy.deepcopy(ENGINE["routes"]["routes"]),
              "divergences": [dict(row, divergence_id=row["id"]) for row in ENGINE["divergences"]["divergences"]],
              "rejections": copy.deepcopy(NATIVE_ENGINE["rejections_unnotified"]["rejections"])}
    engine.update(changes)
    return engine


def _business(data: dict[str, object]) -> dict[str, dict[str, object]]:
    return {entry["business"]: entry for entry in data["business_flows"]}  # type: ignore[union-attr]


# ------------------------------------------------------------------- P5: el ejecutor por flujo de negocio


def test_each_business_flow_names_who_runs_it_and_by_which_scope() -> None:
    data, gaps = reader.apply_executors(_data(_settings()), settings=_settings(), scope_reads={}, engine=_engine())
    business = _business(data)
    # El carrito del legado esta prendido por su scope; nadie nativo lo hace.
    cart = business["carrito"]
    assert cart["executor"] == "legado"
    assert cart["legacy"]["scope"] == "att1-recuperacion v5"
    assert cart["legacy"]["sends"] is True
    assert "el legado sigue mandando por el scope att1-recuperacion v5" in cart["summary"]
    # Lo despues de la compra no tiene legado: lo hace la bienvenida nativa, en cohorte.
    after = business["compra"]
    assert after["legacy"] is None
    assert after["executor"] == "nativo_en_cohorte"
    assert after["native"] == [{"flow_key": "bienvenida", "product_ref": "att1", "state": "cohort",
                                "version": 1, "cohort_members": 1}]
    assert "cohorte de prueba" in after["summary"]
    # Un flujo de negocio apagado en el legado dice que no manda.
    assert business["precheckout"]["executor"] == "nadie"
    assert "no manda" in business["precheckout"]["summary"]
    assert not {gap["code"] for gap in gaps} & {"engine_v1_not_read"}


def test_a_native_cart_next_to_the_legacy_cart_is_both_without_a_double_message() -> None:
    """D1: la clave del nativo es propia (carrito_nativo); su disparador dice que hace el carrito.

    Con la guarda del doble caso (M1a, 20261030000100) el nativo toma cada disparo y el legado manda
    solo los que el nativo no arranca: no hay mensaje doble (E-213, hallazgo 2).
    """
    raw = copy.deepcopy(NATIVE_RAW)
    native = raw["flows"][0]
    # Cambio sobre el fixture: la bienvenida pasa a ser un carrito nativo prendido.
    native.update(flow_key="carrito_nativo", state="prendido")
    native["definition"].update(flujo="carrito_nativo", dispara={"evento": "carrito"})
    data, _ = reader.build_flows(copy.deepcopy(FLOWS_RAW), instance=INSTANCE, settings=_settings())
    data = {**data, "flows": [*data["flows"], *reader.build_native_flows(raw)]}
    out, _ = reader.apply_executors(data, settings=_settings(), scope_reads={}, engine=_engine())
    cart = _business(out)["carrito"]
    assert cart["executor"] == "legado_y_nativo"
    assert cart["double_case_guard"] is True
    assert [n["flow_key"] for n in cart["native"]] == ["carrito_nativo"]
    assert "mensaje doble" not in cart["summary"].replace("no sale un mensaje doble", "")
    assert "el legado sigue mandando" not in cart["summary"]
    assert "manda solo los disparos que el nativo no arranca" in cart["summary"]
    assert "el nativo carrito_nativo está prendido y toma cada disparo que arranca" in cart["summary"]
    # Con el legado pausado, el carrito lo hace solo el nativo.
    paused = copy.deepcopy(data)
    for flow in paused["flows"]:
        if flow["flow_key"] == "carrito":
            flow["status"] = {"state": "paused", "detail": "paused", "since": None}
    out, _ = reader.apply_executors(paused, settings=_settings(), scope_reads={}, engine=_engine())
    assert _business(out)["carrito"]["executor"] == "nativo"


def test_without_the_double_case_guard_both_flows_warn_of_a_double_message() -> None:
    """El primer contacto no tiene la guarda del doble caso: legado y nativo prendidos avisan."""
    raw = copy.deepcopy(NATIVE_RAW)
    native = raw["flows"][0]
    # Cambio sobre el fixture: la bienvenida pasa a ser un primer contacto nativo prendido.
    native.update(flow_key="primer_contacto", state="prendido")
    native["definition"].update(flujo="primer_contacto", dispara={"evento": "intencion"})
    settings = _settings(manifest_flows={"carrito": True, "pago_fallido": True, "precheckout": True},
                         precheckout_enabled=True)
    data, _ = reader.build_flows(copy.deepcopy(FLOWS_RAW), instance=INSTANCE, settings=settings)
    data = {**data, "flows": [*data["flows"], *reader.build_native_flows(raw)]}
    for flow in data["flows"]:
        if flow["flow_key"] == "precheckout":
            flow["status"] = {"state": "on", "detail": "armed", "since": None}
    out, _ = reader.apply_executors(data, settings=settings, scope_reads={}, engine=_engine())
    first = _business(out)["precheckout"]
    assert first["executor"] == "legado_y_nativo" and first["double_case_guard"] is False
    assert "puede salir un mensaje doble" in first["summary"]


def test_the_business_of_a_native_flow_comes_from_its_trigger() -> None:
    assert reader.business_of({"dispara": {"evento": "pago_fallido"}}) == "pago_fallido"
    assert reader.business_of({"dispara": {"evento": "ficha_impresa"}}) == "ficha"
    assert reader.business_of({"dispara": {"evento": "intencion"}}) == "precheckout"
    assert reader.business_of({"dispara": {"evento": "compra"}}) == "compra"
    assert reader.business_of({"dispara": {"silencio": {"lo_ultimo": "agente"}}}) == "descuento"
    assert reader.business_of({"dispara": {"silencio": {"lo_ultimo": "persona"}}}) == "reactivacion"
    assert reader.business_of({"dispara": {}}) is None


def test_the_payment_slip_is_read_from_its_own_scope_when_the_manifest_declares_it() -> None:
    settings = _settings(manifest_flows={"carrito": True, "ficha": True}, payment_slip_enabled=True,
                         payment_slip_scope=reader.ScopeRef("att1-ficha", 1), templates=FICHA_TEMPLATES)
    data, gaps = reader.apply_executors(_data(settings), settings=settings, scope_reads={"ficha": ARMED},
                                        engine=_engine())
    keys = [flow["flow_key"] for flow in data["flows"]]  # type: ignore[union-attr]
    # La ficha va con los del legado, antes de los nativos.
    assert keys.index("ficha") < keys.index("bienvenida")
    ficha = next(flow for flow in data["flows"] if flow["flow_key"] == "ficha")  # type: ignore[union-attr]
    assert ficha["status"] == {"state": "on", "detail": "armed", "since": None}
    assert ficha["version"] == {"scope": "att1-ficha v1"}
    assert ficha["definition"]["dispara"] == {"evento": "ficha_impresa"}
    assert ficha["definition"]["pasos"] == [{"plantilla": "att1_ficha_efectivo_01", "idioma": "es_MX",
                                             "variables": ["nombre", "tienda", "producto", "vencimiento"],
                                             "boton": {"link": "ficha"}}]
    assert ficha["reminders"] == [{"slot": "ficha_vence_hoy", "plantilla": "att1_ficha_vence_hoy_01",
                                   "idioma": "es_MX", "variables": ["nombre", "tienda", "producto"],
                                   "cuando": "el día del vencimiento"}]
    assert ficha["provenance"]["/status"]["ref"] == "get_portable_payment_slip_pilot_runtime_status"
    entry = _business(data)["ficha"]
    assert entry["executor"] == "legado" and entry["legacy"]["scope"] == "att1-ficha v1"
    codes = {(gap["code"], gap["scope"]) for gap in gaps}
    assert ("payment_slip_reminders_anchored_to_due_date", "flow:ficha") in codes
    assert ("payment_slip_observed_not_read", "flow:ficha") in codes


def test_the_payment_slip_with_the_flag_off_or_unread_is_off_or_a_gap() -> None:
    settings = _settings(manifest_flows={"ficha": True}, payment_slip_enabled=False,
                         payment_slip_scope=reader.ScopeRef("att1-ficha", 1))
    data, _ = reader.apply_executors(_data(settings), settings=settings, scope_reads={"ficha": None}, engine=None)
    assert next(f for f in data["flows"] if f["flow_key"] == "ficha")["status"]["state"] == "off"  # type: ignore[union-attr]
    on = _settings(manifest_flows={"ficha": True}, payment_slip_enabled=True,
                   payment_slip_scope=reader.ScopeRef("att1-ficha", 1))
    data, gaps = reader.apply_executors(_data(on), settings=on, scope_reads={"ficha": "not_read"}, engine=None)
    assert next(f for f in data["flows"] if f["flow_key"] == "ficha")["status"]["state"] == "not_configured"  # type: ignore[union-attr]
    codes = {gap["code"] for gap in gaps}
    assert {"legacy_scope_not_read", "engine_v1_not_read"} <= codes
    # Sin el flujo en el manifiesto, la ficha no aparece (como hasta la 1.2.0).
    data, _ = reader.apply_executors(_data(_settings()), settings=_settings(), scope_reads={}, engine=_engine())
    assert "ficha" not in [flow["flow_key"] for flow in data["flows"]]  # type: ignore[union-attr]
    assert "ficha" not in _business(data)


def test_a_native_flow_called_ficha_hides_the_legacy_one_with_its_gap() -> None:
    """D1 prohibe la clave; la base de E-207 reserva solo FLOW_KEYS. La lectura no da dos fichas."""
    raw = copy.deepcopy(NATIVE_RAW)
    # Cambio sobre el fixture: la bienvenida pasa a llamarse ficha.
    raw["flows"][0].update(flow_key="ficha")
    settings = _settings(manifest_flows={"ficha": True}, payment_slip_enabled=True,
                         payment_slip_scope=reader.ScopeRef("att1-ficha", 1))
    data, _ = reader.build_flows(copy.deepcopy(FLOWS_RAW), instance=INSTANCE, settings=settings)
    data = {**data, "flows": [*data["flows"], *reader.build_native_flows(raw)]}
    out, gaps = reader.apply_executors(data, settings=settings, scope_reads={"ficha": ARMED}, engine=None)
    assert [flow["flow_key"] for flow in out["flows"]].count("ficha") == 1  # type: ignore[union-attr]
    assert ("legacy_payment_slip_key_taken", "flow:ficha") in {(g["code"], g["scope"]) for g in gaps}
    assert reader.FLOW_KEYS == ("precheckout", "carrito", "pago_fallido", "reactivacion", "descuento")


def test_the_payment_failure_with_its_own_scope_reports_that_scope_and_not_the_recovery_one() -> None:
    """Maribel: el pago rechazado tiene scope propio; antes la lectura lo mostraba con el de la recuperacion."""
    settings = _settings(payment_failure_own_scope=reader.ScopeRef("att1-pago-rechazado", 2))
    paused = {"configured": True, "runtime_state": "paused", "runtime_generation": 1, "reason_code": "ok"}
    data, gaps = reader.apply_executors(_data(settings), settings=settings, scope_reads={"pago_fallido": paused},
                                        engine=_engine())
    flow = next(f for f in data["flows"] if f["flow_key"] == "pago_fallido")  # type: ignore[union-attr]
    assert flow["version"] == {"scope": "att1-pago-rechazado v2"}
    assert flow["status"]["state"] == "paused"
    assert flow["definition"]["topes"]["bolsa"] == "att1-pago-rechazado"
    assert flow["usage"]["pool"] == "att1-pago-rechazado"
    entry = _business(data)["pago_fallido"]
    assert entry["executor"] == "nadie"
    assert "pausado por el scope att1-pago-rechazado v2" in entry["summary"]
    assert ("legacy_scope_runtime_only", "flow:pago_fallido") in {(g["code"], g["scope"]) for g in gaps}
    # Sin scope propio, el pago se sigue mostrando con el de la recuperacion (lo de antes).
    data, _ = reader.apply_executors(_data(_settings()), settings=_settings(), scope_reads={}, engine=_engine())
    flow = next(f for f in data["flows"] if f["flow_key"] == "pago_fallido")  # type: ignore[union-attr]
    assert flow["version"]["scope"] == "att1-recuperacion v5"


# ------------------------------------------------------------------- P8: el motor v1


def test_a_legacy_flow_with_an_engine_route_carries_its_divergences_and_rejections() -> None:
    rejection = dict(NATIVE_ENGINE["rejections_unnotified"]["rejections"][0])
    # Cambio sobre el fixture: el rechazo capturado (de un nativo) pasa a ser del carrito con ruta.
    rejection.update(flow_key="carrito", reason="fuera_de_bolsa", phase="despacho")
    engine = _engine(rejections=[*_engine()["rejections"], rejection])
    data, gaps = reader.apply_executors(_data(_settings()), settings=_settings(), scope_reads={}, engine=engine)
    cart = next(f for f in data["flows"] if f["flow_key"] == "carrito")  # type: ignore[union-attr]
    assert cart["engine_v1"]["routes"] == [{"product_ref": "att1", "flow_key": "carrito", "executor": "legado",
                                            "legacy_scope_key": "att1-recuperacion", "flow_state": "prendido"}]
    assert [(d["kind"], d["reason"], d["count"]) for d in cart["engine_v1"]["divergences"]] == [
        ("transitorio", "transitorio:forzado_al_legado", 1)]
    assert [(r["phase"], r["reason"]) for r in cart["engine_v1"]["start_rejections"]] == [
        ("despacho", "fuera_de_bolsa")]
    # El rechazo del nativo no se le pega al carrito.
    assert all(r["flow_key"] == "carrito" for r in cart["engine_v1"]["start_rejections"])
    assert cart["provenance"]["/engine_v1"]["from"] == "database"
    assert ("engine_rejections_alertable_only", "flow:carrito") in {(g["code"], g["scope"]) for g in gaps}
    # Un flujo sin ruta no trae engine_v1.
    assert "engine_v1" not in next(f for f in data["flows"] if f["flow_key"] == "pago_fallido")  # type: ignore[union-attr]


def test_the_engine_route_in_motor_makes_the_business_flow_run_by_the_engine_v1() -> None:
    routes = copy.deepcopy(ENGINE["routes"]["routes"])
    # Cambio sobre el fixture: la ruta del carrito pasa al motor (como ATT1 tras la T2.4).
    routes[0]["executor"] = "motor"
    data, _ = reader.apply_executors(_data(_settings(), natives=False), settings=_settings(), scope_reads={},
                                     engine=_engine(routes=routes))
    cart = _business(data)["carrito"]
    assert cart["executor"] == "motor_v1"
    assert "el legado lo corre el motor v1" in cart["summary"]


def test_an_engine_reason_the_base_accepts_is_shown_with_long_numbers_masked() -> None:
    """La regla de la base (^[a-z0-9_:]{1,80}$), no code_ok: E-213, hallazgo 1."""
    divergences = _engine()["divergences"]
    # Cambio sobre el fixture: dos motivos que la base guarda y code_ok rechazaba.
    divergences = [dict(divergences[0], reason="9_algo"), dict(divergences[0], reason="error_interno:1234567")]
    data, gaps = reader.apply_executors(_data(_settings()), settings=_settings(), scope_reads={},
                                        engine=_engine(divergences=divergences))
    cart = next(f for f in data["flows"] if f["flow_key"] == "carrito")  # type: ignore[union-attr]
    assert [d["reason"] for d in cart["engine_v1"]["divergences"]] == ["9_algo", "error_interno:<numero>"]
    assert "engine_v1_not_read" not in {gap["code"] for gap in gaps}


def test_an_engine_row_outside_the_contract_leaves_its_gap_and_the_rest_of_the_flows() -> None:
    routes = copy.deepcopy(ENGINE["routes"]["routes"])
    routes[0]["executor"] = "otro"
    data, gaps = reader.apply_executors(_data(_settings()), settings=_settings(), scope_reads={},
                                        engine=_engine(routes=routes))
    assert "engine_v1_not_read" in {gap["code"] for gap in gaps}
    assert all("engine_v1" not in flow for flow in data["flows"])  # type: ignore[union-attr]
    assert _business(data)["carrito"]["executor"] == "legado"


def test_fifty_engine_rows_say_the_list_may_be_cut() -> None:
    one = _engine()["divergences"][0]
    many = [dict(one, divergence_id=f"x{index}") for index in range(reader.ENGINE_READ_LIMIT)]
    _, gaps = reader.apply_executors(_data(_settings()), settings=_settings(), scope_reads={},
                                     engine=_engine(divergences=many))
    assert "engine_v1_truncated" in {gap["code"] for gap in gaps}
    _, gaps = reader.apply_executors(_data(_settings()), settings=_settings(), scope_reads={}, engine=_engine())
    assert "engine_v1_truncated" not in {gap["code"] for gap in gaps}


def test_two_routes_of_the_same_kind_give_their_shared_executor_or_mixed() -> None:
    route = copy.deepcopy(ENGINE["routes"]["routes"][0])
    # Cambio sobre el fixture: un segundo producto con su carrito en ruta.
    other = dict(route, product_ref="att9", legacy_scope_key="att9-recuperacion")
    for executors, expected in ((("motor", "motor"), "motor"), (("motor", "legado"), "mixto")):
        routes = [dict(route, executor=executors[0]), dict(other, executor=executors[1])]
        data, _ = reader.apply_executors(_data(_settings(), natives=False), settings=_settings(), scope_reads={},
                                         engine=_engine(routes=routes))
        assert _business(data)["carrito"]["legacy"]["route_executor"] == expected


# ------------------------------------------------------------------- la ruta /flows


class EngineReader(FakeReader):
    """FakeReader con las tres lecturas del motor v1, desde los fixtures capturados."""

    def __init__(self, *, engine_failure: Exception | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.engine_failure = engine_failure
        self.engine_calls: list[tuple[str, dict[str, object]]] = []

    async def read_flow_engine_routes(self, instance: dict[str, object]) -> tuple[FlowEngineRoute, ...]:
        self.engine_calls.append(("routes", {}))
        if self.engine_failure is not None:
            raise self.engine_failure
        return tuple(FlowEngineRoute(**row) for row in ENGINE["routes"]["routes"])

    async def read_flow_engine_divergences(self, instance: dict[str, object], *, only_unnotified: bool,
                                           limit: int) -> FlowEngineDivergences:
        self.engine_calls.append(("divergences", {"only_unnotified": only_unnotified, "limit": limit}))
        rows = ENGINE["divergences"]
        return FlowEngineDivergences(unnotified=rows["unnotified"], divergences=tuple(
            FlowEngineDivergence(divergence_id=row["id"], **{k: v for k, v in row.items() if k != "id"})
            for row in rows["divergences"]))

    async def read_flow_engine_start_rejections(self, instance: dict[str, object], *, only_unnotified: bool,
                                                limit: int) -> FlowEngineStartRejections:
        self.engine_calls.append(("rejections", {"only_unnotified": only_unnotified, "limit": limit}))
        rows = NATIVE_ENGINE["rejections_unnotified"]
        return FlowEngineStartRejections(unnotified=rows["unnotified"], rejections=tuple(
            FlowEngineStartRejection(**row) for row in rows["rejections"]))


def test_the_flows_route_answers_business_flows_and_the_engine_v1() -> None:
    reader_ = EngineReader()
    response = _client(reader_).get("/internal/automation-expert/v1/flows", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract_version"] == "1.3.0"
    business = {entry["business"]: entry for entry in body["data"]["business_flows"]}
    # El manifiesto de ATT1 de las pruebas tiene todo apagado: nadie manda por el legado.
    assert business["carrito"]["executor"] == "nadie"
    assert business["carrito"]["legacy"]["state"] == "off"
    cart = next(flow for flow in body["data"]["flows"] if flow["flow_key"] == "carrito")
    assert cart["engine_v1"]["divergences"][0]["reason"] == "transitorio:forzado_al_legado"
    # Las tres lecturas, de toda la historia y sin marcar nada.
    assert reader_.engine_calls == [("routes", {}), ("divergences", {"only_unnotified": False, "limit": 50}),
                                    ("rejections", {"only_unnotified": False, "limit": 50})]
    assert "engine_v1_not_read" not in {gap["code"] for gap in body["gaps"]}


class OddReasonReader(EngineReader):
    """La divergencia capturada, con un motivo que la base acepta y code_ok no (E-213)."""

    async def read_flow_engine_divergences(self, instance: dict[str, object], *, only_unnotified: bool,
                                           limit: int) -> FlowEngineDivergences:
        read = await super().read_flow_engine_divergences(instance, only_unnotified=only_unnotified, limit=limit)
        return FlowEngineDivergences(unnotified=read.unnotified, divergences=tuple(
            FlowEngineDivergence(**{**row.__dict__, "reason": "9_algo"}) for row in read.divergences))


def test_the_flows_route_survives_an_engine_reason_the_base_accepts() -> None:
    response = _client(OddReasonReader()).get("/internal/automation-expert/v1/flows", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    cart = next(flow for flow in body["data"]["flows"] if flow["flow_key"] == "carrito")
    assert cart["engine_v1"]["divergences"][0]["reason"] == "9_algo"
    assert body["data"]["business_flows"]


def test_one_flow_brings_only_its_business_entry() -> None:
    response = _client(EngineReader()).get("/internal/automation-expert/v1/flows/carrito", headers=AUTH)
    assert response.status_code == 200, response.text
    assert [entry["business"] for entry in response.json()["data"]["business_flows"]] == ["carrito"]


def test_without_the_engine_reads_the_flows_still_answer_with_their_gap() -> None:
    from bridge.supabase import SupabaseError

    response = _client(EngineReader(engine_failure=SupabaseError("read_flow_engine_routes_v1_failed: HTTP 404"))).get(
        "/internal/automation-expert/v1/flows", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "engine_v1_not_read" in {gap["code"] for gap in body["gaps"]}
    assert all("engine_v1" not in flow for flow in body["data"]["flows"])
    assert body["data"]["business_flows"]
