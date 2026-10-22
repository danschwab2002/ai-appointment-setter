"""La migracion de las rutas del motor de flujos (20261021000200, la T2.3a).

Lo que se prueba aca sin base: que la consulta de la historia sea la v3
generada (no una copia a mano), que la forma de los pasos de la gemela de SQL
sea la de Python, que los permisos sean solo las dos lecturas, y que el JSON
Schema de read_flow_engine_v1 sea el que usa la prueba de PGlite. El
comportamiento lo prueba validate_flow_engine_v1.mjs (PGlite) y la carrera,
real_postgres_flow_engine.py.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

from bridge.flow_policy import STEPS_BY_LEGACY_KIND

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20261021000200_flow_engine_routes_v1.sql"
SCHEMA = ROOT / "tests" / "fixtures" / "flow_engine" / "read_flow_engine_v1.schema.json"
GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "flow_engine_history_sql", ROOT / "scripts" / "flow_engine_history_sql.py")
assert GENERATOR_SPEC and GENERATOR_SPEC.loader
GENERATOR = importlib.util.module_from_spec(GENERATOR_SPEC)
GENERATOR_SPEC.loader.exec_module(GENERATOR)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return re.sub(r"--[^\n]*", "", _sql())


def test_it_is_one_transaction_and_comes_right_after_the_t4b() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in (ROOT / "supabase" / "migrations").glob("*.sql"))
    position = names.index(MIGRATION.name)
    # Va despues de la T4b, que crea el rol al que concede las lecturas.
    assert names[position - 1] == "20261021000100_automation_expert_change_v1.sql"
    # Y antes del tope de la historia, que solo cambia una constante de
    # read_flow_engine_history_v1.
    assert names[position + 1] == "20261021000300_flow_engine_history_cap_v1.sql"
    # Y despues, la lectura de /flows mas rapida (que no toca nada de esta) y
    # el camino del motor para el carrito (la T2.3b), que reemplaza la
    # capacidad, la guarda y crear la ruta sobre sus definiciones vigentes.
    assert names[position + 2:] == [
        "20261021000400_automation_expert_flows_speed_v1.sql",
        "20261022000100_flow_engine_motor_carrito_v1.sql",
        "20261022000200_flow_engine_bridge_reads_v1.sql",
    ]


def test_the_history_query_is_the_v3_generated_not_a_copy() -> None:
    query = GENERATOR.QUERY.read_text(encoding="utf-8")
    generated = GENERATOR.generate(query)
    assert GENERATOR.embedded(_sql()) == generated
    assert GENERATOR.main(["--check"]) == 0
    # La cabecera de la v3 (la activacion de la v7) no queda: la ventana es p_since.
    assert "pilot_scope_version_activated:" not in generated
    assert "p_since + interval '1 day' as desde" in generated
    assert "gen_random_uuid()::text as sal" in generated


def test_a_lost_anchor_of_the_v3_fails_loudly() -> None:
    query = GENERATOR.QUERY.read_text(encoding="utf-8")
    broken = query.replace("     where rc.source = 'hotmart'\n", "     where rc.source in ('hotmart')\n")
    with pytest.raises(GENERATOR.HistoryQueryError, match="casos_del_producto"):
        GENERATOR.generate(broken)


def test_the_legacy_steps_of_the_sql_twin_are_the_python_ones() -> None:
    literal = re.search(r"\$steps\$(.*?)\$steps\$", _sql(), re.S)
    assert literal
    in_sql = json.loads(literal.group(1))
    in_python = {
        kind: [{"step_key": step.step_key, "coupon_ordinal": step.coupon_ordinal,
                "variables": list(step.variables) if step.variables is not None else None,
                "coupon_variables": list(step.coupon_variables) if step.coupon_variables is not None else None}
               for step in steps]
        for kind, steps in STEPS_BY_LEGACY_KIND.items()
    }
    assert in_sql == in_python


def test_only_the_two_reads_are_granted_and_the_agent_role_behind_to_regrole() -> None:
    code = _code()
    grants = re.findall(r"grant\s+(.+?)\s+to\s+([a-z_]+)", code)
    assert grants == [("execute on function %s", "service_role"),
                      ("execute on function %s", "automation_expert_change")]
    block = code.split("foreach v_function in array array[", 2)[2]
    listed = re.findall(r"'public\.([a-z_0-9]+\([^']*\))'", block.split("]::regprocedure[]", 1)[0])
    assert listed == ["read_flow_engine_v1(jsonb,text)", "read_flow_engine_history_v1(jsonb,text,timestamptz)"]
    assert "if to_regrole('automation_expert_change') is not null then" in code
    assert "if to_regrole('service_role') is not null then" in code
    # Toda funcion nueva se revoca a public y a cada rol de la API.
    created = re.findall(r"create function public\.([a-z_0-9]+)\(", code)
    revoked = re.findall(r"'public\.([a-z_0-9]+)\(", code.split("do $acl$", 1)[1])
    assert sorted(set(created)) == sorted(set(revoked))
    assert len(created) == 23


def test_the_guard_goes_on_the_live_definition_of_seven_functions() -> None:
    sql = _sql()
    for name, migration in (
        ("plan_lancemos_pilot_cart_recovery", "20260930000300"),
        ("plan_portable_payment_failure_recovery", "20261019000200"),
        ("plan_portable_payment_slip_recovery", "20261019000100"),
        ("mark_lancemos_pilot_request_started", "20261001000100"),
        ("mark_portable_payment_failure_request_started", "20261019000700"),
        ("mark_portable_payment_slip_request_started", "20261019000400"),
        ("authorize_lancemos_pilot_request_start", "20261007000100"),
    ):
        assert re.search(rf"--\s+{name}\s\.*\s{migration}\n", sql), name
        assert f"'public.{name}(" in sql, name
    code = _code()
    # El metodo de 20261007000100: el ancla una sola vez y la marca ausente.
    assert "pg_get_functiondef(v_signature::regprocedure)" in code
    assert "unexpected_flow_engine_guard_definition" in code
    assert "create or replace function public.plan_" not in code.lower()
    assert "create or replace function public.mark_" not in code.lower()


# Un validador de JSON Schema con las palabras que usa el archivo, como el de
# la prueba de PGlite: type, const, enum, pattern, minimum, maxItems,
# required, properties, additionalProperties false, items y $ref a $defs.
SCHEMA_KEYWORDS = {"$schema", "$id", "title", "description", "$defs", "$ref", "type", "const", "enum",
                   "pattern", "minimum", "maxItems", "required", "properties", "additionalProperties", "items"}


def _errors(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str = "$") -> list[str]:
    if "$ref" in schema:
        return _errors(value, root["$defs"][schema["$ref"].removeprefix("#/$defs/")], root, path)
    out: list[str] = []
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else kinds
    actual = ("null" if value is None else "boolean" if isinstance(value, bool) else "integer"
              if isinstance(value, int) else "number" if isinstance(value, float) else "string"
              if isinstance(value, str) else "array" if isinstance(value, list) else "object")
    if kinds and actual not in kinds and not (actual == "integer" and "number" in kinds):
        return [f"{path}: tipo {actual}"]
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: const")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: enum")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        out.append(f"{path}: pattern")
    if "minimum" in schema and actual in ("integer", "number") and value < schema["minimum"]:
        out.append(f"{path}: minimum")
    if "maxItems" in schema and isinstance(value, list) and len(value) > schema["maxItems"]:
        out.append(f"{path}: maxItems")
    if isinstance(value, dict):
        out += [f"{path}.{key}: falta" for key in schema.get("required", ()) if key not in value]
        for key, child in value.items():
            if key in schema.get("properties", {}):
                out += _errors(child, schema["properties"][key], root, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                out.append(f"{path}.{key}: de mas")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            out += _errors(item, schema["items"], root, f"{path}[{index}]")
    return out


def _keywords(node: Any) -> set[str]:
    if isinstance(node, dict):
        found = set()
        for key, child in node.items():
            if key in ("properties", "$defs"):
                for sub in child.values():
                    found |= _keywords(sub)
            else:
                found.add(key)
                found |= _keywords(child) if key in ("items",) else set()
        return found
    return set()


def _example() -> dict[str, Any]:
    """La respuesta de la seccion 1 del contrato, con valores."""
    return {
        "contract": "flow-engine-v1",
        "contract_version": "1.0.0",
        "database_at": "2026-10-21T18:00:00.000000Z",
        "engine": {"capabilities": "v1", "rules_version": 2},
        "flows": [{
            "flow_id": "1b000001-0000-4000-8000-00000000c001",
            "product_ref": "att1",
            "flow_key": "carrito",
            "state": "prendido",
            "active_version": 1,
            "generation": 3,
            "route": {"executor": "legado", "legacy_kind": "carrito", "legacy_scope_key": "att1-recuperacion",
                      "generation": 0, "changed_at": "2026-10-21T18:00:00.000000Z"},
            "versions": [{"version": 1, "definition": {"esquema": "setter-flujo/v1"},
                          "definition_sha256": "a" * 64, "published_at": "2026-10-21T18:00:00.000000Z",
                          "pool": "att1-recuperacion", "in_flight": 12}],
        }, {
            "flow_id": "1b000001-0000-4000-8000-00000000c003",
            "product_ref": "att1",
            "flow_key": "precheckout",
            "state": "apagado",
            "active_version": None,
            "generation": 0,
            "route": None,
            "versions": [],
        }],
        "next": None,
    }


def test_the_schema_only_uses_the_keywords_both_validators_know() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert _keywords(schema) <= SCHEMA_KEYWORDS
    for definition in schema["$defs"].values():
        assert _keywords(definition) <= SCHEMA_KEYWORDS


def test_the_contract_example_passes_and_personal_data_does_not() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert _errors(_example(), schema, schema) == []
    with_phone = _example()
    with_phone["flows"][0]["telefono"] = "5215512345678"
    assert _errors(with_phone, schema, schema) == ["$.flows[0].telefono: de mas"]
    with_contact = _example()
    with_contact["flows"][0]["versions"][0]["contact_id"] = "x"
    assert _errors(with_contact, schema, schema) == ["$.flows[0].versions[0].contact_id: de mas"]
    local_time = _example()
    local_time["database_at"] = "2026-10-21T15:00:00.000000-03:00"
    assert _errors(local_time, schema, schema) == ["$.database_at: pattern"]
    bad_executor = _example()
    bad_executor["flows"][0]["route"]["executor"] = "otro"
    assert _errors(bad_executor, schema, schema) == ["$.flows[0].route.executor: enum"]
