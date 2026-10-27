"""Dos flujos nativos con el mismo evento: que no se pierda uno en silencio (E-139).

El dato (E-137, medido en att1-db y en el codigo de la v1.20.2, igual en la
v1.20.3): con el mismo evento para la misma persona el motor abre un solo caso
(``recovery_cases.abandonment_event_id`` es unico). Admite los flujos en orden
de ``flow_id`` (``admit_flow_engine_event_v1``, 20261024000200:963), asi que
gana el de ``flow_id`` menor, que para un flujo nuevo es un UUID al azar, y el
otro queda con ``evento_con_otro_flujo`` (20261024000200:680-685; lo prueba
``validate_flow_engine_native.mjs``, «un evento, un caso»).

Hasta esta rama el validador lo decia solo como la advertencia
``same_trigger_unrelated``, solo para un item con definicion (un ``prender`` de
cohorte a todos no la miraba) y la apagaba con una ``prioridad`` que el motor
no respeta para el mismo evento. La simulacion de un ``nuevo`` contaba los
mensajes «si fuera para todos» sin mirar al otro flujo.

Lo que fija esta rama:

* (D1, D2) un flujo nativo que la propuesta toca y deja ``prendido`` o en
  ``cohorte`` con el mismo evento que otro nativo del mismo producto, tambien
  ``prendido`` o en ``cohorte``, es el bloqueo ``same_event_native_flows``. Vale
  igual en la cohorte (decision de Dan del 2026-10-27): ahi la persona tambien
  recibe uno solo, al azar. Una ``prioridad`` no lo levanta (D3);
* la simulacion de un ``nuevo`` (D4) nombra los flujos con los que comparte el
  evento, en ``comparte_el_evento_con``.

Los flujos del legado (el carrito, el pago rechazado, la ficha, el primer
contacto) no entran: los admite otro camino, con su propio evento, y un caso
del carrito convive con uno nativo (``validate_flow_engine_native.mjs``,
«lo de hoy sin cambios»). Para ellos sigue ``same_trigger_unrelated``.

Los catalogos son los capturados de siempre (la bandeja 11 de ATT1 del
2026-10-21, ver test_flow_validator.py); no hay ningun borde externo nuevo.
"""

from __future__ import annotations

import copy
from typing import Any

from test_flow_validator import (
    CLIENT_OK,
    CONFIRMED_STEP,
    check,
    found,
    native_catalog,
    new_item,
    published,
    welcome,
    world,
)

from bridge import flow_definition as fd
from bridge.flow_change_simulation import build_new_flow_simulation
from bridge.flow_definition import FlowWorld, ProposalItem, WorldFlow

WELCOME_ID = "0000000b-0000-4000-8000-0000000b0001"
SECOND_ID = "0000000b-0000-4000-8000-0000000b0002"
BLOCK = "same_event_native_flows"
OLD_WARNING = "same_trigger_unrelated"


def _native(key: str, flow_id: str, state: str, definition: dict[str, Any] | None = None) -> WorldFlow:
    """Un flujo nativo con su v1 publicada (la bienvenida, si no se dice otra cosa)."""
    written = definition if definition is not None else welcome()
    written = {**written, "flujo": key}
    return WorldFlow(flow_id=flow_id, product_ref="att1", flow_key=key, state=state, active_version=1,
                     generation=1, versions={1: published(written)}, native=True)


def _prender(key: str, flow_id: str, *, a: str | None = None) -> ProposalItem:
    return ProposalItem(producto="att1", flujo=key, flow_id=flow_id, tipo="prender", a=a)


def _apagar(key: str, flow_id: str) -> ProposalItem:
    return ProposalItem(producto="att1", flujo=key, flow_id=flow_id, tipo="apagar")


def _version(key: str, flow_id: str, definition: dict[str, Any]) -> ProposalItem:
    return ProposalItem(producto="att1", flujo=key, flow_id=flow_id, tipo="version",
                        definicion={**definition, "flujo": key}, en_curso="seguir", ok_del_cliente=CLIENT_OK)


def _codes(items: list[ProposalItem], flows: list[WorldFlow], *, phase: str = "publish") -> tuple[set, set]:
    result = check(items, in_world=world(*flows), catalog=native_catalog(), phase=phase)
    return {f.code for f in result.blocks}, {f.code for f in result.warnings}


