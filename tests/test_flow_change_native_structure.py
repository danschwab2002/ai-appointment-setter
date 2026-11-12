"""P28: el dueno le cambia con el AE la estructura a un flujo nativo, sin operador (E-357, flow-change-cycle-v1 1.12.0).

Dan lo aprobo el 30/10 (D9) y el 08/11 fijo que la primera entrega son los 5 flujos de un mensaje: carrito, pago
rechazado con variantes, primer contacto, seguimiento con cupon y bienvenida. Estas pruebas recorren los cinco:

* lo que el motor nativo ejecuta se abre (la repeticion, la prioridad, las ramas y las etiquetas, dentro de la regla 14);
* lo que no ejecuta se bloquea con un motivo que el AE le puede leer al dueno, sin campos, sin la palabra operador;
* el legado (ATT1) sigue como estaba: fuera de la 1b, el mismo codigo de siempre;
* la pagina pide el segundo paso y dice que cambia, y el resumen de la simulacion lo nombra.
"""

from __future__ import annotations

import copy
import re
from dataclasses import replace
from typing import Any

import pytest
from test_flow_validator import (
    CONFIRMED_STEP,
    NATIVE_ID,
    WELCOME_ID,
    _form,
    _follow_up,
    _native_change,
    _native_welcome,
    _routed,
    _running,
    _silence,
    check,
    live,
    native_catalog,
    native_flow,
    published,
    run_new,
    version_item,
    welcome,
    world,
)

from bridge import flow_approval, flow_definition as fd
from bridge.flow_change_simulation import build_native_flow_simulation
from bridge.flow_definition import ROUTE_MOTOR, ROUTE_NOT_READ, FlowWorld, ProposalItem, WorldFlow
from bridge.flow_engine import (
    NATIVE_UNSUPPORTED_PERMISSION,
    NATIVE_UNSUPPORTED_PRIORITY,
    NATIVE_UNSUPPORTED_REPEAT,
    NATIVE_UNSUPPORTED_TRIGGER,
)
from bridge.flow_validator import structural_changes


def _cart() -> dict[str, Any]:
    return welcome(nombre="Carrito", dispara={"evento": "carrito"}, entra={"permiso": "formulario"},
                   pasos=[{"esperar": "90m"}, copy.deepcopy(CONFIRMED_STEP)], termina={"si": ["compra"], "vence": "1d"})


def _refusal() -> dict[str, Any]:
    send = copy.deepcopy(CONFIRMED_STEP)
    branches = {"condicion": {"dato": "tipo_de_rechazo", "es": ["sin_saldo"]}, "entonces": [copy.deepcopy(send)],
                "si_no": [copy.deepcopy(send)]}
    return welcome(nombre="Pago rechazado", dispara={"evento": "pago_fallido"}, entra={"permiso": "formulario"},
                   pasos=[{"esperar": "20m"}, branches], termina={"si": ["compra"], "vence": "1d"})


FIVE = {"carrito": _cart, "pago_rechazado": _refusal, "primer_contacto": _form, "seguimiento": _silence,
        "bienvenida": welcome}
# Los que cuentan la repeticion y la prioridad: todos menos el seguimiento, que corre por su propio barrido.
FOUR = ("carrito", "pago_rechazado", "primer_contacto", "bienvenida")
OUT_OF_SCOPE = {"change_outside_1b_scope"}
STRUCTURE_BLOCKS = {
    "native_change_exits", "native_change_repeat_silence", "native_change_priority_silence",
    "native_change_priority_wins", "native_change_priority_ref", "native_change_entry", "native_change_pool",
    "native_change_other",
}
TECHNICAL = re.compile(r"\boperador\b|motor nativo|\bnativo\b|\blegado\b|\bscope\b|[a-z]+_[a-z]+|\{\{")


def codes(findings: list[Any]) -> set[str]:
    return {finding.code for finding in findings}


# El otro flujo al que se puede esperar: nativo, con un evento distinto del flujo que cambia (dos nativos con el
# mismo evento no se pueden tener prendidos a la vez: ``same_event_native_flows``).
OTHER_KEY = "otro_flujo"
OTHER_ID = "0000000b-0000-4000-8000-0000000b0009"


def _other(name: str) -> WorldFlow:
    definition = welcome() if name != "bienvenida" else _cart()
    return WorldFlow(flow_id=OTHER_ID, product_ref="att1", flow_key=OTHER_KEY, state="prendido", active_version=1,
                     generation=1, versions={1: published({**definition, "flujo": OTHER_KEY})}, native=True)


