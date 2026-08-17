"""Contract tests for the shadow commercial-case root migration."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260816000100_commercial_case_root.sql"
)


def _sql() -> str:
    assert MIGRATION.exists(), "missing commercial-case root migration"
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_cut_a_creates_shadow_root_and_backfills_recoveries_one_to_one() -> None:
    sql = _sql()

    assert "create table public.commercial_cases" in sql
    assert "alter table public.recovery_cases" in sql
    assert "add column commercial_case_id" in sql
    assert "insert into public.commercial_cases" in sql
    assert "select rc.id" in sql
    assert "set commercial_case_id = rc.id" in sql
    assert "alter column commercial_case_id set not null" in sql
    assert "unique (commercial_case_id)" in sql
    assert "recovery_case_id uuid unique" in sql
    assert "references public.recovery_cases(id) on delete cascade" in sql
    assert "authority_mode text not null default 'shadow'" in sql


def test_recovery_remains_authoritative_and_parent_is_synchronized() -> None:
    sql = _sql()

    assert "function public.bind_recovery_commercial_case_id" in sql
    assert "before insert or update on public.recovery_cases" in sql
    assert "new.commercial_case_id := new.id" in sql
    assert "function public.sync_recovery_commercial_case" in sql
    assert "after insert or update on public.recovery_cases" in sql
    assert "case new.status" in sql
    assert "update public.commercial_cases" in sql
    assert "commercial_case_root_mismatch" in sql
    assert "commercial_case_kind_not_enabled" in sql
    assert "trigger commercial_cases_protect_shadow" in sql
    assert "constraint trigger recovery_cases_validate_commercial_case_shadow" in sql
    assert "deferrable initially deferred" in sql
    assert "when 'expired' then 'completed'" in sql
    assert "when 'escalated' then 'disabled'" in sql
    assert "else 'completed'" not in sql
    assert "else 'disabled'" not in sql
    assert "pg_trigger_depth" not in sql
    assert "new.created_at is distinct from v_recovery.created_at" in sql
    assert "new.updated_at is distinct from v_recovery.updated_at" in sql
    assert "created_at = new.created_at" in sql
    assert "updated_at = new.updated_at" in sql
    assert "conversation_id uuid references public.conversations" not in sql
    assert "selected_channel_identity_id uuid references" not in sql


def test_cut_a_does_not_add_runtime_or_effect_entrypoints() -> None:
    sql = _sql()

    assert sql.count("security definer") == 2
    for function_name in (
        "sync_recovery_commercial_case",
        "validate_recovery_commercial_case_shadow",
    ):
        function_block = sql.split(
            f"create function public.{function_name}()", maxsplit=1
        )[1].split("$function$;", maxsplit=1)[0]
        assert "security definer" in function_block
        assert "set search_path = pg_catalog, public, pg_temp" in function_block
    assert "grant execute" not in sql
    assert "scheduled_actions" not in sql
    assert "human_handoff_requests" not in sql
    assert "purchase_intents" not in sql
    assert "insert into public.conversations" not in sql


def test_shadow_table_is_closed_to_api_roles() -> None:
    sql = _sql()

    assert "alter table public.commercial_cases enable row level security" in sql
    assert "revoke all on table public.commercial_cases from public" in sql
    for role in ("anon", "authenticated", "service_role"):
        assert f"where rolname = '{role}'" in sql
        assert f"revoke all on table public.commercial_cases from {role}" in sql
        for function_name in (
            "bind_recovery_commercial_case_id",
            "sync_recovery_commercial_case",
            "protect_commercial_case_shadow",
            "validate_recovery_commercial_case_shadow",
        ):
            assert (
                f"revoke execute on function public.{function_name}() from {role}"
                in sql
            )
