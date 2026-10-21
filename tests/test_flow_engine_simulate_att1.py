"""simulate y la consistencia del motor sobre la captura de ATT1 (diseno de la T3a, §5, §6 y §7).

Fixtures: la captura de ATT1 (``tests/fixtures/flow_engine/att1_recovery_capture_20261016.json``), las
definiciones de la v3, la v4 y la v5 (``tests/fixtures/flow_simulation/att1_definitions_by_scope_version.json``),
las diferencias con nombre de la T1 (``tests/fixtures/flow_engine/att1_parity_named_differences.json``) y las
propuestas medidas (``tests/fixtures/flow_simulation/simulation_proposals.json``).

* **La consistencia (§7):** el replay «como corrio» contra ``flow_parity.parity_report`` en vivo, con sus
  conteos escritos aca: 252 disparos, 248 que llegaron, 213 arranques y 213 envios, de los que 208 los mando
  el legado y 5 son los ``lo_en_curso_sigue``; ninguna E distinta de la T1; P6 en 192 (78, 1 y 113). La
  vigente contra si misma da 0 cambios.
* **Las propuestas (§5.2 y §6):** cada una contra la v5 en toda la ventana. El resumen entero se compara con
  el fixture, y los numeros que importan van escritos aca, no leidos del fixture.

**Dos numeros difieren del diseno, y el motor es el que hace lo del legado:**

- *El pago fallido a 10 minutos* da 211 enviados y 1 que compro, no 210 y 3. Dos pagos fallidos del mismo
  contacto y la misma oferta llegan con un minuto de diferencia (22:03:35Z y 22:04:51Z del 11/10). Con la
  espera, el primero esta esperando cuando llega el segundo, y el legado suma el segundo como evidencia y le
  adelanta la accion: ``due_at = least(due_at, now())`` con la secuencia ``active``, y la accion en
  ``pending``, ``deferred`` o ``retryable_failed`` (``20260904000100_att1_payment_failure_single_initial_contact
  .sql:142-154``). La accion sale a la llegada de la repeticion (22:04:53Z), antes de la compra de las
  22:09:26Z, y la repeticion no arranca otro caso (``created = false``, ``:242``). El prototipo del diseno
  corria cada disparo por separado y daba a los dos como «compro».
- *El tope del dia en 1* da 8 enviados, 1 en curso, 4 que compraron y 199 vencidos, no 8, 1, 2 y 202. Dos
  pagos fallidos compran mientras esperan el cupo del dia siguiente (a los 1,5 y a los 5,82 minutos), y la
  repeticion de arriba se suma al primero en vez de arrancar. En el legado la compra gana el caso abierto y
  cancela la accion diferida: busca el caso del contacto, el producto y la oferta en ``grace_period``,
  ``active`` o ``paused`` (``20260808000100_hotmart_purchase_approved.sql:190-206``), cancela la accion en
  ``pending`` o ``deferred`` con ``purchase_detected`` (``:405-412``) y lo cierra en ``won`` (``:428-444``).
  El prototipo daba «vencio» a todo recorrido que no llegaba a su E, sin mirar la compra. 202 = 199 + 2 + 1.

Las dos quedan fijadas con lineas de tiempo al final, con los constructores de ``tests/test_flow_parity.py``
(importados, no copiados) y datos inventados: uuid de la forma ``00000000-0000-4000-8000-<n>``.

**El desempate a la misma hora** es el del despachador del legado, ``coalesce(next_attempt_at, due_at), sa.id``
(``20260803000100:2621``). Cada recorrido que arranca en las propuestas tiene la accion del legado en la
captura; los toques 2 y 3 no, y lo que depende de su orden lo declara ``sensible_al_orden`` en el fixture.

**Las diferencias con nombre son de la T1** (§9): si su sha256 no es el de ``c7fa8ef``, las pruebas que las leen
se saltean con el motivo, en vez de ponerse rojas por un archivo que no es de la T3a.
"""

from __future__ import annotations

import copy
import hashlib
import heapq
import importlib.util
import json
import os
import statistics
import sys
import tomllib
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_definition as fd
from bridge import flow_replay
from bridge.flow_definition import parse_items
from bridge.flow_engine import PREVIEW_CAPABILITIES, PlanNotSupported
from bridge.flow_parity import Capture
from bridge.flow_replay import (
    ConsistencyReport,
    History,
    Simulation,
    as_ran,
    consistency_report,
    current_versions,
    engine_flows_from_definitions,
    fixed,
    history_from_capture,
    proposed_versions,
    replay,
    simulate,
)
from bridge.flow_schedule import local_day
from bridge.flow_validator import InstanceView, TemplateCatalog

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_recovery_capture_20261016.json"
NAMED_FILE = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_parity_named_differences.json"
SIMULATION = ROOT / "tests" / "fixtures" / "flow_simulation"
DEFINITIONS: dict[str, Any] = json.loads(
    (SIMULATION / "att1_definitions_by_scope_version.json").read_text(encoding="utf-8"))
