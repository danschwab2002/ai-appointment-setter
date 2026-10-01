"""Migracion 20261001000500: en una conversacion adoptada, el caso es el inbound_sales.

La migracion redefine cuatro RPC que buscan el caso de una conversacion
(mark_human_handoff_attended, claim_conversation_reactivation,
resume_paused_conversation y claim_conversation_followup_v1), copiadas de su
definicion vigente, con un solo cambio: si la conversacion tiene el evento
'inbound_adopted_template_conversation' que deja la admision portable
(20261001000400), el conteo y el select del caso miran solo
case_kind = 'inbound_sales'. Sin el evento, lo de antes: la medicion en la base
de Johanna dio 2 conversaciones con un unico cart_recovery, y un filtro
incondicional les cambiaria el resultado.

Este test verifica la forma: que cada funcion sea la vigente mas exactamente
esas lineas, que el filtro este en el conteo y en el select y dependa del
evento, que la guarda de ambiguedad siga, que el evento sea el mismo que
escribe la 000400 y que el ACL quede explicito. El comportamiento, contra la
base sin la 000500, se prueba en
tests/sql/followup_engine/validate_commercial_case_lookups_by_inbound_kind.mjs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261001000500_commercial_case_lookups_by_inbound_kind.sql"
ADOPTION = MIGRATIONS / "20261001000400_portable_inbound_adopts_template_conversation.sql"
VALIDATOR = "validate_commercial_case_lookups_by_inbound_kind.mjs"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"
EVENT = "inbound_adopted_template_conversation"

# Nombre -> (migracion vigente antes de la 000500, firma para el ACL, mensaje
# de ambiguedad).
RPCS = {
    "mark_human_handoff_attended": (
        "20260927000300_handoff_attendance_v1.sql",
        "bigint, timestamptz, timestamptz",
        "mark_human_handoff_attended_ambiguous_case",
    ),
    "claim_conversation_reactivation": (
        "20260927000300_handoff_attendance_v1.sql",
        "bigint, text, text, text, text, bigint, integer, integer, integer, timestamptz",
        "claim_conversation_reactivation_ambiguous_case",
    ),
    "resume_paused_conversation": (
        "20260927000300_handoff_attendance_v1.sql",
        "bigint, text, text, integer, integer, timestamptz",
        "resume_paused_conversation_ambiguous_case",
    ),
    "claim_conversation_followup_v1": (
        "20260928000400_conversation_followup_discount_v1.sql",
        "bigint, bigint, bigint, text, text, text, text, text, text, text, "
        "bigint, bigint, integer, text, timestamptz",
        "claim_conversation_followup_ambiguous_case",
    ),
}

# Lo unico que la 000500 agrega a cada funcion, textual.
DECLARATION = "    v_inbound_only boolean;\n"
ASSIGNMENT = (
    "    -- 20261001000500: si la admision portable adopto esta conversacion (H7),\n"
    "    -- ahi conviven el caso de la plantilla (la sombra cart_recovery) y el\n"
    "    -- inbound_sales de la respuesta, y el caso es el inbound_sales. Sin el\n"
    "    -- evento de adopcion se cuentan todos los casos, como antes.\n"
    "    v_inbound_only := exists (\n"
    "        select 1\n"
    "        from public.conversation_events adoption\n"
    "        where adoption.conversation_id = v_conversation.id\n"
    f"          and adoption.event_type = '{EVENT}'\n"
    "    );\n"
    "\n"
)
PREDICATE = "\n      and (commercial_case.case_kind = 'inbound_sales' or not v_inbound_only)"
CASE_FILTER = "    where commercial_case.conversation_id = v_conversation.id"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _defines(sql: str, name: str) -> bool:
    return re.search(rf"create\s+(?:or\s+replace\s+)?function\s+public\.{name}\s*\(", sql) is not None


def _function(sql: str, name: str) -> str:
    matches = re.findall(
        rf"create or replace function public\.{name}\(.*?\n\$function\$;\n", sql, re.DOTALL
    )
    assert len(matches) == 1, name
    return matches[0]


def test_migration_redefines_exactly_the_four_rpcs_and_nothing_else() -> None:
    sql = _sql()
    defined = re.findall(r"create\s+(?:or\s+replace\s+)?function\s+public\.([a-z0-9_]+)\s*\(", sql)

    assert defined == list(RPCS)
    assert len(re.findall(r"^create or replace function public\.", sql, re.MULTILINE)) == 4
    # Fuera de los cuerpos de las funciones y del bloque de roles solo quedan
    # la transaccion, los timeouts, las cabeceras y los revoke.
    outside = re.sub(r"\$function\$.*?\$function\$", "", sql, flags=re.DOTALL)
    outside = re.sub(r"\$roles\$.*?\$roles\$", "", outside, flags=re.DOTALL)
    executable = re.sub(r"--[^\n]*", "", outside)
    for forbidden in (
        r"\balter\b",
        r"\bdrop\b",
        r"\binsert\b",
        r"\bupdate\b",
        r"\bdelete\b",
        r"\btruncate\b",
        r"\bcreate\s+(or\s+replace\s+)?trigger\b",
        r"\bcreate\s+table\b",
        r"\bcreate\s+(unique\s+)?index\b",
        r"\bcreate\s+function\b",
        r"\bgrant\b",
    ):
        assert not re.search(forbidden, executable, re.IGNORECASE), forbidden


@pytest.mark.parametrize("name", list(RPCS))
def test_each_rpc_is_its_vigente_definition_plus_only_the_filter(name: str) -> None:
    vigente_file, _, _ = RPCS[name]
    # La vigente es la ultima migracion anterior que la define.
    defining = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if path.name < MIGRATION.name and _defines(path.read_text(encoding="utf-8"), name)
    ]
    assert defining[-1] == vigente_file
    vigente = _function((MIGRATIONS / vigente_file).read_text(encoding="utf-8"), name)
    new = _function(_sql(), name)

    # Exactamente una declaracion, una asignacion y el predicado dos veces.
    assert new.count(DECLARATION) == 1
    assert new.count(ASSIGNMENT) == 1
    assert new.count(PREDICATE) == 2
    # Sin esas lineas, la funcion es la vigente caracter por caracter.
    stripped = new.replace(DECLARATION, "").replace(ASSIGNMENT, "").replace(PREDICATE, "")
    assert stripped == vigente


@pytest.mark.parametrize("name", list(RPCS))
def test_the_filter_is_in_the_count_and_in_the_select_after_the_conversation_lock(name: str) -> None:
    _, _, ambiguous = RPCS[name]
    new = _function(_sql(), name)

    lock = new.index("        'chatwoot_conversation_id', p_external_conversation_id::text\n    )\n    for update;")
    assignment = new.index(ASSIGNMENT)
    count = new.index("    select count(*) into v_case_count\n")
    guard = new.index(f"            message = '{ambiguous}';")
    select = new.index("    select commercial_case.* into v_case\n")
    # La conversacion bloqueada, despues el evento, el conteo con su guarda y
    # el select: el conteo y el select usan la misma decision.
    assert lock < assignment < count < guard < select
    assert new[count:guard].count(CASE_FILTER + PREDICATE) == 1
    assert new[guard:].count(CASE_FILTER + PREDICATE) == 1
    assert "    if v_case_count > 1 then\n        raise exception using errcode = 'P0001'," in new
    # El evento sale de la conversacion bloqueada, no de un caso.
    assert "where adoption.conversation_id = v_conversation.id" in new


def test_the_event_is_the_one_the_portable_admission_writes() -> None:
    adoption = ADOPTION.read_text(encoding="utf-8")
    # La 000400 lo escribe en conversation_events, en la misma transaccion en
    # la que la v2 crea el inbound_sales: si cambia el nombre, el filtro deja
    # de ver la adopcion en silencio.
    assert re.search(
        rf"insert into public\.conversation_events \(.*?'{EVENT}'", adoption, re.DOTALL
    )
    # Nadie mas escribe ese evento.
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path not in (ADOPTION, MIGRATION):
            assert EVENT not in path.read_text(encoding="utf-8"), path.name


def test_no_later_migration_redefines_them() -> None:
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name > MIGRATION.name:
            text = path.read_text(encoding="utf-8")
            for name in RPCS:
                assert not _defines(text, name), (path.name, name)


def test_acl_is_restated_for_service_role_only() -> None:
    sql = _sql()
    roles = sql.split("do $roles$", 1)[1].split("$roles$;", 1)[0]
    compact_roles = " ".join(roles.split())
    for name, (_, signature, _) in RPCS.items():
        header = f"on function public.{name}( {signature} )"
        assert f"revoke all on function public.{name}(\n" in sql
        assert " ".join(f"revoke all {header} from public;".split()) in " ".join(sql.split())
        for role in ("anon", "authenticated"):
            assert f"revoke all {header} from {role};" in compact_roles, (name, role)
        assert f"grant execute {header} to service_role;" in compact_roles, name
    assert "if to_regrole('anon') is not null then" in roles
    assert "if to_regrole('authenticated') is not null then" in roles
    assert "if to_regrole('service_role') is not null then" in roles
    assert "to anon" not in compact_roles
    assert "to authenticated" not in compact_roles


def test_migration_is_transactional_with_the_neighbours_timeouts() -> None:
    sql = _sql()
    assert sql.lstrip().startswith("--")
    assert "\nbegin;\n\nset local lock_timeout = '5s';\nset local statement_timeout = '30s';\n" in sql
    assert sql.rstrip().endswith("commit;")


def test_the_validator_runs_in_npm_test() -> None:
    script = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert f"node {VALIDATOR}" in script.split(" && ")
