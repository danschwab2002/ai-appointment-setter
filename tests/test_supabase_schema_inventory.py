from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "scripts" / "supabase_schema_inventory.sql"
ACL_INVENTORY = ROOT / "scripts" / "supabase_acl_inventory.sql"
MIGRATIONS = ROOT / "supabase" / "migrations"


def _without_comments_and_literals(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", "", sql)
    return re.sub(r"'(?:''|[^'])*'", "''", sql)


def test_supabase_inventories_are_catalog_only() -> None:
    forbidden = {
        "alter",
        "call",
        "create",
        "delete",
        "do",
        "drop",
        "grant",
        "insert",
        "revoke",
        "truncate",
        "update",
    }
    for path in (INVENTORY, ACL_INVENTORY):
        executable = _without_comments_and_literals(path.read_text(encoding="utf-8"))
        observed = {
            token.lower() for token in re.findall(r"\b[A-Za-z_]+\b", executable)
        }

        assert forbidden.isdisjoint(observed), path
        assert executable.strip().lower().startswith("with"), path
        assert executable.count(";") == 1, path


def test_supabase_schema_inventory_covers_every_canonical_migration() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    documented = re.findall(r"'(\d{14}_[a-z0-9_]+\.sql)'", sql)
    canonical = [path.name for path in sorted(MIGRATIONS.glob("*.sql"))]

    assert documented == canonical
    assert len(documented) == len(set(documented))


def test_supabase_schema_inventory_reports_non_authoritative_fingerprints() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")

    assert "fingerprint_present" in sql
    assert "fingerprint_absent" in sql
    assert "fingerprint_partial" in sql
    assert "migration_applied" not in sql
    assert "select\n    version" in sql.lower()


def test_absolute_deadline_fingerprint_checks_semantics_and_rejects_chaining() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")

    assert "min(attempt.accepted_at)" in sql
    assert "v_next_due_at := v_sequence_started_at + v_next_delay" in sql
    assert "v_next_due_at := p_now + v_next_delay" in sql
    assert "followup_policy_step_offsets_validate" in sql


def test_supabase_acl_inventory_is_exhaustive_and_allowlisted() -> None:
    sql = ACL_INVENTORY.read_text(encoding="utf-8")
    allowlisted = re.findall(r"\('public\.([a-z0-9_]+\([^']*\))'\)", sql)

    assert len(allowlisted) == 29
    assert len(allowlisted) == len(set(allowlisted))
    assert "admit_precheckout_form_submission(text, jsonb, jsonb)" in allowlisted
    assert "has_function_privilege('anon'" in sql
    assert "has_function_privilege('authenticated'" in sql
    assert "has_function_privilege('service_role'" in sql
    assert "result_type = 'trigger'" in sql
    assert "service_role_allowlist_mismatch" in sql
    assert "security_definer_search_path_missing" in sql