def _published_by_the_ae(name: str, change: Any, *extra: WorldFlow) -> Any:
    """La version de un nativo como la valida la publicacion: fase publish, catalogo real, motor en marcha."""
    base = native_flow(FIVE[name]())
    draft = fd.as_draft(base.versions[1])
    change(draft)
    return check([version_item(draft, "nativo", flow_id=NATIVE_ID)], actor="automation_expert",
                 in_world=world(base, _other(name), *extra), catalog=native_catalog(), instance=_running())


def _waits_for(name: str, ref: str, *extra: WorldFlow) -> Any:
    return _published_by_the_ae(name, lambda d: d["prioridad"]["no_arranca_mientras"].append(ref), *extra)


REF_PATH = "/0/definicion/prioridad/no_arranca_mientras/0"


@pytest.mark.parametrize("name", sorted(FIVE))
def test_the_five_definitions_are_publishable_as_they_are(name: str) -> None:
    result = _published_by_the_ae(name, lambda d: None)
    assert not result.blocks, [(f.code, f.path) for f in result.blocks]


@pytest.mark.parametrize("name", sorted(FOUR))
@pytest.mark.parametrize("change", [
    lambda d: d["topes"]["repeticion"].update(otra_despues_de="nunca"),
    lambda d: d["topes"]["repeticion"].update(una_por="persona_y_oferta", otra_despues_de="30d"),
    lambda d: d["prioridad"]["no_arranca_mientras"].append(OTHER_KEY),
])
def test_the_four_flows_with_an_event_open_repetition_and_priority(name: str, change: Any) -> None:
    result = _published_by_the_ae(name, change)
    assert not result.blocks, [(f.code, f.path) for f in result.blocks]


@pytest.mark.parametrize("name", sorted(FIVE))
def test_every_flow_can_be_renamed_and_nobody_changes_who_enters(name: str) -> None:
    def rename(d: dict[str, Any]) -> None:
        d["nombre"] = "Otro nombre"
    result = _published_by_the_ae(name, rename)
    assert not result.blocks, [(f.code, f.path) for f in result.blocks]
    def who(d: dict[str, Any]) -> None:
        d["entra"]["permiso"] = "conversacion" if name != "seguimiento" else "formulario"
    assert "native_change_entry" in codes(_native_change(FIVE[name](), who))


@pytest.mark.parametrize("name", sorted(FIVE))
def test_no_flow_adds_an_exit_nor_says_it_in_technical_words(name: str) -> None:
    def add(d: dict[str, Any]) -> None:
        d["termina"]["si"] = [*d["termina"].get("si", []), "ficha_vencida"]
    findings = [f for f in _native_change(FIVE[name](), add) if f.code == "native_change_exits"]
    assert len(findings) == 1
    assert not TECHNICAL.search(findings[0].reason), findings[0].reason
    assert not TECHNICAL.search(findings[0].alternative or ""), findings[0].alternative


@pytest.mark.parametrize(("name", "said"), [
    ("bienvenida", "manda su mensaje y termina"),
    ("seguimiento", "compra o contesta"),
    ("primer_contacto", "se da de baja"),
    ("carrito", "que conteste, por ejemplo, no lo corta"),
])
def test_each_flow_says_its_own_exits(name: str, said: str) -> None:
    def add(d: dict[str, Any]) -> None:
        d["termina"]["si"] = ["otra"]
    [finding] = [f for f in _native_change(FIVE[name](), add) if f.code == "native_change_exits"]
    assert said in finding.reason


def test_the_follow_up_does_not_change_repetition_nor_priority() -> None:
    def repeat(d: dict[str, Any]) -> None:
        d["topes"]["repeticion"]["otra_despues_de"] = "24h"
    def priority(d: dict[str, Any]) -> None:
        d["prioridad"]["no_arranca_mientras"].append("bienvenida")
    assert "native_change_repeat_silence" in codes(_native_change(_silence(), repeat))
    assert "native_change_priority_silence" in codes(_native_change(_silence(), priority))


def _publish_blocks(definition: dict[str, Any], change: Any, key: str = "nativo") -> list[Any]:
    """Lo que dice la regla 14 en fase publish, sin mirar el catalogo real."""
    base = native_flow(definition, key=key)
    draft = fd.as_draft(base.versions[1])
    change(draft)
    others = world(base, _native_welcome("bienvenida", WELCOME_ID, "prendido"))
    result = check([version_item(draft, key, flow_id=NATIVE_ID)], actor="automation_expert", in_world=others,
                   catalog=native_catalog())
    return [f for f in result.blocks if f.code == "motor_nativo:forma"]


