"""La politica compilada de un flujo del motor, contra sus casos y contra la fila real de ATT1.

Diseno del motor de flujos, §2.5 y §5.3. Fixtures:

* ``flow_engine/compile_cases.json``: los casos de ``compile_policy``, que T2
  corre tambien contra ``_flow_engine_compile_policy``; ``_formato`` dice como
  se leen.
* ``flow_store/att1_flow_config_20261016.json``: la configuracion de att1-db
  del 2026-10-16 a las 13:15:51Z (leida en solo lectura), con las dos filas de
  ``followup_policy_versions``: ``att1-recuperacion-un-toque`` (la comparten el
  carrito y el pago fallido, con los dos pasos) y
  ``att1-primer-contacto-formulario``.
* ``flow_store/att1_instancia_f01fbcc.toml`` y ``schema_examples_v1.json``:
  lo que el traductor (``tests/flow_store_translator.py``, sin tocar) necesita
  ademas de la configuracion.
* ``flow_store/att1_definitions_20261016.json``: las mismas definiciones,
  normalizadas, como las publica PGlite.

La prueba que pide el diseno: ``compile_policy(translate(config))`` del
carrito y del pago fallido contra la fila de ``att1-recuperacion-un-toque``, y
la del primer contacto contra la suya, con ``differences_from_row`` vacio.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib.util
import json
import sys
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from bridge.flow_policy import (
    COMPARED_FIELDS,
    STEP_KEY_BY_LEGACY_KIND,
    CompiledPolicy,
    PolicyCompileError,
    compile_policy,
    differences_from_row,
    interval_of,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
FLOW_STORE = FIXTURES / "flow_store"
COMPILE_CASES: dict[str, Any] = json.loads(
    (FIXTURES / "flow_engine" / "compile_cases.json").read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = COMPILE_CASES["cases"]

SPEC = importlib.util.spec_from_file_location("flow_store_translator", ROOT / "tests" / "flow_store_translator.py")
assert SPEC and SPEC.loader
TRANSLATOR = importlib.util.module_from_spec(SPEC)
# Una dataclass busca su modulo en sys.modules al definirse.
sys.modules[SPEC.name] = TRANSLATOR
SPEC.loader.exec_module(TRANSLATOR)

CONFIG: dict[str, Any] = json.loads((FLOW_STORE / "att1_flow_config_20261016.json").read_text(encoding="utf-8"))
MANIFEST: dict[str, Any] = tomllib.loads((FLOW_STORE / "att1_instancia_f01fbcc.toml").read_text(encoding="utf-8"))
BLOCKS: list[dict[str, Any]] = json.loads(
    (FLOW_STORE / "schema_examples_v1.json").read_text(encoding="utf-8"))["blocks"]
TRANSLATED = TRANSLATOR.translate_att1(CONFIG, MANIFEST, BLOCKS)
NORMALIZED = {definition["flujo"]: definition for definition in json.loads(
    (FLOW_STORE / "att1_definitions_20261016.json").read_text(encoding="utf-8"))["definitions"]}
ROWS = {row["policy_key"]: row for row in CONFIG["followup_policy_versions"]}

# Por flujo de ATT1: su legacy_kind y la fila de hoy de su politica.
ATT1 = {
    "carrito": ("carrito", "att1-recuperacion-un-toque"),
    "pago_fallido": ("pago_fallido", "att1-recuperacion-un-toque"),
    "precheckout": ("precheckout", "att1-primer-contacto-formulario"),
}
FLOW_ID = "1b000001-0000-4000-8000-00000000c001"


def _compile(case: dict[str, Any]) -> CompiledPolicy:
    data = case["input"]
    return compile_policy(data["definition"], flow_id=data["flow_id"], version=data["version"],
                          legacy_kind=data["legacy_kind"], fixed_zone=data["fixed_zone"])


def _att1(flow_key: str, definition: dict[str, Any]) -> tuple[CompiledPolicy, dict[str, Any]]:
    legacy_kind, policy_key = ATT1[flow_key]
    return compile_policy(definition, flow_id=FLOW_ID, version=1, legacy_kind=legacy_kind), ROWS[policy_key]


# ---------------------------------------------------------------------------
# 1. compile_cases.json, compartido con T2.
# ---------------------------------------------------------------------------

def test_the_shared_cases_have_one_outcome_each() -> None:
    assert set(COMPILE_CASES["_formato"]) >= {"input", "expected", "error"}
    assert len({case["id"] for case in CASES}) == len(CASES)
    for case in CASES:
        assert ("expected" in case) != ("error" in case), case["id"]
        assert set(case["input"]) == {"definition", "flow_id", "version", "legacy_kind", "fixed_zone"}, case["id"]


@pytest.mark.parametrize("case", [case for case in CASES if "expected" in case],
                         ids=[case["id"] for case in CASES if "expected" in case])
def test_compile_policy_gives_the_expected_row(case: dict[str, Any]) -> None:
    assert _compile(case).as_json() == case["expected"]


@pytest.mark.parametrize("case", [case for case in CASES if "error" in case],
                         ids=[case["id"] for case in CASES if "error" in case])
def test_compile_policy_rejects_what_the_v1_does_not_run(case: dict[str, Any]) -> None:
    with pytest.raises(PolicyCompileError) as raised:
        _compile(case)
    assert str(raised.value) == case["error"]


def test_the_compiled_policy_has_one_step_and_one_send() -> None:
    for case in CASES:
        if "expected" in case:
            compiled = _compile(case)
            assert len(compiled.steps) == 1, case["id"]
            assert compiled.max_automatic_messages == 1, case["id"]
            assert compiled.steps[0]["step_key"] == STEP_KEY_BY_LEGACY_KIND[case["input"]["legacy_kind"]]


def test_the_step_of_each_legacy_kind_is_the_one_of_today() -> None:
    assert dict(STEP_KEY_BY_LEGACY_KIND) == {
        "carrito": "first_contact",
        "precheckout": "first_contact",
        "pago_fallido": "payment_failure_first_contact",
    }
    # Son los pasos de las filas de hoy de ATT1.
    today = {step["step_key"] for row in ROWS.values() for step in row["steps"]}
    assert set(STEP_KEY_BY_LEGACY_KIND.values()) == today


# ---------------------------------------------------------------------------
# 2. Contra la fila real de ATT1.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flow_key", ["carrito", "pago_fallido", "precheckout"])
def test_the_translated_flow_of_att1_compiles_to_the_row_of_today(flow_key: str) -> None:
    compiled, row = _att1(flow_key, TRANSLATED[flow_key].definition)
    assert differences_from_row(compiled, row) == ()
    # El unico paso es el de su legacy_kind, aunque la fila compartida tenga los dos.
    assert [dict(step) for step in compiled.steps] == [
        {"mode": "freeform", "step_key": STEP_KEY_BY_LEGACY_KIND[ATT1[flow_key][0]]}]


@pytest.mark.parametrize("flow_key", ["carrito", "pago_fallido", "precheckout"])
def test_the_published_definition_compiles_the_same_as_the_translation(flow_key: str) -> None:
    from_translation, row = _att1(flow_key, TRANSLATED[flow_key].definition)
    from_published, _ = _att1(flow_key, NORMALIZED[flow_key])
    assert from_published == from_translation
    assert differences_from_row(from_published, row) == ()


def test_the_shared_policy_of_att1_has_both_steps() -> None:
    # La diferencia 9 (clase C): el carrito y el pago fallido comparten la fila; la compilada de
    # cada uno lleva solo su paso.
    row = ROWS["att1-recuperacion-un-toque"]
    assert [step["step_key"] for step in row["steps"]] == ["first_contact", "payment_failure_first_contact"]
    cart, _ = _att1("carrito", TRANSLATED["carrito"].definition)
    failure, _ = _att1("pago_fallido", TRANSLATED["pago_fallido"].definition)
    assert cart.steps != failure.steps
    # status es de la fila, no de la compilada.
    same = [field for field in COMPARED_FIELDS if field not in ("steps", "status")]
    assert {field: getattr(cart, field) for field in same} == {field: getattr(failure, field) for field in same}


# ---------------------------------------------------------------------------
# 3. differences_from_row ve cada campo.
# ---------------------------------------------------------------------------

def _changed(row: dict[str, Any], field: str, value: object) -> dict[str, Any]:
    out = copy.deepcopy(row)
    out[field] = value
    return out


@pytest.mark.parametrize(("field", "value", "expected"), [
    ("purpose", "reactivation", ("purpose",)),
    ("timezone", "America/Bogota", ("timezone",)),
    ("business_windows", [{"days": [1, 2, 3, 4, 5, 6, 7], "start": "09:00", "end": "20:59"}], ("business_windows",)),
    ("business_windows", [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "09:00", "end": "21:00"}], ("business_windows",)),
    ("business_windows", [{"days": [1, 2, 3, 4, 5, 6, 7], "start": "09:00", "end": "21:00"}] * 2,
     ("business_windows",)),
    ("grace_period", "00:05:00", ("grace_period",)),
    ("grace_period", "1 mon", ("grace_period",)),
    ("expires_after", "2 days", ("expires_after",)),
    ("max_automatic_messages", 2, ("max_automatic_messages",)),
    ("max_automatic_messages", True, ("max_automatic_messages",)),
    ("steps", [{"mode": "freeform", "step_key": "payment_failure_first_contact"}], ("steps",)),
    ("steps", [{"mode": "template", "step_key": "first_contact"}], ("steps",)),
    ("steps", [{"mode": "freeform", "step_key": "first_contact", "delay": "0 seconds"}], ("steps",)),
    ("steps", None, ("steps",)),
    # La reevaluacion usa el primer paso con ese step_key (20260805000300:214-217): con uno
    # approved_template antes, escala con channel_mode_unsupported aunque el freeform este despues.
    ("steps", [{"mode": "approved_template", "step_key": "first_contact"},
               {"mode": "freeform", "step_key": "first_contact"},
               {"mode": "freeform", "step_key": "payment_failure_first_contact"}], ("steps",)),
    # Una fila retirada escala con policy_step_invalid (20260805000300:339-341).
    ("status", "retired", ("status",)),
    ("status", None, ("status",)),
    # La clave y la version no se miran: la de hoy es otra clave, y la del motor es flujo:<flow_id>.
    ("policy_key", "otra", ()),
    ("version", 7, ()),
    # Lo mismo escrito de otra forma no es una diferencia.
    ("expires_after", "24:00:00", ()),
    ("expires_after", timedelta(days=1), ()),
    ("business_windows", [{"start": "09:00", "end": "21:00", "days": [1, 2, 3, 4, 5, 6, 7]}], ()),
    # El paso del pago fallido antes que el del carrito: el primero con first_contact es el compilado.
    ("steps", [{"mode": "freeform", "step_key": "payment_failure_first_contact"},
               {"mode": "freeform", "step_key": "first_contact"}], ()),
])
def test_differences_from_row_sees_each_field(field: str, value: object, expected: tuple[str, ...]) -> None:
    compiled, row = _att1("carrito", TRANSLATED["carrito"].definition)
    assert differences_from_row(compiled, _changed(row, field, value)) == expected


def test_a_compiled_policy_with_more_than_one_step_differs_from_any_row() -> None:
    compiled, row = _att1("carrito", TRANSLATED["carrito"].definition)
    shared = dataclasses.replace(compiled, steps=tuple(row["steps"]))
    assert differences_from_row(shared, row) == ("steps",)


def test_a_missing_field_is_a_difference() -> None:
    compiled, _ = _att1("carrito", TRANSLATED["carrito"].definition)
    assert differences_from_row(compiled, {}) == COMPARED_FIELDS


@pytest.mark.parametrize(("text", "expected"), [
    ("00:00:00", timedelta(0)),
    ("01:00:00", timedelta(hours=1)),
    ("1 day", timedelta(days=1)),
    ("2 days", timedelta(days=2)),
    ("1 day 02:00:00", timedelta(days=1, hours=2)),
    ("-01:00:00", -timedelta(hours=1)),
    ("00:00:30.5", timedelta(seconds=30, microseconds=500000)),
    ("1 mon", None),
    ("1 day02:00:00", None),
    ("PT1H", None),
    ("", None),
    (3600, None),
])
def test_interval_of_reads_the_postgres_style(text: object, expected: timedelta | None) -> None:
    assert interval_of(text) == expected
