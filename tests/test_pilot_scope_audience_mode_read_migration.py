"""Migracion 20261001000300: la lectura del modo de audiencia de un scope del piloto.

El bridge la usa para la guarda del adaptador de GHL sin aceptacion del riesgo
(docs/contracts/ghl-precheckout-adapter-v1.md, Riesgos): pilot_scope_versions
tiene RLS y esta revocada a todos, asi que el modo solo se puede leer por una
RPC. Este test verifica que la migracion cree UNA funcion nueva y nada mas,
que no redefina ninguna existente, su forma (definer, stable, solo lo
publicado) y que el ACL sea explicito. El comportamiento se prueba en
tests/sql/followup_engine/validate_pilot_scope_audience_mode.mjs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
BASELINE = ROOT / "supabase" / "baseline" / "20260803_public_schema.sql"
MIGRATION = MIGRATIONS / "20261001000300_pilot_scope_audience_mode_read.sql"
VALIDATOR = ROOT / "tests" / "sql" / "followup_engine" / "validate_pilot_scope_audience_mode.mjs"
NAME = "get_lancemos_pilot_scope_audience_mode"
SIGNATURE = "(text,integer)"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _normalized(sql: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"--[^\n]*", "", sql)).strip()


def _function() -> str:
    match = re.search(
        rf"create or replace function public\.{NAME}\(.*?\$function\$;",
        _sql(),
        re.DOTALL,
    )
    assert match is not None
    return match.group(0)


def test_migration_creates_one_new_function_and_replaces_none() -> None:
    sql = _sql()
    defined = re.findall(r"create\s+(?:or\s+replace\s+)?function\s+public\.([a-z0-9_]+)\s*\(", sql)

    assert defined == [NAME]
    # No existia antes, ni definida en un archivo ni nombrada por otra funcion:
    # ninguna instancia ejecutaba nada de lo que esta migracion crea.
    earlier = {BASELINE.name: BASELINE.read_text(encoding="utf-8")}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name < MIGRATION.name:
            earlier[path.name] = path.read_text(encoding="utf-8")
    for filename, text in earlier.items():
        assert NAME not in text, filename
    # Nada posterior la redefine.
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name > MIGRATION.name:
            assert not re.search(
                rf"create\s+(?:or\s+replace\s+)?function\s+public\.{NAME}\s*\(",
                path.read_text(encoding="utf-8"),
            ), path.name
    executable = re.sub(r"--[^\n]*", "", sql)
    assert "pg_get_functiondef" not in executable
    for forbidden in (
        r"\balter\b",
        r"\bdrop\b",
        r"\bcreate\s+(or\s+replace\s+)?trigger\b",
        r"\bcreate\s+table\b",
        r"\bcreate\s+(unique\s+)?index\b",
    ):
        assert not re.search(forbidden, executable, re.IGNORECASE), forbidden


def test_the_read_returns_the_mode_of_a_published_version_only() -> None:
    function = _normalized(_function())
    header, body = function.split(" as $function$ ", 1)

    assert header == (
        f"create or replace function public.{NAME}( p_scope_key text, "
        "p_scope_version integer ) returns text language sql stable security definer "
        "set search_path = public, pg_temp"
    )
    assert body == (
        "select scope.audience_mode from public.pilot_scope_versions scope "
        "where scope.scope_key = p_scope_key and scope.version = p_scope_version "
        "and scope.status = 'published'; $function$;"
    )


def test_acl_is_explicit() -> None:
    sql = _sql()
    compact = _normalized(sql)

    assert "rolname in ('anon','authenticated','service_role')" in compact
    assert f"revoke all on function public.{NAME}{SIGNATURE} from public;" in compact
    assert (
        f"execute format('revoke all on function public.{NAME}{SIGNATURE} from %I',v_role);"
        in compact
    )
    assert f"grant execute on function public.{NAME}{SIGNATURE} to service_role;" in compact
    # El grant va dentro del bloque que tolera que los roles no existan (los
    # validadores PGlite ajenos aplican todas las migraciones sin crearlos).
    roles_block = re.search(r"do \$roles\$.*?\$roles\$;", sql, re.DOTALL)
    assert roles_block is not None
    outside = re.sub(r"--[^\n]*", "", sql.replace(roles_block.group(0), ""))
    assert not re.search(r"^\s*grant\b", outside, re.MULTILINE)
    assert "if exists(select 1 from pg_roles where rolname='service_role') then" in compact
    assert compact.count("grant ") == 1


def test_migration_is_transactional_and_writes_nothing() -> None:
    sql = _sql()
    outside_functions = re.sub(r"\$function\$.*?\$function\$", "", sql, flags=re.DOTALL)
    statements = re.sub(r"--[^\n]*", "", outside_functions).split(";")

    assert sql.isascii()
    assert re.search(
        r"^begin;\nset local lock_timeout = '5s';\nset local statement_timeout = '30s';\n",
        sql,
        re.MULTILINE,
    )
    assert sql.rstrip().endswith("commit;")
    assert not [
        statement
        for statement in statements
        if re.match(r"\s*(insert|update|delete|truncate)\b", statement, re.IGNORECASE)
    ]
    # validate_handoff_postgres.py trata como funcion de derivaciones a toda la
    # que nombre esas tablas, y exige que service_role no la ejecute.
    assert "human_handoff" not in sql


def test_inventories_and_validators_know_the_read() -> None:
    acl = (ROOT / "scripts" / "supabase_acl_inventory.sql").read_text(encoding="utf-8")
    assert f"('public.{NAME}(text, integer)')" in acl
    hardening = (
        ROOT / "tests" / "sql" / "followup_engine" / "validate_acl_hardening.mjs"
    ).read_text(encoding="utf-8")
    assert f"('{NAME}{SIGNATURE}')" in hardening
    assert "result.expected_count !== 120" in hardening
    schema = (ROOT / "scripts" / "supabase_schema_inventory.sql").read_text(encoding="utf-8")
    assert f"'{MIGRATION.name}'" in schema
    assert f"to_regprocedure('public.{NAME}{SIGNATURE}')" in schema
    # El validador de comportamiento esta en la cadena y llama a la RPC real,
    # como service_role y como los roles que no pueden.
    package = json.loads(
        (ROOT / "tests" / "sql" / "followup_engine" / "package.json").read_text(encoding="utf-8")
    )
    assert f"node {VALIDATOR.name}" in package["scripts"]["test"]
    validator = VALIDATOR.read_text(encoding="utf-8")
    assert f"public.{NAME}($1,$2)" in validator
    for role in ("service_role", "anon", "authenticated"):
        assert f"'{role}'" in validator
    assert "42501" in validator