@pytest.mark.parametrize(("name", "change", "said"), [
    # E-483: el carrito manda hasta tres mensajes; con mas de uno, corta si la persona contesta.
    ("carrito", lambda d: d["pasos"].extend([{"esperar": "1h"}, copy.deepcopy(CONFIRMED_STEP)]),
     "corta cuando la persona contesta"),
    ("carrito", lambda d: d["pasos"].append({"esperar": "1h"}), "después del último mensaje no hace nada"),
    ("bienvenida", lambda d: d["pasos"].insert(0, {"condicion": {"dato": "tipo_de_rechazo", "es": ["banco"]},
                                                    "entonces": [copy.deepcopy(CONFIRMED_STEP)],
                                                    "si_no": [copy.deepcopy(CONFIRMED_STEP)]}), "pago rechazado"),
    ("pago_rechazado", lambda d: d["pasos"].append({"esperar": "1h"}), "al final del flujo"),
])
def test_what_the_engine_does_not_run_is_said_in_business_words(name: str, change: Any, said: str) -> None:
    blocks = _publish_blocks(FIVE[name](), change)
    assert blocks, "no volvio frenado"
    assert any(said in (f.reason or "") for f in blocks), [f.reason for f in blocks]
    for finding in blocks:
        assert not TECHNICAL.search(finding.reason or ""), finding.reason


def test_the_operator_keeps_the_technical_text_of_rule_14() -> None:
    base = native_flow(_cart())
    draft = fd.as_draft(base.versions[1])
    draft["pasos"].extend([{"esperar": "1h"}, copy.deepcopy(CONFIRMED_STEP)])
    result = check([version_item(draft, "nativo", flow_id=NATIVE_ID)], in_world=world(base), catalog=native_catalog())
    assert "El motor nativo de esta versión no ejecuta eso" in " ".join(f.reason or "" for f in result.blocks)


def test_a_flow_that_only_the_legacy_runs_does_not_stop_a_flow() -> None:
    def wait_for_cart(d: dict[str, Any]) -> None:
        d["prioridad"]["no_arranca_mientras"].append("carrito")
    assert "native_change_priority_ref" in codes(_native_change(_form(), wait_for_cart))


@pytest.mark.parametrize("name", sorted(FOUR))
def test_a_flow_cannot_wait_for_the_follow_up_because_it_has_no_run_to_compare(name: str) -> None:
    # Se publicaria y nunca frenaria: la corrida del seguimiento no esta en flow_engine_runs (D-P22-1).
    follow_up = WorldFlow(flow_id="0000000b-0000-4000-8000-0000000b000a", product_ref="att1", flow_key="seguimiento_cupon",
                          state="prendido", active_version=1, generation=1,
                          versions={1: published({**_silence(), "flujo": "seguimiento_cupon"})}, native=True)
    [finding] = [f for f in _waits_for(name, "seguimiento_cupon", follow_up).blocks
                 if f.code == "native_change_priority_ref"]
    assert finding.path == REF_PATH
    assert "con este no cambiaría nada" in finding.reason and "El seguimiento" in finding.reason
    assert not TECHNICAL.search(finding.reason), finding.reason


def test_a_flow_cannot_wait_for_itself() -> None:
    assert ("native_change_priority_ref", REF_PATH) in [(f.code, f.path) for f in _waits_for("carrito", "nativo").blocks]


def test_a_flow_cannot_wait_for_a_flow_of_another_product() -> None:
    elsewhere = WorldFlow(flow_id="0000000b-0000-4000-8000-0000000b000b", product_ref="att9", flow_key="carrito_b",
                          state="prendido", active_version=1, generation=1,
                          versions={1: published({**_cart(), "flujo": "carrito_b"})}, native=True)
    assert ("native_change_priority_ref", REF_PATH) in [(f.code, f.path)
                                                        for f in _waits_for("carrito", "att9/carrito_b", elsewhere).blocks]


def test_a_reference_with_the_product_in_the_text_would_never_match_in_the_base() -> None:
    # La base compara flow_key con el texto tal cual: «att1/otro_flujo» nunca es igual a «otro_flujo».
    assert ("native_change_priority_ref", REF_PATH) in [(f.code, f.path)
                                                        for f in _waits_for("carrito", f"att1/{OTHER_KEY}").blocks]


