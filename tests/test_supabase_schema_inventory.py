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


def test_hotmart_base_search_path_fingerprint_uses_exact_signatures() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    fingerprint = sql.split("'20260820000200'", 1)[1].split(")\nselect", 1)[0]

    assert re.search(
        r"to_regprocedure\(\s*'public\._admit_hotmart_purchase_approved_base\(text,jsonb\)'\s*\)",
        fingerprint,
    )
    assert re.search(
        r"to_regprocedure\(\s*'public\._admit_hotmart_cart_abandonment_base\(text,jsonb\)'\s*\)",
        fingerprint,
    )
    assert "proname in" not in fingerprint


def test_hotmart_contract_fingerprint_uses_exact_legacy_and_wrapper_signatures() -> None:
    sql = INVENTORY.read_text(encoding="utf-8")
    fingerprint = sql.split("'20260820000400'", 1)[1].split(")\nselect", 1)[0]
    compact_fingerprint = re.sub(r"\s+", "", fingerprint)

    for signature in (
        "public.admit_hotmart_purchase_approved(text,jsonb)",
        "public.admit_hotmart_cart_abandonment(text,jsonb)",
        "public.admit_and_correlate_hotmart_purchase_approved(text,jsonb,text,text)",
        "public.admit_and_correlate_hotmart_cart_abandonment(text,jsonb,text,text)",
    ):
        assert f"to_regprocedure('{signature}')" in compact_fingerprint
    assert "proname" not in fingerprint


def test_supabase_acl_inventory_is_exhaustive_and_allowlisted() -> None:
    sql = ACL_INVENTORY.read_text(encoding="utf-8")
    allowlisted = re.findall(r"\('public\.([a-z0-9_]+\([^']*\))'\)", sql)

    assert len(allowlisted) == 45
    assert len(allowlisted) == len(set(allowlisted))
    assert "admit_precheckout_form_submission(text, jsonb, jsonb)" in allowlisted
    assert "admit_observed_lead_precheckout(text, jsonb, jsonb)" in allowlisted
    assert "admit_johanna_payment_failure(text, jsonb, text, text)" in allowlisted
    assert "correlate_hotmart_purchase_intent(uuid)" in allowlisted
    assert (
        "begin_johanna_abandonment_one_shot(text, uuid, text, bigint, bigint, text, integer, bigint)"
        in allowlisted
    )
    assert (
        "finish_johanna_abandonment_one_shot(uuid, text, bigint, bigint, text)"
        in allowlisted
    )
    assert (
        "reconcile_johanna_abandonment_one_shot(text, bigint, bigint)"
        in allowlisted
    )
    assert (
        "begin_johanna_abandonment_hotmart_auto(text, uuid, uuid, text, bigint, bigint, text, integer, bigint)"
        in allowlisted
    )
    assert (
        "list_operator_unresolved_correlations(text, text, integer, uuid)"
        in allowlisted
    )
    assert (
        "get_operator_unresolved_correlation(text, text, uuid)" in allowlisted
    )
    assert (
        "list_due_hotmart_abandonment_reevaluations(timestamp with time zone, integer)"
        in allowlisted
    )
    assert (
        "reevaluate_hotmart_abandonment_timer(uuid, timestamp with time zone)"
        in allowlisted
    )
    assert "admit_and_correlate_hotmart_purchase_approved(text, jsonb, text, text)" in allowlisted
    assert "admit_and_correlate_hotmart_cart_abandonment(text, jsonb, text, text)" in allowlisted
    assert "admit_hotmart_purchase_approved(text, jsonb)" not in allowlisted
    assert "admit_hotmart_cart_abandonment(text, jsonb)" not in allowlisted
    assert "begin_precheckout_test_first_touch(text, uuid, text, bigint, bigint)" in allowlisted
    assert "finish_precheckout_test_first_touch(uuid, text, bigint, bigint, text)" in allowlisted
    assert "has_function_privilege('anon'" in sql
    assert "has_function_privilege('authenticated'" in sql
    assert "has_function_privilege('service_role'" in sql
    assert "result_type = 'trigger'" in sql
    assert "service_role_allowlist_mismatch" in sql
    assert "security_definer_search_path_missing" in sql
