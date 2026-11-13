"""E-483 (C9 del mapa «el AE maneja todos los flujos», 2026-11-12): la vista previa y el dibujo de varios toques.

Medido sobre lo que ya habia (v1.35.0): la simulacion de un nativo (``build_new_flow_simulation`` y
``build_native_flow_simulation``) cuenta disparos y personas del evento con
``read_flow_engine_native_trigger_counts_v1``, no recorridos: no decia nada por toque. El dibujo de la pagina de
aprobacion (``_render_steps``) y el del Automation Expert (``draw_flow``, el lienzo) ya muestran los toques, las
esperas y las ramas de un nativo de varios toques (C1 y C3): lo fijan las pruebas de abajo.

Lo nuevo: con mas de un envio en algun camino, el resumen suma ``toques``: por toque, su camino, la espera y
cuantos llegan. Del primer toque de un flujo sin ramas, «como maximo» las personas que conto la lectura; de un
toque siguiente o de una rama, ``sin_base`` con el motivo, porque la lectura no tiene con que estimarlo. Con un
solo toque el resumen es el de antes (lo que corre en ATT1 y Maribel es de un toque).

Los datos: el carrito de tres toques de ATT1 traducido de su configuracion real
(``tests/fixtures/flow_simulation/att1_cart_three_touches_a2_36h.json``, ``vigente``), el pago rechazado completo
de C3 (``test_e483_native_condition_after_send.FULL``, con las plantillas capturadas de Maribel), los conteos de
Maribel del 31/10 (``test_flow_change_native_simulation.MARIBEL_COUNTS``) y la lectura de ``/flows`` de ATT1
del 21/10 (``tests/fixtures/automation_expert_draw/att1_flows_20261021.json``).
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from test_automation_expert_draw import DIBUJO, FIXTURE as DRAW_FIXTURE
from test_e483_native_condition_after_send import FULL as PAYMENT_FAILURE_FULL
from test_flow_change_native_simulation import MARIBEL_COUNTS

from bridge.flow_approval import _render_steps, _simulation_summary
from bridge.flow_change_operator import world_from_store
from bridge.flow_change_simulation import (
    TOUCH_AT_MOST,
    TOUCH_BRANCH,
    TOUCH_NEXT,
    TOUCH_NO_BASIS,
    build_native_flow_simulation,
    build_new_flow_simulation,
    native_touches,
)
from bridge.flow_definition import parse_items

ROOT = Path(__file__).resolve().parents[1]
CART_FIXTURE = ROOT / "tests" / "fixtures" / "flow_simulation" / "att1_cart_three_touches_a2_36h.json"
CART = json.loads(CART_FIXTURE.read_text(encoding="utf-8"))["vigente"][0]["definicion"]
FLOW_ID = "1b000003-0000-4000-8000-0000000c0001"
PAYMENT_ID = "0b3c5f00-0e30-4000-8000-0000000e4833"
# Los conteos de Maribel del 31/10 con el producto y el evento de cada flujo de aca.
CART_COUNTS = {**MARIBEL_COUNTS, "product_ref": "att1", "trigger": "carrito"}
PAYMENT_COUNTS = {**MARIBEL_COUNTS, "product_ref": "maribel", "trigger": "pago_fallido"}


def _new_item(definition: dict[str, Any], product: str = "att1", flow: str = "carrito") -> Any:
    [item] = parse_items([{"producto": product, "flujo": flow, "tipo": "nuevo", "definicion": definition,
                          "prender": "cohorte"}])
    return item


def _one_touch(definition: dict[str, Any]) -> dict[str, Any]:
    one = copy.deepcopy(definition)
    one["pasos"] = one["pasos"][:1]
    return one


# -- La simulacion ---------------------------------------------------------------------------


def test_the_three_touch_cart_says_who_reaches_each_touch_and_what_has_no_basis() -> None:
    built = build_new_flow_simulation(CART_COUNTS, [_new_item(CART)])
    assert built["status"] == "built"
    people = MARIBEL_COUNTS["people"]
    assert built["summary"]["toques"] == [
        {"toque": 1, "espera": None, "plantilla": "att1_carrito_descuento_01", "llegan": people, "cota": TOUCH_AT_MOST},
        {"toque": 2, "espera": "24h", "plantilla": "att1_carrito_dudas_01", "llegan": None, "cota": TOUCH_NO_BASIS,
         "por_que": TOUCH_NEXT},
        {"toque": 3, "espera": "48h", "plantilla": "att1_ultima_renovacion_01", "llegan": None,
         "cota": TOUCH_NO_BASIS, "por_que": TOUCH_NEXT},
    ]
    # Nada inventado: el unico numero es el que conto la lectura.
    assert [touch["llegan"] for touch in built["summary"]["toques"] if touch["llegan"] is not None] == [people]


def test_one_touch_keeps_the_summary_of_before() -> None:
    built = build_new_flow_simulation(CART_COUNTS, [_new_item(_one_touch(CART))])
    assert built["status"] == "built"
    assert "toques" not in built["summary"]
    assert native_touches(_one_touch(CART), 59) is None


def test_the_payment_failure_branches_say_each_path_and_have_no_basis_per_branch() -> None:
    touches = native_touches(PAYMENT_FAILURE_FULL, 59)
    assert touches is not None
    assert [(t["camino"], t["toque"], t["espera"], t["cota"], t["por_que"]) for t in touches] == [
        (path, touch, "15m" if touch == 1 else "24h", TOUCH_NO_BASIS, TOUCH_BRANCH if touch == 1 else TOUCH_NEXT)
        for path in (1, 2, 3, 4) for touch in (1, 2)]
    assert [t["condicion"] for t in touches if t["toque"] == 1] == [
        {"dato": "descuento_previo", "es": ["si"]}, {"dato": "rechazos", "es": ["varios"]},
        {"dato": "tipo_de_rechazo", "es": ["sin_saldo"]}, None]
    assert all(t["llegan"] is None for t in touches)


def _world(definition: dict[str, Any], *, product: str, flow: str, flow_id: str, state: str = "cohorte") -> Any:
    return world_from_store({"flows": [{
        "flow_id": flow_id, "product_ref": product, "flow_key": flow, "state": state, "active_version": 1,
        "generation": 2, "changed_at": "2026-10-31T18:00:00Z", "changed_by_kind": "operador",
        "native": True, "versions": {"1": definition}}]})


def test_a_version_of_a_native_multitouch_flow_carries_its_touches() -> None:
    world = _world(_one_touch(CART), product="att1", flow="carrito", flow_id=FLOW_ID)
    [item] = parse_items([{"producto": "att1", "flujo": "carrito", "flow_id": FLOW_ID, "tipo": "version",
                           "definicion": CART, "en_curso": "seguir"}])
    built = build_native_flow_simulation(CART_COUNTS, [item], world=world)
    assert built["status"] == "built"
    assert [touch["toque"] for touch in built["summary"]["toques"]] == [1, 2, 3]
    # Apagar no manda: no lleva toques.
    [off] = parse_items([{"producto": "att1", "flujo": "carrito", "flow_id": FLOW_ID, "tipo": "apagar"}])
    multi = _world(CART, product="att1", flow="carrito", flow_id=FLOW_ID)
    assert "toques" not in build_native_flow_simulation(CART_COUNTS, [off], world=multi)["summary"]


def test_the_page_says_it_per_touch_without_inventing() -> None:
    built = build_new_flow_simulation(CART_COUNTS, [_new_item(CART)])
    html = _simulation_summary(built)
    assert "Cuántos llegan a cada mensaje" in html
    assert "Mensaje 1: como máximo <strong>59</strong> personas" in html
    assert "Mensaje 2 (24 horas después): sin base para estimarlo: depende de quién compra, contesta o se le " \
           "vence antes" in html
    branched = build_new_flow_simulation(PAYMENT_COUNTS, [_new_item(PAYMENT_FAILURE_FULL, "maribel", "pago_fallido")])
    assert branched["status"] == "built"
    page = _simulation_summary(branched)
    assert "Camino 1, mensaje 1 (15 minutos después): sin base para estimarlo: el conteo no separa" in page
    assert "como máximo" not in page
    # Un toque: la pagina de antes.
    assert "Cuántos llegan" not in _simulation_summary(build_new_flow_simulation(CART_COUNTS,
                                                                                 [_new_item(_one_touch(CART))]))


# -- El dibujo -------------------------------------------------------------------------------


def test_the_approval_page_draws_the_touches_the_waits_and_the_branches() -> None:
    cart = _render_steps(CART["pasos"], "/pasos", {})
    assert cart.count('class="message"') == 3 and cart.count("Espera ") == 2
    branched = _render_steps(PAYMENT_FAILURE_FULL["pasos"], "/pasos", {})
    assert branched.count('class="message"') == 8 and branched.count("Espera ") == 5
    assert branched.count("Si se cumple") == 3 and branched.count("Si no") == 3


def _drawn(flow_key: str, definition: dict[str, Any]) -> dict[str, Any]:
    capture = json.loads(DRAW_FIXTURE.read_text(encoding="utf-8"))
    [flow] = [f for f in capture["data"]["flows"] if f["flow_key"] == flow_key]
    flow["definition"] = copy.deepcopy(definition)
    html, _, errors = DIBUJO.armar_dibujo(capture, flow_key)
    assert errors == [] and html is not None
    match = re.search(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S)
    assert match is not None
    return json.loads(match.group(1).replace("<\\/", "</"))["flujos"][0]["versiones"]["hoy"]


def test_the_drawing_of_the_ae_shows_the_touches_the_waits_and_the_branches() -> None:
    cart = _drawn("carrito", CART)
    assert [node["tipo"] for node in cart["nodos"]] == [
        "disparador", "plantilla", "espera", "plantilla", "espera", "plantilla", "fin"]
    branched = _drawn("pago_fallido", PAYMENT_FAILURE_FULL)
    kinds = [node["tipo"] for node in branched["nodos"]]
    assert kinds.count("plantilla") == 8 and kinds.count("condicion") == 3 and kinds.count("espera") == 5
    labels = [edge["etiqueta"] for edge in branched["aristas"]]
    assert labels.count("sí") == 3 and labels.count("no") == 3
