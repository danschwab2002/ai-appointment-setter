"""E-622: prender (y apagar) un flujo con disparo por audiencia arma su simulacion con la audiencia real.

El defecto, que dejo abierto E-621 (2026-11-16, reporte E-621 §7): un flujo nuevo de audiencia (``manual``,
``fecha`` o ``cada``) ya se publica para la cohorte, pero pasarlo a todos (un ``prender``) valida con la
simulacion de un flujo nativo, que cuenta los disparos del evento del flujo con
``read_flow_engine_native_trigger_counts_v1``. Un disparo por audiencia no tiene evento:
``native_flow_counts_request`` devolvia ``simulation_native_flow_counts_invalid`` sin leer nada, y la pagina no
publica sin simulacion construida. Solo lo podia publicar el operador con ``--sin-simulacion``.

Ahora un ``prender`` o un ``apagar`` de un flujo de audiencia pide ``read_flow_audience_reach_v1``
(20261116000100, solo para el rol del agente): cuantas personas cumplen hoy su ``para``, sin telefonos ni
contactos. La forma de la respuesta es la que devuelve la funcion en PGlite
(``tests/sql/followup_engine/validate_flow_audience_reach.mjs``, que la corre sobre el mundo de ATT1).
"""

from __future__ import annotations

import copy
import io

import httpx
import pytest
from test_e621_new_audience_flow_simulation import _item as _new_audience_item
from test_flow_change_api_app import PostgrestEmulator, _client
from test_flow_change_new_flow import _ENV, _propose, _validate
from test_flow_change_operator_script import FakeDocker, FakeTerminal
from test_flow_change_operator_script import script as operator_script

from bridge.flow_approval import _simulation_summary
from bridge.flow_change_operator import validate_proposal, world_from_store
from bridge.flow_change_simulation import (
    build_native_audience_reach_simulation,
    native_flow_counts_request,
)
from bridge.flow_definition import parse_items

AVISO_ID = "0000000d-0000-4000-8000-0000000a7622"


def _published(form: str = "manual", **changes: object) -> dict:
    """La definicion normalizada que deja el AE al proponer el nuevo de E-621, como publicada v1."""
    emulator = PostgrestEmulator()
    client, _ = _client(emulator)
    saved = _propose(client, [_new_audience_item(form, **changes)])
    assert saved.status_code == 201, saved.text
    definition = copy.deepcopy(emulator.proposals[saved.json()["data"]["proposal_id"]]["items"][0]["definicion"])
    definition.update(version=1, estado="publicada")
    return definition


def _aviso_flow(form: str = "manual", state: str = "cohorte", **changes: object) -> dict:
    return {"flow_id": AVISO_ID, "product_ref": "att1", "flow_key": f"aviso_{form}_prueba", "state": state,
            "active_version": 1, "generation": 2, "changed_at": "2026-11-16T14:00:00Z",
            "changed_by_kind": "operador", "native": True, "versions": {"1": _published(form, **changes)}}


def _reach(**changes: object) -> dict:
    """La respuesta de read_flow_audience_reach_v1, con la forma de la funcion."""
    reach = {"contract": "flow-audience-reach-v1", "flow_id": AVISO_ID, "version": 1, "product_ref": "att1",
             "state": "cohorte", "status": "clienta", "measured_at": "2026-11-16T15:00:00.123+00:00",
             "candidates": 41, "opted_out": 3, "already_customer": 0, "matching": 38, "without_contact": 2,
             "ambiguous": 1, "read_capped": False}
    reach.update(changes)
    return reach


class WithReach(PostgrestEmulator):
    """La base con la migracion de E-622: contesta read_flow_audience_reach_v1 para el flujo del almacen."""

    def __init__(self, reach: dict | None) -> None:
        super().__init__()
        self.reach = reach

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/read_flow_audience_reach_v1") and self.reach is not None:
            self.requests.append(("read_flow_audience_reach_v1", __import__("json").loads(request.content), {}))
            return httpx.Response(200, json=self.reach)
        return super().handler(request)


@pytest.fixture
def image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "1.40.1")
    monkeypatch.setenv("GIT_SHA", "0386f8b0964056a5db96ae263a3dce67f57d78d5")


def _prender(form: str = "manual", tipo: str = "prender") -> dict:
    return {"producto": "att1", "flujo": f"aviso_{form}_prueba", "flow_id": AVISO_ID, "tipo": tipo}


