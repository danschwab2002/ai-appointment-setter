"""E-668: una version (o un volver) de un flujo con disparo por audiencia arma su simulacion.

El defecto (Maribel v1.40.4, 2026-11-17 23:30Z): la propuesta ``c95d3185`` cambia solo
``/topes/repeticion/otra_despues_de`` (7d -> 20h) de ``aviso_manual_prueba`` (``dispara: {manual: {}}``, en
cohorte). La ruta del AE la valido con ``simulacion: {"status": "failed", "reason_code":
"simulation_native_flow_counts_invalid"}``: ``native_flow_counts_request`` solo pedia la audiencia
(``read_flow_audience_reach_v1``, E-622) para un ``prender`` o un ``apagar``; para una ``version`` buscaba el
evento del flujo, y un disparo por audiencia no tiene. Sin simulacion construida la pagina no publica, y el
operador tampoco (su validador no conocia las capacidades del motor: ver test_e668_operator_runtime.py).

Ahora (D1) una version o un volver de un flujo de audiencia con la misma audiencia que la version activa (el
mismo ``para`` y el mismo ``producto``) se simula con la audiencia de hoy de la version activa. Si la audiencia
cambia, ``simulation_audience_version_audience_changed``: la lectura de la base cuenta el ``para`` de la version
activa y no hay una que cuente el de una definicion sin publicar.

Dato real: ``tests/fixtures/automation_expert_change/maribel_audience_version_c95d3185_20261117.json``
(maribel_db, solo lectura, 2026-11-18 03:35Z).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from test_e622_audience_reach_simulation import AVISO_ID, WithReach, _aviso_flow, _reach
from test_flow_change_api_app import _client
from test_flow_change_new_flow import _propose, _validate
from test_flow_change_operator_script import script as operator_script

from bridge.flow_approval import _simulation_summary
from bridge.flow_change_operator import validate_proposal, world_from_store
from bridge.flow_change_simulation import (
    AUDIENCE_VERSION_CHANGED,
    build_native_audience_reach_simulation,
    native_flow_counts_request,
)
from bridge.flow_definition import parse_items

FIXTURE = json.loads((Path(__file__).parent / "fixtures/automation_expert_change/"
                      "maribel_audience_version_c95d3185_20261117.json").read_text(encoding="utf-8"))
FLOW = FIXTURE["store_flow"]
FLOW_ID = FLOW["flow_id"]
STORE = {"flows": [FLOW]}
ITEMS = FIXTURE["proposal"]["items"]


def _maribel_reach(**changes: object) -> dict:
    """La respuesta de read_flow_audience_reach_v1 para la version activa de aviso_manual_prueba (forma de E-622)."""
    reach = {"contract": "flow-audience-reach-v1", "flow_id": FLOW_ID, "version": 1, "product_ref": "maribel",
             "state": "cohorte", "status": "prospecta", "measured_at": "2026-11-18T03:40:00.000+00:00",
             "candidates": 12, "opted_out": 1, "already_customer": 2, "matching": 9, "without_contact": 0,
             "ambiguous": 0, "read_capped": False}
    reach.update(changes)
    return reach


def _with_para(items: list, **para: object) -> list:
    changed = copy.deepcopy(items)
    changed[0]["definicion"]["para"] = {**changed[0]["definicion"]["para"], **para}
    return changed


# ----------------------------------------------------------------- el dato real


def test_the_stored_validation_is_the_failure_this_fixes() -> None:
    preview = FIXTURE["stored_validation"]["preview"]
    assert preview["simulacion"] == {"status": "failed", "reason_code": "simulation_native_flow_counts_invalid"}
    assert [change["diff"] for change in preview["cambios"]] == [
        [{"ruta": "/topes/repeticion/otra_despues_de", "antes": "7d", "despues": "20h"}]]
    assert FLOW["state"] == "cohorte" and FLOW["versions"]["1"]["dispara"] == {"manual": {}}


def test_the_bridge_and_the_operator_ask_for_the_audience_of_the_active_version() -> None:
    asked = {"audience_reach": {"product_ref": "maribel", "flow_id": FLOW_ID, "version": 1}}
    changed = {"simulation_unavailable": AUDIENCE_VERSION_CHANGED}
    to_event = copy.deepcopy(ITEMS)
    to_event[0]["definicion"]["dispara"] = {"evento": "compra"}
    back = [{"producto": "maribel", "flujo": "aviso_manual_prueba", "flow_id": FLOW_ID, "tipo": "volver",
             "version": 1}]
    cases = [
        (ITEMS, asked),  # antes: simulation_native_flow_counts_invalid
        (back, asked),
        (_with_para(ITEMS, formulario_en="7d"), changed),
        (_with_para(ITEMS, es="clienta"), changed),
        (to_event, changed),
    ]
    for items, expected in cases:
        assert native_flow_counts_request(STORE, items) == expected, items
        assert operator_script._native_flow_counts_request(STORE, items) == expected, items  # noqa: SLF001


def test_the_version_of_maribel_builds_with_the_audience_of_today() -> None:
    world = world_from_store(STORE)
    items = parse_items(ITEMS)
    built = build_native_audience_reach_simulation(_maribel_reach(), items, world=world)
    assert built["status"] == "built", built
    summary = built["summary"]
    assert {key: summary[key] for key in ("flujo", "tipo", "estado_hoy", "queda_en", "alcance", "disparador",
                                          "cumplen_hoy", "por_envio", "ya_clientas", "con_baja")} == {
        "flujo": "maribel/aviso_manual_prueba", "tipo": "version", "estado_hoy": "cohorte", "queda_en": "cohorte",
        "alcance": "cohorte", "disparador": "manual", "cumplen_hoy": 9, "por_envio": 9, "ya_clientas": 2,
        "con_baja": 1}
    assert summary["audiencia"] == {"es": "prospecta", "producto": "maribel", "formulario_en_dias": 30}
    assert "dejarian_de_recibirlo" not in summary
    # La repeticion es estructura (P28, E-357): la pagina lo nombra y pide el segundo paso, como en un evento.
    assert summary["cambia_estructura"] == ["topes/repeticion"]
    page = _simulation_summary(built)
    assert "no se pudo armar" not in page and "--sin-simulacion" not in page
    assert "Hoy cumplen la audiencia <strong>9</strong> personas" in page
    assert "solo le llega a la cohorte de prueba" in page
    assert "Para todos" not in page
    assert "Cambia la estructura del flujo" in page


def test_the_simulation_does_not_believe_the_request() -> None:
    world = world_from_store(STORE)
    changed = build_native_audience_reach_simulation(_maribel_reach(), parse_items(_with_para(ITEMS, es="clienta")),
                                                     world=world)
    assert changed == {"status": "failed", "reason_code": AUDIENCE_VERSION_CHANGED}
    invalid = {"status": "failed", "reason_code": "simulation_audience_reach_invalid"}
    for wrong in (_maribel_reach(version=2), _maribel_reach(status="clienta"), _maribel_reach(product_ref="att1")):
        assert build_native_audience_reach_simulation(wrong, parse_items(ITEMS), world=world) == invalid, wrong


def test_a_return_to_the_same_audience_names_the_version() -> None:
    flow = copy.deepcopy(FLOW)
    flow["versions"]["2"] = {**copy.deepcopy(flow["versions"]["1"]), "version": 2}
    flow["active_version"] = 2
    world = world_from_store({"flows": [flow]})
    back = parse_items([{"producto": "maribel", "flujo": "aviso_manual_prueba", "flow_id": FLOW_ID, "tipo": "volver",
                         "version": 1, "en_curso": "seguir"}])
    built = build_native_audience_reach_simulation(_maribel_reach(version=2), back, world=world)
    assert built["status"] == "built", built
    assert (built["summary"]["tipo"], built["summary"]["vuelve_a_version"]) == ("volver", 1)


@pytest.mark.parametrize(("state", "text"), [("prendido", "Esta versión le llegaría a quienes entren"),
                                            ("apagado", "no le llega a nadie")])
def test_the_page_says_who_the_flow_reaches_today(state: str, text: str) -> None:
    flow = {**copy.deepcopy(FLOW), "state": state}
    built = build_native_audience_reach_simulation(_maribel_reach(state=state), parse_items(ITEMS),
                                                   world=world_from_store({"flows": [flow]}))
    assert built["status"] == "built", built
    assert text in _simulation_summary(built)


# ----------------------------------------------------------------- por la ruta del agente (la app real)


@pytest.fixture
def image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "1.40.4")
    monkeypatch.setenv("GIT_SHA", "a499d860fab5c0f8555e48af01ef05a70cd6f218")


def test_the_agent_validates_a_version_of_an_audience_flow_with_a_built_simulation(image: None) -> None:
    """El mismo cambio que c95d3185 (la repeticion de 7d a 20h), en la instancia de prueba de la app."""
    emulator = WithReach(_reach())
    flow = _aviso_flow()
    emulator.flows.append(flow)
    client, _ = _client(emulator)
    definition = copy.deepcopy(flow["versions"]["1"])
    definition.pop("version", None)
    definition["estado"] = "borrador"
    definition["topes"] = {**definition.get("topes", {}),
                           "repeticion": {"una_por": "persona", "otra_despues_de": "20h"}}
    item = {"producto": "att1", "flujo": "aviso_manual_prueba", "flow_id": AVISO_ID, "tipo": "version",
            "en_curso": "seguir", "definicion": definition}
    saved = _propose(client, [item], {AVISO_ID: {"active_version": 1}})
    assert saved.status_code == 201, saved.text
    validated = _validate(client, saved.json()["data"]["proposal_id"])
    assert validated.status_code == 200, validated.text
    data = validated.json()["data"]
    simulation = data["preview"]["simulacion"]
    # Antes: {"status": "failed", "reason_code": "simulation_native_flow_counts_invalid"}.
    assert simulation["status"] == "built", simulation
    assert (simulation["summary"]["tipo"], simulation["summary"]["alcance"]) == ("version", "cohorte")
    [request] = [body for function, body, _ in emulator.requests if function == "read_flow_audience_reach_v1"]
    assert (request["p_flow_id"], request["p_version"]) == (AVISO_ID, 1)
    assert "simulation_failed" not in [gap["code"] for gap in data["preview"]["huecos"]]


# ----------------------------------------------------------------- el validador del operador, con el runtime


def _operator_payload(runtime: dict | None) -> dict:
    instance = {"tenant_ref": "lancemos", "ally_ref": "maribel", "products": ["maribel"],
                "time_zone": "America/Mexico_City"}
    if runtime is not None:
        instance["runtime"] = runtime
    proposal = FIXTURE["proposal"]
    return {"native_audience_reach": _maribel_reach(), "instance": instance, "store": STORE, "catalog": None,
            "proposal": {key: proposal[key] for key in ("id", "revision", "items", "draft_base", "purpose",
                                                        "requested_by_kind")}}


ENV = {"SETTER_VERSION": "1.40.4", "GIT_SHA": "a499d860fab5c0f8555e48af01ef05a70cd6f218"}


def test_without_the_runtime_the_operator_blocks_the_audience_flow() -> None:
    """Lo que vio Dan el 16/11 con operador-de-flujos.sh: sin runtime, el motor nativo no ejecuta la audiencia."""
    out = validate_proposal(_operator_payload(None), environ=ENV)
    codes = {block["code"] for block in out["result"]["blocks"]}
    assert {"motor_nativo:disparador", "motor_nativo:permiso", "motor_nativo:forma"} <= codes, codes


def test_with_the_runtime_of_the_bridge_the_operator_validates_it_like_the_agent() -> None:
    out = validate_proposal(_operator_payload(FIXTURE["runtime"]), environ=ENV)
    codes = [block["code"] for block in out["result"]["blocks"]]
    assert not [code for code in codes if code.startswith("motor_nativo:")], codes
    assert out["preview"]["simulacion"]["status"] == "built", out["preview"]["simulacion"]
