"""La simulacion de un cambio sobre un flujo nativo, sin la ruta del carrito (E-241).

El caso real (E-238, medido en Maribel el 2026-10-31 21:30Z en solo lectura):
Maribel no tiene ruta del carrito en ``flow_engine_routes`` (ni en legado ni en
motor), asi que ``read_flow_engine_history_v1`` corta con
``flow_engine_history_cart_route_not_found`` (P0002) y la simulacion de todo
item que no fuera ``nuevo`` quedaba ``failed`` con ``flow_engine_unavailable``.
Pasar la bienvenida nativa de la cohorte a todos (un ``prender``) no se podia
publicar desde la pagina, que exige la simulacion ``built``. La lectura de los
conteos, en cambio, contesto ``{"events": 59, "people": 59}`` para la compra en 7
dias: son los numeros que usa el doble de aca.

Ahora un ``prender``, ``version``, ``volver`` o ``apagar`` de un flujo nativo
cuenta los disparos de su evento con ``read_flow_engine_native_trigger_counts_v1``,
en el bridge y en el comando del operador por igual. Lo del legado (el carrito de
ATT1 y los demas flujos con ``legacy_kind``) sigue por la historia, sin cambios.
"""

from __future__ import annotations

import copy
import io

import httpx
import pytest
from test_flow_change_api_app import AUTH, PREFIX, PostgrestEmulator, _client
from test_flow_change_new_flow import _ENV, _new_item, _propose, _published_welcome, _validate
from test_flow_change_operator_script import FakeDocker, FakeTerminal
from test_flow_change_operator_script import script as operator_script

from bridge.flow_approval import _simulation_summary
from bridge.flow_change_operator import world_from_store
from bridge.flow_change_simulation import (
    build_native_flow_simulation,
    native_flow_counts_request,
)
from bridge.flow_definition import parse_items

WELCOME_ID = "0000000b-0000-4000-8000-0000000a7701"
CART_ID = "00000001-0000-4000-8000-0000000a7701"
# Lo que contesto la lectura de los conteos en Maribel el 31/10 (E-238 §1), para la compra en 7 dias.
MARIBEL_COUNTS = {"contract": "flow-engine-v2", "since": "2026-10-24T21:30:00Z", "until": "2026-10-31T21:30:00Z",
                  "events": 59, "people": 59}
# La forma con que PostgREST devuelve el raise de read_flow_engine_history_v1 sin ruta del carrito
# (20261021000200, errcode P0002), la misma de los errores capturados del 21/10.
CART_ROUTE_NOT_FOUND = {"code": "P0002", "details": None, "hint": None,
                        "message": "flow_engine_history_cart_route_not_found"}