PROPOSALS: dict[str, Any] = json.loads((SIMULATION / "simulation_proposals.json").read_text(encoding="utf-8"))
RAW: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
# El sha256 de las diferencias con nombre de la T1 en c7fa8ef.
NAMED_SHA256_C7FA8EF = "57a4160635322ac91ea6f5de4863e91445b4ee8a7303f942565630da610d34ea"
CAPTURE = Capture.from_json(RAW)
HISTORY = history_from_capture(CAPTURE)
VERSIONS = engine_flows_from_definitions(DEFINITIONS)

CART = ("att1", "carrito")
FAILED = ("att1", "pago_fallido")
ZONE = "America/Mexico_City"
# Los 5 carritos del 12/10 que el motor manda y el legado no (lo_en_curso_sigue), escritos aca: el oraculo no
# pasa por el archivo de las diferencias con nombre.
NAMED_FIRINGS = (
    "2462eaf0-b397-45b0-9aea-2ad8c77e42ec",
    "3a069f16-20d7-414a-b937-aa4322b508f6",
    "3ee575f2-ed23-453a-ac01-f2f18bedd14a",
    "ca2e6472-b656-4b45-b1ee-eb7e961e5b76",
    "d418879a-83af-4fa8-a553-a518d6e8a3fc",
)
# La repeticion del pago fallido del 11/10 (F#11) y el disparo al que se suma.
REPEATED_FAILURE = "beee7665"
FIRST_FAILURE = "1d2cca46"

# Los constructores de las lineas de tiempo de la T1 (§2.1).
SPEC = importlib.util.spec_from_file_location("t1_parity_timelines_simulate", ROOT / "tests" / "test_flow_parity.py")
assert SPEC and SPEC.loader
T1 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = T1
SPEC.loader.exec_module(T1)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def named() -> list[dict[str, Any]]:
    """Las diferencias con nombre de la T1, o saltear: si cambiaron desde c7fa8ef, la T3a no las juzga."""
    data = NAMED_FILE.read_bytes()
    current = hashlib.sha256(data).hexdigest()
    if current != NAMED_SHA256_C7FA8EF:
        pytest.skip(f"{NAMED_FILE.name} cambio desde c7fa8ef (sha256 {current}): la consistencia de la T3a es "
                    "contra las de c7fa8ef. La 1c la vuelve a medir.")
    return json.loads(data)["diferencias"]


@cache
def _report() -> ConsistencyReport:
    return consistency_report(HISTORY, VERSIONS, named())


def report() -> ConsistencyReport:
    named()
    return _report()


@cache
def simulation(name: str) -> Simulation:
    [proposal] = [item for item in PROPOSALS["propuestas"] if item["nombre"] == name]
    current = current_versions(HISTORY, VERSIONS)
    versions, chosen = proposed_versions(VERSIONS, current, parse_items(proposal["items"]))
    capabilities = {"capabilities": PREVIEW_CAPABILITIES} if proposal["varios_toques"] else {}
    return simulate(HISTORY, versions, fixed(current), fixed(chosen), **capabilities)


def steps(result: Simulation, flow: str) -> list[dict[str, Any]]:
    return result.summary["propuesta"]["flujos"][flow]["pasos"]


def by_prefix(result: Simulation, prefix: str) -> Any:
    [run] = [run for run in result.proposed.runs if run.firing_ref.startswith(prefix)]
    return run


# ---------------------------------------------------------------------------
# La consistencia (§7).
# ---------------------------------------------------------------------------

def test_the_consistency_against_the_t1_gives_the_numbers_of_the_design() -> None:
    result = report()
    assert result.ok()
    assert result.self_changes == 0
    assert dict(result.replay_counts) == {
        "disparos": 252, "llegaron": 248, "arranques": 213, "envios": 213, "envios_del_legado": 208}
    # El lado de la T1 sale de parity_report en vivo; los numeros de la T1 ya los fija
    # tests/test_flow_engine_parity_att1.py.
    assert dict(result.t1_counts) == {"disparos": 252, "llegaron": 248, "arranques": 213, "envios_del_legado": 208}
    assert result.e_mismatches == ()
    assert result.extra_sends == result.extra_explained_by_named == NAMED_FIRINGS
    assert result.named_not_in_replay == ()
    p6 = {"cedieron": 192, "las_dos": 78, "solo_persona": 1, "solo_intencion": 113, "sin_ganador": 0,
          "ganadores_sin_cancelacion": 0}
    assert dict(result.p6) == dict(result.t1_p6) == p6


