"""E-657 (2026-11-17): una propuesta con dos flujos nuevos de disparadores distintos se simula entera.

Medido en Maribel el 16/11 (v1.40.3, Forever Young): el Automation Expert armo una propuesta con dos
flujos nuevos (el carrito y la bienvenida, propuesta ``1fd1a5e0-fb7b-4e75-be9b-a64d4bd66b71``) y la
vista previa quedo ``failed`` con ``simulation_new_flow_mixed``: ``_simulation_inputs``
(``src/bridge/flow_change_api.py``) y su gemela del operador (``scripts/flow_change_operator.py``,
``read_engine_inputs``) cortaban toda propuesta con un ``nuevo`` y mas de un item, y
``build_new_flow_simulation`` exigia un solo item. La pagina de aprobacion no publica sin simulacion
(decision 10 del 2026-10-27), asi que instalar un cliente era publicar los flujos de a uno.

Lo nuevo: si todos los items son ``nuevo`` (del mismo producto), cada uno se simula por separado, con su
lectura, y el resumen de la propuesta es ``{"flujos": [<resumen de cada uno>]}`` con el sha256 de todo.
Si uno no se puede simular, la propuesta queda ``failed`` con el motivo de ese. Mezclar un ``nuevo`` con
otro tipo de item sigue en ``simulation_new_flow_mixed``. La base solo mira ``status`` y
``summary_sha256`` (``_flow_change_approve``, 20261021000100): no cambia nada de la base ni del contrato
con el agente.

El otro hallazgo (publicar una propuesta deja vieja a cualquier otra abierta) es por diseno del contrato
(``flow-change-cycle-v1``, «Las dos bases»): la base de la validacion es la de toda la instancia. Queda la
prueba de lo que pediria la base por flujo, en ``xfail`` estricto, para el dia que se decida.
"""

from __future__ import annotations

import pytest
from test_flow_change_api_app import AUTH, PREFIX, SLACK, PostgrestEmulator, _client
from test_flow_change_new_flow import CLIENT_OK, COUNTS, PURPOSE, _welcome

from bridge.flow_approval import _simulation_summary
from bridge.flow_change_operator import world_from_store
from bridge import flow_change_simulation as sim
from bridge.flow_change_simulation import summary_sha256
from bridge.flow_definition import parse_items


@pytest.fixture
def image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "1.40.3")
    monkeypatch.setenv("GIT_SHA", "f4d6100bebb86d8e8949b424b1e6db01e523dd8d")


def _item(flujo: str, evento: str) -> dict:
    definition = _welcome(nombre=f"Prueba {flujo}", dispara={"evento": evento})
    if evento != "compra":
        # Sin compra no hay permiso de compra ni etiqueta de clienta (reglas 3 y 5 del validador).
        definition.update(entra={"permiso": "formulario"}, pasos=definition["pasos"][:1])
    return {"producto": "att1", "flujo": flujo, "tipo": "nuevo", "prender": "cohorte",
            "definicion": definition, "ok_del_cliente": CLIENT_OK}


def _counts(trigger: str, events: int, people: int) -> dict:
    return {"contract": "flow-engine-v2", "product_ref": "att1", "trigger": trigger,
            "since": "2026-11-09T18:00:00Z", "until": "2026-11-16T18:00:00Z", "events": events, "people": people}


def test_a_proposal_with_two_new_flows_of_different_triggers_is_simulated(image: None) -> None:
    emulator = PostgrestEmulator()
    emulator.new_flow_counts = dict(COUNTS)
    client, _ = _client(emulator)
    saved = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json={
        "items": [_item("bienvenida", "compra"), _item("recupero", "carrito")], "base": {},
        "purpose": PURPOSE, "requested_by": SLACK})
    assert saved.status_code == 201, saved.text
    proposal_id = saved.json()["data"]["proposal_id"]
    validated = client.post(f"{PREFIX}/flow-changes/{proposal_id}/validate", headers=AUTH,
                            json={"revision": 1, "requested_by": SLACK})
    assert validated.status_code == 200, validated.text
    out = validated.json()["data"]
    assert out["status"] == "validada" and out["blocks"] == []
    assert "simulation_not_built" not in [gap["code"] for gap in out["preview"]["huecos"]]
    simulation = out["preview"]["simulacion"]
    assert simulation["status"] == "built", simulation
    flows = simulation["summary"]["flujos"]
    assert [(flow["flujo_nuevo"], flow["disparador"]) for flow in flows] == [
        ("att1/bienvenida", "compra"), ("att1/recupero", "carrito")]
    assert simulation["summary_sha256"] == summary_sha256(simulation["summary"])
    counts = [body["p_trigger"] for function, body, _ in emulator.requests
              if function == "read_flow_engine_native_trigger_counts_v1"]
    assert counts == ["compra", "carrito"]
    # La pagina dice la simulacion de cada flujo, con su nombre.
    page = _simulation_summary(simulation)
    assert "att1/bienvenida" in page and "att1/recupero" in page and "no se pudo armar" not in page


