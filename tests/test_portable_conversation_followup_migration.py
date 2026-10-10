"""Migracion 20261010000100: la reserva del seguimiento con cupon del runtime
con manifiesto (claim_portable_conversation_followup_v1), leida como texto.

La migracion deriva la funcion nueva de la definicion VIVA de
claim_conversation_followup_v1 (la que deja 20261001000500) con tres
reemplazos exactos: el nombre, la reserva del link (la portable, la del link
del agente con manifiesto) y una barrera despues de calcular v_inbound_only:
una conversacion que no fue adoptada (sin respuesta a una plantilla nuestra)
da 'blocked_not_template_reply' y no reserva nada.

Aca se fija el texto: que los tres textos estan exactamente una vez en la
definicion vigente, que la derivada es la compartida mas solo esos cambios,
que la migracion cuenta y falla cerrado con 55000, que sin la reserva portable
(la base de Johanna) avisa y no crea nada, que los permisos van guardados y
que los inventarios la conocen. La ejecucion contra la cadena de ATT1 la
prueba en PGlite tests/sql/followup_engine/validate_portable_conversation_followup.mjs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bridge.supabase import FOLLOWUP_CLAIM_OUTCOMES

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261010000100_portable_conversation_followup_claim.sql"
SHARED_IN_FORCE = MIGRATIONS / "20261001000500_commercial_case_lookups_by_inbound_kind.sql"
ADOPTION = MIGRATIONS / "20261001000400_portable_inbound_adopts_template_conversation.sql"
SCHEMA_INVENTORY = ROOT / "scripts" / "supabase_schema_inventory.sql"
ACL_INVENTORY = ROOT / "scripts" / "supabase_acl_inventory.sql"
SQL_SUITE = ROOT / "tests" / "sql" / "followup_engine"

SHARED = "claim_conversation_followup_v1"
PORTABLE = "claim_portable_conversation_followup_v1"
ARGS = "bigint,bigint,bigint,text,text,text,text,text,text,text,bigint,bigint,integer,text,timestamptz"
ARGS_SPACED = (
    "bigint, bigint, bigint, text, text, text, text, text, text, text, "
    "bigint, bigint, integer, text, timestamptz"
)
# Como la escribe oidvectortypes (el inventario) y regprocedure (el validador).
ARGS_CATALOG = ARGS.replace("timestamptz", "timestamp with time zone")
EVENT = "inbound_adopted_template_conversation"
CONVERSATION_LOCK = (
    "        'chatwoot_conversation_id', p_external_conversation_id::text\n"
    "    )\n"
    "    for update;"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _sql_literals(fragment: str) -> str:
    """Concatena los literales SQL de un fragmento: '' pasa a ' y el \\n de los
    E'' a un salto de linea (el unico escape que usa la migracion)."""
    parts = []
    for prefix, literal in re.findall(r"(E?)'((?:[^']|'')*)'", fragment):
        literal = literal.replace("''", "'")
        if prefix:
            literal = literal.replace("\\n", "\n")
        parts.append(literal)
    return "".join(parts)


def _constant(name: str) -> str:
    """El valor de una constante de texto del bloque de la derivacion."""
    sql = _sql()
    head = f"    {name} constant text :="
    start = sql.index(head) + len(head)
    return _sql_literals(sql[start : sql.index(";\n", start)])


def _template_reply_block() -> str:
    parts = _sql().split("$bloque$")
    assert len(parts) == 3
    return parts[1]


def _shared_in_force() -> str:
    sql = SHARED_IN_FORCE.read_text(encoding="utf-8")
    start = sql.index(f"create or replace function public.{SHARED}(")
    return sql[start : sql.index("$function$;", start) + len("$function$")]


def _derived() -> str:
    """Lo que ejecuta la migracion, aplicado a la definicion vigente."""
    end = _constant("v_inbound_only_end")
    return (
        _shared_in_force()
        .replace(_constant("v_shared_head"), _constant("v_portable_head"))
        .replace(_constant("v_shared_reserve_call"), _constant("v_portable_reserve_call"))
        .replace(end, end + _template_reply_block())
    )


def _executable(sql: str) -> str:
    """El codigo sin el bloque que se inserta, sin comentarios y sin literales."""
    sql = re.sub(r"\$bloque\$.*?\$bloque\$", "", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"'(?:''|[^'])*'", "''", sql)


def test_migration_comes_after_the_adoption_fix_and_writes_nothing_by_hand() -> None:
    versions = sorted(path.name.split("_", 1)[0] for path in MIGRATIONS.glob("*.sql"))
    position = versions.index("20261010000100")
    assert versions[position - 1] == "20261009000200"

    sql = _sql()
    assert sql.lstrip().startswith("-- Migration:")
    assert "\nbegin;\n\nset local lock_timeout = '5s';\nset local statement_timeout = '30s';\n" in sql
    assert sql.rstrip().endswith("commit;")
    # La funcion nueva sale del execute sobre la definicion viva: el texto no
    # define funciones, ni toca tablas o filas por su cuenta.
    executable = _executable(sql)
    for forbidden in (
        r"\bcreate\b",
        r"\balter\b",
        r"\bdrop\b",
        r"\binsert\b",
        r"\bupdate\b",
        r"\bdelete\b",
        r"\btruncate\b",
    ):
        assert not re.search(forbidden, executable, re.IGNORECASE), forbidden
    assert executable.count("execute replace(") == 1


def test_the_three_texts_are_exactly_once_in_the_definition_in_force() -> None:
    # La vigente es la de 20261001000500: nadie la redefine despues.
    defining = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if re.search(
            rf"create\s+(?:or\s+replace\s+)?function\s+public\.{SHARED}\s*\(",
            path.read_text(encoding="utf-8"),
        )
    ]
    assert defining[-1] == SHARED_IN_FORCE.name
    assert _constant("v_shared_head") == f"public.{SHARED}("
    assert _constant("v_portable_head") == f"public.{PORTABLE}("
    assert _constant("v_shared_reserve_call") == "from public.reserve_chatwoot_checkout_issuance_v2("
    assert _constant("v_portable_reserve_call") == "from public.reserve_portable_checkout_issuance_v2("
    # El tercero es el cierre del calculo de v_inbound_only, con el evento que
    # escribe la 000400.
    assert _constant("v_inbound_only_end") == (
        f"          and adoption.event_type = '{EVENT}'\n    );\n"
    )

    shared = _shared_in_force()
    for name in ("v_shared_head", "v_shared_reserve_call", "v_inbound_only_end"):
        assert shared.count(_constant(name)) == 1, name
    assert shared.count("    v_inbound_only := exists (\n") == 1
    assert shared.index("    v_inbound_only := exists (\n") < shared.index(
        _constant("v_inbound_only_end")
    )
    # Todavia no nombra lo que la migracion agrega.
    assert "reserve_portable_checkout_issuance_v2" not in shared
    assert "blocked_not_template_reply" not in shared

    # En el archivo el evento va partido: solo la 000400 lo escribe y solo la
    # 000500 lo nombra entero (test_commercial_case_lookups_by_inbound_kind_migration).
    assert EVENT not in _sql()
    assert re.search(
        rf"insert into public\.conversation_events \(.*?'{EVENT}'",
        ADOPTION.read_text(encoding="utf-8"),
        re.DOTALL,
    )


def test_the_derived_function_is_the_shared_one_plus_only_the_barrier() -> None:
    derived = _derived()
    block = _template_reply_block()

    assert derived.startswith(f"create or replace function public.{PORTABLE}(\n")
    assert derived.count(block) == 1
    assert derived.count("from public.reserve_portable_checkout_issuance_v2(") == 1
    assert "reserve_chatwoot_checkout_issuance_v2" not in derived
    # Sin el bloque y con los dos nombres de vuelta, es la compartida caracter
    # por caracter: mismas barreras, mismos argumentos a la reserva, mismo
    # resultado y mismos errores.
    reverted = (
        derived.replace(block, "")
        .replace(_constant("v_portable_head"), _constant("v_shared_head"))
        .replace(_constant("v_portable_reserve_call"), _constant("v_shared_reserve_call"))
    )
    assert reverted == _shared_in_force()


def test_the_barrier_runs_after_the_adoption_check_and_before_anything_is_written() -> None:
    block = _template_reply_block()
    assert block.startswith("\n    -- portable_followup_template_reply: begin\n")
    assert block.endswith("    -- portable_followup_template_reply: end\n")
    assert (
        "    if not v_inbound_only then\n"
        "        outcome := 'blocked_not_template_reply';\n"
        "        return next;\n"
        "        return;\n"
        "    end if;\n"
    ) in block
    for forbidden in ("insert", "update", "delete", "perform", "select"):
        assert forbidden not in block.lower(), forbidden

    derived = _derived()
    order = [
        derived.index(CONVERSATION_LOCK),
        derived.index("outcome := 'blocked_followup_limit';"),
        derived.index("    v_inbound_only := exists (\n"),
        derived.index("    if not v_inbound_only then\n"),
        derived.index("    select count(*) into v_case_count\n"),
        derived.index("outcome := 'blocked_pending_handoff';"),
        derived.index("outcome := 'purchase_already_approved';"),
        derived.index("from public.reserve_portable_checkout_issuance_v2("),
        derived.index("insert into public.conversation_followup_events ("),
    ]
    assert order == sorted(order)


def test_every_outcome_the_portable_rpc_writes_is_known_to_the_bridge() -> None:
    # El cliente falla cerrado ante un outcome desconocido: la barrera nueva
    # tiene que estar en su lista. Los issuance_* repiten el de la reserva.
    outcomes = set(re.findall(r"outcome := '([a-z_]+)';", _derived()))
    assert "blocked_not_template_reply" in outcomes
    assert outcomes <= FOLLOWUP_CLAIM_OUTCOMES


def test_the_migration_counts_each_text_and_fails_closed_with_55000() -> None:
    compact = " ".join(_sql().split())
    assert _constant("v_shared_claim") == f"public.{SHARED}({ARGS})"
    assert _constant("v_portable_claim") == f"public.{PORTABLE}({ARGS})"
    assert "v_definition := pg_get_functiondef(to_regprocedure(v_shared_claim));" in compact
    assert "if v_definition is null or length(" in compact
    for name in ("v_shared_head", "v_shared_reserve_call", "v_inbound_only_end"):
        assert (
            f"length(v_definition) - length(replace(v_definition, {name}, '')) <> length({name})"
            in compact
        ), name
    assert "position('reserve_portable_checkout_issuance_v2' in v_definition) > 0" in compact
    assert "position('blocked_not_template_reply' in v_definition) > 0" in compact
    failure = (
        "raise exception using errcode = '55000', "
        "message = 'unexpected_conversation_followup_claim_definition'"
    )
    execute = (
        "execute replace( replace( replace(v_definition, v_shared_head, v_portable_head), "
        "v_shared_reserve_call, v_portable_reserve_call ), v_inbound_only_end, "
        "v_inbound_only_end || v_template_reply_block );"
    )
    assert failure in compact and execute in compact
    assert compact.index(failure) < compact.index(execute)
    # Despues del execute se verifica por presencia que la funcion quedo.
    created = "v_created := pg_get_functiondef(to_regprocedure(v_portable_claim));"
    assert compact.index(execute) < compact.index(created)
    assert "position('portable_followup_template_reply: begin' in v_created) = 0" in compact
    assert "position(v_portable_reserve_call in v_created) = 0" in compact
    assert "message = 'portable_conversation_followup_claim_not_created'" in compact
    assert _sql().count("errcode = '55000'") == 3


def test_a_second_run_recreates_the_same_function() -> None:
    # Los chequeos leen solo la compartida, que no cambia: una segunda corrida
    # pasa igual y el create or replace de pg_get_functiondef recrea la misma
    # funcion. Nada frena porque la portable ya exista.
    body = _sql().split("do $followup$", 1)[1].split("$followup$;", 1)[0]
    compact = " ".join(body.split())
    execute_at = compact.index("execute replace(")
    # La portable solo se mira despues del execute, para verificar que quedo.
    assert compact.count("to_regprocedure(v_portable_claim)") == 1
    assert compact.index("to_regprocedure(v_portable_claim)") > execute_at


def test_without_the_portable_reserve_it_warns_and_creates_nothing() -> None:
    assert _constant("v_portable_reserve") == (
        "public.reserve_portable_checkout_issuance_v2(uuid,text,bigint,bigint,bigint,text,text,timestamptz)"
    )
    body = _sql().split("do $followup$", 1)[1].split("$followup$;", 1)[0]
    compact = " ".join(body[body.index("\nbegin\n") :].split())
    # Lo primero, antes de leer la compartida: aviso y return.
    assert compact.startswith(
        "begin if to_regprocedure(v_portable_reserve) is null then raise notice "
        "'portable_conversation_followup_claim: % no existe en esta base (sin 20261001000100); "
        "no se creo nada', v_portable_reserve; return; end if;"
    )
    assert compact.index("return; end if;") < compact.index("pg_get_functiondef")
    # Los permisos tambien: sin la funcion nueva no hay nada que tocar.
    roles = _sql().split("do $roles$", 1)[1].split("$roles$;", 1)[0]
    assert " ".join(roles.split()).startswith(
        f"begin if to_regprocedure( 'public.{PORTABLE}({ARGS})' ) is null then return; end if;"
    )


def test_only_service_role_executes_and_every_grant_is_guarded() -> None:
    sql = _sql()
    roles = sql.split("do $roles$", 1)[1].split("$roles$;", 1)[0]
    compact = " ".join(roles.split())
    header = f"on function public.{PORTABLE}( {ARGS_SPACED} )"

    assert f"revoke all {header} from public;" in compact
    for role in ("anon", "authenticated"):
        assert f"if to_regrole('{role}') is not null then revoke all {header} from {role}; end if;" in compact
    assert (
        f"if to_regrole('service_role') is not null then grant execute {header} to service_role; end if;"
        in compact
    )
    assert compact.count("grant ") == 1
    assert "to anon" not in compact and "to authenticated" not in compact
    # Y se verifica el resultado efectivo, solo con los roles que existen.
    assert "where rolname = 'service_role' and not has_function_privilege(" in compact
    assert "where rolname in ('anon', 'authenticated') and has_function_privilege(" in compact
    assert "message = 'portable_conversation_followup_claim_acl_unexpected'" in compact
    # Fuera del bloque guardado no hay permisos sueltos.
    outside = _executable(sql.replace(roles, ""))
    assert not re.search(r"\b(grant|revoke)\b", outside, re.IGNORECASE)


def test_the_inventories_and_the_sql_suite_know_the_function() -> None:
    inventory = SCHEMA_INVENTORY.read_text(encoding="utf-8")
    row = inventory.split("'20261010000100',", 1)[1].split(")\nselect", 1)[0]
    assert "'20261010000100_portable_conversation_followup_claim.sql'" in row
    for marker in (
        f"to_regprocedure('public.{PORTABLE}({ARGS})')",
        "from public.reserve_portable_checkout_issuance_v2(",
        "portable_followup_template_reply: begin",
        "''blocked_not_template_reply''",
        "has_function_privilege('service_role', oid, 'EXECUTE')",
        f"to_regprocedure('public.{SHARED}({ARGS})')",
    ):
        assert marker in row, marker
    # Cuatro marcadores, todos atados a la portable (absent en Johanna).
    assert re.sub(r"\s+", "", row).endswith(",4,'portable_conversation_followup_claim'")

    signature = f"public.{PORTABLE}({ARGS_CATALOG.replace(',', ', ')})"
    assert f"('{signature}')" in ACL_INVENTORY.read_text(encoding="utf-8")
    acl_validator = (SQL_SUITE / "validate_acl_hardening.mjs").read_text(encoding="utf-8")
    assert f"('{PORTABLE}({ARGS_CATALOG})')" in acl_validator
    assert "result.expected_count !== 120" in acl_validator
    # Lee human_handoff_requests: el validador de Postgres real la permite.
    handoff = (SQL_SUITE / "validate_handoff_postgres.py").read_text(encoding="utf-8")
    assert f"'{PORTABLE}'," in handoff


def test_the_behavioural_validator_runs_in_the_sql_suite() -> None:
    """Un validador que no corre en la cadena no protege nada."""
    validator_name = "validate_portable_conversation_followup.mjs"
    script = json.loads((SQL_SUITE / "package.json").read_text(encoding="utf-8"))["scripts"]["test"]
    assert script.split(" && ").count(f"node {validator_name}") == 1
    validator = (SQL_SUITE / validator_name).read_text(encoding="utf-8")
    assert MIGRATION.name in validator
    # Las dos reservas reales, con la adopcion y el link por las RPC reales:
    # el validador nunca escribe a mano el evento de adopcion.
    for name in (PORTABLE, SHARED):
        assert f"'{name}'" in validator
    assert "public.admit_portable_inbound_commercial_case_v1($1,$2,$3,$4)" in validator
    assert "public.reserve_portable_checkout_issuance_v2(" in validator
    assert "insert into public.conversation_events" not in validator
    # El dato real: la plantilla de F1 y la conversacion 21 de F4.
    assert "chatwoot_inbox_11_message_templates_20261010.json" in validator
    assert "chatwoot_followup_candidate_inbox_11_20261010.json" in validator