def test_the_current_is_the_v5_and_against_itself_it_changes_nothing() -> None:
    current = current_versions(HISTORY, VERSIONS)
    assert current == {CART: 5, FAILED: 5}
    same = simulate(HISTORY, VERSIONS, fixed(current), fixed(current))
    assert same.changes == ()
    assert same.summary["cambios"] == 0


def test_the_v5_on_the_whole_window_is_not_what_happened() -> None:
    # Por eso simulate compara la vigente en toda la ventana, no «como corrio»: contra lo que paso, la v5
    # contra si misma daria 5 cambios (los 5 carritos del 12/10 salen al llegar con 80 por dia).
    against_what_happened = simulate(HISTORY, VERSIONS, as_ran(), fixed({CART: 5, FAILED: 5}))
    assert sorted(change.firing_ref for change in against_what_happened.changes) == list(NAMED_FIRINGS)


def test_named_with_an_invented_uuid_is_not_ok() -> None:
    entries = copy.deepcopy(named())
    entries.append({**entries[0], "disparo": "00000000-0000-4000-8000-000000000999"})
    result = consistency_report(HISTORY, VERSIONS, entries)
    assert result.named_not_in_replay == ("00000000-0000-4000-8000-000000000999",)
    assert not result.ok()


def test_named_without_one_uuid_is_not_ok() -> None:
    entries = [entry for entry in named() if entry["disparo"] != NAMED_FIRINGS[0]]
    result = consistency_report(HISTORY, VERSIONS, entries)
    assert result.extra_sends == NAMED_FIRINGS
    assert result.extra_explained_by_named == NAMED_FIRINGS[1:]
    assert not result.ok()


def test_the_equalities_that_make_the_prototype_count() -> None:
    # §7.1, punto 5: el ancla es creation_date_ms y el vencimiento, el ancla mas 1 dia, en las 213 acciones de la
    # ventana. Las definiciones (40/400, 40/1000 y 80/1000, de 09 a 21, vence 1d y sin espera) las fija
    # tests/test_flow_engine_definitions_fixture.py.
    events = {event["id"]: event for event in CAPTURE.events}
    cases = {case["id"]: case for case in CAPTURE.cases}
    window = {firing.source_ref for firing in HISTORY.firings}
    seen = 0
    for action in CAPTURE.actions:
        event = events[cases[action["recovery_case_id"]]["abandonment_event_id"]]
        if event["id"] not in window:
            continue
        seen += 1
        anchor = at(action["anchor_observed_at"])
        assert anchor == datetime.fromtimestamp(event["creation_date_ms"] / 1000, timezone.utc)
        assert at(action["expires_at"]) - anchor == timedelta(days=1)
    assert seen == 213


# ---------------------------------------------------------------------------
# Las propuestas medidas (§5.2 y §6).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [item["nombre"] for item in PROPOSALS["propuestas"]])
def test_each_proposal_gives_its_summary(name: str) -> None:
    [proposal] = [item for item in PROPOSALS["propuestas"] if item["nombre"] == name]
    result = simulation(name)
    assert result.summary["cambios"] == len(result.changes) == proposal["esperado"]["cambios"]
    assert json.loads(json.dumps(result.summary["propuesta"])) == proposal["esperado"]["propuesta"]
    # La vigente es la misma en todas: la v5 de los dos flujos.
    assert result.summary["vigente"]["fin"] == {"enviado": 213, "no_arranco": 35, "no_llego": 4}


def test_the_cart_at_one_hour() -> None:
    result = simulation("carrito_a_1h")
    summary = result.summary["propuesta"]
    assert (summary["recibieron"], summary["en_curso"], result.summary["cambios"]) == (212, 1, 141)
    assert "compro" not in summary["fin"]
    # La baja sin saber: en 140 de los 212 enviados, 135 despues del envio observado y 5 sin envio observado.
    sent = [run for run in result.proposed.runs if run.sends]
    late = [run for run in sent if "baja" in run.steps[0].unknown]
    assert len(late) == 140
    assert sum(1 for run in late if run.firing_ref in HISTORY.observed_sends) == 135
    assert steps(result, "carrito")[0] == {"paso": 0, "enviados": 207, "terminan": {"en_curso": 1},
                                           "movidos_por_el_tope": 0, "cota": "como_maximo"}


