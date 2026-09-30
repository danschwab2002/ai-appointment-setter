"""Migracion 20260930000100: el permiso del pago fallido lo concede la intencion.

plan_portable_payment_failure_recovery se copia de su definicion vigente. Este
test verifica que la copia sea exacta salvo los dos cambios del plan (el punto
'system' y el bloque marcado ``consented_intent_grant``), y que el helper nuevo
quede privado. El comportamiento se prueba en
tests/sql/followup_engine/validate_commercial_ally_payment_failure_recovery.mjs.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20260930000100_payment_failure_consented_intent_authorization.sql"
VIGENT = MIGRATIONS / "20260929000100_pilot_scope_additional_offers.sql"
PLAN = "plan_portable_payment_failure_recovery"
HELPER = "_portable_consented_intent_reason"
HELPER_SIGNATURE = f"public.{HELPER}(uuid,uuid,text)"
MARKED_BLOCK = re.compile(
    r"[ \t]*-- consented_intent_grant: begin\n.*?-- consented_intent_grant: end\n",
    re.DOTALL,
)


def _function(sql: str, name: str) -> str:
    pattern = re.compile(
        rf"create\s+or\s+replace\s+function\s+public\.{name}\s*\(.*?\$function\$;",
        re.DOTALL,
    )
    matches = pattern.findall(sql)
    assert len(matches) == 1, f"expected one definition of {name}"
    return matches[0]


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


def test_the_copied_definition_is_the_vigent_one() -> None:
    later = [
        path.name
        for path in sorted(MIGRATIONS.glob("*.sql"))
        if VIGENT.name < path.name < MIGRATION.name
        and f"function public.{PLAN}(" in path.read_text(encoding="utf-8")
    ]

    assert later == []


def test_plan_is_the_vigent_definition_plus_two_changes() -> None:
    vigent = _function(VIGENT.read_text(encoding="utf-8"), PLAN)
    new = _function(MIGRATION.read_text(encoding="utf-8"), PLAN)

    blocks = MARKED_BLOCK.findall(new)
    assert len(blocks) == 2
    stripped = MARKED_BLOCK.sub("", new)
    assert stripped.count("and point.source in ('hotmart', 'system')") == 1
    stripped = stripped.replace(
        "and point.source in ('hotmart', 'system')",
        "and point.source = 'hotmart'",
    )

    assert _normalized(stripped) == _normalized(vigent)


def test_grant_runs_after_planning_with_the_contact_locked() -> None:
    new = _function(MIGRATION.read_text(encoding="utf-8"), PLAN)
    grant = MARKED_BLOCK.findall(new)[1]
    compact = _normalized(grant)

    assert new.index("from public.plan_payment_failure_recovery_with_identity(") < (
        new.index(grant)
    )
    assert new.index(grant) < new.index("insert into public.pilot_recovery_case_bindings")
    assert compact.index("from public.contacts contact where contact.id = p_contact_id for update") < (
        compact.index(f"from public.{HELPER}(")
    )
    assert "if v_consent_reason = 'consented_intent_ok' and not exists (" in compact
    assert "'allowed', 'system'," in compact
    assert "'reason', 'precheckout_whatsapp_consent'" in compact
    for key in (
        "purchase_intent_id",
        "precheckout_submission_id",
        "consent_copy_version",
        "webhook_event_id",
        "recovery_case_id",
    ):
        assert f"'{key}'," in compact


def test_helper_is_a_private_stable_invoker() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    helper = _normalized(_function(sql, HELPER))
    compact = _normalized(sql)

    assert "language plpgsql stable security invoker" in helper
    assert "set search_path = pg_catalog, public, pg_temp" in helper
    assert f"revoke all on function {HELPER_SIGNATURE} from public;" in compact
    assert (
        f"execute format('revoke all on function {HELPER_SIGNATURE} from %I',v_role);"
        in compact
    )
    assert "rolname in ('anon','authenticated','service_role')" in compact
    executable = re.sub(r"--[^\n]*", "", sql)
    assert not re.search(rf"\bgrant\b[^;]*{HELPER}", executable)
    assert re.search(
        rf"grant execute on function public\.{PLAN}\([^)]*\) to service_role;",
        executable,
    )


def test_helper_is_johanna_criterion_without_fixed_values() -> None:
    helper = _normalized(_function(MIGRATION.read_text(encoding="utf-8"), HELPER)).lower()

    for predicate in (
        "v_intent.lifecycle_state <> 'waiting_for_purchase'",
        "or v_intent.provisional",
        "or not v_intent.provider_observed",
        "'identity_conflict', 'tracking_incomplete', 'expired_unknown'",
        "not v_intent.whatsapp_contact_authorized",
        "not v_intent.activation_authorized",
        "v_intent.normalized_phone <> p_destination_phone",
        "point.contact_id = p_contact_id",
        "submission.contract_version = '1.1.0'",
        "#>> '{consent,whatsapp_contact}' = 'true'",
        "#>> '{consent,marketing_optin}' = 'true'",
        "#>> '{consent,copy_version}' = v_binding.consent_copy_version",
        "conflict.resolved_at is null",
        "binding.status = 'active'",
    ):
        assert predicate in helper, predicate
    for literal in ("johanna", "bxjge6zq", "8104005", "'lancemos'", "psicologajohanna"):
        assert literal not in helper, literal
    reasons = set(re.findall(r"reason_code := '([a-z_]+)'", helper))
    assert reasons == {
        "consented_intent_input_invalid",
        "consented_intent_not_found",
        "consented_intent_not_live",
        "consented_intent_not_authorized",
        "consented_intent_phone_mismatch",
        "consented_intent_binding_unavailable",
        "consented_intent_submission_missing",
        "consented_intent_ok",
    }


def test_migration_is_transactional_and_seeds_nothing() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    outside_functions = re.sub(r"\$function\$.*?\$function\$", "", sql, flags=re.DOTALL)

    assert re.search(r"^begin;\nset local lock_timeout = '5s';\n", sql, re.MULTILINE)
    assert sql.rstrip().endswith("commit;")
    assert not re.search(r"\b(insert|update|delete)\b", outside_functions, re.IGNORECASE)
    assert not re.search(r"\b(alter|drop)\s+(table|function)\b", sql, re.IGNORECASE)