# ----------------------------------------------------------------- a todos: bloqueo


def test_turning_on_for_all_a_native_flow_that_shares_the_event_with_one_on_for_all_is_blocked() -> None:
    # La bienvenida esta prendida para todos y la segunda, probada en la cohorte, se pasa a todos.
    flows = [_native("bienvenida", WELCOME_ID, "prendido"), _native("bienvenida_ae", SECOND_ID, "cohorte")]
    blocks, _ = _codes([_prender("bienvenida_ae", SECOND_ID)], flows)
    assert BLOCK in blocks


def test_the_block_points_at_the_item_and_says_how_to_get_out() -> None:
    flows = [_native("bienvenida", WELCOME_ID, "prendido"), _native("bienvenida_ae", SECOND_ID, "cohorte")]
    result = check([_prender("bienvenida_ae", SECOND_ID)], in_world=world(*flows), catalog=native_catalog())
    [finding] = [f for f in result.blocks if f.code == BLOCK]
    assert finding.path == "/0"
    assert finding.kind == "ciclo" and finding.rule is None
    assert finding.alternative and "pausar" in finding.alternative.lower()


def test_a_declared_priority_does_not_lift_the_block_because_the_engine_does_not_honor_it() -> None:
    # D3: con el mismo evento el motor admite por flow_id y no mira prioridad.
    second = welcome(prioridad={"no_arranca_mientras": ["bienvenida"]})
    flows = [_native("bienvenida", WELCOME_ID, "prendido"),
             _native("bienvenida_ae", SECOND_ID, "cohorte", second)]
    blocks, _ = _codes([_prender("bienvenida_ae", SECOND_ID)], flows)
    assert BLOCK in blocks


def test_a_new_version_of_a_flow_on_for_all_with_the_event_of_another_on_for_all_is_blocked() -> None:
    # Las dos prendidas con eventos distintos no chocan... hasta que una version cambia de evento no se
    # puede (trigger_change_is_another_flow). El caso real: las dos ya estan prendidas (de antes de esta
    # regla) y se publica una version de una: el bloqueo dice que la propuesta deja el choque.
    flows = [_native("bienvenida", WELCOME_ID, "prendido"), _native("bienvenida_ae", SECOND_ID, "prendido")]
    draft = fd.as_draft(flows[1].versions[1])
    blocks, _ = _codes([_version("bienvenida_ae", SECOND_ID, draft)], flows)
    assert BLOCK in blocks


def test_turning_off_the_other_in_the_same_proposal_clears_the_block() -> None:
    flows = [_native("bienvenida", WELCOME_ID, "prendido"), _native("bienvenida_ae", SECOND_ID, "cohorte")]
    blocks, _ = _codes([_apagar("bienvenida", WELCOME_ID), _prender("bienvenida_ae", SECOND_ID)], flows)
    assert BLOCK not in blocks


def test_a_paused_or_off_other_flow_does_not_compete() -> None:
    # Pausado no abre caso (flujo_pausado sale antes del chequeo del evento); apagado ni se admite.
    for state in ("pausado", "apagado"):
        flows = [_native("bienvenida", WELCOME_ID, state), _native("bienvenida_ae", SECOND_ID, "cohorte")]
        blocks, _ = _codes([_prender("bienvenida_ae", SECOND_ID)], flows)
        assert BLOCK not in blocks, state


def test_the_block_also_stops_the_draft() -> None:
    # El agente se entera al guardar, no recien al validar.
    flows = [_native("bienvenida", WELCOME_ID, "prendido"), _native("bienvenida_ae", SECOND_ID, "cohorte")]
    blocks, _ = _codes([_prender("bienvenida_ae", SECOND_ID)], flows, phase="draft")
    assert BLOCK in blocks


# ----------------------------------------------------------------- en la cohorte: tambien bloqueo (D2)


def test_a_new_flow_in_the_cohort_with_the_event_of_one_on_for_all_is_blocked() -> None:
    # El caso del AE creando un flujo en Maribel al lado de uno que ya corre.
    flows = [_native("bienvenida", WELCOME_ID, "prendido")]
    result = check([new_item(welcome(), "bienvenida_ae")], actor="automation_expert",
                   in_world=world(*flows), catalog=native_catalog(), draft_base={})
    blocks = [f for f in result.blocks if f.code == BLOCK]
    assert len(blocks) == 1 and blocks[0].path == "/0/definicion/dispara"
    # La vieja advertencia no se repite para el mismo par.
    assert OLD_WARNING not in {f.code for f in result.warnings}