@pytest.mark.parametrize("route", [ROUTE_MOTOR, ROUTE_NOT_READ])
def test_a_flow_can_wait_for_one_the_engine_route_runs(route: str) -> None:
    routed = replace(live("carrito"), route_executor=route)
    result = _waits_for("primer_contacto", "carrito", routed)
    assert "native_change_priority_ref" not in codes(list(result.blocks))


def test_an_exit_change_is_said_once_even_when_the_engine_would_not_run_it_either() -> None:
    # E-483: sumar contesta ya se publica en el carrito (la lee el nativo de varios toques); al seguimiento
    # (el silencio) no se le cambian las salidas.
    def add(d: dict[str, Any]) -> None:
        d["termina"]["si"].remove("contesta")
    blocks = _published_by_the_ae("seguimiento", add).blocks
    assert [f.code for f in blocks if f.path == "/0/definicion/termina/si"] == ["native_change_exits"]


def test_a_new_follow_up_without_both_exits_is_told_to_put_them() -> None:
    # Sacar el cambio no arregla nada: el seguimiento necesita las dos salidas.
    definition = {**_silence(), "termina": {"si": ["compra"]}}
    [finding] = [f for f in run_new(definition, "seguimiento_nuevo", instance=_running())
                 if f.code == "motor_nativo:forma" and f.path.endswith("/termina/si")]
    assert "compra o contesta" in finding.reason and "Poné las dos salidas" in finding.alternative
    assert "hoy no se puede" not in finding.alternative
    assert not TECHNICAL.search(finding.reason) and not TECHNICAL.search(finding.alternative)


def test_the_legacy_keeps_the_same_scope_block() -> None:
    # Lo mismo que hoy, con un carrito de la ruta del motor (ATT1): su estructura sigue siendo del operador.
    from test_flow_validator import draft_of, live

    for field, change in (("/0/definicion/topes/repeticion", lambda d: d["topes"]["repeticion"].update(otra_despues_de="nunca")),
                          ("/0/definicion/prioridad/no_arranca_mientras",
                           lambda d: d["prioridad"]["no_arranca_mientras"].append("descuento"))):
        result = check([version_item(draft_of("carrito", change), "carrito")], actor="automation_expert",
                       in_world=world(live("carrito")))
        assert ("change_outside_1b_scope", field) in [(f.code, f.path) for f in result.blocks]


# ------------------------------------------------------------------ structural_changes, la misma cuenta


def test_structural_changes_ignores_what_the_ae_changes_in_any_flow_and_the_name() -> None:
    active = native_flow(_cart()).versions[1]
    draft = copy.deepcopy(active)
    draft["nombre"] = "Otro"
    draft["pasos"][0] = {"esperar": "2h"}
    draft["topes"]["por_dia"] = 77
    assert structural_changes(active, draft) == ()
    draft["topes"]["repeticion"]["otra_despues_de"] = "nunca"
    draft["pasos"].append({"etiquetar": "cliente"})
    assert structural_changes(active, draft) == ("pasos", "topes/repeticion")


# ----------------------------------------------------------------- la pagina


def _flow_row(definition: dict[str, Any], state: str) -> dict[str, Any]:
    return {"flow_id": NATIVE_ID, "state": state, "native": True, "active_version": 1,
            "versions": {"1": native_flow(definition, state=state).versions[1]}}


def _version(definition: dict[str, Any]) -> dict[str, Any]:
    return {"producto": "att1", "flujo": "nativo", "flow_id": NATIVE_ID, "tipo": "version", "definicion": definition,
            "en_curso": "seguir"}


@pytest.mark.parametrize("state", ["cohorte", "prendido"])
def test_a_structure_change_asks_the_second_step_even_in_the_test_group(state: str) -> None:
    row = _flow_row(_cart(), state)
    draft = copy.deepcopy(row["versions"]["1"])
    draft["topes"]["repeticion"]["otra_despues_de"] = "nunca"
    reasons = flow_approval.second_step_reasons([_version(draft)], [row])
    assert ("att1/nativo", "cambia_estructura:topes/repeticion") in reasons
    assert len(reasons) == (2 if state == "prendido" else 1)


