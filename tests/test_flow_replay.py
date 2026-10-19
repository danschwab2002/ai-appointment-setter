"""El replay del motor: sobre el fixture de ATT1 y sobre lineas de tiempo escritas a mano.

Diseno de la T3a, §4, §6, §7 y §8.4. Fixtures: la captura de ATT1
(``tests/fixtures/flow_engine/att1_recovery_capture_20261016.json``) y las
definiciones de la v3, la v4 y la v5
(``tests/fixtures/flow_simulation/att1_definitions_by_scope_version.json``).

* **El fixture, «como corrio»:** 252 disparos, 248 que llegaron, 213 arranques y
  213 envios con la misma E que la T1 (comparada en vivo), de los que 5 son las
  diferencias con nombre ``lo_en_curso_sigue``; P6 en 192 (78, 1 y 113); lo en
  curso a las 06:00Z del 13/10; y el invariante de la v1 (a lo sumo un envio
  por recorrido).
* **La puerta es una sola:** un espia de ``flow_engine.evaluate_gate`` ve un
  «mandar» a la hora exacta de cada uno de los 213 envios, y con un centinela
  que siempre dice «vencer» el replay no manda nada.
* **El orden de las filas no importa:** la captura con las secciones invertidas
  da el mismo replay.
* **Las lineas de tiempo de §8.4**, con los constructores de
  ``tests/test_flow_parity.py`` (importados, no copiados) y las definiciones de
  ATT1 cambiadas en lo que cada una mira.

Los datos de las lineas de tiempo son inventados: uuid de la forma
``00000000-0000-4000-8000-<n>``.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib.util
import json
import sys
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_engine
from bridge.flow_engine import (
    ENGINE_CAPABILITIES_V1,
    PREVIEW_CAPABILITIES,
    GateDecision,
    PlanNotSupported,
)
from bridge.flow_parity import Capture, eligible_at
from bridge.flow_replay import (
    History,
    Replay,
    RunOutcome,
    Versions,
    as_ran,
    engine_flows_from_definitions,
    exceptions_for,
    fixed,
    flow_summary,
    history_from_capture,
    in_flight_at,
    replay,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_recovery_capture_20261016.json"
DEFINITIONS: dict[str, Any] = json.loads(
    (ROOT / "tests" / "fixtures" / "flow_simulation" / "att1_definitions_by_scope_version.json"
     ).read_text(encoding="utf-8"))
RAW: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
CAPTURE = Capture.from_json(RAW)
HISTORY = history_from_capture(CAPTURE)
VERSIONS = engine_flows_from_definitions(DEFINITIONS)
AS_RAN = replay(HISTORY, VERSIONS, as_ran())

# Los constructores de las lineas de tiempo de la T1 (§2.1).
SPEC = importlib.util.spec_from_file_location("t1_parity_timelines_replay", ROOT / "tests" / "test_flow_parity.py")
assert SPEC and SPEC.loader
T1 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = T1
SPEC.loader.exec_module(T1)

CART = ("att1", "carrito")
FAILED = ("att1", "pago_fallido")
V4 = fixed({CART: 4, FAILED: 4})
NAMED_FIRINGS = frozenset({
    "ca2e6472-b656-4b45-b1ee-eb7e961e5b76",
    "d418879a-83af-4fa8-a553-a518d6e8a3fc",
    "2462eaf0-b397-45b0-9aea-2ad8c77e42ec",
    "3a069f16-20d7-414a-b937-aa4322b508f6",
    "3ee575f2-ed23-453a-ac01-f2f18bedd14a",
})
CANCELED = "PURCHASE_CANCELED"
SUPERSEDED = "superseded_by_provider_event"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _run(result: Replay, ref: str) -> RunOutcome:
    [found] = [run for run in result.runs if run.firing_ref == ref]
    return found


# ---------------------------------------------------------------------------
# El fixture, «como corrio» (§7.2).
# ---------------------------------------------------------------------------

def test_the_fixture_as_it_ran_gives_the_numbers_of_the_design() -> None:
    runs = AS_RAN.runs
    assert len(HISTORY.firings) == len(runs) == 252
    assert sum(1 for run in runs if run.end != "no_llego") == 248
    assert sum(1 for run in runs if run.plan is not None) == 213
    assert dict(AS_RAN.counts()) == {"enviado": 213, "no_arranco": 35, "no_llego": 4}
    assert Counter(run.start.reason for run in runs if run.end == "no_arranco") == {
        "no_entra:permiso": 26, "baja": 3, None: 5, "ya_en_curso": 1}
    assert Counter(run.flow for run in runs if run.end == "enviado") == {CART: 208, FAILED: 5}


def test_the_213_sends_have_the_e_of_the_t1() -> None:
    # La E de la T1, calculada en vivo con eligible_at sobre la accion de cada disparo.
    actions = {action["recovery_case_id"]: action for action in CAPTURE.actions}
    sent = [run for run in AS_RAN.runs if run.end == "enviado"]
    assert len(sent) == 213
    for run in sent:
        case = CAPTURE.case_of_event(run.firing_ref)
        assert case is not None and case["abandonment_event_id"] == run.firing_ref
        assert run.sends == (eligible_at(CAPTURE, actions[case["id"]]),), run.firing_ref


def test_the_five_carts_of_the_12th_are_the_named_differences() -> None:
    noted = {run.firing_ref: run for run in AS_RAN.runs if run.legacy_note is not None}
    assert set(noted) == NAMED_FIRINGS
    for run in noted.values():
        [step] = run.steps
        assert (step.at, step.status, step.moved_by) == (
            at("2026-10-13T15:00:00Z"), "enviado", ("bolsa_dia", "franja"))
        assert run.legacy_note == "lo_en_curso_sigue: el legado lo rechaza por la version (v5 activa, recorrido v4)"
    # En ningun otro envio de la ventana aparece el tope.
    assert {run.firing_ref for run in AS_RAN.runs
            if any("bolsa_dia" in step.moved_by for step in run.steps)} == NAMED_FIRINGS


def test_p6_with_the_intervals_of_the_replay() -> None:
    ceded = AS_RAN.supersessions
    assert len(ceded) == 192
    groups = Counter("las_dos" if {"persona", "intencion"} <= item.routes else
                     "solo_persona" if "persona" in item.routes else "solo_intencion" for item in ceded)
    assert groups == {"las_dos": 78, "solo_persona": 1, "solo_intencion": 113}
    assert [item.plan_ref for item in ceded if item.winner_ref is None] == []
    assert AS_RAN.winners_without_cancel == ()


def test_the_predicted_bolsa_of_the_12th_and_the_13th() -> None:
    # El 12/10: 40 de la v4 mas 2 de la v5, como lo observado. El 13/10 sube de 29 a 34 por los 5.
    assert AS_RAN.bolsa_by_day[("att1-recuperacion", "2026-10-12")] == 42
    assert AS_RAN.bolsa_by_day[("att1-recuperacion", "2026-10-13")] == 34


def test_in_flight_at_six_of_the_13th() -> None:
    assert dict(in_flight_at(AS_RAN, at("2026-10-13T06:00:00Z"))) == {CART: 6, FAILED: 1}


def test_with_the_v1_no_run_sends_twice_and_none_is_looked_at_after_its_send(
        monkeypatch: pytest.MonkeyPatch) -> None:
    real = flow_engine.evaluate_gate
    after_a_send: list[str] = []

    def spy(flow, run, plan, facts, now, **kwargs):  # type: ignore[no-untyped-def]
        if run.sends:
            after_a_send.append(run.firing.source_ref)
        return real(flow, run, plan, facts, now, **kwargs)

    monkeypatch.setattr(flow_engine, "evaluate_gate", spy)
    result = replay(HISTORY, VERSIONS, as_ran())
    assert len(result.runs) == 252
    assert max(len(run.sends) for run in result.runs) == 1
    assert all(len(run.steps) <= 1 for run in result.runs)
    assert after_a_send == []


def test_with_the_v1_a_plan_of_three_sends_still_sends_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # Aunque el plan traiga tres pasos, las capacidades de la v1 dejan uno: el replay no vuelve a
    # meter el recorrido en la cola y la puerta, con la v1, nunca manda un segundo.
    real = flow_engine.plan_steps

    def three(flow, firing, *, capabilities=ENGINE_CAPABILITIES_V1):  # type: ignore[no-untyped-def]
        plan = real(flow, firing, capabilities=capabilities)
        first = plan.steps[0]
        steps = (first, dataclasses.replace(first, index=1, after=timedelta(hours=1)),
                 dataclasses.replace(first, index=2, after=timedelta(hours=2)))
        return dataclasses.replace(plan, steps=steps, expires_after=timedelta(days=4))

    monkeypatch.setattr(flow_engine, "plan_steps", three)
    result = replay(HISTORY, VERSIONS, as_ran())
    assert dict(result.counts()) == {"enviado": 213, "no_arranco": 35, "no_llego": 4}
    assert all(len(run.steps) <= 1 and len(run.sends) <= 1 for run in result.runs)


# ---------------------------------------------------------------------------
# La puerta es una sola: el espia de evaluate_gate, con centinela (§8.1).
# ---------------------------------------------------------------------------

def test_each_send_is_a_mandar_of_evaluate_gate_at_its_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    real = flow_engine.evaluate_gate
    seen: set[tuple[str, datetime]] = set()

    def spy(flow, run, plan, facts, now, **kwargs):  # type: ignore[no-untyped-def]
        decision = real(flow, run, plan, facts, now, **kwargs)
        if decision.decision == "mandar":
            seen.add((run.firing.source_ref, now))
        return decision

    monkeypatch.setattr(flow_engine, "evaluate_gate", spy)
    result = replay(HISTORY, VERSIONS, as_ran())
    sends = {(run.firing_ref, when) for run in result.runs for when in run.sends}
    assert len(sends) == 213
    assert sends <= seen


def test_with_a_sentinel_gate_nothing_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    def sentinel(*args, **kwargs):  # type: ignore[no-untyped-def]
        return GateDecision("vencer", "centinela", None, None)

    monkeypatch.setattr(flow_engine, "evaluate_gate", sentinel)
    result = replay(HISTORY, VERSIONS, as_ran())
    started = {run.firing_ref: run for run in result.runs if run.plan is not None}
    assert sum(len(run.sends) for run in result.runs) == 0
    # Los 213 arranques de «como corrio» terminan con el motivo del centinela. Uno mas arranca: la
    # repeticion del pago fallido, porque el centinela ya cerro el recorrido al que se sumaba.
    assert {run.firing_ref for run in AS_RAN.runs if run.plan is not None} <= set(started)
    assert len(started) == 214
    assert all(run.end == "vencio" and run.steps[-1].reason == "centinela" for run in started.values())


# ---------------------------------------------------------------------------
# El orden de las filas no importa (§7.1, punto 4).
# ---------------------------------------------------------------------------

def _reversed(data: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(data)
    for key, value in out.items():
        if isinstance(value, list):
            out[key] = list(reversed(value))
    for key, value in out["config"].items():
        if isinstance(value, list):
            out["config"][key] = list(reversed(value))
    return out


def test_the_capture_with_its_sections_reversed_gives_the_same_replay() -> None:
    capture = Capture.from_json(_reversed(RAW))
    # El orden cambio de verdad: en el fixture los disparos vienen por hora.
    assert [event["id"] for event in capture.firings()] != [event["id"] for event in CAPTURE.firings()]
    result = replay(history_from_capture(capture), VERSIONS, as_ran())
    assert result.runs == AS_RAN.runs
    assert result.supersessions == AS_RAN.supersessions
    assert result.bolsa_by_day == AS_RAN.bolsa_by_day


# ---------------------------------------------------------------------------
# Las lineas de tiempo (§8.4).
# ---------------------------------------------------------------------------

SEND = copy.deepcopy(DEFINITIONS["versions"]["4"]["carrito"]["pasos"][0])


def versions(*, cart: Callable[[dict[str, Any]], None] | None = None,
             failed: Callable[[dict[str, Any]], None] | None = None) -> Versions:
    """Las definiciones de ATT1 con la v4 del carrito o del pago fallido cambiada."""
    document = copy.deepcopy(DEFINITIONS)
    for key, change in (("carrito", cart), ("pago_fallido", failed)):
        if change is not None:
            change(document["versions"]["4"][key])
    return engine_flows_from_definitions(document)


def per_day(cap: int, *, vence: str | None = None, total: int | None = None) -> Callable[[dict[str, Any]], None]:
    def change(definition: dict[str, Any]) -> None:
        definition["topes"]["por_dia"] = cap
        if total is not None:
            definition["topes"]["total"] = total
        if vence is not None:
            definition["termina"]["vence"] = vence
    return change


def steps(*items: Any, vence: str | None = None) -> Callable[[dict[str, Any]], None]:
    def change(definition: dict[str, Any]) -> None:
        definition["pasos"] = [copy.deepcopy(item) for item in items]
        if vence is not None:
            definition["termina"]["vence"] = vence
    return change


def history(events: list[dict[str, Any]], *, who: tuple[int, ...] = (1,), **sections: Any) -> History:
    return history_from_capture(Capture.from_json(T1.build(
        events=events, contacts=[T1.contact(number) for number in who], **sections)))


def purchase(number: int, when: str, *, who: int, offer: str = T1.OFFER) -> dict[str, Any]:
    ms = int(at(when).timestamp() * 1000)
    return {
        "id": T1.uid(number), "event_type": "PURCHASE_APPROVED", "received_at": T1.shifted(when, seconds=1),
        "processing_status": "processed", "approved_date_ms": ms, "creation_date_ms": ms,
        "product_id": T1.PRODUCT, "offer_code": offer, "person_key": T1.person(who),
        "purchase_status": "APPROVED", "purchase_shape_ok": True,
    }


def paused(since: str, until: str) -> list[dict[str, Any]]:
    return [*T1.ARMED_V4, T1.control_event(since, 4, 3, "paused"), T1.control_event(until, 4, 4, "armed")]


WAIT_60 = steps({"esperar": "60m"}, SEND)


def test_a_day_cart_and_a_night_cart() -> None:
    result = replay(history([T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1),
                             T1.cart(2, "2026-10-10T05:00:00.000000Z", who=2)], who=(1, 2)), VERSIONS, V4)
    day, night = _run(result, T1.uid(1)), _run(result, T1.uid(2))
    assert (day.sends, day.steps[0].moved_by) == ((at("2026-10-10T18:00:00Z"),), ())
    assert (night.sends, night.steps[0].moved_by) == ((at("2026-10-10T15:00:00Z"),), ("franja",))


@pytest.mark.parametrize(("vence", "second"), [("1d", None), ("3d", "2026-10-11T15:00:00Z")])
def test_two_night_carts_with_a_day_cap_of_one(vence: str, second: str | None) -> None:
    # Los dos esperan a las 09:00 de CDMX del 10/10: sale el primero de la cola (uid 1, aunque
    # llego despues); el segundo ve ese cupo concedido a la misma hora y pasa al dia siguiente.
    events = [T1.cart(2, "2026-10-10T05:00:00.000000Z", who=2), T1.cart(1, "2026-10-10T05:30:00.000000Z", who=1)]
    result = replay(history(events, who=(1, 2)), versions(cart=per_day(1, vence=vence)), V4)
    assert _run(result, T1.uid(1)).sends == (at("2026-10-10T15:00:00Z"),)
    later = _run(result, T1.uid(2))
    if second is None:
        assert (later.end, later.sends) == ("vencio", ())
    else:
        assert later.sends == (at(second),)
        assert "bolsa_dia" in later.steps[0].moved_by


def test_two_night_carts_give_the_same_with_the_sections_reversed() -> None:
    events = [T1.cart(2, "2026-10-10T05:00:00.000000Z", who=2), T1.cart(1, "2026-10-10T05:30:00.000000Z", who=1)]
    data = T1.build(events=events, contacts=[T1.contact(1), T1.contact(2)])
    flows = versions(cart=per_day(1, vence="3d"))
    straight = replay(history_from_capture(Capture.from_json(data)), flows, V4)
    backwards = replay(history_from_capture(Capture.from_json(_reversed(data))), flows, V4)
    assert backwards.runs == straight.runs
    assert _run(backwards, T1.uid(1)).sends == (at("2026-10-10T15:00:00Z"),)


def test_the_day_cap_spent_waits_for_the_local_midnight_and_then_the_window() -> None:
    spent = [T1.authorization(1, "2026-10-10T15:00:01.000000Z", "2026-10-10"),
             T1.authorization(2, "2026-10-10T16:00:00.000000Z", "2026-10-10")]
    result = replay(history([T1.cart(1, "2026-10-10T20:00:00.000000Z", who=1)], authorizations=spent),
                    versions(cart=per_day(2)), V4)
    [step] = _run(result, T1.uid(1)).steps
    assert (step.at, step.status, step.moved_by) == (at("2026-10-11T15:00:00Z"), "enviado", ("bolsa_dia", "franja"))


def test_the_total_cap_spent_does_not_come_back() -> None:
    spent = [T1.authorization(1, "2026-10-09T15:00:01.000000Z", "2026-10-09"),
             T1.authorization(2, "2026-10-09T16:00:00.000000Z", "2026-10-09")]
    result = replay(history([T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1)], authorizations=spent),
                    versions(cart=per_day(2, total=2)), V4)
    run = _run(result, T1.uid(1))
    assert (run.end, run.sends, run.end_at) == ("vencio", (), at("2026-10-11T18:00:00Z"))


def test_a_paused_control_with_a_run_waiting_sends_at_the_rearm() -> None:
    result = replay(history([T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1)],
                            control=paused("2026-10-10T17:00:00.000000Z", "2026-10-10T18:30:00.000000Z")),
                    VERSIONS, V4)
    [step] = _run(result, T1.uid(1)).steps
    assert (step.at, step.moved_by) == (at("2026-10-10T18:30:00Z"), ("control",))


def test_the_two_causes_of_the_12th_in_small() -> None:
    # Llega con la v4 y su tope del dia (2) gastado; despues se activa la v5. El motor sigue con
    # la v4 y manda al dia siguiente; legacy_note dice que el legado lo rechazaria por la version.
    control = [
        *T1.ARMED_V4,
        T1.control_event("2026-10-11T00:20:23.000000Z", 4, 3, "paused"),
        T1.control_event("2026-10-11T00:20:27.000000Z", 5, 4, None),
        T1.control_event("2026-10-11T00:20:37.000000Z", 5, 5, "armed"),
    ]
    spent = [T1.authorization(1, "2026-10-10T15:00:01.000000Z", "2026-10-10"),
             T1.authorization(2, "2026-10-10T16:00:00.000000Z", "2026-10-10")]
    result = replay(history([T1.cart(1, "2026-10-10T22:00:00.000000Z", who=1)], control=control,
                            authorizations=spent, scopes=[T1.scope_row(4, per_day=2), T1.scope_row(5)]),
                    versions(cart=per_day(2)), as_ran())
    run = _run(result, T1.uid(1))
    assert run.version == 4
    assert run.sends == (at("2026-10-11T15:00:00Z"),)
    assert run.steps[0].moved_by == ("bolsa_dia", "franja")
    assert run.legacy_note == "lo_en_curso_sigue: el legado lo rechaza por la version (v5 activa, recorrido v4)"


def _failed_twice(**sections: Any) -> History:
    events = [T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1, event_type=CANCELED),
              T1.cart(2, "2026-10-10T18:20:00.000000Z", who=1, event_type=CANCELED)]
    return history(events, **sections)


def test_f11_a_repeated_payment_failure_brings_the_send_forward() -> None:
    # Espera de 60 minutos y la repeticion a los 20: el legado adelanta la accion a la llegada.
    result = replay(_failed_twice(), versions(failed=WAIT_60), V4)
    first, second = _run(result, T1.uid(1)), _run(result, T1.uid(2))
    assert (second.end, second.start.reason, second.start.joins_run) == ("no_arranco", "ya_en_curso", T1.uid(1))
    assert first.sends == (at("2026-10-10T18:20:01Z"),)


@pytest.mark.parametrize(("rearm", "expected"), [
    ("2026-10-10T18:40:00.000000Z", "2026-10-10T18:40:00Z"),   # reescrita a los 20, sale al rearmar
    ("2026-10-10T19:30:00.000000Z", "2026-10-10T19:30:00Z"),   # el rearme despues de los 60: sale al rearmar
])
def test_f11_with_the_control_not_armed_rewrites_and_sends_at_the_rearm(rearm: str, expected: str) -> None:
    # El control sin armar no pausa la secuencia: el legado reescribe el due_at con la secuencia
    # 'active' (20260904000100:145 y :149) y solo la pausan pause y escalate (20260803000100:1213-1216).
    # El arranque se rechaza hasta el rearme, y ahi sale.
    result = replay(_failed_twice(control=paused("2026-10-10T18:10:00.000000Z", rearm)),
                    versions(failed=WAIT_60), V4)
    assert _run(result, T1.uid(2)).start.reason == "ya_en_curso"
    assert _run(result, T1.uid(1)).sends == (at(expected),)


def test_a_cart_repeated_while_it_waits_keeps_its_e() -> None:
    # F#11 es solo del pago fallido: el carrito repetido es ya_en_curso y su E sigue a los 60.
    events = [T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1), T1.cart(2, "2026-10-10T18:20:00.000000Z", who=1)]
    result = replay(history(events), versions(cart=WAIT_60), V4)
    assert _run(result, T1.uid(2)).start.reason == "ya_en_curso"
    assert _run(result, T1.uid(1)).sends == (at("2026-10-10T19:00:00Z"),)


def test_a_cart_with_a_payment_failure_running_does_not_start() -> None:
    events = [T1.cart(1, "2026-10-10T05:00:00.000000Z", who=1, event_type=CANCELED),
              T1.cart(2, "2026-10-10T06:00:00.000000Z", who=1)]
    result = replay(history(events), VERSIONS, V4)
    cart = _run(result, T1.uid(2))
    assert (cart.end, cart.start.reason, cart.plan) == ("no_arranco", "no_arranca_mientras:pago_fallido", None)
    assert _run(result, T1.uid(1)).sends == (at("2026-10-10T15:00:00Z"),)


def test_a_payment_failure_with_two_correlations_wins_by_intent_over_each() -> None:
    event = T1.cart(1, "2026-10-10T17:30:00.000000Z", who=3, intent=101, event_type=CANCELED)
    event["payment_failure_correlation"] = {"outcome": "resolved", "purchase_intent_id": T1.uid(102)}
    plans = [T1.plan(101, 1, "2026-10-10T17:00:00.000000Z", status="cancelled", reason=SUPERSEDED,
                     closed_at="2026-10-10T18:00:00.000000Z", number=1),
             T1.plan(102, 2, "2026-10-10T17:10:00.000000Z", status="cancelled", reason=SUPERSEDED,
                     closed_at="2026-10-10T18:05:00.000000Z", number=2)]
    result = replay(history([event], who=(1, 2, 3), first_contacts=plans), VERSIONS, V4)
    assert _run(result, T1.uid(1)).firing.intent_refs == frozenset({T1.uid(101), T1.uid(102)})
    assert sorted((item.plan_ref, item.winner_ref, item.matched_by, item.routes) for item in result.supersessions) == [
        (T1.uid(5001), T1.uid(1), "intencion", frozenset({"intencion"})),
        (T1.uid(5002), T1.uid(1), "intencion", frozenset({"intencion"})),
    ]


def test_a_purchase_before_e_ends_the_run_without_a_send_or_an_authorization() -> None:
    events = [T1.cart(1, "2026-10-10T05:00:00.000000Z", who=1), purchase(50, "2026-10-10T08:00:00.000000Z", who=1)]
    result = replay(history(events), VERSIONS, V4)
    run = _run(result, T1.uid(1))
    assert (run.end, run.sends, run.end_at) == ("compro", (), at("2026-10-10T08:00:00Z"))
    assert sum(result.bolsa_by_day.values()) == 0


@pytest.mark.parametrize(("bought", "offer"), [
    ("2026-10-09T12:00:00.000000Z", None),         # 30 h antes, la misma oferta
    ("2026-10-10T17:54:59.999000Z", None),         # un milisegundo antes de los 5 minutos
    ("2026-10-10T17:55:00.000000Z", None),         # justo 5 minutos antes
    ("2026-10-09T12:00:00.000000Z", "otraofer"),   # 30 h antes, otra oferta del producto
])
def test_a_purchase_of_the_product_at_any_time_before_the_firing_ends_the_run(bought: str,
                                                                              offer: str | None) -> None:
    # 20261019000300: la reevaluacion cancela el primer toque si la misma persona compro el producto,
    # de cualquier oferta y sin ventana (_portable_case_purchase_stop_reason). Antes cortaba solo la de
    # la misma oferta aprobada desde 5 minutos antes del disparo (20260808000200:70-77).
    extra = {} if offer is None else {"offer": offer}
    events = [purchase(50, bought, who=1, **extra), T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1)]
    run = _run(replay(history(events), VERSIONS, V4), T1.uid(1))
    assert run.start.starts
    assert (run.end, run.sends) == ("compro", ())


def test_a_purchase_of_another_offer_after_the_firing_ends_the_run() -> None:
    # 20261019000300: otra oferta del mismo producto corta igual (antes no: offer_code is not distinct
    # from, 20260808000100:202). Compra a las 08:00Z, antes del envio de las 15:00Z: no manda nada.
    events = [T1.cart(1, "2026-10-10T05:00:00.000000Z", who=1),
              purchase(50, "2026-10-10T08:00:00.000000Z", who=1, offer="otraofer")]
    run = _run(replay(history(events), VERSIONS, V4), T1.uid(1))
    assert (run.end, run.sends, run.end_at) == ("compro", (), at("2026-10-10T08:00:00Z"))


def test_a_purchase_of_another_person_does_not_end_the_run() -> None:
    # El control: la compra del producto de otra persona no corta.
    events = [T1.cart(1, "2026-10-10T05:00:00.000000Z", who=1),
              purchase(50, "2026-10-10T08:00:00.000000Z", who=2, offer="otraofer")]
    run = _run(replay(history(events), VERSIONS, V4), T1.uid(1))
    assert (run.end, run.sends) == ("enviado", (at("2026-10-10T15:00:00Z"),))


def test_a_purchase_between_the_creation_and_the_arrival_wins_the_run_from_the_start() -> None:
    # Aprobada medio segundo despues de la creacion y antes de que llegue el disparo: el legado la
    # vincula al crear el caso (20260808000200:70-77), y el recorrido nace ganado.
    events = [T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1), purchase(50, "2026-10-10T18:00:00.500000Z", who=1)]
    result = replay(history(events), VERSIONS, V4)
    run = _run(result, T1.uid(1))
    assert (run.end, run.sends) == ("compro", ())
    assert sum(result.bolsa_by_day.values()) == 0


@pytest.mark.parametrize("wait", [False, True])
def test_a_firing_without_contact_does_not_look_at_the_repetition_and_says_so(wait: bool) -> None:
    events = [T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1), T1.cart(2, "2026-10-10T18:20:00.000000Z", who=1)]
    for event in events:
        del event["person_key"]
    result = replay(history(events, who=()), versions(cart=WAIT_60) if wait else VERSIONS, V4)
    for number in (1, 2):
        run = _run(result, T1.uid(number))
        assert run.start.starts and run.firing.contact_ref is None
        assert "repeticion" in run.unknown
        assert len(run.sends) == 1
        # Sin espera, el envio sale al disparo y la baja se sabe: lo unico que no se sabe es la repeticion.
        assert ("baja" in run.steps[0].unknown) is wait
    # La repeticion sin contacto deja el numero como maximo, aunque el paso no tenga nada mas sin saber.
    assert flow_summary(result)["carrito"]["pasos"][0]["cota"] == "como_maximo"


def test_a_flow_turned_off_in_the_proposal_starts_nothing() -> None:
    result = replay(history([T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1)]), VERSIONS,
                    fixed({CART: None, FAILED: 4}))
    run = _run(result, T1.uid(1))
    assert (run.end, run.start.reason, run.plan) == ("no_arranco", "flujo_apagado", None)


def test_three_steps_with_the_preview_follow_the_legacy_rule() -> None:
    # Toque 2 a 13 h del primero (cae de noche, sale a las 09:00) y toque 3 a 48 h del primero,
    # no a 35 h del segundo (20260813000100:337-350).
    flows = versions(cart=steps(SEND, {"esperar": "13h"}, SEND, {"esperar": "35h"}, SEND, vence="4d"))
    run = _run(replay(history([T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1)]), flows, V4,
                      capabilities=PREVIEW_CAPABILITIES), T1.uid(1))
    assert run.sends == (at("2026-10-10T18:00:00Z"), at("2026-10-11T15:00:00Z"), at("2026-10-12T18:00:00Z"))
    assert run.steps[1].moved_by == ("franja",)
    assert all({"respuesta", "baja"} <= step.unknown for step in run.steps[1:])
    assert run.end == "enviado"


def test_the_common_expiry_leaves_a_step_out() -> None:
    flows = versions(cart=steps(SEND, {"esperar": "13h"}, SEND, {"esperar": "35h"}, SEND, vence="2d"))
    run = _run(replay(history([T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1)]), flows, V4,
                      capabilities=PREVIEW_CAPABILITIES), T1.uid(1))
    assert run.sends == (at("2026-10-10T18:00:00Z"), at("2026-10-11T15:00:00Z"))
    assert (run.steps[-1].status, run.steps[-1].reason) == ("fuera_de_plazo", "next_step_outside_expiration")
    assert (run.end, run.end_at) == ("fuera_de_plazo", at("2026-10-11T15:00:00Z"))


def test_a_step_whose_due_at_already_passed_goes_with_the_previous_never_before() -> None:
    # El toque 2 se demora por el control hasta las 21:00Z; el 3 debia salir a las 20:00Z.
    flows = versions(cart=steps(SEND, {"esperar": "1h"}, SEND, {"esperar": "1h"}, SEND, vence="4d"))
    run = _run(replay(history([T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1)],
                              control=paused("2026-10-10T18:30:00.000000Z", "2026-10-10T21:00:00.000000Z")),
                      flows, V4, capabilities=PREVIEW_CAPABILITIES), T1.uid(1))
    assert run.sends == (at("2026-10-10T18:00:00Z"), at("2026-10-10T21:00:00Z"), at("2026-10-10T21:00:00Z"))


def test_a_step_behind_the_contesta_exit_without_the_reply_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Con un disparo de evento la definicion no puede tener contesta (not_supported): la prueba
    # pone la salida en el plan, que es lo que lee el replay.
    real = flow_engine.plan_steps

    def with_reply_exit(flow, firing, *, capabilities=ENGINE_CAPABILITIES_V1):  # type: ignore[no-untyped-def]
        plan = real(flow, firing, capabilities=capabilities)
        return dataclasses.replace(plan, exits=plan.exits | {"contesta"})

    monkeypatch.setattr(flow_engine, "plan_steps", with_reply_exit)
    flows = versions(cart=steps(SEND, {"esperar": "1h"}, SEND, vence="2d"))
    run = _run(replay(history([T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1)]), flows, V4,
                      capabilities=PREVIEW_CAPABILITIES), T1.uid(1))
    first, second = run.steps
    assert (first.status, second.status) == ("enviado", "sin_saber")
    assert second.at == at("2026-10-10T19:00:00Z") and "respuesta" in second.unknown
    assert run.sends == (at("2026-10-10T18:00:00Z"),)


def test_a_purchase_between_two_touches_ends_the_run_at_its_hour() -> None:
    # La compra entre el primer toque y el segundo: el recorrido se vuelve a mirar a la hora de la
    # compra, y no a la del toque siguiente. Lo en curso al otro dia ya no lo cuenta.
    flows = versions(cart=steps(SEND, {"esperar": "24h"}, SEND, vence="4d"))
    events = [T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1), purchase(50, "2026-10-10T20:00:00.000000Z", who=1)]
    result = replay(history(events), flows, V4, capabilities=PREVIEW_CAPABILITIES)
    run = _run(result, T1.uid(1))
    assert run.sends == (at("2026-10-10T18:00:00Z"),)
    assert (run.end, run.end_at) == ("compro", at("2026-10-10T20:00:00Z"))
    assert dict(in_flight_at(result, at("2026-10-11T12:00:00Z"))) == {}


@pytest.mark.parametrize(("code", "unknown"), [
    ("pilot_scope_version_mismatch", True),     # se rechaza antes de mirar la audiencia
    ("pilot_runtime_not_armed", True),
    ("pilot_daily_budget_exhausted", False),    # la audiencia ya se miro
])
def test_a_start_the_adapter_rejected_before_the_audience_leaves_the_permission_unknown(code: str,
                                                                                         unknown: bool) -> None:
    # pilot_scope_version_mismatch y pilot_runtime_not_armed salen antes de verificar la audiencia
    # (20260930000300:270-300): el permiso no se sabe, y el envio cuenta como maximo.
    event = T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1)
    event.update(processing_status="failed", processing_error=f"pilot_scope_rejected:{code}")
    result = replay(history([event]), VERSIONS, V4)
    run = _run(result, T1.uid(1))
    assert run.start.starts and run.sends == (at("2026-10-10T18:00:00Z"),)
    assert ("permiso" in run.unknown) is unknown
    assert flow_summary(result)["carrito"]["pasos"][0]["cota"] == ("como_maximo" if unknown else "exacta")


def _night_carts_with_actions(first_action: int, second_action: int) -> History:
    events = [T1.cart(1, "2026-10-10T05:00:00.000000Z", who=1), T1.cart(2, "2026-10-10T05:30:00.000000Z", who=2)]
    cases = [T1.case_of(event, who=who) for event, who in zip(events, (1, 2), strict=True)]
    actions = [T1.action_of(case) for case in cases]
    for action, number in zip(actions, (first_action, second_action), strict=True):
        action["id"] = T1.uid(number)
    return history(events, who=(1, 2), cases=cases, actions=actions)


@pytest.mark.parametrize(("first_action", "second_action", "winner"), [(2001, 2002, 1), (2002, 2001, 2)])
def test_two_night_carts_with_the_cap_at_one_tie_by_the_legacy_action_id(first_action: int, second_action: int,
                                                                          winner: int) -> None:
    # Los dos quedan con el due_at en las 09:00 de CDMX: el despachador los toma por
    # coalesce(next_attempt_at, due_at) y despues por el id de la accion (20260803000100:2621).
    result = replay(_night_carts_with_actions(first_action, second_action), versions(cart=per_day(1, vence="3d")), V4)
    loser = 3 - winner
    assert _run(result, T1.uid(winner)).sends == (at("2026-10-10T15:00:00Z"),)
    assert _run(result, T1.uid(loser)).sends == (at("2026-10-11T15:00:00Z"),)


def test_with_the_cap_at_one_the_older_due_at_goes_first_the_next_morning() -> None:
    # A llega de dia con el tope gastado: su due_at queda a su hora. B llega de noche y se difiere a
    # las 09:00. A la manana siguiente los dos esperan a las 09:00, y el legado toma primero el
    # due_at mas viejo (20260803000100:2621), aunque B tenga el uuid menor.
    spent = [T1.authorization(1, "2026-10-10T15:00:01.000000Z", "2026-10-10")]
    events = [T1.cart(2, "2026-10-10T18:00:00.000000Z", who=2), T1.cart(1, "2026-10-11T05:00:00.000000Z", who=1)]
    result = replay(history(events, who=(1, 2), authorizations=spent), versions(cart=per_day(1, vence="3d")), V4)
    assert _run(result, T1.uid(2)).sends == (at("2026-10-11T15:00:00Z"),)
    assert _run(result, T1.uid(1)).sends == (at("2026-10-12T15:00:00Z"),)


def test_with_the_v1_a_definition_of_two_templates_is_not_supported() -> None:
    flows = versions(cart=steps(SEND, {"esperar": "1h"}, SEND, vence="2d"))
    with pytest.raises(PlanNotSupported) as raised:
        replay(history([T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1)]), flows, V4)
    assert "engine_not_supported:mas_de_un_envio" in raised.value.codes


def test_an_e_after_the_capture_is_in_flight_with_the_purchase_unknown() -> None:
    flows = versions(cart=steps({"esperar": "2h"}, SEND))
    run = _run(replay(history([T1.cart(1, "2026-10-16T20:00:00.000000Z", who=1)]), flows, V4), T1.uid(1))
    [step] = run.steps
    assert (step.at, step.status) == (at("2026-10-16T22:00:00Z"), "en_curso")
    assert "compra" in step.unknown
    assert (run.end, run.sends, run.end_at) == ("en_curso", (), None)


@pytest.mark.parametrize(("wait", "expected", "unknown"), [
    (None, "2026-10-10T18:00:00Z", False),
    ("1h", "2026-10-10T19:00:00Z", True),
])
def test_an_e_after_the_observed_send_carries_the_opt_out_unknown(wait: str | None, expected: str,
                                                                  unknown: bool) -> None:
    event = T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1, intent=101)
    case = T1.case_of(event, who=1, closed_at="2026-10-10T18:00:05.000000Z")
    action = T1.action_of(case, sent_at="2026-10-10T18:00:05.000000Z")
    flows = VERSIONS if wait is None else versions(cart=steps({"esperar": wait}, SEND))
    run = _run(replay(history([event], cases=[case], actions=[action]), flows, V4), T1.uid(1))
    [step] = run.steps
    assert (step.at, step.status) == (at(expected), "enviado")
    assert ("baja" in step.unknown) is unknown


def test_in_flight_counts_only_the_run_that_is_waiting() -> None:
    events = [T1.cart(1, "2026-10-10T05:00:00.000000Z", who=1), T1.cart(2, "2026-10-09T18:00:00.000000Z", who=2)]
    result = replay(history(events, who=(1, 2)), VERSIONS, V4)
    assert _run(result, T1.uid(2)).sends == (at("2026-10-09T18:00:00Z"),)
    assert dict(in_flight_at(result, at("2026-10-10T10:00:00Z"))) == {CART: 1}
    assert dict(in_flight_at(result, at("2026-10-10T16:00:00Z"))) == {}


def test_every_engine_flow_has_the_sql_and_dispatch_exceptions() -> None:
    for by_version in VERSIONS.values():
        for flow in by_version.values():
            assert flow.exceptions == exceptions_for(flow.legacy_kind) == exceptions_for(None)
            assert len(flow.exceptions) == 10
