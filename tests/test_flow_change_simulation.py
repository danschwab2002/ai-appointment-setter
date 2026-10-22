"""La simulacion de la vista previa (flow-engine-v1, «La simulacion de una propuesta»; la T4c).

Fixtures capturados:

* la historia: la captura v3 de ATT1 del 2026-10-21 17:16Z
  (``tests/fixtures/flow_engine/att1_recovery_capture_v3_20261021.json``, la de la T2.2), con la forma que
  devuelve ``read_flow_engine_history_v1``;
* las definiciones del carrito y del pago fallido de ATT1, v3 a v5
  (``tests/fixtures/flow_simulation/att1_definitions_by_scope_version.json``), puestas con la forma de
  ``read_flow_engine_v1`` (seccion 1 del contrato) por ``_read_doc``.

Los numeros de la espera de 6 horas van escritos aca: si el motor cambia, este test lo dice.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from bridge.flow_change_simulation import CAMBIOS_MAX, build_simulation, engine_flows_from_read, summary_sha256
from bridge.flow_definition import parse_items
from bridge.flow_replay import engine_flows_from_definitions

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEFINITIONS = json.loads((FIXTURES / "flow_simulation/att1_definitions_by_scope_version.json").read_text())
HISTORY = json.loads((FIXTURES / "flow_engine/att1_recovery_capture_v3_20261021.json").read_text())
ZONE = DEFINITIONS["instance"]["time_zone"]
CART_ID = DEFINITIONS["flows"]["carrito"]["flow_id"]
FAILED_ID = DEFINITIONS["flows"]["pago_fallido"]["flow_id"]


def _read_doc(active: int = 5) -> dict:
    flows = []
    for key, meta in DEFINITIONS["flows"].items():
        rows = [{"version": int(number), "definition": DEFINITIONS["versions"][number][key],
                 "definition_sha256": "0" * 64, "published_at": "2026-10-13T00:00:00Z",
                 "pool": DEFINITIONS["scope_key"], "in_flight": 0}
                for number in sorted(DEFINITIONS["versions"], key=int) if key in DEFINITIONS["versions"][number]]
        flows.append({"flow_id": meta["flow_id"], "product_ref": "att1", "flow_key": key, "state": "prendido",
                      "active_version": active, "generation": 1,
                      "route": {"executor": "legado", "legacy_kind": meta["legacy_kind"],
                                "legacy_scope_key": DEFINITIONS["scope_key"], "generation": 0,
                                "changed_at": "2026-10-13T00:00:00Z"},
                      "versions": rows})
    return {"contract": "flow-engine-v1", "contract_version": "1.0.0", "database_at": "2026-10-21T17:16:07Z",
            "engine": {"capabilities": "v1", "rules_version": 2}, "flows": flows, "next": None}


def _cart(steps: list | None = None) -> dict:
    definition = copy.deepcopy(DEFINITIONS["versions"]["5"]["carrito"])
    definition.pop("version", None)
    definition["estado"] = "borrador"
    if steps is not None:
        definition["pasos"] = steps
    return definition


def _items(*raw: dict):
    return parse_items(list(raw))


def _version_item(definition: dict, producto: str = "att1") -> dict:
    return {"producto": producto, "flujo": "carrito", "flow_id": CART_ID, "tipo": "version",
            "definicion": definition, "en_curso": "seguir"}


def test_the_engine_read_gives_the_same_versions_as_the_parity_document() -> None:
    assert engine_flows_from_read(_read_doc(), time_zone=ZONE) == engine_flows_from_definitions(DEFINITIONS)


def test_a_six_hour_wait_before_a1_is_simulated_on_the_history_of_att1() -> None:
    wait = _cart([{"esperar": "6h"}, *_cart()["pasos"]])
    simulation = build_simulation(_read_doc(), HISTORY, _items(_version_item(wait)), time_zone=ZONE)
    assert simulation["status"] == "built"
    summary = simulation["summary"]
    assert (summary["disparos"], summary["cambios"]) == (79, 52)
    assert (summary["vigente"]["recibieron"], summary["propuesta"]["recibieron"]) == (60, 53)
    assert summary["propuesta"]["en_curso"] == 6
    assert simulation["summary_sha256"] == summary_sha256(summary)
    assert len(simulation["cambios"]) == CAMBIOS_MAX
    assert {change["flujo"] for change in simulation["cambios"]} == {"carrito"}
    # Entra holgado en la columna de la vista previa (128 KiB).
    assert len(json.dumps(simulation).encode()) < 16 * 1024


def test_the_same_proposal_on_the_same_history_gives_the_same_hash() -> None:
    wait = _cart([{"esperar": "6h"}, *_cart()["pasos"]])
    first = build_simulation(_read_doc(), HISTORY, _items(_version_item(wait)), time_zone=ZONE)
    second = build_simulation(copy.deepcopy(_read_doc()), copy.deepcopy(HISTORY), _items(_version_item(wait)),
                              time_zone=ZONE)
    assert first["summary_sha256"] == second["summary_sha256"]


def test_going_back_to_the_active_version_changes_nothing() -> None:
    item = {"producto": "att1", "flujo": "carrito", "flow_id": CART_ID, "tipo": "volver", "version": 5,
            "en_curso": "seguir"}
    simulation = build_simulation(_read_doc(), HISTORY, _items(item), time_zone=ZONE)
    assert simulation["status"] == "built" and simulation["summary"]["cambios"] == 0


def test_turning_the_cart_off_leaves_it_without_sends() -> None:
    item = {"producto": "att1", "flujo": "carrito", "flow_id": CART_ID, "tipo": "apagar"}
    simulation = build_simulation(_read_doc(), HISTORY, _items(item), time_zone=ZONE)
    assert simulation["status"] == "built"
    # Un flujo sin recorridos no aparece en el resumen.
    assert "carrito" not in simulation["summary"]["propuesta"]["flujos"]
    assert simulation["summary"]["vigente"]["flujos"]["carrito"]["recorridos"] == 59


def _with_phone(history: dict) -> dict:
    broken = copy.deepcopy(history)
    broken["telefono"] = "5215512345678"
    return broken


@pytest.mark.parametrize(("engine", "history", "reason"), [
    (_read_doc(), {"ok": False, "reason_code": "flow_engine_history_too_large"}, "flow_engine_history_too_large"),
    # La forma es la prueba de privacidad: una clave de mas, con un telefono, no pasa.
    (_read_doc(), _with_phone(HISTORY), "flow_engine_history_invalid"),
    (_read_doc(), None, "flow_engine_history_invalid"),
    ({**_read_doc(), "engine": {"capabilities": "v2"}}, HISTORY, "flow_engine_capabilities_unknown"),
    ({**_read_doc(), "contract": "otro"}, HISTORY, "flow_engine_read_invalid"),
    (None, HISTORY, "flow_engine_read_invalid"),
])
def test_what_cannot_be_simulated_is_failed_with_its_reason(engine, history, reason: str) -> None:
    simulation = build_simulation(engine, history, _items(_version_item(_cart())), time_zone=ZONE)
    assert simulation == {"status": "failed", "reason_code": reason}


def test_more_sends_than_the_engine_runs_is_failed_with_the_engine_code() -> None:
    template = _cart()["pasos"][0]
    steps = [step for _ in range(4) for step in ({"esperar": "1h"}, template)]
    simulation = build_simulation(_read_doc(), HISTORY, _items(_version_item(_cart(steps))), time_zone=ZONE)
    assert simulation["status"] == "failed"
    assert simulation["reason_code"].startswith("engine_not_supported")


def test_a_proposal_over_two_products_is_not_simulated() -> None:
    other = {"producto": "otro", "flujo": "pago_fallido", "flow_id": FAILED_ID, "tipo": "apagar"}
    simulation = build_simulation(_read_doc(), HISTORY, _items(_version_item(_cart()), other), time_zone=ZONE)
    assert simulation == {"status": "failed", "reason_code": "simulation_multi_product"}


# El caso de ATT1 del 22/10 19:45Z (v1.16.2): ``volver att1/carrito 1`` con el carrito en la v2. La lectura del
# motor trae la activa y las versiones con recorridos abiertos, y la v1 no estaba: la simulacion daba
# ``proposal_version_unknown`` y la vuelta salio con ``--sin-simulacion``. El almacen trae las dos.
VOLVER = json.loads((FIXTURES / "flow_simulation/att1_volver_carrito_20261022.json").read_text())


def _engine_versions(engine: dict, key: str) -> list[int]:
    return [row["version"] for flow in engine["flows"] if flow["flow_key"] == key for row in flow["versions"]]


def test_going_back_to_a_version_the_engine_read_does_not_bring_fails_without_the_store() -> None:
    assert _engine_versions(VOLVER["engine"], "carrito") == [2]
    assert sorted(next(f for f in VOLVER["store"]["flows"] if f["flow_key"] == "carrito")["versions"]) == ["1", "2"]
    simulation = build_simulation(VOLVER["engine"], HISTORY, _items(VOLVER["item"]), time_zone=ZONE)
    assert simulation == {"status": "failed", "reason_code": "proposal_version_unknown"}


def test_going_back_to_a_version_the_engine_read_does_not_bring_is_simulated_from_the_store() -> None:
    engine = copy.deepcopy(VOLVER["engine"])
    simulation = build_simulation(engine, HISTORY, _items(VOLVER["item"]), time_zone=ZONE, store=VOLVER["store"])
    assert simulation["status"] == "built"
    summary = simulation["summary"]
    # La v2 espera 6 h antes del A1 y la v1 no: es la espera de 6 horas al reves (52 cambios). Uno menos que
    # alli en cada lado porque aca el pago fallido esta apagado, como en ATT1, y su recorrido no recibe.
    assert (summary["disparos"], summary["cambios"]) == (79, 52)
    assert (summary["vigente"]["recibieron"], summary["propuesta"]["recibieron"]) == (52, 59)
    assert "pago_fallido" not in summary["vigente"]["flujos"]
    assert simulation["summary_sha256"] == summary_sha256(summary)
    # La lectura que recibio no se toca.
    assert engine == VOLVER["engine"]


def test_the_store_changes_nothing_for_a_new_version_or_a_return_to_the_active_one() -> None:
    wait = _cart([{"esperar": "6h"}, *_cart()["pasos"]])
    back = {"producto": "att1", "flujo": "carrito", "flow_id": CART_ID, "tipo": "volver", "version": 5,
            "en_curso": "seguir"}
    store = {"flows": [{"flow_id": CART_ID, "versions": {"1": _cart()}}]}
    for item in (_version_item(wait), back):
        assert build_simulation(_read_doc(), HISTORY, _items(item), time_zone=ZONE, store=store) == \
            build_simulation(_read_doc(), HISTORY, _items(item), time_zone=ZONE)


def test_a_return_target_missing_from_the_store_too_is_still_unknown() -> None:
    store = copy.deepcopy(VOLVER["store"])
    for flow in store["flows"]:
        flow["versions"].pop("1", None)
    simulation = build_simulation(VOLVER["engine"], HISTORY, _items(VOLVER["item"]), time_zone=ZONE, store=store)
    assert simulation == {"status": "failed", "reason_code": "proposal_version_unknown"}
