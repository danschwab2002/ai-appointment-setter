from pathlib import Path
import re


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "20260805000100_followup_identity_audit.sql"
)


def _sql() -> str:
    assert MIGRATION.exists(), "missing follow-up identity audit migration"
    return re.sub(r"\s+", " ", MIGRATION.read_text(encoding="utf-8").lower()).strip()


def test_resolved_identity_transition_records_a_matched_attempt_atomically() -> None:
    sql = _sql()

    assert "create function public.record_resolved_identity_attempt()" in sql
    assert "before update on public.recovery_cases" in sql
    assert "new.identity_resolution_status = 'resolved'" in sql
    assert "new.selected_channel_identity_id is not null" in sql
    assert "old.identity_resolution_status is distinct from new.identity_resolution_status" in sql
    assert "old.selected_channel_identity_id is distinct from new.selected_channel_identity_id" in sql
    assert "insert into public.identity_resolution_attempts" in sql
    assert "strategy" in sql and "'other'" in sql
    assert "status" in sql and "'matched'" in sql
    assert "matched_channel_identity_id" in sql
    assert "selected_channel_identity_transition" in sql
    assert "new.identity_resolution_attempt_count" in sql
    assert "new.identity_resolution_last_attempt_at" in sql


def test_existing_resolved_cases_without_attempts_are_backfilled_idempotently() -> None:
    sql = _sql()

    assert "where rc.identity_resolution_status = 'resolved'" in sql
    assert "not exists" in sql
    assert "ira.recovery_case_id = rc.id" in sql
    assert "update public.recovery_cases rc" in sql
    assert "identity_resolution_attempt_count" in sql
    assert "identity_resolution_last_attempt_at" in sql


def test_identity_audit_trigger_helper_is_not_api_callable() -> None:
    sql = _sql()

    signature = "public.record_resolved_identity_attempt()"
    assert f"revoke execute on function {signature} from public" in sql
    for role in ("anon", "authenticated", "service_role"):
        assert f"rolname = '{role}'" in sql
        assert f"revoke execute on function {signature} from {role}" in sql