def test_each_new_flow_is_simulated_with_its_own_reading() -> None:
    items = parse_items([_item("bienvenida", "compra"), _item("recupero", "carrito")])
    built = sim.build_new_flows_simulation([{"new_flow_counts": _counts("compra", 5, 4)},
                                        {"new_flow_counts": _counts("carrito", 9, 7)}], items)
    assert built["status"] == "built"
    assert [(flow["disparos"], flow["personas"]) for flow in built["summary"]["flujos"]] == [(5, 4), (9, 7)]
    # Las lecturas cruzadas no cierran: cada una tiene que ser la de su flujo.
    crossed = sim.build_new_flows_simulation([{"new_flow_counts": _counts("carrito", 9, 7)},
                                          {"new_flow_counts": _counts("compra", 5, 4)}], items)
    assert crossed == {"status": "failed", "reason_code": "simulation_new_flow_counts_invalid"}
    # Una lectura que falto deja la propuesta sin simulacion, con el motivo de ese flujo.
    missing = sim.build_new_flows_simulation([{"new_flow_counts": _counts("compra", 5, 4)},
                                          {"simulation_unavailable": "flow_engine_unavailable"}], items)
    assert missing == {"status": "failed", "reason_code": "flow_engine_unavailable"}
    # Dos nuevos con el mismo evento: cada uno nombra al otro (el motor abre un solo caso por evento).
    twins = parse_items([_item("bienvenida", "compra"), _item("otra", "compra")])
    shared = sim.build_new_flows_simulation([{"new_flow_counts": _counts("compra", 5, 4)}] * 2, twins)
    assert [flow["comparte_el_evento_con"] for flow in shared["summary"]["flujos"]] == [
        [{"flujo": "att1/otra", "estado": "nuevo"}], [{"flujo": "att1/bienvenida", "estado": "nuevo"}]]


def test_a_single_new_flow_keeps_the_summary_of_before() -> None:
    [item] = parse_items([_item("bienvenida", "compra")])
    built = sim.build_new_flows_simulation([{"new_flow_counts": _counts("compra", 2, 2)}], [item])
    assert "flujos" not in built["summary"] and built["summary"]["flujo_nuevo"] == "att1/bienvenida"


_FLOW_A = "0000000a-0000-4000-8000-00000000e657"
_FLOW_B = "0000000b-0000-4000-8000-00000000e657"


@pytest.mark.xfail(strict=True, reason="E-657, solo diseno: la base de la validacion cubre la instancia entera "
                   "por contrato (flow-change-cycle-v1, «Las dos bases», reglas 8 y 10)")
def test_the_base_of_a_proposal_only_names_the_flows_it_touches() -> None:
    def flow(flow_id: str, key: str) -> dict:
        return {"flow_id": flow_id, "product_ref": "att1", "flow_key": key, "state": "cohorte", "native": True,
                "active_version": 1, "generation": 3, "versions": {"1": _welcome(dispara={"evento": "compra"})}}

    world = world_from_store({"database_at": "2026-11-17T03:00:00Z", "products": ["att1"],
                              "flows": [flow(_FLOW_A, "bienvenida"), flow(_FLOW_B, "recupero")]})
    items = parse_items([{"producto": "att1", "flujo": "bienvenida", "flow_id": _FLOW_A, "tipo": "apagar"}])
    assert set(world.base_for(items)) == {_FLOW_A}