# ----------------------------------------------------------------- por la ruta del agente


@pytest.mark.parametrize("form", ["manual", "fecha", "cada"])
def test_passing_an_audience_flow_to_everyone_builds_the_simulation_with_the_real_audience(
        image: None, form: str) -> None:
    emulator = WithReach(_reach())
    emulator.flows.append(_aviso_flow(form))
    client, _ = _client(emulator)
    saved = _propose(client, [_prender(form)], {AVISO_ID: {"active_version": 1}})
    assert saved.status_code == 201, saved.text
    validated = _validate(client, saved.json()["data"]["proposal_id"])
    assert validated.status_code == 200, validated.text
    simulation = validated.json()["data"]["preview"]["simulacion"]
    # Antes: {"status": "failed", "reason_code": "simulation_native_flow_counts_invalid"}.
    assert simulation["status"] == "built", simulation
    summary = simulation["summary"]
    assert {key: summary[key] for key in ("flujo", "tipo", "estado_hoy", "queda_en", "disparador", "cumplen_hoy",
                                          "por_envio", "cota")} == {
        "flujo": f"att1/aviso_{form}_prueba", "tipo": "prender", "estado_hoy": "cohorte", "queda_en": "prendido",
        "disparador": form, "cumplen_hoy": 38, "por_envio": 38, "cota": "como_maximo"}
    assert summary["audiencia"] == {"es": "clienta", "producto": "att1"}
    [request] = [body for function, body, _ in emulator.requests if function == "read_flow_audience_reach_v1"]
    assert (request["p_flow_id"], request["p_version"]) == (AVISO_ID, 1)
    # Sin evento no se piden los conteos del evento.
    assert "read_flow_engine_native_trigger_counts_v1" not in [function for function, _, _ in emulator.requests]
    page = _simulation_summary(simulation)
    assert "no se pudo armar" not in page and "--sin-simulacion" not in page
    assert "Hoy cumplen la audiencia <strong>38</strong> personas" in page


def test_turning_off_an_audience_flow_says_who_stops_receiving_it(image: None) -> None:
    emulator = WithReach(_reach(state="prendido"))
    emulator.flows.append(_aviso_flow("cada", state="prendido"))
    client, _ = _client(emulator)
    saved = _propose(client, [_prender("cada", "apagar")], {AVISO_ID: {"active_version": 1}})
    out = _validate(client, saved.json()["data"]["proposal_id"]).json()["data"]
    simulation = out["preview"]["simulacion"]
    assert simulation["status"] == "built", simulation
    assert (simulation["summary"]["queda_en"], simulation["summary"]["dejarian_de_recibirlo"]) == ("apagado", 38)
    assert "dejarían de recibir el próximo envío" in _simulation_summary(simulation, turns_off=True)


def test_without_the_reach_reading_the_preview_says_why(image: None) -> None:
    emulator = WithReach(None)  # una base sin 20261116000100: PostgREST contesta 403
    emulator.flows.append(_aviso_flow())
    client, _ = _client(emulator)
    saved = _propose(client, [_prender()], {AVISO_ID: {"active_version": 1}})
    out = _validate(client, saved.json()["data"]["proposal_id"]).json()["data"]
    assert out["preview"]["simulacion"] == {"status": "failed", "reason_code": "simulation_audience_reach_unavailable"}


# ----------------------------------------------------------------- lo que se pide y lo que se valida


def test_the_bridge_and_the_operator_ask_for_the_same_reading() -> None:
    audience = {"flows": [_aviso_flow()]}
    asked = {"audience_reach": {"product_ref": "att1", "flow_id": AVISO_ID, "version": 1}}
    cases = [
        (audience, [_prender()], asked),
        (audience, [_prender(tipo="apagar")], asked),
        # E-668: una version de un flujo de audiencia con la misma audiencia tambien la cuenta (antes,
        # simulation_native_flow_counts_invalid). Lo demas de la version: test_e668_audience_version_simulation.py.
        (audience, [{**_prender(), "tipo": "version", "definicion": _published()}], asked),
        (audience, [_prender(), _prender()], {"simulation_unavailable": "simulation_native_flow_mixed"}),
        ({"flows": [{**_aviso_flow(), "active_version": None}]}, [_prender()],
         {"simulation_unavailable": "simulation_native_flow_counts_invalid"}),
    ]
    for store, items, expected in cases:
        assert native_flow_counts_request(store, items) == expected, items
        assert operator_script._native_flow_counts_request(store, items) == expected, items  # noqa: SLF001


