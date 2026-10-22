"""Lo que el bridge de la T2.4 lee y marca del motor (20261022000200).

Sin base: que sea una transaccion y la ultima migracion, que solo cree sus
tres funciones (ninguna existente cambia: ni create or replace, ni alter, ni
drop, ni un permiso de tabla), que las tres sean security definer con
search_path fijo, y que el permiso sea solo de service_role. El comportamiento
lo prueba validate_flow_engine_bridge_reads_v1.mjs (PGlite), que ademas esta en
la cadena de npm test.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261022000200_flow_engine_bridge_reads_v1.sql"
SQL_SUITE = ROOT / "tests" / "sql" / "followup_engine"
FUNCTIONS = (
    "read_flow_engine_routes_v1",
    "read_flow_engine_divergences_v1",
    "mark_flow_engine_divergence_notified_v1",
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return re.sub(r"--[^\n]*", "", _sql()).lower()


def test_it_is_one_transaction_and_comes_right_after_the_motor() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    position = names.index(MIGRATION.name)
    assert names[position - 1] == "20261022000100_flow_engine_motor_carrito_v1.sql"
    # Lo que venga despues no redefine estas tres funciones.
    for later in names[position + 1:]:
        text = (MIGRATIONS / later).read_text(encoding="utf-8")
        for name in FUNCTIONS:
            assert f"function public.{name}(" not in text, (later, name)
    assert "flow_engine_bridge_reads_already_applied" in sql
    assert "flow_engine_bridge_reads_prerequisite_missing" in sql


def test_it_only_creates_its_three_functions() -> None:
    code = _code()
    created = re.findall(r"create function public\.([a-z_0-9]+)\(", code)
    assert sorted(created) == sorted(FUNCTIONS)
    assert "create or replace" not in code
    assert "alter function" not in code and "alter table" not in code
    assert "drop " not in code and "pg_get_functiondef" not in code
    assert "grant select" not in code and "on table" not in code


def test_the_three_are_security_definer_with_a_fixed_search_path() -> None:
    code = _code()
    for name in FUNCTIONS:
        body = code.split(f"create function public.{name}(", 1)[1].split("$function$;", 1)[0]
        assert "security definer" in body, name
        assert "set search_path = pg_catalog, public, pg_temp" in body, name
    # Las lecturas no escriben; la marca solo toca notified_at, si estaba nula.
    for name in FUNCTIONS[:2]:
        body = code.split(f"create function public.{name}(", 1)[1].split("$function$;", 1)[0]
        assert "\nstable\n" in body and "update " not in body and "insert " not in body, name
    mark = code.split("create function public.mark_flow_engine_divergence_notified_v1(", 1)[1]
    assert "set notified_at = p_notified_at" in mark
    assert "where id = p_id and notified_at is null" in mark


def test_only_service_role_executes_them() -> None:
    code = _code()
    acl = code.split("do $acl$", 1)[1]
    for signature in (
        "public.read_flow_engine_routes_v1(jsonb)",
        "public.read_flow_engine_divergences_v1(jsonb,boolean,integer)",
        "public.mark_flow_engine_divergence_notified_v1(uuid,timestamptz)",
    ):
        assert f"'{signature}'" in acl
    assert "revoke all on function %s from public" in acl
    assert "grant execute on function %s to service_role" in acl
    assert "anon" in acl and "to anon" not in acl and "to authenticated" not in acl


def test_the_sql_suite_runs_the_validator() -> None:
    package = json.loads((SQL_SUITE / "package.json").read_text(encoding="utf-8"))
    assert "node validate_flow_engine_bridge_reads_v1.mjs" in package["scripts"]["test"]
    validator = (SQL_SUITE / "validate_flow_engine_bridge_reads_v1.mjs").read_text(encoding="utf-8")
    assert "20261022000200_flow_engine_bridge_reads_v1.sql" in validator