def test_changing_a_wait_of_a_flow_in_the_test_group_is_still_one_click() -> None:
    row = _flow_row(_cart(), "cohorte")
    draft = copy.deepcopy(row["versions"]["1"])
    draft["pasos"][0] = {"esperar": "2h"}
    draft["nombre"] = "Otro nombre"
    assert flow_approval.second_step_reasons([_version(draft)], [row]) == []


def test_going_back_to_a_version_with_another_structure_asks_the_second_step_too() -> None:
    row = _flow_row(_cart(), "cohorte")
    older = copy.deepcopy(row["versions"]["1"])
    older["topes"]["repeticion"]["otra_despues_de"] = "nunca"
    row["versions"]["2"] = older
    item = {"producto": "att1", "flujo": "nativo", "flow_id": NATIVE_ID, "tipo": "volver", "version": 2, "en_curso": "seguir"}
    assert flow_approval.second_step_reasons([item], [row]) == [("att1/nativo", "cambia_estructura:topes/repeticion")]


def test_the_legacy_keeps_its_clicks_on_a_structure_change() -> None:
    row = {**_flow_row(_cart(), "prendido"), "native": False}
    draft = copy.deepcopy(row["versions"]["1"])
    draft["topes"]["repeticion"]["otra_despues_de"] = "nunca"
    assert flow_approval.second_step_reasons([_version(draft)], [row]) == [("att1/nativo", "llega_a_todos")]


def test_the_second_step_says_what_changes_and_who_it_reaches() -> None:
    reasons = [("att1/nativo", "cambia_estructura:topes/repeticion")]
    preview = {"simulacion": {"status": "built", "summary": {"personas": 12}}, "en_curso": {"recorridos": 0}}
    html = flow_approval._render_second_step({"validation": {"preview": preview}, "fingerprint": "f"}, proposal_id="p",
                                             csrf="c", confirm="x", reasons=reasons, others=[])
    assert "cada cuánto" in html and "12 personas" in html and "quienes ya lo recibieron" in html
    assert "cohorte de prueba o corta" not in html
    assert "cambia_estructura" not in html


def _native_version_summary(**extra: Any) -> dict[str, Any]:
    return {"status": "built", "summary": {"disparos": 30, "personas": 20, "tipo": "version", "estado_hoy": "prendido",
                                           "queda_en": "prendido", "alcance": "todos", **extra}}


def test_the_page_says_the_structure_change_in_words_only_when_there_is_one() -> None:
    html = flow_approval._simulation_summary(_native_version_summary(cambia_estructura=["topes/repeticion"]))
    assert "Cambia la estructura del flujo" in html and "cada cuánto" in html and "topes/repeticion" not in html
    assert "Cambia la estructura" not in flow_approval._simulation_summary(_native_version_summary())


@pytest.mark.parametrize("code", ["motor_nativo:forma", NATIVE_UNSUPPORTED_TRIGGER, NATIVE_UNSUPPORTED_PERMISSION,
                                  NATIVE_UNSUPPORTED_REPEAT, NATIVE_UNSUPPORTED_PRIORITY])
def test_a_publish_the_base_refuses_is_said_in_words(code: str) -> None:
    html = flow_approval._render_done({"hecho": code})
    assert "No se publicó" in html and "No se hizo" not in html and code not in html


# ------------------------------------------------------------------ la simulacion


def _counts(event: str) -> dict[str, Any]:
    return {"contract": "flow-engine-v2", "trigger": event, "product_ref": "att1", "since": "2026-11-01",
            "until": "2026-11-08", "events": 30, "people": 20}


def _sim_item(definition: dict[str, Any]) -> ProposalItem:
    return ProposalItem(producto="att1", flujo="nativo", flow_id=NATIVE_ID, tipo="version", definicion=definition, en_curso="seguir")


def test_the_simulation_names_the_structure_change_only_when_there_is_one() -> None:
    flow = native_flow(_cart())
    same = copy.deepcopy(flow.versions[1])
    same["pasos"][0] = {"esperar": "2h"}
    sim = build_native_flow_simulation(_counts("carrito"), [_sim_item(same)], world=FlowWorld([flow]))
    assert sim["status"] == "built" and "cambia_estructura" not in sim["summary"]
    changed = copy.deepcopy(flow.versions[1])
    changed["topes"]["repeticion"]["otra_despues_de"] = "nunca"
    sim = build_native_flow_simulation(_counts("carrito"), [_sim_item(changed)], world=FlowWorld([flow]))
    assert sim["summary"]["cambia_estructura"] == ["topes/repeticion"] and sim["summary"]["alcance"] == "todos"
