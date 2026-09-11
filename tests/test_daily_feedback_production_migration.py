from __future__ import annotations

from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260910000100_daily_feedback_production_v1.sql"
)


def _sql() -> str:
    return MIGRATION.read_text()


def test_daily_feedback_migration_declares_the_shared_authoritative_state() -> None:
    sql = _sql()
    for table in (
        "daily_feedback_reviewer_bindings",
        "daily_feedback_schedules",
        "daily_feedback_batches",
        "daily_feedback_items",
        "daily_feedback_decisions",
        "daily_feedback_oidc_states",
        "daily_feedback_sessions",
        "daily_feedback_workflow_commands",
        "daily_feedback_purge_tombstones",
    ):
        assert f"create table public.{table}" in sql
        assert f"'{table}'" in sql
    assert "enable row level security" in sql


def test_daily_feedback_migration_has_fenced_collection_and_notification_rpcs() -> None:
    sql = _sql()
    for function in (
        "configure_daily_feedback_scope_v1",
        "claim_daily_feedback_collection_v1",
        "commit_daily_feedback_batch_v1",
        "fail_daily_feedback_collection_v1",
        "claim_daily_feedback_notification_v1",
        "mark_daily_feedback_notification_started_v1",
        "complete_daily_feedback_notification_v1",
        "retry_daily_feedback_notification_v1",
        "purge_expired_daily_feedback_v1",
    ):
        assert f"function public.{function}" in sql
    assert "for update skip locked" in sql
    assert "lease_generation" in sql
    assert "lease_owner" in sql


def test_daily_feedback_migration_reauthorizes_each_web_operation() -> None:
    sql = _sql()
    for function in (
        "begin_daily_feedback_oidc_v1",
        "complete_daily_feedback_oidc_v1",
        "get_daily_feedback_review_page_v1",
        "record_daily_feedback_decision_v1",
    ):
        assert f"function public.{function}" in sql
    assert "binding_generation" in sql
    assert "retention_expires_at" in sql
    assert "slack_team_id" in sql
    assert "slack_user_id" in sql


def test_daily_feedback_migration_is_rpc_only_and_keeps_feedback_literal() -> None:
    sql = _sql()
    assert "verbatim_feedback" in sql
    assert "candidate_change" not in sql
    assert "grant execute on function" in sql
    assert "revoke all on table public.%I from public" in sql
    assert "from pg_roles where rolname in ('anon','authenticated','service_role')" in sql
    assert "revoke all on all functions in schema public" not in sql
    assert "if exists(select 1 from pg_roles where rolname='service_role')" in sql
    assert "from anon, authenticated, service_role" not in sql
    assert "from anon, authenticated" not in sql
    assert "from service_role;" not in sql
    assert "grant select" not in sql.lower()
