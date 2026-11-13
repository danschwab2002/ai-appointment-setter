"""C10: pasar el carrito del legado al nativo sin mensajes dobles, con varios toques (E-483).

La base la mide ``tests/sql/followup_engine/validate_e483_native_legacy_double_case_touches.mjs``
(PGlite, la cadena entera): la guarda del doble caso (20261030000100 y la del pago
rechazado de 20261030000200) cubre los toques siguientes, porque un caso con un toque
esperando sigue ``active``. Esa prueba deja lo que contesta la base en
``tests/fixtures/flow_engine/native_legacy_double_case_touches_rows.json``. Aca:

* el bridge hace con esas filas lo que dice C10: ``caso_del_legado_en_curso`` y
  ``flujo_pausado`` vuelven al legado (que contesta como hoy), y ``ya_en_curso`` del
  carrito nativo deja el evento sin el legado; el que ya empezo sigue (D-C10-1);
* la paridad de la politica del carrito de ATT1: la definicion que la base compilo por
  la ruta motor es la traduccion del 21/10 (la que fija ``test_flow_policy.py``), su
  fila es la de la gemela de Python y la de la captura v3 de att1-db
  (``att1_recovery_capture_v3_20261021.json``, ``att1-carrito-tres-toques`` v1); la
  nativa, la misma salvo las diferencias con nombre de ``NAMED``. Una diferencia sin
  nombre, o un nombre que ya no ocurre, es rojo.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from bridge.flow_policy import compile_policy, differences_from_row, interval_of
from bridge.native_flows import NATIVE_CART_UNCLAIMED_REASONS, NATIVE_PAYMENT_FAILURE_ABSORBED_REASONS

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
FLOW_STORE = FIXTURES / "flow_store"
ROWS: dict[str, Any] = json.loads(
    (FIXTURES / "flow_engine" / "native_legacy_double_case_touches_rows.json").read_text(encoding="utf-8"))
CAPTURE_V3: dict[str, Any] = json.loads(
    (FIXTURES / "flow_engine" / "att1_recovery_capture_v3_20261021.json").read_text(encoding="utf-8"))
[CAPTURED_CART] = [row for row in CAPTURE_V3["config"]["followup_policy_versions"]
                   if (row["policy_key"], row["version"]) == ("att1-carrito-tres-toques", 1)]

SPEC = importlib.util.spec_from_file_location("flow_store_translator", ROOT / "tests" / "flow_store_translator.py")
assert SPEC and SPEC.loader
TRANSLATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TRANSLATOR
SPEC.loader.exec_module(TRANSLATOR)
CONFIG_21: dict[str, Any] = json.loads((FLOW_STORE / "att1_flow_config_20261021.json").read_text(encoding="utf-8"))
MANIFEST: dict[str, Any] = tomllib.loads((FLOW_STORE / "att1_instancia_f01fbcc.toml").read_text(encoding="utf-8"))
BLOCKS: list[dict[str, Any]] = json.loads(
    (FLOW_STORE / "schema_examples_v1.json").read_text(encoding="utf-8"))["blocks"]
TRANSLATED_21 = TRANSLATOR.translate_att1(CONFIG_21, MANIFEST, BLOCKS, TRANSLATOR.ATT1_TOUCHES_20261021)
FLOW_ID = "0b3c5f00-0e30-4000-8000-0000000c1000"

# Las diferencias de la fila nativa contra la de la ruta motor (y la de la captura), con su porque.
NAMED: dict[str, tuple[Any, Any, str]] = {
    "steps[0].coupon_ordinal": (
        None, 1,
        "a1_cupon_por_flag: en el legado y en el motor v1 el cupon de A1 lo gobierna "
        "RECOVERY_DISCOUNT_FIRST_TOUCH_ENABLED y el paso va sin marca; el nativo lo declara (C2, 20261112210500)"),
    "steps[1].step_key": (
        "cart_second_touch", "native_touch_2",
        "la ranura del toque 2 nativo (20261112210000): el bridge compone el paso nativo por su contexto, no por el step_key"),
    "steps[2].step_key": (
        "cart_last_touch", "native_touch_3",
        "la ranura del toque 3 nativo (20261112210000); el cupon 2 y la espera de Z son los mismos"),
}
# Lo que la definicion nativa cambia de la del motor, y por que la base no la abre sin eso.
DEFINITION_CHANGES = {
    "flujo": ("carrito", "carrito_nativo", "flow_key_reserved_for_legacy: la clave del legado no se publica nativa"),
    "termina": ({"si": ["compra"], "vence": "5d"}, {"si": ["compra", "contesta"], "vence": "5d"},
                "un nativo de varios toques pide contesta (20261112210000); el legado corta con prospect_replied"),
    "topes.bolsa": ("att1-recuperacion", "att1-carrito-nativo",
                    "flow_engine_pool_is_legacy_scope: la bolsa nativa no puede ser el scope del legado"),
}


def _delay_hours(step: dict[str, Any]) -> float | None:
    delay = step.get("delay")
    if delay is None:
        return None
    text = str(delay)
    if text.endswith(" hours"):
        return float(text.split()[0])
    parsed = interval_of(text)
    assert parsed is not None, text
    return parsed.total_seconds() / 3600


def _flat(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("timezone", "business_windows", "grace_period", "expires_after", "max_automatic_messages", "purpose"):
        value = row[key]
        out[key] = interval_of(value) if key in ("grace_period", "expires_after") else value
    for index, step in enumerate(row["steps"]):
        for key in ("mode", "step_key", "delay", "approved_template", "coupon_ordinal"):
            out[f"steps[{index}].{key}"] = _delay_hours(step) if key == "delay" else step.get(key)
    return out


def _diff(left: dict[str, Any], right: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    a, b = _flat(left), _flat(right)
    return {key: (a.get(key), b.get(key)) for key in sorted(set(a) | set(b)) if a.get(key) != b.get(key)}


def test_the_motor_definition_is_the_translation_of_att1_of_21_10() -> None:
    assert ROWS["paridad"]["definicion_motor"] == json.loads(json.dumps(TRANSLATED_21["carrito"].definition))


def test_the_native_definition_changes_only_what_the_base_asks() -> None:
    motor, native = ROWS["paridad"]["definicion_motor"], ROWS["paridad"]["definicion_nativa"]
    changed = {key for key in set(motor) | set(native) if motor.get(key) != native.get(key)}
    assert changed == {"flujo", "termina", "topes"}
    assert {key: value for key, value in native["topes"].items() if key != "bolsa"} == {
        key: value for key, value in motor["topes"].items() if key != "bolsa"}
    for key, (before, after, _why) in DEFINITION_CHANGES.items():
        head, _, tail = key.partition(".")
        assert (motor[head][tail] if tail else motor[head]) == before, key
        assert (native[head][tail] if tail else native[head]) == after, key


def test_the_motor_row_of_the_base_is_the_python_twin_and_the_captured_row() -> None:
    compiled = compile_policy(TRANSLATED_21["carrito"].definition, flow_id=FLOW_ID, version=1, legacy_kind="carrito")
    assert differences_from_row(compiled, CAPTURED_CART) == ()
    assert _diff(ROWS["paridad"]["motor"], CAPTURED_CART) == {}
    twin = compiled.as_json()
    twin["grace_period"] = str(compiled.grace_period)
    twin["expires_after"] = f"{int(compiled.expires_after.total_seconds()) // 3600}:00:00"
    assert _diff(ROWS["paridad"]["motor"], twin) == {}


def test_the_native_row_differs_from_the_motor_and_the_capture_only_by_name() -> None:
    against_motor = _diff(ROWS["paridad"]["motor"], ROWS["paridad"]["nativo"])
    against_capture = _diff(CAPTURED_CART, ROWS["paridad"]["nativo"])
    expected = {key: (before, after) for key, (before, after, _why) in NAMED.items()}
    assert against_motor == expected
    assert against_capture == expected


def test_the_waits_of_the_three_touches_are_the_same() -> None:
    hours = [[_delay_hours(step) for step in row["steps"]]
             for row in (ROWS["paridad"]["motor"], ROWS["paridad"]["nativo"], CAPTURED_CART)]
    assert hours == [[None, 24.0, 72.0]] * 3


def test_the_one_that_started_goes_on_and_the_bridge_reads_it_that_way() -> None:
    # Legado en curso con A2 esperando: el nativo no arranca, el carrito vuelve al legado y A2 sale.
    legacy = ROWS["legado_en_curso"]
    assert legacy["status"] == "active"
    assert legacy["nativo"] == "caso_del_legado_en_curso" and legacy["nativo"] in NATIVE_CART_UNCLAIMED_REASONS
    assert legacy["legado"] == "pilot_case_binding_conflict"
    assert legacy["a2_del_legado"].startswith("execute:")
    # Nativo en curso con el toque 2 esperando: ya_en_curso no es «sin reclamo» (el evento queda failed,
    # sin el legado); pausado, el legado no abre caso y el toque 2 sale.
    native = ROWS["nativo_en_curso"]
    assert native["status"] == "active"
    assert native["otra_vez"] == "ya_en_curso" and native["otra_vez"] not in NATIVE_CART_UNCLAIMED_REASONS
    assert "flujo_pausado" in NATIVE_CART_UNCLAIMED_REASONS
    assert native["legado"] == "pilot_scope_rejected:flujo_nativo_en_curso"
    assert native["toque_2"].startswith("execute:")
    # La ruta motor de ATT1 contra el nativo en cohorte.
    motor = ROWS["ruta_motor"]
    assert motor["nativo_sobre_motor"] in NATIVE_CART_UNCLAIMED_REASONS
    assert motor["motor_otra_vez"] == "ya_en_curso"
    assert motor["fuera_de_cohorte"] == "ya_en_curso" and motor["fuera_de_cohorte"] not in NATIVE_CART_UNCLAIMED_REASONS
    assert motor["motor_sobre_nativo"] == "legado_rechazo:pilot_scope_rejected:flujo_nativo_en_curso"


def test_the_shared_scope_of_maribel() -> None:
    shared = ROWS["scope_compartido"]
    assert shared["pago_legado_en_curso"] == {"pago_nativo": "caso_del_legado_en_curso",
                                              "carrito_nativo": "caso_del_legado_en_curso"}
    paying = shared["pago_nativo_en_curso"]
    # El rechazo nuevo con el pago nativo prendido se suma al recorrido (el bridge lo deja processed).
    assert paying["otro_rechazo"] in NATIVE_PAYMENT_FAILURE_ABSORBED_REASONS
    assert paying["pausado"] in NATIVE_CART_UNCLAIMED_REASONS
    assert paying["legado_del_pago"] == paying["legado_del_carrito"] == "pilot_scope_rejected:flujo_nativo_en_curso"
    # Un carrito en curso y un pago rechazado nuevo: lo mismo con el legado que con el nativo.
    assert shared["carrito_en_curso_y_pago"] == {"legado_pago": True, "nativo_pago": True}


def test_the_new_test_runs_last_with_npm_test() -> None:
    package = json.loads((ROOT / "tests" / "sql" / "followup_engine" / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["test"].endswith(
        " && node validate_flow_audience_native_e2e.mjs && node validate_e483_native_legacy_double_case_touches.mjs --check && node validate_e483_native_person_cap.mjs"
        " && node validate_e528_native_payment_slip.mjs && node validate_identity_link_decider.mjs")