def test_the_payment_failure_at_ten_minutes() -> None:
    result = simulation("pago_fallido_a_10m")
    summary = result.summary["propuesta"]
    assert (summary["recibieron"], summary["fin"].get("compro"), result.summary["cambios"]) == (211, 1, 4)
    assert summary["no_arrancaron"]["ya_en_curso"] == 2
    # La que compra lo hace a 1,5 minutos del rechazo. Las otras dos que compraron solas (a los 4,55 y 5,82
    # minutos) son el par del 11/10: la repeticion adelanta el envio a su llegada, antes de la compra (F#11).
    [bought] = [run for run in result.proposed.runs if run.end == "compro"]
    # Se cierra a la hora de la compra, no a los 10 minutos.
    assert bought.end_at == at("2026-10-11T15:09:15Z")
    assert timedelta(minutes=1) < bought.end_at - bought.firing.at < timedelta(minutes=2)
    first, repeated = by_prefix(result, FIRST_FAILURE), by_prefix(result, REPEATED_FAILURE)
    assert (repeated.start.reason, repeated.start.joins_run) == ("ya_en_curso", first.firing_ref)
    assert first.sends == (repeated.firing.received_at,)
    assert first.sends[0] < at("2026-10-11T22:09:26Z")


def test_the_day_cap_at_one() -> None:
    result = simulation("tope_del_dia_en_1")
    summary = result.summary["propuesta"]
    assert summary["fin"] == {"compro": 4, "en_curso": 1, "enviado": 8, "no_arranco": 36, "no_llego": 4,
                              "vencio": 199}
    # Los 8 que salen, los 4 que compran y los 199 que vencen no dependen del desempate; quienes son, si:
    # con el del legado (el due_at y el id de la accion) cambian 213 disparos contra la v5.
    assert result.summary["cambios"] == 213
    started = [run for run in result.proposed.runs if run.plan is not None]
    assert len(started) == 212 and all(run.firing_ref in HISTORY.action_ref for run in started)
    sends = sorted(when for run in result.proposed.runs for when in run.sends)
    assert sends == [at(f"2026-10-{day:02d}T15:00:00Z") for day in range(9, 17)]
    days = Counter(local_day(ZONE, when) for when in sends)
    assert max(days.values()) == 1
    # El cupo del 08/10 ya lo ocupa una autorizacion sembrada; el del 17/10 cae despues de la captura.
    assert summary["dias_al_tope"] == [f"2026-10-{day:02d}" for day in range(8, 17)]
    [waiting] = [run for run in result.proposed.runs if run.end == "en_curso"]
    assert waiting.steps[0].at == at("2026-10-17T15:00:00Z")
    # Dos de los que compran son pagos fallidos que compran esperando el cupo del dia siguiente.
    bought = [run for run in result.proposed.runs if run.end == "compro"]
    assert Counter(run.flow for run in bought) == {CART: 2, FAILED: 2}
    assert all("bolsa_dia" in run.steps[0].moved_by for run in bought)


@pytest.mark.parametrize(("name", "received", "changes"), [
    ("horario_de_10_a_20", 212, 119),
    ("horario_de_10_a_20_solo_carrito", 213, 117),
])
def test_the_window_from_ten_to_eight(name: str, received: int, changes: int) -> None:
    result = simulation(name)
    summary = result.summary["propuesta"]
    assert (summary["recibieron"], result.summary["cambios"]) == (received, changes)
    assert summary["fin"].get("compro", 0) == 213 - received


def test_three_touches_with_four_days() -> None:
    result = simulation("tres_toques_vence_4d")
    assert steps(result, "carrito") == [
        {"paso": 0, "enviados": 207, "terminan": {"en_curso": 1}, "movidos_por_el_tope": 49, "cota": "como_maximo"},
        {"paso": 1, "enviados": 183, "terminan": {"compro": 2, "en_curso": 22}, "movidos_por_el_tope": 24,
         "cota": "como_maximo"},
        {"paso": 2, "enviados": 157, "terminan": {"compro": 1, "en_curso": 25}, "movidos_por_el_tope": 41,
         "cota": "como_maximo"},
    ]
    assert steps(result, "pago_fallido") == [
        {"paso": 0, "enviados": 5, "terminan": {}, "movidos_por_el_tope": 0, "cota": "exacta"}]
    # El tope de 80 no alcanza: 5 de los 9 dias locales llegan a 80, con el maximo justo en 80.
    assert result.summary["propuesta"]["dias_al_tope"] == [f"2026-10-{day}" for day in range(11, 16)]
    assert max(result.proposed.bolsa_by_day.values()) == 80
    # Los primeros mensajes que mueve el tope salen de 12 h 6 min a 23 h 53 min tarde (mediana, 19 h 43 min).
    late = sorted(run.steps[0].at - (run.firing.at + run.plan.steps[0].after)
                  for run in result.proposed.runs
                  if run.flow == CART and run.steps and "bolsa_dia" in run.steps[0].moved_by)
    assert len(late) == 49
    assert (late[0], late[-1], statistics.median(late)) == (
        timedelta(hours=12, minutes=6, seconds=32, milliseconds=301),
        timedelta(hours=23, minutes=53, seconds=10, milliseconds=121),
        timedelta(hours=19, minutes=42, seconds=55, milliseconds=398))
    # Medido desde el primer envio, en 1 de los 157 el toque 3 sale a la misma hora que el 2.
    gaps = [run.sends[2] - run.sends[1] for run in result.proposed.runs if len(run.sends) == 3]
    assert len(gaps) == 157
    assert [gap for gap in gaps if gap < timedelta(hours=24)] == [timedelta(0)]


