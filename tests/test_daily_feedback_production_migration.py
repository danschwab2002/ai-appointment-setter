from __future__ import annotations

from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260910000100_daily_feedback_production_v1.sql"
)
FENCING_MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260911000100_daily_feedback_notification_fencing_v1.sql"
)
MULTI_REVIEWER_MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260911000200_daily_feedback_multi_reviewer_ownership_v1.sql"
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


def test_multi_reviewer_migration_snapshots_batch_authority_and_ownership() -> None:
    sql = MULTI_REVIEWER_MIGRATION.read_text()
    assert "create table public.daily_feedback_batch_reviewer_bindings" in sql
    assert "deletion_accountable boolean not null" in sql
    assert "accountable_reviewer_refs text[] not null" in sql
    assert "accountable_reviewers jsonb not null" in sql
    assert "reviewer_binding_generation" in sql
    assert "slack_user_id text not null" in sql
    assert "purge_actor_ref text not null" in sql
    assert "reviewer_set_hash" in sql
    assert "insert into public.daily_feedback_batch_reviewer_bindings" in sql
    assert "daily_feedback_purge_tombstones_immutable" in sql
    assert "daily_feedback_tombstone_immutable_guard" in sql


def test_multi_reviewer_migration_reauthorizes_against_the_batch_snapshot() -> None:
    sql = MULTI_REVIEWER_MIGRATION.read_text()
    for function in (
        "configure_daily_feedback_scope_v2",
        "claim_daily_feedback_collection_v1",
        "commit_daily_feedback_batch_v1",
        "complete_daily_feedback_oidc_v1",
        "get_daily_feedback_review_page_v1",
        "purge_expired_daily_feedback_v2",
    ):
        assert f"function public.{function}" in sql
    assert "daily_feedback_batch_reviewer_bindings" in sql
    assert "daily_feedback_config_v1_disabled" in sql
    assert "legacy_reviewer_set_blocked" in sql
    assert "notification_state in ('pending','retry','claimed')" in sql
    assert "notification_state in ('pending','retry','claimed','delivery_unknown')" not in sql
    assert "claim_daily_feedback_notification_v1" in sql
    assert "get_daily_feedback_readiness_v1" in sql
    assert "revoke execute on function public.configure_daily_feedback_scope_v1" in sql
    assert "revoke execute on function public.purge_expired_daily_feedback_v1" in sql


def test_multi_reviewer_configuration_is_a_closed_canonical_set() -> None:
    sql = MULTI_REVIEWER_MIGRATION.read_text()
    for marker in (
        "duplicate_reviewer_ref",
        "duplicate_slack_user_id",
        "unknown_reviewer_key",
        "all_reviewers_must_be_deletion_accountable",
        "jsonb_array_length(p_reviewers) <> 4",
        "where (s.enabled or p_force)",
        "jsonb_array_elements(p_reviewers)",
        "order by reviewer_ref",
    ):
        assert marker in sql


def test_purge_uses_server_time_and_bounded_non_null_limit() -> None:
    sql = MULTI_REVIEWER_MIGRATION.read_text()
    assert "v_authoritative_now:=clock_timestamp()" in sql
    assert "retention_expires_at<=v_authoritative_now" in sql
    assert "p_limit is null or p_limit not between 1 and 100" in sql
    assert "retention_expires_at<=p_now" not in sql


def test_notification_retry_inputs_are_closed_and_bounded() -> None:
    sql = FENCING_MIGRATION.read_text()
    assert "invalid_notification_retry" in sql
    assert "p_error_code !~ '^[a-z][a-z0-9_]{2,63}$'" in sql
    assert "p_retry_seconds is null or p_retry_seconds not between 1 and 900" in sql
