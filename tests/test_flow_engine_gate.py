"""La puerta del motor: el orden de la decision y la primera hora elegible.

Diseno de la T3a, §3.1, §8.1 y §8.2. Fixtures (``tests/fixtures/flow_simulation/``):

* ``gate_cases.json``: un renglon por motivo de la puerta y uno por cada par
  que pide un orden, escritos a mano, con la columna ``legado``: lo que hizo el
  legado en el caso generado del que sale el renglon, copiado de
  ``flow_engine/legacy_recovery_cases.json`` en ``c7fa8ef``. Es el unico
  oraculo de la tabla que no escribio la misma mano que el codigo, hasta la T2.
* ``att1_definitions_by_scope_version.json``: la v5 del carrito.

**El legado que cambia no la pone roja** (§9): si el sha256 de
``legacy_recovery_cases.json`` no es el de la cabecera, la prueba de la copia
se saltea con el motivo (``pytest.skip``, que el resumen cuenta) y no compara.
La columna ``legado`` se compara siempre contra el esperado: esa comparacion no
lee el legado. En el renglon que el legado difiere (``defer``), el ``due_at``
reprogramado de la columna es el oraculo de ``next_at``.

Las lineas de tiempo de ``first_eligible`` usan dobles de ``ControlLine`` y
``BolsaLedger``: el control como pares (hora, estado) y la bolsa como una lista
de horas, contadas estrictamente antes (el libro observado) o con los empates
(el predicho, §2.2).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_engine as engine
from bridge.flow_engine import (
    ENGINE_CAPABILITIES_V1,
    LEGACY_REASON_TO_MOTIVO,
    PREVIEW_CAPABILITIES,
    PROPOSED_TO_CONTRACT_OWNER,
    EngineFlow,
    Firing,
    GateFacts,
    RunView,
    evaluate_gate,
    first_eligible,
    plan_steps,
)
from bridge.flow_parity import EXCEPTIONS_V1, NAMED_CLASSES
from bridge.flow_schedule import Franja, franja_of, local_day

ROOT = Path(__file__).resolve().parents[1]
SIMULATION = ROOT / "tests" / "fixtures" / "flow_simulation"
GATE_CASES: dict[str, Any] = json.loads((SIMULATION / "gate_cases.json").read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = GATE_CASES["casos"]
DEFINITIONS: dict[str, Any] = json.loads(
    (SIMULATION / "att1_definitions_by_scope_version.json").read_text(encoding="utf-8"))
LEGACY_PATH = ROOT / GATE_CASES["_legado"]["archivo"]

ZONE = "America/Mexico_City"
CDMX = franja_of({"zona": ZONE, "desde": "09:00", "hasta": "21:00"})
SQL_AND_DISPATCH = frozenset(item.name for item in EXCEPTIONS_V1 if item.layer in ("sql", "despacho"))
# La lista cerrada de motivos (automation-expert-read-v1.md:170-219), con los
# parametros como los escribe el contrato.
MOTIVOS = frozenset({
    "ya_en_curso", "no_arranca_mientras:<flujo>", "le_gano:<flujo>", "no_entra:permiso", "flujo_pausado",
    "fuera_de_horario", "tope_del_flujo", "tope_por_persona", "identidad:<motivo>", "meta:<regla>", "meta:nombre",
    "envio_incierto", "vencio", "baja", "compro", "ficha_vencida", "tomada_por_el_equipo", "contesto",
    "flujo_apagado", "ya_no_entra", "sin_horario", "completo",
})
# Las diferencias con nombre que puede llevar un renglon cuyo esperado no es el
# del legado: las excepciones del catalogo y las de la paridad de la T1.
NAMED = frozenset(item.name for item in EXCEPTIONS_V1) | {"lo_en_curso_sigue"}
# Las decisiones del legado, por de donde salen, en las de la puerta del motor.
# Un arranque rechazado se reintenta en cada lease hasta vencer: esperar.
LEGACY_DECISIONS = {
    ("reevaluacion", "defer"): "esperar",
    ("reevaluacion", "escalate"): "escalar",
    ("reevaluacion", "expire"): "vencer",
    ("reevaluacion", "cancel"): "cancelar",
    ("reevaluacion", "pause"): "pausar",
    ("arranque", "request_started"): "mandar",
    ("arranque", "rechazado"): "esperar",
    ("final", "cancelled"): "cancelar",
    ("final", "expired"): "vencer",
}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


class LineControl:
    """``ControlLine`` sobre pares (hora, estado): el estado vale desde su hora."""

    def __init__(self, steps: Sequence[tuple[datetime, str]]) -> None:
        self.steps = sorted(steps)

    def state_at(self, when: datetime) -> str:
        position = bisect_right([step for step, _ in self.steps], when) - 1
        if position < 0:
            raise ValueError("control_without_state")
        return self.steps[position][1]

    def next_armed(self, when: datetime) -> datetime | None:
        return next((step for step, state in self.steps if step > when and state == "armed"), None)


ALWAYS_ARMED = LineControl([(at("2026-01-01T00:00:00Z"), "armed")])


class ListLedger:
    """``BolsaLedger`` sobre horas: estrictamente antes (observado) o con los empates (predicho)."""

    def __init__(self, instants: Sequence[datetime] = (), *, ties: bool = False) -> None:
        self.instants = sorted(instants)
        self.ties = ties

    def counts_before(self, when: datetime, zone: str) -> tuple[int, int]:
        count = (bisect_right if self.ties else bisect_left)(self.instants, when)
        seen = self.instants[:count]
        day = local_day(zone, when)
        return count, sum(1 for instant in seen if local_day(zone, instant) == day)


def _set(document: dict[str, Any], pointer: str, value: Any) -> None:
    parts = pointer.lstrip("/").split("/")
    target = document
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def cart_v5(changes: dict[str, Any] | None = None, *, exceptions: frozenset[str] = SQL_AND_DISPATCH) -> EngineFlow:
    definition = copy.deepcopy(DEFINITIONS["versions"]["5"]["carrito"])
    for pointer, value in (changes or {}).items():
        _set(definition, pointer, value)
    return EngineFlow(
        flow_id=DEFINITIONS["flows"]["carrito"]["flow_id"], product_ref="att1", flow_key="carrito", version=5,
        definition=definition, legacy_kind="carrito", fixed_zone=ZONE, pool="att1-recuperacion",
        exceptions=exceptions,
    )


def firing(when: datetime, number: int = 1) -> Firing:
    return Firing(
        source_ref=f"00000000-0000-4000-8000-{number:012d}", event="carrito", at=when,
        received_at=when + timedelta(seconds=1), product_ref="att1", offer_ref="oferta",
        contact_ref=f"00000000-0000-4000-8000-{9000 + number:012d}", intent_refs=frozenset(), permission="formulario",
    )


def run_of(flow: EngineFlow, fired: Firing, sends: tuple[datetime, ...] = (), *, capabilities=ENGINE_CAPABILITIES_V1):
    plan = plan_steps(flow, fired, capabilities=capabilities)
    run = RunView(run_ref="r1", flow=flow.ref, version=flow.version, firing=fired,
                  started_at=fired.at + plan.steps[0].after, sends=sends, closed_at=None)
    return run, plan


def _facts(base: datetime, facts: dict[str, Any]) -> GateFacts:
    def instant(key: str) -> datetime | None:
        return None if key not in facts else base + timedelta(seconds=facts[key])

    return GateFacts(
        blocked_at=instant("baja"), purchase_at=instant("compra"), takeover_at=instant("toma"),
        replied_at=instant("respuesta"), purchase_conflict_open=bool(facts.get("conflicto_de_compra")),
        intent_alive_until=instant("intencion_viva_hasta"), unknown=frozenset(),
    )


def _evaluate(case: dict[str, Any], *, flow: EngineFlow | None = None):
    flow = flow or cart_v5(case["cambios"])
    base = at(case["disparo"])
    run, plan = run_of(flow, firing(base))
    control = LineControl([(base + timedelta(seconds=offset), state) for offset, state in case["control"]])
    ledger = ListLedger([base + timedelta(seconds=offset) for offset in case["autorizaciones"]])
    now = base + timedelta(seconds=case["ahora"])
    return base, evaluate_gate(flow, run, plan, _facts(base, case["hechos"]), now, control=control, bolsa=ledger)


def _expected(case: dict[str, Any], base: datetime) -> tuple[str, str | None, datetime | None]:
    expected = case["esperado"]
    next_at = None if expected["next_at"] is None else base + timedelta(seconds=expected["next_at"])
    return expected["decision"], expected["reason"], next_at


# ---------------------------------------------------------------------------
# 1. gate_cases.json.
# ---------------------------------------------------------------------------

def test_the_table_has_one_row_per_id_and_the_closed_reasons() -> None:
    assert len({case["id"] for case in CASES}) == len(CASES)
    for case in CASES:
        assert case["flujo"] == "carrito_v5", case["id"]
        assert case["esperado"]["decision"] in engine.DECISIONS, case["id"]
        reason = case["esperado"]["reason"]
        assert reason is None or reason in MOTIVOS, case["id"]
        assert all(offset < case["ahora"] for offset in case["autorizaciones"]), case["id"]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_evaluate_gate_gives_the_row(case: dict[str, Any]) -> None:
    base, decision = _evaluate(case)
    assert (decision.decision, decision.reason, decision.next_at) == _expected(case, base)


def test_the_pairs_that_ask_for_an_order_are_in_the_table() -> None:
    ids = {case["id"] for case in CASES}
    assert {"baja_y_compra_a_la_vez", "compra_y_tope", "pausado_y_franja_cerrada"} <= ids


def _legacy_as_engine(legado: dict[str, Any]) -> tuple[str, str | None]:
    decision = LEGACY_DECISIONS[(legado["fuente"], legado["decision"])]
    if decision == "mandar":
        return decision, None
    return decision, LEGACY_REASON_TO_MOTIVO[legado["motivo"]]


def test_each_row_with_legado_is_what_the_legacy_did_or_a_named_difference() -> None:
    with_legacy = [case for case in CASES if "legado" in case]
    assert {case["legado"]["caso"] for case in with_legacy} == {
        "G1", "G2", "G3", "G4", "G5", "G6", "G15", "G18", "G19", "G21", "G23", "G25"}
    named = []
    for case in with_legacy:
        legado = case["legado"]
        expected = (case["esperado"]["decision"], case["esperado"]["reason"])
        if "clase" in legado:
            assert legado["clase"] in NAMED_CLASSES and legado["nombre"] in NAMED, case["id"]
            assert _legacy_as_engine(legado) != expected, case["id"]
            named.append((legado["caso"], legado["clase"], legado["nombre"]))
        else:
            assert _legacy_as_engine(legado) == expected, case["id"]
    assert named == [("G21", "C", "lo_en_curso_sigue")]


def test_the_rescheduled_due_at_of_the_legacy_is_the_next_at() -> None:
    # Donde la reevaluacion del legado difiere la accion, el due_at que escribio es la hora en que el motor
    # vuelve a mirar: los dos en segundos desde el disparo, con la misma geometria que el caso generado.
    deferred = [case for case in CASES if case.get("legado", {}).get("decision") == "defer"]
    assert [case["id"] for case in deferred] == ["fuera_de_horario"]
    for case in deferred:
        assert case["esperado"]["decision"] == "esperar", case["id"]
        assert case["esperado"]["next_at"] == case["legado"]["due_at"], case["id"]


def test_the_legado_column_is_a_copy_of_the_generated_cases() -> None:
    current = hashlib.sha256(LEGACY_PATH.read_bytes()).hexdigest()
    if current != GATE_CASES["_legado"]["sha256"]:
        pytest.skip(f"{GATE_CASES['_legado']['archivo']} cambio desde c7fa8ef (sha256 {current}): la columna "
                    "legado de gate_cases.json sigue con lo de c7fa8ef y no se compara. La 1c la regenera.")
    generated = {case["id"]: case for case in json.loads(LEGACY_PATH.read_text(encoding="utf-8"))["casos"]}
    for case in CASES:
        legado = case.get("legado")
        if legado is None:
            continue
        source = generated[legado["caso"]]
        if legado["fuente"] == "final":
            action = source["final"]["acciones"][legado["accion"]]
            found = (action["estado"], action["terminal_reason"])
        else:
            step = next(step for step in source["pasos"]
                        if step["paso"] == legado["fuente"] and step.get("accion") == legado["accion"])
            found = (step["decision"] if legado["fuente"] == "reevaluacion" else step["resultado"], step["motivo"])
        assert found == (legado["decision"], legado["motivo"]), case["id"]
        assert source["final"]["acciones"][legado["accion"]]["due_at"] == legado["due_at"], case["id"]


def test_the_header_keeps_the_sha256_of_the_legacy_at_c7fa8ef() -> None:
    assert GATE_CASES["_legado"] == {
        "archivo": "tests/fixtures/flow_engine/legacy_recovery_cases.json", "base": "c7fa8ef",
        "sha256": "e34d3815c16c88c3681892f6cd376fc6b5c0cb2f0f6aad751129523308f962ed",
    }


# ---------------------------------------------------------------------------
# 2. La puerta, directo.
# ---------------------------------------------------------------------------

DAY = at("2026-10-14T18:00:00Z")      # 12:00 de CDMX


def test_with_the_v1_and_a_send_done_the_gate_never_says_mandar() -> None:
    flow = cart_v5()
    fired = firing(DAY)
    run, plan = run_of(flow, fired, sends=(DAY,))
    for seconds in (0, 60, 3600, 7200, 40000, 86399):
        for facts in (engine.NO_FACTS, GateFacts(None, None, None, None, False, None, frozenset({"respuesta"}))):
            decision = evaluate_gate(flow, run, plan, facts, DAY + timedelta(seconds=seconds),
                                     control=ALWAYS_ARMED, bolsa=ListLedger())
            assert decision.decision != "mandar", seconds
    decision = evaluate_gate(flow, run, plan, engine.NO_FACTS, DAY + timedelta(hours=1),
                             control=ALWAYS_ARMED, bolsa=ListLedger())
    assert (decision.decision, decision.reason) == ("cancelar", "completo")


TWO_SENDS = {"/pasos": [
    {"plantilla": "att1_carrito_abandonado_01", "idioma": "es_MX", "variables": ["nombre", "producto"]},
    {"esperar": "24h"},
    {"plantilla": "att1_carrito_abandonado_01", "idioma": "es_MX", "variables": ["nombre", "producto"]},
], "/termina/vence": "4d"}


# Las capacidades de antes de la T2.1: un envio por camino, sin espera entre envios.
ONE_SEND_CAPABILITIES = dataclasses.replace(ENGINE_CAPABILITIES_V1, max_sends_per_path=1,
                                            waits_between_sends=False)


def test_a_two_send_plan_with_a_one_send_capability_closes_after_the_first() -> None:
    # El plan de la vista previa, mirado con una capacidad de un envio: la
    # puerta no deja salir el segundo aunque el plan lo tenga.
    flow = cart_v5(TWO_SENDS)
    fired = firing(DAY)
    run, plan = run_of(flow, fired, sends=(DAY,), capabilities=PREVIEW_CAPABILITIES)
    assert len(plan.steps) == 2
    now = DAY + timedelta(hours=24)
    one = evaluate_gate(flow, run, plan, engine.NO_FACTS, now, control=ALWAYS_ARMED, bolsa=ListLedger(),
                        capabilities=ONE_SEND_CAPABILITIES)
    assert (one.decision, one.reason) == ("cancelar", "completo")
    preview = evaluate_gate(flow, run, plan, engine.NO_FACTS, now, control=ALWAYS_ARMED, bolsa=ListLedger(),
                            capabilities=PREVIEW_CAPABILITIES)
    assert preview.decision == "mandar"
    early = evaluate_gate(flow, run, plan, engine.NO_FACTS, now - timedelta(hours=1), control=ALWAYS_ARMED,
                          bolsa=ListLedger(), capabilities=PREVIEW_CAPABILITIES)
    assert (early.decision, early.reason, early.next_at) == ("esperar", None, now)


def test_with_the_v1_of_the_t2_1_the_second_send_goes_out() -> None:
    # Desde la T2.1 la v1 ejecuta hasta tres envios por camino: el segundo sale a su hora,
    # y el plan es el mismo que el de la vista previa.
    flow = cart_v5(TWO_SENDS)
    fired = firing(DAY)
    run, plan = run_of(flow, fired, sends=(DAY,))
    assert plan == run_of(flow, fired, sends=(DAY,), capabilities=PREVIEW_CAPABILITIES)[1]
    assert len(plan.steps) == 2
    now = DAY + timedelta(hours=24)
    v1 = evaluate_gate(flow, run, plan, engine.NO_FACTS, now, control=ALWAYS_ARMED, bolsa=ListLedger())
    assert v1.decision == "mandar"
    early = evaluate_gate(flow, run, plan, engine.NO_FACTS, now - timedelta(hours=1), control=ALWAYS_ARMED,
                          bolsa=ListLedger())
    assert (early.decision, early.reason, early.next_at) == ("esperar", None, now)
    # Con los dos enviados, el recorrido termina completo.
    done, _ = run_of(flow, fired, sends=(DAY, now))
    closed = evaluate_gate(flow, done, plan, engine.NO_FACTS, now + timedelta(hours=1), control=ALWAYS_ARMED,
                           bolsa=ListLedger())
    assert (closed.decision, closed.reason) == ("cancelar", "completo")


def test_after_a_send_a_reply_and_a_takeover_end_the_run() -> None:
    flow = cart_v5(TWO_SENDS)
    fired = firing(DAY)
    run, plan = run_of(flow, fired, sends=(DAY,), capabilities=PREVIEW_CAPABILITIES)
    now = DAY + timedelta(hours=24)
    replied = GateFacts(None, None, None, DAY + timedelta(hours=2), False, None, frozenset())
    decision = evaluate_gate(flow, run, plan, replied, now, control=ALWAYS_ARMED, bolsa=ListLedger(),
                             capabilities=PREVIEW_CAPABILITIES)
    assert (decision.decision, decision.reason) == ("cancelar", "contesto")
    taken = GateFacts(None, None, DAY + timedelta(hours=2), None, False, None, frozenset())
    decision = evaluate_gate(flow, run, plan, taken, now, control=ALWAYS_ARMED, bolsa=ListLedger(),
                             capabilities=PREVIEW_CAPABILITIES)
    assert (decision.decision, decision.reason) == ("pausar", "tomada_por_el_equipo")


def test_without_the_exceptions_the_gate_does_what_the_schema_says() -> None:
    # Sin la excepcion 1 la toma humana frena antes del envio; sin la 3, sin
    # horario vence en vez de escalar; sin la 4 y la 4c, la intencion y el
    # conflicto no frenan.
    flow = cart_v5({"/termina/vence": "2h"}, exceptions=frozenset())
    night = at("2026-10-14T05:00:00Z")
    run, plan = run_of(flow, firing(night))
    decision = evaluate_gate(flow, run, plan, engine.NO_FACTS, night, control=ALWAYS_ARMED, bolsa=ListLedger())
    assert (decision.decision, decision.reason) == ("vencer", "sin_horario")
    flow = cart_v5(exceptions=frozenset())
    run, plan = run_of(flow, firing(DAY))
    now = DAY + timedelta(minutes=10)
    taken = GateFacts(None, None, DAY, None, False, None, frozenset())
    decision = evaluate_gate(flow, run, plan, taken, now, control=ALWAYS_ARMED, bolsa=ListLedger())
    assert (decision.decision, decision.reason) == ("pausar", "tomada_por_el_equipo")
    other = GateFacts(None, None, None, None, True, DAY, frozenset())
    decision = evaluate_gate(flow, run, plan, other, now, control=ALWAYS_ARMED, bolsa=ListLedger())
    assert decision.decision == "mandar"


def test_mandar_carries_what_moved_the_hour_from_the_start() -> None:
    # La puerta mirada a la hora E de un carrito de noche: el envio dice que lo
    # movio la franja, desde el comienzo del paso.
    night = at("2026-10-14T05:00:00Z")
    flow = cart_v5()
    run, plan = run_of(flow, firing(night))
    opens = at("2026-10-14T15:00:00Z")
    decision = evaluate_gate(flow, run, plan, engine.NO_FACTS, opens, control=ALWAYS_ARMED, bolsa=ListLedger())
    assert decision.decision == "mandar"
    assert decision.eligible == engine.Eligible(opens, ("franja",), None)


def test_the_reasons_of_the_legacy_table_are_closed() -> None:
    assert all(value is None or value in MOTIVOS for value in LEGACY_REASON_TO_MOTIVO.values())
    # Las tres propuestas al dueno del contrato son codigos de la tabla, no hechos nuevos.
    assert set(PROPOSED_TO_CONTRACT_OWNER) <= set(LEGACY_REASON_TO_MOTIVO)
    assert LEGACY_REASON_TO_MOTIVO["payment_failure_correlation_unresolved"] is None
    assert PROPOSED_TO_CONTRACT_OWNER["payment_failure_correlation_unresolved"] == "no_entra:permiso"


# ---------------------------------------------------------------------------
# 3. first_eligible sobre lineas de tiempo.
# ---------------------------------------------------------------------------

def _first(start: datetime, *, control=ALWAYS_ARMED, bolsa=None, per_day=None, total=None,
           franja: Franja = CDMX, days: float = 1) -> engine.Eligible:
    return first_eligible(start, start + timedelta(days=days), franja=franja, control=control,
                          bolsa=bolsa or ListLedger(), zone=ZONE, per_day=per_day, total=total)


def test_the_control_paused_moves_e_to_the_rearm() -> None:
    control = LineControl([(DAY - timedelta(days=1), "armed"), (DAY - timedelta(minutes=1), "paused"),
                           (DAY + timedelta(hours=1), "armed")])
    assert _first(DAY, control=control) == engine.Eligible(DAY + timedelta(hours=1), ("control",), None)
    never = LineControl([(DAY - timedelta(days=1), "armed"), (DAY - timedelta(minutes=1), "paused")])
    assert _first(DAY, control=never) == engine.Eligible(None, (), "control_sin_volver")


def test_two_waits_that_end_at_the_same_hour_go_by_name() -> None:
    # El control se rearma justo cuando abre la franja: las dos mueven la hora, y van por nombre.
    night = at("2026-10-14T05:00:00Z")
    opens = at("2026-10-14T15:00:00Z")
    control = LineControl([(night - timedelta(days=1), "armed"), (night - timedelta(minutes=1), "paused"),
                           (opens, "armed")])
    assert _first(night, control=control) == engine.Eligible(opens, ("control", "franja"), None)


def test_the_window_moves_e_to_the_next_opening_and_the_end_minute_counts_whole() -> None:
    night = at("2026-10-14T05:00:00Z")
    assert _first(night) == engine.Eligible(at("2026-10-14T15:00:00Z"), ("franja",), None)
    # 21:00:30 de CDMX: el minuto de fin entra entero.
    late = at("2026-10-15T03:00:30Z")
    assert _first(late) == engine.Eligible(late, (), None)
    assert _first(night, days=10 / 24) == engine.Eligible(None, ("franja",), "vencio")


def test_the_day_cap_moves_e_to_the_local_midnight_and_then_to_the_opening() -> None:
    spent = ListLedger([DAY - timedelta(minutes=1)])
    assert _first(DAY, bolsa=spent, per_day=1) == engine.Eligible(
        at("2026-10-15T15:00:00Z"), ("bolsa_dia", "franja"), None)
    # El dia local anterior no cuenta.
    yesterday = ListLedger([DAY - timedelta(days=1)])
    assert _first(DAY, bolsa=yesterday, per_day=1) == engine.Eligible(DAY, (), None)
    # Sin tope del dia (sin_tope), la bolsa llena no frena.
    assert _first(DAY, bolsa=spent, per_day=None) == engine.Eligible(DAY, (), None)


def test_the_total_cap_does_not_come_back() -> None:
    spent = ListLedger([DAY - timedelta(days=3), DAY - timedelta(minutes=1)])
    assert _first(DAY, bolsa=spent, total=2) == engine.Eligible(None, (), "bolsa_total")
    assert _first(DAY, bolsa=spent, total=3) == engine.Eligible(DAY, (), None)


def test_without_an_opening_it_stops_with_sin_apertura(monkeypatch: pytest.MonkeyPatch) -> None:
    # Con una sola ventana todos los dias siempre hay una apertura: el doble de
    # next_open que da None prueba la rama.
    monkeypatch.setattr(engine, "next_open", lambda franja, when: None)
    night = at("2026-10-14T05:00:00Z")
    assert _first(night) == engine.Eligible(None, (), "sin_apertura")


def test_the_predicted_ledger_sees_what_was_granted_at_the_same_instant() -> None:
    # Dos carritos de noche con el tope en 1 (§8.4): el primero sale a las 09:00;
    # el segundo, mirado a la misma hora, ve la autorizacion del primero solo en
    # el libro predicho (con los empates), y pasa al dia siguiente.
    night = at("2026-10-14T05:00:00Z")
    opens = at("2026-10-14T15:00:00Z")
    first = _first(night, bolsa=ListLedger(ties=True), per_day=1, days=3)
    assert first.at == opens
    granted = [opens]
    predicted = _first(night, bolsa=ListLedger(granted, ties=True), per_day=1, days=3)
    assert predicted == engine.Eligible(at("2026-10-15T15:00:00Z"), ("franja", "bolsa_dia", "franja"), None)
    observed = _first(night, bolsa=ListLedger(granted), per_day=1, days=3)
    assert observed.at == opens
    # Con el plazo de 1 dia, el segundo vence.
    assert _first(night, bolsa=ListLedger(granted, ties=True), per_day=1, days=1).at is None


def test_a_change_in_the_legacy_cases_skips_and_does_not_compare(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    # La sesion comercial regenera los casos generados: la copia se saltea con el motivo y no compara.
    changed = tmp_path / "legacy_recovery_cases.json"
    changed.write_text(LEGACY_PATH.read_text(encoding="utf-8").replace("G21", "G99"), encoding="utf-8")
    monkeypatch.setitem(globals(), "LEGACY_PATH", changed)
    with pytest.raises(pytest.skip.Exception, match="cambio desde c7fa8ef"):
        test_the_legado_column_is_a_copy_of_the_generated_cases()
