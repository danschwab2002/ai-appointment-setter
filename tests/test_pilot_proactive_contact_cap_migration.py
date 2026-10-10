"""Text checks of 20261007000100: the per-person proactive cap across flows.

The behaviour (the cap across two scopes of a tenant, the replay, another person,
the other form of the phone, another tenant, the window, max 2, the closed table)
is proven in PGlite by tests/sql/followup_engine/validate_pilot_proactive_contact_cap.mjs.
These checks pin what PGlite cannot: that the migration applies on every live
definition of the shared gate (Johanna does not have the 2026-09-29 to
2026-10-01 migrations), that it creates no function, and that the inventory and
the SQL suite know about it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261007000100_pilot_proactive_contact_cap.sql"
INVENTORY = ROOT / "scripts" / "supabase_schema_inventory.sql"
SQL_SUITE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"

ANCHOR = "    v_local_date := (v_authorized_at at time zone v_scope.timezone)::date;\n"
# Every migration that (re)defines the shared gate, in the order they apply.
GATE_DEFINITIONS = (
    "20260810000100_lancemos_pilot_boundary.sql",
    "20260929000100_pilot_scope_additional_offers.sql",
    "20260929000200_pilot_scope_additional_source_events.sql",
    "20260930000300_pilot_scope_audience_mode.sql",
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _gate_body(filename: str) -> str:
    sql = (MIGRATIONS / filename).read_text(encoding="utf-8")
    start = sql.index("create or replace function public.authorize_lancemos_pilot_request_start(")
    end = sql.index("$function$;", sql.index("as $function$", start))
    return sql[start:end]


def test_migration_comes_right_after_the_sck_one_and_creates_no_function() -> None:
    versions = sorted(path.name.split("_", 1)[0] for path in MIGRATIONS.glob("*.sql"))
    position = versions.index("20261007000100")
    assert versions[position - 1] == "20261005000100"
    # Despues van el arreglo de la adopcion (20261009000200) y la reserva
    # portable del seguimiento con cupon (20261010000100).
    assert versions[position + 1:] == ["20261009000200", "20261010000100"]

    sql = _sql()
    assert sql.lstrip().startswith("-- Migration:")
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    # The gate is changed in place (create or replace keeps its grants) and the
    # DO blocks are anonymous: nothing new to grant or revoke but the table.
    assert "create function" not in sql
    assert "create or replace function" not in sql
    assert "drop function" not in sql


def test_every_definition_of_the_gate_has_the_anchor_exactly_once() -> None:
    names = sorted(
        path.name
        for path in MIGRATIONS.glob("*.sql")
        if "create or replace function public.authorize_lancemos_pilot_request_start("
        in path.read_text(encoding="utf-8")
    )
    assert tuple(names) == GATE_DEFINITIONS
    for name in GATE_DEFINITIONS:
        body = _gate_body(name)
        assert body.count(ANCHOR) == 1, name
        # The block reads the selected identity and the scope tenant, which every
        # definition has before the anchor.
        assert body.index("into v_identity") < body.index(ANCHOR), name
        assert "v_scope.tenant_key" in body, name


def test_the_block_goes_before_the_scope_budgets_and_fails_closed() -> None:
    sql = _sql()
    assert "v_anchor constant text :=\n" in sql
    assert "E'    v_local_date := (v_authorized_at at time zone v_scope.timezone)::date;\\n'" in sql
    # Insert before the anchor, so the cap runs after the replay, the cohort and
    # the audience, and before the total and daily budgets of the scope.
    assert "execute replace(v_definition, v_anchor, v_block || v_anchor);" in sql
    # Exactly once, never twice, never on a definition someone changed.
    assert "<> length(v_anchor)" in sql
    assert "position('pilot_proactive_contact_cap: begin' in v_definition) > 0" in sql
    assert sql.count("errcode = '55000'") == 3
    compact = re.sub(r"\s+", " ", sql)
    # Serialized per person across scopes: the control lock is per scope.
    assert "pg_advisory_xact_lock(hashtextextended(" in compact
    assert "''pilot-proactive-contact-cap:'' || v_scope.tenant_key" in compact
    # Counted across every scope of the tenant, by contact and by phone form.
    assert "where scope_row.tenant_key = v_scope.tenant_key" in compact
    assert "public._whatsapp_phone_variants(v_identity.external_user_id)" in compact
    assert "and other.account_id = v_identity.account_id" in compact
    assert "''pilot_contact_proactive_cap_reached''::text" in compact


def test_the_cap_table_is_opt_in_and_closed_to_the_api() -> None:
    sql = _sql()
    assert "create table public.pilot_proactive_contact_caps (" in sql
    assert "tenant_key text primary key" in sql
    assert "check (max_request_starts between 1 and 20)" in sql
    assert "alter table public.pilot_proactive_contact_caps enable row level security;" in sql
    assert "revoke all on table public.pilot_proactive_contact_caps from public;" in sql
    assert "rolname in ('anon','authenticated','service_role')" in sql
    assert "revoke all on table public.pilot_proactive_contact_caps from %I" in sql
    # Nothing seeds a tenant: without a row there is no cap.
    assert "insert into public.pilot_proactive_contact_caps" not in sql


def test_the_inventory_fingerprints_the_migration() -> None:
    inventory = INVENTORY.read_text(encoding="utf-8")
    block = inventory.split("'20261007000100',", 1)[1].split("'pilot_proactive_contact_cap'", 1)[0]
    assert "'20261007000100_pilot_proactive_contact_cap.sql'" in block
    for marker in (
        "relation.relname = 'pilot_proactive_contact_caps'",
        "to_regclass('public.pilot_proactive_contact_caps') is null then 0",
        "pilot_outbound_authorizations_contact_time_idx",
        "authorize_lancemos_pilot_request_start(text,integer,text,bigint,bigint,text,text,text,text,text,text,uuid,uuid,uuid,timestamptz)",
        "pilot_proactive_contact_cap: begin",
    ):
        assert marker in block
    assert re.sub(r"\s+", "", block).endswith(",4,")


def test_the_sql_suite_runs_the_validator() -> None:
    script = json.loads(SQL_SUITE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert "node validate_pilot_proactive_contact_cap.mjs" in script
