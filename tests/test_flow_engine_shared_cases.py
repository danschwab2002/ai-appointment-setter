"""Los casos compartidos de la T1 contra el motor: el horario y la politica.

Diseno de la T3a, §8.3. Fixtures (``tests/fixtures/flow_engine/``, de la T1, sin tocar):

* ``window_cases.json`` ya ata ``flow_schedule`` con la reevaluacion real en las
  dos capas. La puerta del motor usa ``is_open`` y ``next_open`` y nada mas:
  aca se mira que ``first_eligible``, con el control armado y sin topes, de la
  ``at`` del caso si ``is_open`` es verdadero y el ``next_open`` del caso si no,
  antes de su ``expires_at``; y que ``evaluate_gate`` decida lo que decidio la
  base (``reevaluation``). Se compara contra los valores escritos en el JSON,
  no contra ``next_open(at)``, que es la misma funcion que usa la puerta.
* ``compile_cases.json`` ya ata ``compile_policy``. Para cada caso con fila
  esperada, ``plan_steps`` da la franja de esa fila (comparada por ``is_open``
  y ``next_open`` sobre un juego de horas: con ``siempre``, ``franja_of`` da
  ``always`` y la fila, de 00:00 a 23:59), el comienzo de su gracia y el
  vencimiento de su ``expires_after``, y un paso del plan por cada paso de la
  fila, con su ``step_key`` y su espera (desde la T2.1, varios). Para cada caso
  con error, ``plan_steps`` da un codigo de la misma familia (la tabla
  ``EQUIVALENT``, o ``PLAN_ERROR_BY_CASE`` si el mismo codigo de la politica
  tiene dos respuestas del plan), o «no aplica»: el flow_id, la version, el
  legacy_kind y la forma de su flujo del legado no los mira.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_engine as engine
from bridge.flow_engine import EngineFlow, Firing, PlanInvalid, PlanNotSupported, RunView, evaluate_gate
from bridge.flow_parity import EXCEPTIONS_V1
from bridge.flow_schedule import franja_of, franja_of_policy, is_open, next_open

ROOT = Path(__file__).resolve().parents[1]
FLOW_ENGINE = ROOT / "tests" / "fixtures" / "flow_engine"
WINDOW_CASES: list[dict[str, Any]] = json.loads(
    (FLOW_ENGINE / "window_cases.json").read_text(encoding="utf-8"))["cases"]
COMPILE_CASES: list[dict[str, Any]] = json.loads(
    (FLOW_ENGINE / "compile_cases.json").read_text(encoding="utf-8"))["cases"]
SQL_AND_DISPATCH = frozenset(item.name for item in EXCEPTIONS_V1 if item.layer in ("sql", "despacho"))
# Los topes que le faltan a las definiciones minimas de compile_cases.json (solo
# traen horario, pasos y termina). No cambian la franja, la gracia ni el plazo.
CAPS = {"bolsa": "att1-recuperacion", "por_dia": 80, "total": 1000}
# El error de compile_policy y el de plan_steps, de la misma familia. None es
# «no aplica»: plan_steps no lo mira y da un plan.
EQUIVALENT: dict[str, str | None] = {
    "compile_policy_flow_id_invalid": None,
    "compile_policy_version_invalid": None,
    "compile_policy_legacy_kind_without_step": None,
    "compile_policy_horario_invalid": "plan_horario_invalid",
    "compile_policy_step_not_supported:conversar": "engine_not_supported:conversar",
    "compile_policy_step_not_supported:condicion": "engine_not_supported:condicion",
    "compile_policy_step_not_supported:derivar": "engine_not_supported:derivar",
    "compile_policy_step_not_supported:desconocido": "engine_not_supported:desconocido",
    # La forma del flujo del legado es de la politica: el plan no la conoce (salvo el
    # tope de envios de las capacidades, que va por caso en PLAN_ERROR_BY_CASE).
    "flow_policy_not_compilable:more_sends_than_steps": None,
    "flow_policy_not_compilable:coupon_not_allowed:cart_second_touch": None,
    "flow_policy_not_compilable:coupon_missing:cart_last_touch": None,
    "flow_policy_not_compilable:fixed_coupon:first_contact": None,
    "flow_policy_not_compilable:variables_differ:cart_second_touch": None,
    "flow_policy_not_compilable:variables_differ:first_contact": None,
    "flow_policy_not_compilable:espera_entre_envios:cart_second_touch": "engine_not_supported:espera_entre_envios",
    "compile_policy_step_after_send": "engine_not_supported:espera_despues_del_envio",
    "compile_policy_without_send": "plan_without_send",
    "compile_policy_wait_invalid": "plan_wait_invalid",
    "compile_policy_vence_invalid": "plan_vence_invalid",
}
# Los casos en que el plan responde distinto que el resto de su codigo: cuatro envios
# pasan el tope de las capacidades v1 (tres por camino).
PLAN_ERROR_BY_CASE: dict[str, str] = {
    "cuarto_envio": "engine_not_supported:demasiados_envios",
    # Dos envios sin espera entre ellos: el motor tampoco los ejecuta (T2.1).
    "dos_plantillas": "engine_not_supported:espera_entre_envios",
}


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


class Armed:
    def state_at(self, at: datetime) -> str:
        return "armed"

    def next_armed(self, at: datetime) -> datetime | None:
        return None


class Empty:
    def counts_before(self, at: datetime, zone: str) -> tuple[int, int]:
        return 0, 0


def firing(at: datetime) -> Firing:
    return Firing(source_ref="00000000-0000-4000-8000-000000000001", event="carrito", at=at, received_at=at,
                  product_ref="att1", offer_ref=None, contact_ref=None, intent_refs=frozenset(),
                  permission="formulario")


def flow_from_case(case: dict[str, Any]) -> EngineFlow:
    data = case["input"]
    definition = copy.deepcopy(data["definition"])
    definition.setdefault("topes", CAPS)
    return EngineFlow(
        flow_id=data["flow_id"] if isinstance(data["flow_id"], str) else "", product_ref="att1",
        flow_key="carrito", version=data["version"], definition=definition, legacy_kind=data["legacy_kind"],
        fixed_zone=data["fixed_zone"], pool="att1-recuperacion", exceptions=SQL_AND_DISPATCH,
    )


# ---------------------------------------------------------------------------
# 1. window_cases.json.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", WINDOW_CASES, ids=[case["id"] for case in WINDOW_CASES])
def test_first_eligible_gives_the_at_or_the_next_open_of_the_case(case: dict[str, Any]) -> None:
    at, expires = instant(case["at"]), instant(case["expires_at"])
    expected_open = instant(case["next_open"])
    for franja in (franja_of(case["horario"], fixed_zone=case["fixed_zone"]), franja_of_policy(case["policy"])):
        found = engine.first_eligible(at, expires, franja=franja, control=Armed(), bolsa=Empty(),
                                      zone=franja.zone or "UTC", per_day=None, total=None)
        if case["is_open"]:
            assert found == engine.Eligible(at, (), None)
        elif expected_open < expires:
            assert found == engine.Eligible(expected_open, ("franja",), None)
        else:
            assert found == engine.Eligible(None, ("franja",), "vencio")


# La reevaluacion de la base, en la decision de la puerta del motor.
REEVALUATION = {
    ("execute", "eligible_for_execution"): ("mandar", None),
    ("defer", "business_window_closed"): ("esperar", "fuera_de_horario"),
    ("escalate", "no_business_window_before_expiry"): ("escalar", "sin_horario"),
}


@pytest.mark.parametrize("case", WINDOW_CASES, ids=[case["id"] for case in WINDOW_CASES])
def test_evaluate_gate_decides_what_the_base_decided(case: dict[str, Any]) -> None:
    # Un flujo de un envio, sin espera, con la franja y el plazo del caso: la
    # puerta mirada en at.
    at, expires = instant(case["at"]), instant(case["expires_at"])
    definition = {
        "dispara": {"evento": "carrito"}, "horario": case["horario"],
        "pasos": [{"plantilla": "att1_carrito_abandonado_01", "idioma": "es_MX", "variables": ["nombre"]}],
        "termina": {"si": ["compra"], "vence": "1d"}, "topes": CAPS,
    }
    flow = EngineFlow("1b000003-0000-4000-8000-0000000c0001", "att1", "carrito", 1, definition, "carrito",
                      case["fixed_zone"], "att1-recuperacion", SQL_AND_DISPATCH)
    fired = firing(at)
    plan = engine.plan_steps(flow, fired)
    # El plazo del caso no siempre es de un dia: va en el plan.
    plan = engine.Plan(plan.steps, expires - at, plan.franja, plan.pool, plan.per_day, plan.total, plan.exits)
    run = RunView("r1", flow.ref, 1, fired, at, (), None)
    decision = evaluate_gate(flow, run, plan, engine.NO_FACTS, at, control=Armed(), bolsa=Empty())
    reevaluation = case["reevaluation"]
    assert (decision.decision, decision.reason) == REEVALUATION[(reevaluation["decision"], reevaluation["reason_code"])]
    if reevaluation["decision"] == "defer":
        assert decision.next_at == instant(reevaluation["due_at"])


# ---------------------------------------------------------------------------
# 2. compile_cases.json.
# ---------------------------------------------------------------------------

EXPECTED = [case for case in COMPILE_CASES if "expected" in case]
ERRORS = [case for case in COMPILE_CASES if "error" in case]
# Un juego de horas que cruza los bordes de cada franja de los casos: cada 15
# minutos de dos dias, con los segundos 0 y 59.
HOURS = [datetime(2026, 10, 16, tzinfo=timezone.utc) + timedelta(minutes=15 * step, seconds=second)
         for step in range(2 * 24 * 4) for second in (0, 59)]
HOURS += [datetime(2026, 3, 29, tzinfo=timezone.utc) + timedelta(minutes=15 * step) for step in range(24 * 4)]


@pytest.mark.parametrize("case", EXPECTED, ids=[case["id"] for case in EXPECTED])
def test_plan_steps_gives_the_franja_the_grace_and_the_deadline_of_the_compiled_row(case: dict[str, Any]) -> None:
    flow = flow_from_case(case)
    plan = engine.plan_steps(flow, firing(HOURS[0]))
    expected = case["expected"]
    # CompiledPolicy no es un Mapping: la fila es as_json(), la del fixture.
    row_franja = franja_of_policy(expected)
    for hour in HOURS:
        assert is_open(plan.franja, hour) == is_open(row_franja, hour), (case["id"], hour)
        assert next_open(plan.franja, hour) == next_open(row_franja, hour), (case["id"], hour)
    assert expected["max_automatic_messages"] == len(plan.steps) == len(expected["steps"])
    assert plan.steps[0].after == timedelta(seconds=expected["grace_period_seconds"])
    assert plan.expires_after == timedelta(seconds=expected["expires_after_seconds"])
    assert [step.step_key for step in plan.steps] == [step["step_key"] for step in expected["steps"]]
    # Desde el segundo, la espera desde el primer envio es el delay de la fila.
    for step, row_step in zip(plan.steps[1:], expected["steps"][1:]):
        assert step.after == delay_of(row_step["delay"]), case["id"]


def delay_of(text: str) -> timedelta:
    number, unit = text.split()
    return timedelta(minutes=int(number) * {"minute": 1, "hour": 60, "day": 24 * 60}[unit.rstrip("s")])


def test_every_compile_error_has_its_equivalent() -> None:
    assert {case["error"] for case in ERRORS} == set(EQUIVALENT)
    assert set(PLAN_ERROR_BY_CASE) <= {case["id"] for case in ERRORS}


@pytest.mark.parametrize("case", ERRORS, ids=[case["id"] for case in ERRORS])
def test_plan_steps_gives_an_error_of_the_same_family(case: dict[str, Any]) -> None:
    flow = flow_from_case(case)
    expected = PLAN_ERROR_BY_CASE.get(case["id"], EQUIVALENT[case["error"]])
    if expected is None:
        plan = engine.plan_steps(flow, firing(HOURS[0]))
        assert plan.steps
        return
    with pytest.raises((PlanInvalid, PlanNotSupported)) as raised:
        engine.plan_steps(flow, firing(HOURS[0]))
    assert str(raised.value) == expected
    if expected.startswith("engine_not_supported:"):
        assert engine.not_supported(flow.definition)[0] == expected
