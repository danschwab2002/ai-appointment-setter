"""Text checks of 20261016000100: the flow definition store (1b, piece 1).

The behaviour (the cycle, the guards, the invariants, the permissions as
PostgREST would use them, nothing old changing) is proven in PGlite by
tests/sql/followup_engine/validate_flow_store.mjs, and the races on a real
PostgreSQL 17 by tests/sql/followup_engine/real_postgres_flow_store.py. These
checks pin what has to hold in the text of the migration itself: it creates no
role and concedes nothing, it redefines no function, every function it creates
is security invoker with a fixed search_path and is in the permissions block,
and it only touches the four new tables.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261016000100_flow_change_store_v1.sql"
SQL_SUITE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"

NEW_TABLES = ("flow_change_proposals", "flow_controls", "flow_definitions", "flow_change_events")

# The closed list of functions of the design (sections 2 and 3): the two helpers of the
# CHECK constraints, the five trigger functions, the 17 internal helpers and the
# seven functions of the owner.
CHECK_HELPERS = ("_flow_store_actor_is_valid", "_flow_store_free_text_is_valid")
TRIGGER_FUNCTIONS = (
    "reject_flow_store_mutation",
    "validate_flow_control_change",
    "record_flow_control_change",
    "validate_flow_definition_insert",
    "validate_flow_change_proposal_change",
)
INTERNAL_HELPERS = (
    "_flow_store_instance",
    "_flow_store_product_is_bound",
    "_flow_store_base",
    "_flow_change_item_flow_ids",
    "_flow_change_items_are_valid",
    "_flow_change_findings_are_valid",
    "_flow_change_result_is_valid",
    "_flow_change_validator_is_valid",
    "_flow_change_fingerprint",
    "_flow_change_event",
    "_flow_change_expire_if_due",
    "_flow_change_save_draft",
    "_flow_change_record_validation",
    "_flow_change_approve",
    "_flow_change_publish",
    "_flow_change_pause",
    "_flow_change_discard",
)
OPERATOR_FUNCTIONS = (
    "operator_register_flow_v1",
    "operator_save_flow_change_draft_v1",
    "operator_record_flow_change_validation_v1",
    "operator_approve_flow_change_v1",
    "operator_publish_flow_change_v1",
    "operator_pause_flow_v1",
    "operator_discard_flow_change_v1",
)
# The ones that wait for row locks carry lock_timeout as a clause of the
# function (a local set_config would last until the end of the caller's
# transaction): validating, approving, publishing and pausing.
WAITS_FOR_LOCKS = (
    "_flow_change_record_validation",
    "_flow_change_approve",
    "_flow_change_publish",
    "_flow_change_pause",
    "operator_record_flow_change_validation_v1",
    "operator_approve_flow_change_v1",
    "operator_publish_flow_change_v1",
    "operator_pause_flow_v1",
)
TRIGGERS = {
    "flow_controls_validate": ("before insert or update", "flow_controls", "validate_flow_control_change"),
    "flow_controls_record_change": ("after insert or update", "flow_controls", "record_flow_control_change"),
    "flow_controls_permanent": ("before delete", "flow_controls", "reject_flow_store_mutation"),
    "flow_controls_no_truncate": ("before truncate", "flow_controls", "reject_flow_store_mutation"),
    "flow_controls_set_updated_at": ("before update", "flow_controls", "set_updated_at"),
    "flow_definitions_validate": ("before insert", "flow_definitions", "validate_flow_definition_insert"),
    "flow_definitions_immutable": ("before update or delete", "flow_definitions", "reject_flow_store_mutation"),
    "flow_definitions_no_truncate": ("before truncate", "flow_definitions", "reject_flow_store_mutation"),
    "flow_change_proposals_validate": (
        "before update",
        "flow_change_proposals",
        "validate_flow_change_proposal_change",
    ),
    "flow_change_proposals_permanent": ("before delete", "flow_change_proposals", "reject_flow_store_mutation"),
    "flow_change_proposals_no_truncate": (
        "before truncate",
        "flow_change_proposals",
        "reject_flow_store_mutation",
    ),
    "flow_change_proposals_set_updated_at": ("before update", "flow_change_proposals", "set_updated_at"),
    "flow_change_events_append_only": ("before update or delete", "flow_change_events", "reject_flow_store_mutation"),
    "flow_change_events_no_truncate": ("before truncate", "flow_change_events", "reject_flow_store_mutation"),
}


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _without_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _functions(sql: str) -> dict[str, str]:
    """Each created function, from its create to the end of its body."""
    bodies: dict[str, str] = {}
    for match in re.finditer(r"create function public\.(\w+)\(", sql):
        end = sql.index("$function$;", sql.index("$function$", match.end()) + len("$function$"))
        assert match.group(1) not in bodies, match.group(1)
        bodies[match.group(1)] = sql[match.start() : end]
    return bodies


def _escaped_free_text_class() -> str:
    """The character class of the free text, written with ASCII escapes.

    Built from code points so that no escape sequence is typed here: the class
    is C0 and C1, the soft hyphen, the direction and zero-width marks, the line
    and paragraph separators, the bidi isolates, the deprecated format
    characters, the BOM, the interlinear annotation anchors and the tags
    (design, section 2.3, and the review of the code, C#7). A code point above
    U+FFFF is written with the eight digits of the regular expressions of
    PostgreSQL.
    """
    ranges = (
        (0x0001, 0x001F),
        (0x007F, 0x009F),
        (0x00AD, None),
        (0x061C, None),
        (0x180E, None),
        (0x200B, 0x200F),
        (0x2028, 0x202E),
        (0x2060, 0x2064),
        (0x2066, 0x206F),
        (0xFEFF, None),
        (0xFFF9, 0xFFFB),
        (0xE0000, 0xE007F),
    )

    def escape(code: int) -> str:
        return "\\" + (f"u{code:04x}" if code <= 0xFFFF else f"U{code:08x}")

    return "[" + "".join(
        escape(first) + ("" if last is None else "-" + escape(last)) for first, last in ranges
    ) + "]"


def _acl_block(sql: str) -> str:
    start = sql.index("do $acl$")
    return sql[start : sql.index("$acl$;", start + len("do $acl$"))]


def test_the_migration_is_the_tail_of_the_chain() -> None:
    versions = sorted(path.name.split("_", 1)[0] for path in MIGRATIONS.glob("*.sql"))
    assert versions[-1] == "20261016000100"
    assert versions[-2] == "20261015000200"


def test_no_role_no_grant_no_redefinition_and_no_definer() -> None:
    sql = _sql().lower()
    for forbidden in ("create role", "alter role", "grant ", "create or replace", "security definer"):
        assert forbidden not in sql, forbidden
    for forbidden in ("alter default privileges", "drop ", "truncate public.", "insert into public.commercial"):
        assert forbidden not in sql, forbidden
    assert "set role" not in sql
    assert "session_replication_role" not in sql


def test_it_opens_with_begin_and_the_lock_timeout_and_closes_with_commit() -> None:
    sql = _sql()
    statements = _without_comments(sql).strip()
    assert statements.startswith(
        "begin;\n\nset local lock_timeout = '5s';\nset local statement_timeout = '30s';\n"
    )
    assert statements.endswith("commit;")
    assert statements.count("begin;") == 1
    assert statements.count("commit;") == 1


def test_it_creates_exactly_the_closed_list_of_functions() -> None:
    functions = _functions(_sql())
    expected = CHECK_HELPERS + TRIGGER_FUNCTIONS + INTERNAL_HELPERS + OPERATOR_FUNCTIONS
    assert len(expected) == 31
    assert sorted(functions) == sorted(expected)
    assert _sql().count("create function ") == 31


def test_every_function_is_security_invoker_with_a_fixed_search_path() -> None:
    for name, body in _functions(_sql()).items():
        header = body[: body.index("as $function$")]
        assert "\nsecurity invoker\n" in header, name
        search_paths = re.findall(r"\nset search_path = ([^\n]+)\n", header)
        assert search_paths in (["pg_catalog, public, pg_temp"], ["pg_catalog, pg_temp"]), name
        if name in OPERATOR_FUNCTIONS or name in TRIGGER_FUNCTIONS:
            assert search_paths == ["pg_catalog, public, pg_temp"], name


def test_only_the_functions_that_wait_for_locks_carry_the_lock_timeout() -> None:
    for name, body in _functions(_sql()).items():
        header = body[: body.index("as $function$")]
        assert ("\nset lock_timeout = '5s'\n" in header) == (name in WAITS_FOR_LOCKS), name


def test_the_check_helpers_are_immutable_and_the_fingerprint_is_stable() -> None:
    functions = _functions(_sql())
    for name in CHECK_HELPERS:
        assert "\nimmutable\n" in functions[name], name
    # convert_to is STABLE in the catalog.
    assert "\nstable\n" in functions["_flow_change_fingerprint"]
    for name in OPERATOR_FUNCTIONS:
        assert "\nvolatile\n" in functions[name], name


def test_every_created_function_is_in_the_permissions_block() -> None:
    sql = _sql()
    block = _acl_block(sql)
    listed = re.findall(r"'public\.(\w+)\(", block)
    assert sorted(listed) == sorted(_functions(sql))
    assert len(listed) == len(set(listed)) == 31
    assert "]::regprocedure[] loop" in block
    assert "execute format('revoke all on function %s from public', v_function);" in block
    assert "execute format('revoke all on function %s from %I', v_function, v_role);" in block
    assert block.count("where r.rolname in ('anon', 'authenticated', 'service_role')") == 2
    # The four tables, from public and from each of the three roles.
    compact = re.sub(r"\s+", " ", block)
    tables = (
        "public.flow_controls, public.flow_definitions, public.flow_change_proposals, "
        "public.flow_change_events"
    )
    assert f"revoke all on table {tables} from public;" in compact
    assert f"'revoke all on table public.flow_controls, public.flow_definitions, ' 'public.flow_change_proposals, public.flow_change_events from %I'" in compact


def test_it_only_touches_the_four_new_tables() -> None:
    sql = _without_comments(_sql())
    created = re.findall(r"create table public\.(\w+)", sql)
    assert created == list(NEW_TABLES)
    # Every foreign key points to one of the new tables: a key to a live table
    # would take a lock on it.
    assert sorted(set(re.findall(r"references public\.(\w+)", sql))) == sorted(
        {"flow_change_proposals", "flow_controls", "flow_definitions"}
    )
    assert set(re.findall(r"alter table public\.(\w+)", sql)) <= set(NEW_TABLES)
    assert set(re.findall(r"\bon public\.(\w+)", sql)) <= set(NEW_TABLES)
    for table in NEW_TABLES:
        assert f"alter table public.{table} enable row level security;" in sql, table
    # The id of the events is a uuid: no sequence, no identity column.
    assert re.search(r"\bas identity\b", sql) is None
    assert "serial" not in sql
    assert "create sequence" not in sql


def test_the_fourteen_triggers() -> None:
    sql = _without_comments(_sql())
    found = re.findall(
        r"create trigger (\w+)\n(before|after) ([a-z ]+?) on public\.(\w+)\n"
        r"for each (row|statement) execute function public\.(\w+)\(\);",
        sql,
    )
    assert len(found) == 14
    observed = {name: (f"{when} {events}", table, function) for name, when, events, table, _, function in found}
    assert observed == TRIGGERS
    for name, _, events, _, level, _ in found:
        assert (level == "statement") == ("truncate" in events), name
    # set_updated_at is the one of the baseline, used as it is.
    assert "create function public.set_updated_at" not in sql


def test_the_guards_compare_with_is_distinct_from() -> None:
    functions = _functions(_sql())
    record = functions["_flow_change_record_validation"]
    assert "if v_draft_now is distinct from v_proposal.draft_base\n       or p_validated_base is distinct from v_base then" in record
    for name in ("_flow_change_approve", "_flow_change_publish"):
        assert "if v_base is distinct from v_proposal.base then" in functions[name], name
    assert "if p_base is distinct from v_current then" in functions["_flow_change_save_draft"]
    # The base carries the generation of the flows of the proposal.
    base = functions["_flow_store_base"]
    assert "jsonb_build_object('active_version', c.active_version, 'generation', c.generation)" in base
    # Publishing looks at the deadline once, with every lock taken.
    publish = functions["_flow_change_publish"]
    assert publish.index("for update;\n    if public._flow_change_expire_if_due(v_proposal) then") > publish.index(
        "where c.tenant_ref = p_tenant_ref and c.ally_ref = p_ally_ref\n     order by c.flow_id\n     for update;"
    )
    assert publish.count("_flow_change_expire_if_due") == 1
    # The first write comes after every check that returns a result.
    first_write = publish.index("insert into public.flow_definitions")
    for check in (
        "'flow_change_revalidation_blocked'",
        "'flow_change_stale_base'",
        "'flow_change_rollback_target_invalid'",
        "'flow_change_state_transition_invalid'",
        "'flow_change_flow_has_no_version'",
        "'flow_store_product_not_bound'",
    ):
        assert publish.index(check) < first_write, check


def test_the_zone_is_checked_only_with_an_object_schedule() -> None:
    trigger = _functions(_sql())["validate_flow_definition_insert"]
    assert "if jsonb_typeof(new.definition -> 'horario') = 'object' then" in trigger
    assert "elsif new.definition -> 'horario' is distinct from '\"siempre\"'::jsonb then" in trigger
    assert "order by d.version\n         limit 1;" in trigger
    assert "new.definition_sha256 := encode(sha256(convert_to(new.definition::text, 'UTF8')), 'hex');" in trigger


def test_the_free_text_class_is_written_with_ascii_escapes() -> None:
    sql = _sql()
    assert sql.isascii()
    helper = _functions(sql)["_flow_store_free_text_is_valid"]
    assert f"p_text !~ '{_escaped_free_text_class()}'" in helper
    assert "[[:cntrl:]]" not in _without_comments(sql)


def test_the_sql_suite_runs_the_store_validator() -> None:
    script = json.loads(SQL_SUITE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert "node validate_flow_store.mjs" in script.split(" && ")