class _Reversed(str):
    """Un id que ordena al reves: el desempate inverso de lo que la captura no trae."""

    def __lt__(self, other: object) -> bool:
        return str.__gt__(self, other)  # type: ignore[operator]

    def __gt__(self, other: object) -> bool:
        return str.__lt__(self, other)  # type: ignore[operator]

    __hash__ = str.__hash__


def _paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        return [path for key in sorted(set(before) | set(after))
                for path in _paths(before.get(key), after.get(key), f"{prefix}.{key}" if prefix else key)]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [path for index, pair in enumerate(zip(before, after, strict=True))
                for path in _paths(*pair, f"{prefix}[{index}]")]
    return [] if before == after else [prefix]


@pytest.mark.parametrize("name", [item["nombre"] for item in PROPOSALS["propuestas"]])
def test_the_numbers_that_depend_on_an_unknown_order_are_declared(name: str,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    # Con el desempate invertido en lo que la captura no trae (los toques 2 y 3, y un recorrido sin accion del
    # legado), cambian exactamente los numeros de sensible_al_orden.
    [proposal] = [item for item in PROPOSALS["propuestas"] if item["nombre"] == name]
    expected = simulation(name)

    def push(self: Any, when: datetime, run: Any) -> None:
        run.generation += 1
        key = run.order_ref
        if run.sends or key == run.firing.source_ref:
            key = _Reversed(key)
        heapq.heappush(self.queue, (when, run.due, key, 1, run.generation))

    monkeypatch.setattr(flow_replay._Replayer, "push", push)
    simulation.cache_clear()
    try:
        reversed_order = simulation(name)
    finally:
        simulation.cache_clear()
    differ = _paths(json.loads(json.dumps(expected.summary["propuesta"])),
                    json.loads(json.dumps(reversed_order.summary["propuesta"])))
    if len(expected.changes) != len(reversed_order.changes):
        differ.insert(0, "cambios")
    assert differ == proposal["sensible_al_orden"]


def test_three_touches_with_one_day_send_no_second_touch() -> None:
    result = simulation("tres_toques_vence_1d")
    assert steps(result, "carrito") == [
        {"paso": 0, "enviados": 207, "terminan": {"en_curso": 1}, "movidos_por_el_tope": 0, "cota": "como_maximo"},
        {"paso": 1, "enviados": 0, "terminan": {"fuera_de_plazo": 207}, "movidos_por_el_tope": 0, "cota": "exacta"},
    ]


def test_three_touches_with_the_v1_simulate_like_the_preview() -> None:
    # Desde la T2.1 la v1 ejecuta hasta tres envios por camino: los tres toques dan lo mismo
    # que con la vista previa.
    [proposal] = [item for item in PROPOSALS["propuestas"] if item["nombre"] == "tres_toques_vence_4d"]
    current = current_versions(HISTORY, VERSIONS)
    versions, chosen = proposed_versions(VERSIONS, current, parse_items(proposal["items"]))
    with_v1 = simulate(HISTORY, versions, fixed(current), fixed(chosen))
    assert with_v1.summary == simulation("tres_toques_vence_4d").summary


def test_four_touches_with_the_v1_are_not_supported() -> None:
    [proposal] = [item for item in PROPOSALS["propuestas"] if item["nombre"] == "tres_toques_vence_4d"]
    items = copy.deepcopy(proposal["items"])
    pasos = items[0]["definicion"]["pasos"]
    pasos.extend([{"esperar": "24h"}, copy.deepcopy(pasos[-1])])
    current = current_versions(HISTORY, VERSIONS)
    versions, chosen = proposed_versions(VERSIONS, current, parse_items(items))
    with pytest.raises(PlanNotSupported) as raised:
        simulate(HISTORY, versions, fixed(current), fixed(chosen))
    assert "engine_not_supported:demasiados_envios" in raised.value.codes


def test_turning_a_flow_off_starts_nothing_of_it() -> None:
    off = [{"producto": "att1", "flujo": "pago_fallido", "tipo": "apagar",
            "flow_id": DEFINITIONS["flows"]["pago_fallido"]["flow_id"]}]
    current = current_versions(HISTORY, VERSIONS)
    versions, chosen = proposed_versions(VERSIONS, current, parse_items(off))
    result = simulate(HISTORY, versions, fixed(current), fixed(chosen))
    assert chosen == {CART: 5, FAILED: None}
    assert sum(1 for run in result.proposed.runs if run.flow == FAILED and run.plan is not None) == 0
    assert "pago_fallido" not in result.summary["propuesta"]["flujos"]


def test_an_in_flight_policy_other_than_follow_is_not_supported() -> None:
    [proposal] = [item for item in PROPOSALS["propuestas"] if item["nombre"] == "carrito_a_1h"]
    items = copy.deepcopy(proposal["items"])
    items[0]["en_curso"] = "cancelar"
    with pytest.raises(ValueError, match="en_curso_not_supported"):
        proposed_versions(VERSIONS, current_versions(HISTORY, VERSIONS), parse_items(items))


# ---------------------------------------------------------------------------
# Las dos diferencias con el diseno, en chico, con el legado citado.
# ---------------------------------------------------------------------------

def _definitions(key: str, change: Any) -> Any:
    document = copy.deepcopy(DEFINITIONS)
    change(document["versions"]["4"][key])
    return engine_flows_from_definitions(document)


def _wait(minutes: int) -> Any:
    def change(definition: dict[str, Any]) -> None:
        definition["pasos"].insert(0, {"esperar": f"{minutes}m"})
    return change


def _purchase(number: int, when: str, *, who: int) -> dict[str, Any]:
    ms = int(at(when).timestamp() * 1000)
    return {
        "id": T1.uid(number), "event_type": "PURCHASE_APPROVED", "received_at": T1.shifted(when, seconds=1),
        "processing_status": "processed", "approved_date_ms": ms, "creation_date_ms": ms,
        "product_id": T1.PRODUCT, "offer_code": T1.OFFER, "person_key": T1.person(who),
        "purchase_status": "APPROVED", "purchase_shape_ok": True,
    }


def _history(events: list[dict[str, Any]], **sections: Any) -> History:
    return history_from_capture(Capture.from_json(T1.build(events=events, contacts=[T1.contact(1)], **sections)))


V4 = fixed({CART: 4, FAILED: 4})


@pytest.mark.parametrize(("repeated", "end", "sent"), [
    # La repeticion al minuto, con la secuencia activa: el legado adelanta la accion a su llegada
    # (least(due_at, now()), 20260904000100:142-154) y sale antes de la compra de los 5 minutos.
    (True, "enviado", ("2026-10-10T18:01:01Z",)),
    # Sin la repeticion, la espera de 10 minutos llega despues de la compra.
    (False, "compro", ()),
])
def test_a_repeated_payment_failure_brings_the_send_before_the_purchase(repeated: bool, end: str,
                                                                       sent: tuple[str, ...]) -> None:
    failures = [T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1, event_type="PURCHASE_CANCELED")]
    if repeated:
        failures.append(T1.cart(2, "2026-10-10T18:01:00.000000Z", who=1, event_type="PURCHASE_CANCELED"))
    history = _history([*failures, _purchase(50, "2026-10-10T18:05:00.000000Z", who=1)])
    result = replay(history, _definitions("pago_fallido", _wait(10)), V4)
    first = next(run for run in result.runs if run.firing_ref == T1.uid(1))
    assert (first.end, first.sends) == (end, tuple(at(text) for text in sent))
    # Sin la repeticion se cierra a la hora de la compra, como el legado (20260808000100:428-444), no a la de
    # su espera.
    assert first.end_at == (at(sent[0]) if repeated else at("2026-10-10T18:05:00Z"))
    if repeated:
        second = next(run for run in result.runs if run.firing_ref == T1.uid(2))
        assert (second.end, second.start.reason, second.start.joins_run) == ("no_arranco", "ya_en_curso", T1.uid(1))


