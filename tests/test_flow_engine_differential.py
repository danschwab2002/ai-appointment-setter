"""La diferencial entre la puerta del motor y la de la T1, mientras las dos existan.

Diseno de la T3a, §2.2. ``flow_parity.eligible_at`` y ``flow_parity._wins`` no se
tocan hasta la T3b (la sesion de los flujos comerciales tiene ``flow_parity.py``).
Esta prueba ata las dos puertas llamandolas en vivo, sin copiar numeros de un
lado al otro:

1. ``flow_engine.first_eligible``, con ``CaptureControl``, el libro observado y
   ``observed_gate_inputs``, contra ``flow_parity.eligible_at``, sobre las 213
   acciones de la ventana del fixture de ATT1 y sobre las lineas de tiempo de
   ``tests/test_flow_parity.py`` (el control, la franja, la gracia, los topes del
   dia y total, otro dia local y la version del recorrido);
2. dos oraculos que no pasan por ninguna de las dos: la E es el ``due_at`` que
   escribio el legado en los 208 enviados, y las 5 diferencias con nombre dan
   ``2026-10-13T15:00:00Z``, con sus uuid escritos aca;
3. P6: las cesiones de ``flow_replay`` con ``flow_engine.wins`` y los intervalos
   observados son las de ``flow_parity.supersessions`` (192, con 78, 1 y 113) y
   los ganadores sin cancelacion, los de ``winners_without_cancel`` (0), sobre el
   fixture y sobre las lineas de tiempo de la T1.

Si la sesion comercial cambia la semantica de ``eligible_at`` o de las cesiones,
esta prueba se pone roja: es justo lo que tiene que avisar (§9).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bridge.flow_engine import first_eligible
from bridge.flow_parity import Capture, eligible_at, send_timings, supersessions, winners_without_cancel
from bridge.flow_replay import (
    CaptureControl,
    ObservedLedger,
    history_from_capture,
    observed_gate_inputs,
    observed_supersessions,
    observed_winners_without_cancel,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_recovery_capture_20261016.json"
RAW: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
CAPTURE = Capture.from_json(RAW)
HISTORY = history_from_capture(CAPTURE)

# Los constructores de las lineas de tiempo de la T1, sin copiarlos (§2.1).
SPEC = importlib.util.spec_from_file_location("t1_parity_timelines", ROOT / "tests" / "test_flow_parity.py")
assert SPEC and SPEC.loader
T1 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = T1
SPEC.loader.exec_module(T1)

# Las 5 diferencias con nombre de la T1 (lo_en_curso_sigue), escritas aca y no
# leidas de att1_parity_named_differences.json: el oraculo no pasa por el archivo.
NAMED_FIRINGS = (
    "ca2e6472-b656-4b45-b1ee-eb7e961e5b76",
    "d418879a-83af-4fa8-a553-a518d6e8a3fc",
    "2462eaf0-b397-45b0-9aea-2ad8c77e42ec",
    "3a069f16-20d7-414a-b937-aa4322b508f6",
    "3ee575f2-ed23-453a-ac01-f2f18bedd14a",
)
NINE_ON_THE_13TH = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)


def engine_e(capture: Capture, action: Any) -> datetime | None:
    """La E del motor con la puerta observada: lo que delegaria ``eligible_at`` en la T3b (§10)."""
    gate = observed_gate_inputs(capture, action)
    return first_eligible(
        gate.start, gate.expires, franja=gate.franja, control=CaptureControl(capture, gate.bolsa),
        bolsa=ObservedLedger(capture, gate.bolsa), zone=gate.zone, per_day=gate.per_day, total=gate.total,
    ).at


def window_actions(capture: Capture) -> list[tuple[str, Any]]:
    """Las acciones de los casos que ancla un disparo desde «desde», con su disparo."""
    actions = {action["recovery_case_id"]: action for action in capture.actions}
    out = []
    for event in capture.firings():
        case = capture.case_of_event(event["id"])
        if case is not None and case["abandonment_event_id"] == event["id"] and case["id"] in actions:
            out.append((event["id"], actions[case["id"]]))
    return out


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 1. first_eligible contra eligible_at, en vivo.
# ---------------------------------------------------------------------------

def test_first_eligible_is_eligible_at_on_the_213_actions_of_the_window() -> None:
    pairs = window_actions(CAPTURE)
    assert len(pairs) == 213
    differ = [ref for ref, action in pairs if engine_e(CAPTURE, action) != eligible_at(CAPTURE, action)]
    assert differ == []
    # No es trivial: las 213 tienen una E, 89 + 5 corridas de su ancla.
    assert all(eligible_at(CAPTURE, action) is not None for _, action in pairs)


def _timeline(anchor: str, **extra: Any) -> tuple[Capture, Any]:
    return T1.one_cart(anchor, **extra)


def _spent(*rows: tuple[int, str, str]) -> list[dict[str, Any]]:
    return [T1.authorization(number, when, day) for number, when, day in rows]


_NEW_VERSION_CONTROL = [
    *T1.ARMED_V4,
    T1.control_event("2026-10-11T00:20:23.000000Z", 4, 3, "paused"),
    T1.control_event("2026-10-11T00:20:27.000000Z", 5, 4, None),
    T1.control_event("2026-10-11T00:20:37.000000Z", 5, 5, "armed"),
]
_TWO_SPENT = _spent((1, "2026-10-10T15:00:01.000000Z", "2026-10-10"), (2, "2026-10-10T16:00:00.000000Z", "2026-10-10"))

# Cada linea de tiempo de eligible_at en tests/test_flow_parity.py, armada con sus constructores.
TIMELINES: dict[str, Callable[[], tuple[Capture, Any]]] = {
    "carrito_de_dia": lambda: _timeline("2026-10-10T18:00:00.000000Z"),
    "carrito_de_noche": lambda: _timeline("2026-10-10T05:00:00.000000Z"),
    "minuto_de_fin_21_00_30": lambda: _timeline("2026-10-11T03:00:30.000000Z"),
    "e_desde_el_ancla_y_no_el_due_at": lambda: _timeline(
        "2026-10-10T18:00:00.000000Z", due_at="2026-10-10T20:00:00.000000Z", sent_at="2026-10-10T20:00:05.000000Z"),
    "gracia_de_30": lambda: _timeline("2026-10-10T18:00:00.000000Z", policies=[T1.GRACE_30]),
    "gracia_que_cruza_el_fin": lambda: _timeline("2026-10-11T02:45:00.000000Z", policies=[T1.GRACE_30]),
    "control_inactivo_hasta_armar": lambda: _timeline(
        "2026-10-11T00:20:30.000000Z", control=_NEW_VERSION_CONTROL, scopes=[T1.scope_row(4), T1.scope_row(5)]),
    "control_pausado": lambda: _timeline("2026-10-10T18:00:00.000000Z", control=[
        *T1.ARMED_V4, T1.control_event("2026-10-10T17:00:00.000000Z", 4, 3, "paused"),
        T1.control_event("2026-10-10T18:30:00.000000Z", 4, 4, "armed")]),
    "control_rearmado_de_noche": lambda: _timeline("2026-10-10T18:00:00.000000Z", control=[
        *T1.ARMED_V4, T1.control_event("2026-10-10T17:00:00.000000Z", 4, 3, "paused"),
        T1.control_event("2026-10-11T04:00:00.000000Z", 4, 4, "armed")]),
    "control_pausado_hasta_vencer": lambda: _timeline("2026-10-10T18:00:00.000000Z", control=[
        *T1.ARMED_V4, T1.control_event("2026-10-10T17:00:00.000000Z", 4, 3, "paused")]),
    "tope_del_dia_gastado": lambda: _timeline(
        "2026-10-10T20:00:00.000000Z", authorizations=_TWO_SPENT, scopes=[T1.scope_row(4, per_day=2)]),
    "tope_cuenta_solo_lo_anterior": lambda: _timeline(
        "2026-10-10T20:00:00.000000Z",
        authorizations=_spent((1, "2026-10-10T15:00:01.000000Z", "2026-10-10"),
                              (2, "2026-10-10T21:00:00.000000Z", "2026-10-10")),
        scopes=[T1.scope_row(4, per_day=2)]),
    "tope_de_otro_dia_local": lambda: _timeline(
        "2026-10-10T18:00:00.000000Z",
        authorizations=_spent((1, "2026-10-10T02:00:00.000000Z", "2026-10-09"),
                              (2, "2026-10-10T03:00:00.000000Z", "2026-10-09")),
        scopes=[T1.scope_row(4, per_day=2)]),
    "tope_total_gastado": lambda: _timeline(
        "2026-10-10T18:00:00.000000Z",
        authorizations=_spent((1, "2026-10-09T15:00:01.000000Z", "2026-10-09"),
                              (2, "2026-10-09T16:00:00.000000Z", "2026-10-09")),
        scopes=[T1.scope_row(4, per_day=2, total=2)]),
    "version_nueva_no_frena_v4_antes": lambda: _timeline(
        "2026-10-10T22:00:00.000000Z", control=_NEW_VERSION_CONTROL, authorizations=_TWO_SPENT,
        scopes=[T1.scope_row(4, per_day=2), T1.scope_row(5, per_day=80)]),
    "version_nueva_no_frena_v4_despues": lambda: _timeline(
        "2026-10-11T00:21:00.000000Z", control=_NEW_VERSION_CONTROL, authorizations=_TWO_SPENT,
        scopes=[T1.scope_row(4, per_day=2), T1.scope_row(5, per_day=80)]),
    "version_nueva_con_sus_topes_v5": lambda: _timeline(
        "2026-10-11T00:21:00.000000Z", version=5, control=_NEW_VERSION_CONTROL, authorizations=_TWO_SPENT,
        scopes=[T1.scope_row(4, per_day=2), T1.scope_row(5, per_day=80)]),
}


@pytest.mark.parametrize("name", sorted(TIMELINES))
def test_first_eligible_is_eligible_at_on_the_timelines_of_the_t1(name: str) -> None:
    capture, action = TIMELINES[name]()
    assert engine_e(capture, action) == eligible_at(capture, action)


def test_the_timelines_exercise_what_the_real_data_does_not() -> None:
    # Sobre el dato real solo la franja mueve la E (test_p3_on_the_real_data_only_the_window_moves_e):
    # las lineas de tiempo tienen que dar E distintas del ancla por el control y por los topes, y None.
    values = {name: eligible_at(*TIMELINES[name]()) for name in TIMELINES}
    assert values["control_pausado"] == _instant("2026-10-10T18:30:00Z")
    assert values["tope_del_dia_gastado"] == _instant("2026-10-11T15:00:00Z")
    assert values["minuto_de_fin_21_00_30"] == _instant("2026-10-11T03:00:30Z")
    assert values["tope_total_gastado"] is None and values["control_pausado_hasta_vencer"] is None


# ---------------------------------------------------------------------------
# 2. Los dos oraculos que no pasan por ninguna de las dos puertas.
# ---------------------------------------------------------------------------

def test_e_is_the_due_at_that_the_legacy_wrote_in_the_208_sends() -> None:
    actions = dict(window_actions(CAPTURE))
    sent = [timing.firing_ref for timing in send_timings(CAPTURE)]
    assert len(sent) == 208
    for ref in sent:
        assert engine_e(CAPTURE, actions[ref]) == _instant(actions[ref]["due_at"]), ref


def test_the_five_named_differences_give_nine_of_mexico_city_on_the_13th() -> None:
    actions = dict(window_actions(CAPTURE))
    sent = {timing.firing_ref for timing in send_timings(CAPTURE)}
    assert set(actions) - sent == set(NAMED_FIRINGS)
    for ref in NAMED_FIRINGS:
        assert engine_e(CAPTURE, actions[ref]) == NINE_ON_THE_13TH, ref


# ---------------------------------------------------------------------------
# 3. P6: flow_engine.wins con los intervalos observados, contra la T1.
# ---------------------------------------------------------------------------

def _routes(items: Any) -> dict[str, int]:
    groups: Counter[str] = Counter()
    for item in items:
        if {"persona", "intencion"} <= item.routes:
            groups["las_dos"] += 1
        elif "persona" in item.routes:
            groups["solo_persona"] += 1
        elif "intencion" in item.routes:
            groups["solo_intencion"] += 1
    return dict(groups)


def _by_stop(items: Any) -> tuple[Any, ...]:
    # flow_replay las ordena por la hora del corte y el plan; la T1, por el orden de las filas.
    return tuple(sorted(items, key=lambda item: (item.stopped_at, item.plan_ref)))


def test_p6_with_engine_wins_is_the_t1_on_the_fixture() -> None:
    mine = observed_supersessions(HISTORY)
    assert mine == _by_stop(supersessions(CAPTURE))
    assert len(mine) == 192
    assert _routes(mine) == {"las_dos": 78, "solo_persona": 1, "solo_intencion": 113}
    assert [item for item in mine if item.winner_ref is None] == []
    assert observed_winners_without_cancel(HISTORY) == winners_without_cancel(CAPTURE) == ()


def _p6_timelines() -> dict[str, Capture]:
    def capture(**sections: Any) -> Capture:
        return Capture.from_json(T1.build(**sections))

    superseded = "superseded_by_provider_event"
    out: dict[str, Capture] = {}
    # Por intencion, desde otro telefono.
    event = T1.cart(1, "2026-10-10T17:30:00.000000Z", who=2, intent=101)
    case = T1.case_of(event, who=2, closed_at="2026-10-10T17:30:15.000000Z")
    out["intencion_desde_otro_telefono"] = capture(
        events=[event], cases=[case], actions=[T1.action_of(case, sent_at="2026-10-10T17:30:15.000000Z")],
        contacts=[T1.contact(1), T1.contact(2)],
        first_contacts=[T1.plan(101, 1, "2026-10-10T17:00:00.000000Z", status="cancelled", reason=superseded,
                                closed_at="2026-10-10T18:00:00.000000Z")])
    # Por persona, con el caso en curso; y por las dos rutas.
    for name, intent in (("persona_con_el_caso_en_curso", 102), ("las_dos_rutas", 101)):
        event = T1.cart(1, "2026-10-10T05:00:00.000000Z", who=1, intent=intent)
        case = T1.case_of(event, who=1, closed_at="2026-10-10T15:00:10.000000Z")
        out[name] = capture(
            events=[event], cases=[case], actions=[T1.action_of(case, sent_at="2026-10-10T15:00:10.000000Z")],
            contacts=[T1.contact(1)],
            first_contacts=[{"purchase_intent_id": T1.uid(101), "contact_id": T1.uid(9001), "outcome": "not_planned",
                             "reason_code": superseded, "created_at": "2026-10-10T06:00:00.000000Z"}])
    # Ante un disparo sin caso y sin telefono.
    event = {**T1.cart(1, "2026-10-10T17:30:00.000000Z", who=2, intent=101), "processing_status": "failed",
             "processing_error": "create_recovery_case_failed"}
    del event["person_key"]
    out["disparo_sin_caso"] = capture(
        events=[event], contacts=[T1.contact(1)],
        first_contacts=[T1.plan(101, 1, "2026-10-10T17:00:00.000000Z", status="cancelled", reason=superseded,
                                closed_at="2026-10-10T18:00:00.000000Z")])
    # El carrito de la persona ya salio y es de otra intencion: sin ganador.
    event = T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1, intent=102)
    case = T1.case_of(event, who=1, closed_at="2026-10-10T18:00:10.000000Z")
    out["carrito_ya_enviado_sin_la_intencion"] = capture(
        events=[event], cases=[case], actions=[T1.action_of(case, sent_at="2026-10-10T18:00:10.000000Z")],
        contacts=[T1.contact(1)],
        first_contacts=[T1.plan(101, 1, "2026-10-10T17:00:00.000000Z", status="cancelled", reason=superseded,
                                closed_at="2026-10-10T19:00:00.000000Z")])
    # Un ganador sin cancelacion.
    event = T1.cart(1, "2026-10-10T17:30:00.000000Z", who=1, intent=101)
    case = T1.case_of(event, who=1, closed_at="2026-10-10T17:30:15.000000Z")
    out["ganador_sin_cancelacion"] = capture(
        events=[event], cases=[case], actions=[T1.action_of(case, sent_at="2026-10-10T17:30:15.000000Z")],
        contacts=[T1.contact(1)],
        first_contacts=[T1.plan(101, 1, "2026-10-10T17:00:00.000000Z", status="accepted_by_chatwoot",
                                reason="accepted_by_chatwoot", closed_at="2026-10-10T18:00:01.000000Z",
                                sent_at="2026-10-10T18:00:00.000000Z")])
    # Un carrito de la misma persona que nunca corrio no le gana.
    event = {**T1.cart(1, "2026-10-10T17:30:00.000000Z", who=1), "processing_status": "failed",
             "processing_error": "pilot_scope_rejected:pilot_audience_intent_unresolved"}
    out["carrito_que_nunca_corrio"] = capture(
        events=[event], contacts=[T1.contact(1)],
        first_contacts=[T1.plan(101, 1, "2026-10-10T17:00:00.000000Z", status="accepted_by_chatwoot",
                                reason="accepted_by_chatwoot", closed_at="2026-10-10T18:00:01.000000Z",
                                sent_at="2026-10-10T18:00:00.000000Z")])
    return out


P6_TIMELINES = _p6_timelines()


@pytest.mark.parametrize("name", sorted(P6_TIMELINES))
def test_p6_with_engine_wins_is_the_t1_on_its_timelines(name: str) -> None:
    capture = P6_TIMELINES[name]
    history = history_from_capture(capture)
    assert observed_supersessions(history) == _by_stop(supersessions(capture))
    assert sorted(observed_winners_without_cancel(history)) == sorted(winners_without_cancel(capture))


def test_the_p6_timelines_are_not_all_empty() -> None:
    found = {name: supersessions(capture) for name, capture in P6_TIMELINES.items()}
    assert [(item.winner_ref, item.matched_by) for item in found["intencion_desde_otro_telefono"]] == [
        (T1.uid(1), "intencion")]
    assert [item.routes for item in found["las_dos_rutas"]] == [frozenset({"persona", "intencion"})]
    assert [item.winner_ref for item in found["carrito_ya_enviado_sin_la_intencion"]] == [None]
    assert winners_without_cancel(P6_TIMELINES["ganador_sin_cancelacion"]) == ((T1.uid(1), T1.uid(5001)),)