def test_two_flows_in_the_cohort_with_the_same_event_are_blocked() -> None:
    flows = [_native("bienvenida", WELCOME_ID, "cohorte")]
    result = check([new_item(welcome(), "bienvenida_ae")], actor="automation_expert",
                   in_world=world(*flows), catalog=native_catalog(), draft_base={})
    assert BLOCK in {f.code for f in result.blocks}


def test_back_to_the_cohort_next_to_one_on_for_all_is_blocked() -> None:
    flows = [_native("bienvenida", WELCOME_ID, "prendido"), _native("bienvenida_ae", SECOND_ID, "pausado")]
    blocks, _ = _codes([_prender("bienvenida_ae", SECOND_ID, a="cohorte")], flows)
    assert BLOCK in blocks


def test_turning_on_for_all_next_to_one_in_the_cohort_is_blocked() -> None:
    flows = [_native("bienvenida", WELCOME_ID, "cohorte"), _native("bienvenida_ae", SECOND_ID, "pausado")]
    blocks, _ = _codes([_prender("bienvenida_ae", SECOND_ID)], flows)
    assert BLOCK in blocks


def test_another_event_or_another_product_does_not_compete() -> None:
    other_event = _native("bienvenida", WELCOME_ID, "prendido")
    other_event_def = copy.deepcopy(other_event.versions[1])
    other_event_def["dispara"] = {"evento": "carrito"}
    flows = [WorldFlow(flow_id=WELCOME_ID, product_ref="att1", flow_key="bienvenida", state="prendido",
                       active_version=1, generation=1, versions={1: other_event_def}, native=True),
             _native("bienvenida_ae", SECOND_ID, "cohorte")]
    blocks, _ = _codes([_prender("bienvenida_ae", SECOND_ID)], flows)
    assert BLOCK not in blocks


def test_legacy_flows_keep_the_old_warning() -> None:
    # Dos carritos del legado con el mismo disparador: la advertencia de siempre, sin bloqueo.
    from test_flow_validator import _same_trigger

    codes = found(_same_trigger(False))
    assert OLD_WARNING in codes and BLOCK not in codes


def test_one_native_flow_alone_is_clean() -> None:
    flows = [_native("bienvenida_ae", SECOND_ID, "cohorte")]
    blocks, _ = _codes([_prender("bienvenida_ae", SECOND_ID)], flows)
    assert BLOCK not in blocks
    alone = check([new_item(welcome(), "bienvenida_ae")], actor="automation_expert", in_world=world(),
                  catalog=native_catalog(), draft_base={})
    assert not found(alone) & {BLOCK}


# ----------------------------------------------------------------- la simulacion de un nuevo


COUNTS = {"contract": "flow-engine-v2", "since": "2026-10-20T15:00:00Z", "until": "2026-10-27T15:00:00Z",
          "events": 3, "people": 3, "product_ref": "att1", "trigger": "compra"}


def test_the_new_flow_simulation_names_the_flows_that_share_its_event() -> None:
    item = new_item(welcome(), "bienvenida_ae")
    flows = FlowWorld([_native("bienvenida", WELCOME_ID, "prendido"),
                       _native("otra", "0000000b-0000-4000-8000-0000000b0003", "apagado")])
    built = build_new_flow_simulation(COUNTS, [item], world=flows)
    assert built["status"] == "built"
    assert built["summary"]["comparte_el_evento_con"] == [{"flujo": "att1/bienvenida", "estado": "prendido"}]


def test_the_new_flow_simulation_without_competitors_is_the_same_as_before() -> None:
    item = new_item(welcome(), "bienvenida_ae")
    before = build_new_flow_simulation(COUNTS, [item])
    assert build_new_flow_simulation(COUNTS, [item], world=FlowWorld([])) == before
    assert "comparte_el_evento_con" not in before["summary"]


def test_the_welcome_step_is_the_captured_utility_template() -> None:
    # Guarda contra un cambio de los helpers: la definicion de prueba es la nativa de siempre.
    assert welcome()["pasos"][0] == CONFIRMED_STEP