class WithoutCartRoute(PostgrestEmulator):
    """Una base como la de Maribel: la historia del motor corta porque no hay ruta del carrito."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/read_flow_engine_history_v1"):
            self.requests.append(("read_flow_engine_history_v1", {}, {}))
            return httpx.Response(404, json=CART_ROUTE_NOT_FOUND)
        return super().handler(request)


def _welcome_flow(state: str = "cohorte", *, native: bool = True, versions: dict | None = None) -> dict:
    return {"flow_id": WELCOME_ID, "product_ref": "att1", "flow_key": "bienvenida", "state": state,
            "active_version": 1, "generation": 2, "changed_at": "2026-10-31T18:00:00Z",
            "changed_by_kind": "operador", "native": native,
            "versions": versions if versions is not None else {"1": copy.deepcopy(_published_welcome())}}


@pytest.fixture
def image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "1.24.2")
    monkeypatch.setenv("GIT_SHA", "380441b000000000000000000000000000000000")


# ----------------------------------------------------------------- el caso de Maribel, por el bridge


def test_passing_a_native_flow_from_the_cohort_to_everyone_simulates_without_the_cart_route(image: None) -> None:
    emulator = WithoutCartRoute()
    emulator.new_flow_counts = dict(MARIBEL_COUNTS)
    emulator.flows.append(_welcome_flow())
    client, _ = _client(emulator)
    item = {"producto": "att1", "flujo": "bienvenida", "flow_id": WELCOME_ID, "tipo": "prender"}
    saved = _propose(client, [item], {WELCOME_ID: {"active_version": 1}})
    assert saved.status_code == 201, saved.text
    validated = _validate(client, saved.json()["data"]["proposal_id"])
    assert validated.status_code == 200, validated.text
    out = validated.json()["data"]
    assert out["status"] == "validada", out["blocks"]
    simulation = out["preview"]["simulacion"]
    # Antes: {"status": "failed", "reason_code": "flow_engine_unavailable"}, y la pagina no publicaba.
    assert simulation["status"] == "built", simulation
    assert {key: simulation["summary"][key] for key in
            ("flujo", "tipo", "estado_hoy", "disparador", "disparos", "personas")} == {
        "flujo": "att1/bienvenida", "tipo": "prender", "estado_hoy": "cohorte", "disparador": "compra",
        "disparos": 59, "personas": 59}
    assert len(simulation["summary_sha256"]) == 64 and simulation["cambios"] == []
    assert "simulation_failed" not in [gap["code"] for gap in out["preview"]["huecos"]]
    functions = [function for function, _, _ in emulator.requests]
    assert "read_flow_engine_history_v1" not in functions
    [counts] = [body for function, body, _ in emulator.requests
                if function == "read_flow_engine_native_trigger_counts_v1"]
    assert (counts["p_product_ref"], counts["p_trigger"]) == ("att1", "compra")
    # Lo que la pagina muestra con esa simulacion (y lo que la deja publicar: status built).
    assert "<strong>59</strong>" in _simulation_summary(simulation)


def test_without_the_counts_reading_a_native_change_still_says_why(image: None) -> None:
    emulator = WithoutCartRoute()  # sin read_flow_engine_native_trigger_counts_v1: 403
    emulator.flows.append(_welcome_flow())
    client, _ = _client(emulator)
    item = {"producto": "att1", "flujo": "bienvenida", "flow_id": WELCOME_ID, "tipo": "prender"}
    saved = _propose(client, [item], {WELCOME_ID: {"active_version": 1}}).json()["data"]
    out = _validate(client, saved["proposal_id"]).json()["data"]
    assert out["preview"]["simulacion"] == {"status": "failed", "reason_code": "flow_engine_unavailable"}


def test_the_legacy_cart_still_goes_through_the_history(image: None) -> None:
    # ATT1: el carrito tiene legacy_kind; su cambio sigue por la historia del motor, como antes.
    emulator = PostgrestEmulator()
    emulator.new_flow_counts = dict(MARIBEL_COUNTS)
    emulator.flows.append(_welcome_flow())
    client, _ = _client(emulator)
    item = {"producto": "att1", "flujo": "carrito", "flow_id": CART_ID, "tipo": "apagar"}
    saved = _propose(client, [item], {CART_ID: {"active_version": 1}})
    assert saved.status_code == 201, saved.text
    _validate(client, saved.json()["data"]["proposal_id"])
    functions = [function for function, _, _ in emulator.requests]
    assert "read_flow_engine_history_v1" in functions
    assert "read_flow_engine_native_trigger_counts_v1" not in functions


# ----------------------------------------------------------------- que lectura pide


def _store(*flows: dict) -> dict:
    cart = {"flow_id": CART_ID, "product_ref": "att1", "flow_key": "carrito", "state": "prendido",
            "active_version": 1, "native": False,
            "versions": {"1": {"dispara": {"evento": "carrito"}}}}
    return {"flows": [cart, *flows]}


NATIVE = {"flow_id": WELCOME_ID, "product_ref": "att1", "flow_key": "bienvenida", "state": "cohorte",
          "active_version": 2, "native": True,
          "versions": {"1": {"dispara": {"evento": "carrito"}}, "2": {"dispara": {"evento": "compra"}}}}
PRENDER = {"producto": "att1", "flujo": "bienvenida", "flow_id": WELCOME_ID, "tipo": "prender"}
CASES = [
    # (store, items, lo que se pide)
    (_store(NATIVE), [PRENDER], {"product_ref": "att1", "trigger": "compra"}),
    (_store(NATIVE), [{**PRENDER, "tipo": "apagar"}], {"product_ref": "att1", "trigger": "compra"}),
    (_store(NATIVE), [{**PRENDER, "tipo": "volver", "version": 1, "en_curso": "seguir"}],
     {"product_ref": "att1", "trigger": "carrito"}),
    (_store(NATIVE), [{**PRENDER, "tipo": "version", "en_curso": "seguir",
                       "definicion": {"dispara": {"evento": "pago_rechazado"}}}],
     {"product_ref": "att1", "trigger": "pago_rechazado"}),
    # En cohorte siempre es nativo, aunque el almacen viejo no traiga native.
    (_store({**NATIVE, "native": None}), [PRENDER], {"product_ref": "att1", "trigger": "compra"}),
    # Lo que sigue por la historia: un flujo del legado, o uno nativo mezclado con uno del legado.
    (_store(NATIVE), [{"producto": "att1", "flujo": "carrito", "flow_id": CART_ID, "tipo": "apagar"}], None),
    (_store(NATIVE), [PRENDER, {"producto": "att1", "flujo": "carrito", "flow_id": CART_ID, "tipo": "apagar"}],
     None),
    (_store({**NATIVE, "native": False, "state": "prendido"}), [PRENDER], None),
    (_store(NATIVE), [_new_item()], None),
    (None, [PRENDER], None),
    (_store(NATIVE), [], None),
    # Nativos, pero no se puede pedir.
    (_store(NATIVE, {**NATIVE, "flow_id": "0000000c-0000-4000-8000-0000000a7701", "flow_key": "otra"}),
     [PRENDER, {**PRENDER, "flujo": "otra", "flow_id": "0000000c-0000-4000-8000-0000000a7701"}],
     {"simulation_unavailable": "simulation_native_flow_mixed"}),
    (_store({**NATIVE, "versions": {}}), [PRENDER],
     {"simulation_unavailable": "simulation_native_flow_counts_invalid"}),
]


@pytest.mark.parametrize(("store", "items", "expected"), CASES)
def test_the_bridge_and_the_operator_ask_for_the_same_reading(store, items, expected) -> None:
    assert native_flow_counts_request(store, items) == expected
    assert operator_script._native_flow_counts_request(store, items) == expected


# ----------------------------------------------------------------- el validador


def _world(*flows: dict):
    return world_from_store({"flows": [_welcome_flow(), *flows]} if not flows else {"flows": list(flows)})


def test_the_native_simulation_counts_and_refuses_what_does_not_fit() -> None:
    world = _world()
    [item] = parse_items([PRENDER])
    counts = {**MARIBEL_COUNTS, "product_ref": "att1", "trigger": "compra"}
    built = build_native_flow_simulation(counts, [item], world=world)
    assert built["status"] == "built" and built["summary"]["disparos"] == 59
    assert "comparte_el_evento_con" not in built["summary"]  # el flujo no comparte el evento consigo mismo
    invalid = {"status": "failed", "reason_code": "simulation_native_flow_counts_invalid"}
    assert build_native_flow_simulation({**counts, "people": 60}, [item], world=world) == invalid
    assert build_native_flow_simulation({**counts, "trigger": "carrito"}, [item], world=world) == invalid
    assert build_native_flow_simulation({**counts, "product_ref": "otro"}, [item], world=world) == invalid
    assert build_native_flow_simulation({**counts, "otra": 1}, [item], world=world) == invalid
    assert build_native_flow_simulation({**counts, "events": -1}, [item], world=world) == invalid
    mixed = {"status": "failed", "reason_code": "simulation_native_flow_mixed"}
    assert build_native_flow_simulation(counts, [item], world=None) == mixed
    legacy = world_from_store({"flows": [_welcome_flow("prendido", native=False)]})
    assert build_native_flow_simulation(counts, [item], world=legacy) == mixed


def test_the_validator_uses_the_native_counts(image: None) -> None:
    from bridge.flow_change_operator import validate_proposal

    store = {"flows": [_welcome_flow()]}
    out = validate_proposal({
        "native_flow_counts": {**MARIBEL_COUNTS, "product_ref": "att1", "trigger": "compra"},
        "instance": {"tenant_ref": "lancemos", "ally_ref": "att1", "products": ["att1"],
                     "time_zone": "America/Mexico_City"},
        "store": store,
        "proposal": {"id": "00000000-0000-4000-8000-000000000241", "revision": 1, "items": [PRENDER],
                     "draft_base": {WELCOME_ID: {"active_version": 1}}, "purpose": "pasarla a todos",
                     "requested_by_kind": "automation_expert"},
        "catalog": None,
    }, environ={"SETTER_VERSION": "1.24.2", "GIT_SHA": "380441b"})
    assert out["preview"]["simulacion"]["status"] == "built"
    assert out["preview"]["simulacion"]["summary"]["personas"] == 59


# ----------------------------------------------------------------- el comando del operador


def test_the_operator_counts_the_event_of_a_native_flow_without_the_history() -> None:
    docker = FakeDocker(engine=True)
    ops = operator_script.Ops(operator_script.Config.from_env({**_ENV}), docker=docker, terminal=FakeTerminal(),
                              out=io.StringIO(), code=lambda: "abc123")
    inputs = ops.read_engine_inputs([PRENDER], _store(NATIVE))
    assert "native_flow_counts" in inputs
    [counts_sql] = [stdin for argv, stdin in docker.calls if "read_flow_engine_native_trigger_counts_v1" in stdin]
    assert "trigger\tcompra" in counts_sql and "product\tatt1" in counts_sql
    assert not any("read_flow_engine_history_v1" in stdin for argv, stdin in docker.calls)
    # El carrito del legado sigue por el motor y la historia.
    docker = FakeDocker(engine=True)
    ops = operator_script.Ops(operator_script.Config.from_env({**_ENV}), docker=docker, terminal=FakeTerminal(),
                              out=io.StringIO(), code=lambda: "abc123")
    cart = {"producto": "att1", "flujo": "carrito", "flow_id": CART_ID, "tipo": "apagar"}
    assert "engine" in ops.read_engine_inputs([cart], _store(NATIVE))
    assert any("read_flow_engine_history_v1" in stdin for argv, stdin in docker.calls)