def test_the_simulation_refuses_a_reach_that_is_not_of_the_flow() -> None:
    world = world_from_store({"flows": [_aviso_flow()]})
    [item] = parse_items([_prender()])
    assert build_native_audience_reach_simulation(_reach(), [item], world=world)["status"] == "built"
    invalid = {"status": "failed", "reason_code": "simulation_audience_reach_invalid"}
    for wrong in (_reach(flow_id="0000000e-0000-4000-8000-0000000a7622"), _reach(version=2),
                  _reach(product_ref="otro"), _reach(status="prospecta"), _reach(contract="flow-engine-v2"),
                  _reach(matching=-1), _reach(matching=40), _reach(read_capped="no"), {**_reach(), "telefono": 1}):
        assert build_native_audience_reach_simulation(wrong, [item], world=world) == invalid, wrong
    mixed = {"status": "failed", "reason_code": "simulation_native_flow_mixed"}
    assert build_native_audience_reach_simulation(_reach(), [item], world=None) == mixed


def test_the_cap_per_send_labels_and_repetition_make_it_a_maximum() -> None:
    world = world_from_store({"flows": [_aviso_flow(
        "fecha", para={"es": "prospecta", "formulario_en": "14d", "etiquetas": ["black"]},
        entra={"permiso": "formulario"}, ritmo={"por_hora": 50, "por_ocurrencia": 30})]})
    [item] = parse_items([_prender("fecha")])
    built = build_native_audience_reach_simulation(
        _reach(status="prospecta", already_customer=2, matching=36), [item], world=world)
    summary = built["summary"]
    assert (summary["cumplen_hoy"], summary["por_envio"], summary["ya_clientas"]) == (36, 30, 2)
    assert summary["por_que"] == ["etiquetas_de_chatwoot", "repeticion", "tope_por_ocurrencia"]
    assert summary["audiencia"]["formulario_en_dias"] == 14
    page = _simulation_summary(built)
    assert "como máximo a <strong>30</strong> personas" in page
    assert "las etiquetas las mira la lista en Chatwoot" in page


def test_the_validator_uses_the_reach(image: None) -> None:
    store = {"flows": [_aviso_flow()]}
    out = validate_proposal({
        "native_audience_reach": _reach(),
        "instance": {"tenant_ref": "lancemos", "ally_ref": "att1", "products": ["att1"],
                     "time_zone": "America/Mexico_City"},
        "store": store,
        "proposal": {"id": "00000000-0000-4000-8000-000000000622", "revision": 1, "items": [_prender()],
                     "draft_base": {AVISO_ID: {"active_version": 1}}, "purpose": "pasarlo a todos",
                     "requested_by_kind": "automation_expert"},
        "catalog": None,
    }, environ={"SETTER_VERSION": "1.40.1", "GIT_SHA": "0386f8b"})
    assert out["preview"]["simulacion"]["status"] == "built"
    assert out["preview"]["simulacion"]["summary"]["cumplen_hoy"] == 38


# ----------------------------------------------------------------- el comando del operador


class ReachDocker(FakeDocker):
    def run(self, argv, stdin=""):
        if "read_flow_audience_reach_v1" in stdin:
            self.calls.append((list(argv), stdin))
            return __import__("subprocess").CompletedProcess(list(argv), 0, __import__("json").dumps(_reach()) + "\n", "")
        return super().run(argv, stdin)


def test_the_operator_reads_the_reach_of_an_audience_flow() -> None:
    docker = ReachDocker(engine=True)
    ops = operator_script.Ops(operator_script.Config.from_env({**_ENV}), docker=docker, terminal=FakeTerminal(),
                              out=io.StringIO(), code=lambda: "abc123")
    assert ops.read_engine_inputs([_prender()], {"flows": [_aviso_flow()]}) == {"native_audience_reach": _reach()}
    [stdin] = [stdin for _, stdin in docker.calls if "read_flow_audience_reach_v1" in stdin]
    assert "begin transaction read only" in stdin
    assert "read_flow_engine_native_trigger_counts_v1" not in "".join(stdin for _, stdin in docker.calls)