def test_a_purchase_during_the_first_wait_closes_the_run_before_the_next_firing() -> None:
    # La compra a los 5 minutos cierra el recorrido; un carrito de la misma persona a los 7 minutos arranca el
    # suyo, en vez de quedar ya_en_curso detras de una espera que el legado ya habia cancelado.
    events = [T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1), _purchase(50, "2026-10-10T18:05:00.000000Z", who=1),
              T1.cart(2, "2026-10-10T18:07:00.000000Z", who=1)]
    result = replay(_history(events), _definitions("carrito", _wait(10)), V4)
    first, second = (next(run for run in result.runs if run.firing_ref == T1.uid(number)) for number in (1, 2))
    assert (first.end, first.end_at) == ("compro", at("2026-10-10T18:05:00Z"))
    assert second.start.starts


def test_a_purchase_while_waiting_for_the_day_cap_wins_the_run() -> None:
    # El cupo del dia ya esta gastado y el recorrido espera la medianoche y la franja; la compra a los 90 s
    # lo gana (20260808000100:190-206, :405-412 y :428-444): termina en compro a esa hora, no en vencio.
    def cap(definition: dict[str, Any]) -> None:
        definition["topes"]["por_dia"] = 1

    spent = [T1.authorization(1, "2026-10-10T15:00:01.000000Z", "2026-10-10")]
    history = _history([T1.cart(1, "2026-10-10T18:00:00.000000Z", who=1, event_type="PURCHASE_CANCELED"),
                        _purchase(50, "2026-10-10T18:01:30.000000Z", who=1)], authorizations=spent)
    [run] = [run for run in replay(history, _definitions("pago_fallido", cap), V4).runs if run.plan is not None]
    assert (run.end, run.sends, run.end_at) == ("compro", (), at("2026-10-10T18:01:30Z"))
    assert run.steps[0].moved_by == ("bolsa_dia", "franja")


def test_a_change_in_the_named_differences_skips_and_does_not_go_red(monkeypatch: pytest.MonkeyPatch,
                                                                      tmp_path: Path) -> None:
    # La sesion comercial regenera las diferencias con nombre de la T1: la consistencia de la T3a se saltea con
    # el motivo, no se pone roja por un archivo que no es suyo.
    changed = tmp_path / NAMED_FILE.name
    changed.write_text(NAMED_FILE.read_text(encoding="utf-8").replace("lo_en_curso_sigue", "otra"), encoding="utf-8")
    monkeypatch.setitem(globals(), "NAMED_FILE", changed)
    with pytest.raises(pytest.skip.Exception, match="cambio desde c7fa8ef"):
        test_named_without_one_uuid_is_not_ok()


# ---------------------------------------------------------------------------
# El primer cambio de Brayan (T2.1): A2 a las 36 h en vez de 24, sobre el carrito de tres toques.
# ---------------------------------------------------------------------------
#
# El documento de versiones (``flow_simulation/att1_cart_three_touches_a2_36h.json``) lo genera esta
# prueba: la vigente es el carrito de att1-recuperacion v7 como lo traduce tests/flow_store_translator.py de
# la captura del 21/10 (con ATT1_TOUCHES_20261021), y la propuesta, el mismo con A2 a las 36 h y Z a las
# 72 h del primero, como antes. Se corre sobre la captura del 16/10, con el pago fallido en su v5. Para
# regenerarlo: ``FLOW_SIMULATION_FIXTURE_DIR=tests/fixtures/flow_simulation uv run pytest
# tests/test_flow_engine_simulate_att1.py -k a2_at_36``.

A2_36H_FILE = SIMULATION / "att1_cart_three_touches_a2_36h.json"
FLOW_STORE_DIR = ROOT / "tests" / "fixtures" / "flow_store"


def _translated_cart_v7() -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("flow_store_translator", ROOT / "tests" / "flow_store_translator.py")
    assert spec and spec.loader
    translator = importlib.util.module_from_spec(spec)
    # Una dataclass busca su modulo en sys.modules al definirse.
    sys.modules[spec.name] = translator
    spec.loader.exec_module(translator)
    config = json.loads((FLOW_STORE_DIR / "att1_flow_config_20261021.json").read_text(encoding="utf-8"))
    manifest = tomllib.loads((FLOW_STORE_DIR / "att1_instancia_f01fbcc.toml").read_text(encoding="utf-8"))
    blocks = json.loads((FLOW_STORE_DIR / "schema_examples_v1.json").read_text(encoding="utf-8"))["blocks"]
    translated = translator.translate_att1(config, manifest, blocks, translator.ATT1_TOUCHES_20261021)
    instance = DEFINITIONS["instance"]
    catalog = TemplateCatalog.from_inbox_payload(json.loads(
        (ROOT / "tests" / "fixtures" / "chatwoot_inbox_11_message_templates_20261010.json").read_text(
            encoding="utf-8")))
    result = fd.normalize_definition(
        translated["carrito"].definition, new_flow=True, catalog=catalog,
        instance=InstanceView(instance["tenant_ref"], instance["ally_ref"], frozenset(instance["products"]),
                              instance["time_zone"]))
    assert result.findings == ()
    return fd.as_draft(fd.as_published(result.definition, 1))


def _a2_36h_document() -> dict[str, Any]:
    current = _translated_cart_v7()
    proposed = copy.deepcopy(current)
    assert [step.get("esperar") for step in proposed["pasos"]] == [None, "24h", None, "48h", None]
    proposed["pasos"][1]["esperar"] = "36h"
    proposed["pasos"][3]["esperar"] = "36h"
    flow_id = DEFINITIONS["flows"]["carrito"]["flow_id"]

    def item(definition: dict[str, Any]) -> dict[str, Any]:
        return {"producto": "att1", "flujo": "carrito", "flow_id": flow_id, "tipo": "version",
                "definicion": definition, "en_curso": "seguir"}

    return {
        "_doc": [
            "El primer cambio de Brayan, simulado (T2.1, 2026-10-21): A2 a las 36 h del primer envio en vez de 24,"
            " con Z a las 72 h, como antes. Lo genera tests/test_flow_engine_simulate_att1.py"
            " (test_a2_at_36_hours_document_is_what_the_translator_gives).",
            "vigente: el carrito de att1-recuperacion v7 (att1-carrito-tres-toques v1), traducido de"
            " flow_store/att1_flow_config_20261021.json con ATT1_TOUCHES_20261021 y normalizado. propuesta: el"
            " mismo con las dos esperas en 36h. Cada uno es una version nueva sobre la v5 de"
            " att1_definitions_by_scope_version.json; el pago fallido sigue en su v5.",
            "regenerar: FLOW_SIMULATION_FIXTURE_DIR=tests/fixtures/flow_simulation uv run pytest"
            " tests/test_flow_engine_simulate_att1.py -k a2_at_36",
        ],
        "vigente": [item(current)],
        "propuesta": [item(proposed)],
    }


def _a2_36h() -> Simulation:
    document = json.loads(A2_36H_FILE.read_text(encoding="utf-8"))
    current = current_versions(HISTORY, VERSIONS)
    with_v7, chosen_v7 = proposed_versions(VERSIONS, current, parse_items(document["vigente"]))
    with_36h, chosen_36h = proposed_versions(with_v7, chosen_v7, parse_items(document["propuesta"]))
    return simulate(HISTORY, with_36h, fixed(chosen_v7), fixed(chosen_36h))


def test_a2_at_36_hours_document_is_what_the_translator_gives() -> None:
    document = _a2_36h_document()
    target = os.environ.get("FLOW_SIMULATION_FIXTURE_DIR")
    if target:
        (Path(target) / A2_36H_FILE.name).write_text(
            json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return
    assert json.loads(A2_36H_FILE.read_text(encoding="utf-8")) == document, (
        f"{A2_36H_FILE.name} no es el de hoy: regenerarlo con FLOW_SIMULATION_FIXTURE_DIR")


def test_a2_at_36_hours_changes_only_the_second_step_of_the_cart() -> None:
    # Los cambios caen en el paso 1 (A2): sale 12 h despues. A1 queda igual, y Z, que va a las 72 h del
    # primer envio en las dos, tambien. La unica diferencia en Z es el horizonte de la captura: 10 recorridos
    # que en la vigente ya mandaron A2 y esperan Z, en la propuesta todavia esperan A2 (Z no esta planeada).
    result = _a2_36h()
    assert result.summary["cambios"] == len(result.changes) == 206
    waiting_for_a2 = 0
    for change in result.changes:
        old, new = change.before, change.after
        assert old.flow == new.flow == CART, change.firing_ref
        assert old.start == new.start, change.firing_ref
        before = {step.index: (step.at, step.status, step.reason) for step in old.steps}
        after = {step.index: (step.at, step.status, step.reason) for step in new.steps}
        assert before[0] == after[0], change.firing_ref
        assert before[1][0] != after[1][0], change.firing_ref
        if after[1][1] == "en_curso" and before[1][1] != "en_curso":
            assert 2 not in after and before[2][1] == "en_curso", change.firing_ref
            waiting_for_a2 += 1
        else:
            assert before.get(2) == after.get(2), change.firing_ref
    assert waiting_for_a2 == 10
    steps = {name: result.summary[name]["flujos"]["carrito"]["pasos"] for name in ("vigente", "propuesta")}
    assert steps["vigente"][0] == steps["propuesta"][0]
    for name in ("vigente", "propuesta"):
        # Z: los mismos enviados y las mismas compras; lo en curso se corre al paso 1.
        assert (steps[name][2]["enviados"], steps[name][2]["terminan"]["compro"]) == (150, 2)
    assert (steps["vigente"][1]["enviados"], steps["propuesta"][1]["enviados"]) == (188, 178)
    assert result.summary["vigente"]["fin"] == result.summary["propuesta"]["fin"]
    # El pago fallido no cambia.
    assert (result.summary["vigente"]["flujos"]["pago_fallido"]
            == result.summary["propuesta"]["flujos"]["pago_fallido"])
