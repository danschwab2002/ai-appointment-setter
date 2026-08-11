from pathlib import Path
import re


MIGRATION = Path(
    "supabase/migrations/20260810000400_executable_human_handoff.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def _function_body(sql: str, name: str) -> str:
    pattern = re.compile(
        rf"create\s+or\s+replace\s+function\s+public\.{name}\s*\(.*?"
        rf"as\s+\$function\$(.*?)\$function\$;",
        re.DOTALL,
    )
    match = pattern.search(sql)
    assert match is not None, f"missing function {name}"
    return match.group(1)


def test_handoff_migration_adds_physically_constrained_requests_and_effects() -> None:
    sql = _sql()

    assert "create table public.human_handoff_projection_policies" in sql
    assert "expected_team_id bigint not null" in sql
    assert "unique (policy_key, policy_version)" in sql
    assert "active boolean not null default false" in sql
    assert "private_note_body text not null" in sql

    assert "create table public.human_handoff_requests" in sql
    assert "command_key text not null unique" in sql
    assert "conversation_id uuid not null" in sql
    assert "projection_policy_key text not null" in sql
    assert "projection_policy_version integer not null" in sql
    assert "scope_key text not null" in sql
    assert "scope_version integer not null" in sql
    assert "chatwoot_account_id bigint not null" in sql
    assert "external_conversation_id bigint not null" in sql
    assert "expected_team_id bigint not null" in sql
    assert "primary_reason_code text not null" in sql
    assert "references public.followup_delivery_attempts(id)" in sql
    assert "references public.delivery_attempts(id)" not in sql
    assert "create unique index human_handoff_requests_one_live_per_case_idx" in sql
    assert re.search(
        r"where\s+status\s+in\s*\(\s*'requested'\s*,\s*'projection_failed'\s*\)",
        sql,
    )

    assert "create table public.human_handoff_projection_effects" in sql
    assert "effect_kind text not null" in sql
    assert "'assignment', 'private_note'" in sql
    assert "'delivery_unknown'" in sql
    assert "unique (handoff_request_id, effect_kind)" in sql
    assert "lease_generation bigint not null default 0" in sql


def test_handoff_migration_keeps_projection_state_separate_from_durable_stop() -> None:
    sql = _sql()

    assert "status text not null default 'requested'" in sql
    assert "projection_failed" in sql
    assert "dead_letter" in sql
    assert "effect_status text not null default 'pending'" in sql
    assert "retryable_failed" in sql


def test_handoff_tables_deny_direct_api_role_writes() -> None:
    sql = _sql()

    for table in (
        "human_handoff_projection_policies",
        "human_handoff_requests",
        "human_handoff_request_evidence",
        "human_handoff_projection_effects",
    ):
        assert f"alter table public.{table} enable row level security" in sql
        assert f"revoke all on table public.{table}" in sql


def test_request_handoff_commits_stop_before_projection() -> None:
    sql = _sql()
    body = _function_body(sql, "request_human_handoff")

    assert "from public.contacts" in body
    assert "from public.recovery_cases" in body
    assert "from public.followup_sequences" in body
    assert "from public.scheduled_actions" in body
    assert "from public.followup_delivery_attempts" in body
    assert body.index("from public.contacts") < body.index(
        "from public.recovery_cases"
    )
    assert body.index("from public.recovery_cases") < body.index(
        "from public.followup_sequences"
    )
    assert body.index("from public.followup_sequences") < body.index(
        "from public.scheduled_actions"
    )
    assert body.index("from public.scheduled_actions") < body.index(
        "from public.followup_delivery_attempts"
    )

    assert "handoff_conversation_unavailable" in body
    assert "insert into public.human_handoff_requests" in body
    assert "insert into public.human_handoff_projection_effects" in body
    assert "failed_before_request" in body
    assert "delivery_unknown" in body
    assert "human_handoff_after_request_started" in body
    assert "status = 'paused'" in body
    assert "status = 'paused_human'" in body
    assert "automation_status = 'paused'" in body


def test_request_handoff_derives_projection_policy_and_is_service_role_only() -> None:
    sql = _sql()
    body = _function_body(sql, "request_human_handoff")

    assert "from public.human_handoff_projection_policies" in body
    assert "policy.active" in body
    assert "policy.expected_team_id" in body
    assert "p_expected_team_id" not in body
    assert "handoff_projection_policy_unavailable" in body
    assert "from public.pilot_recovery_case_bindings" in body
    assert "from public.pilot_scope_versions" in body
    assert "handoff_pilot_scope_mismatch" in body
    assert "handoff_channel_identity_scope_mismatch" in body
    assert "revoke execute on function public.request_human_handoff" in sql
    assert "grant execute on function public.request_human_handoff" in sql


def test_handoff_policy_snapshots_and_evidence_are_immutable() -> None:
    sql = _sql()

    assert "human_handoff_projection_policies_immutable" in sql
    assert "human_handoff_requests_protect_identity" in sql
    assert "human_handoff_request_evidence_append_only" in sql
    assert "human_handoff_projection_effects_protect_identity" in sql
    assert "human_handoff_policy_version_is_immutable" in sql
    assert "human_handoff_request_identity_is_immutable" in sql
    assert "human_handoff_evidence_is_append_only" in sql
    body = _function_body(sql, "request_human_handoff")
    assert "v_evidence.reason_code <> p_reason_code" in body
    assert "p_requested_by = 'agent' and p_source_attempt_id is null" in body
    assert "pg_advisory_xact_lock" in body
    assert "hashtextextended('human_handoff_command:' || p_command_key" in body
    assert "action.lease_expires_at > v_now" in body
    assert "reconciliation_deadline = 'infinity'::timestamptz" in body
    assert "v_existing.projection_policy_key <> p_projection_policy_key" in body
    assert "v_evidence.requested_by <> p_requested_by" in body
    assert "v_evidence.source_action_id is distinct from p_source_action_id" in body


def test_projection_effects_are_claimed_from_canonical_durable_context() -> None:
    body = _function_body(_sql(), "claim_human_handoff_projection_effects")

    assert "for update of effect skip locked" in body
    assert "request.status in ('requested', 'projection_failed', 'dead_letter')" in body
    assert "effect.lease_expires_at <= v_now" in body
    assert "update public.human_handoff_projection_effects" in body
    assert "lease_generation = effect.lease_generation + 1" in body
    assert "join public.human_handoff_requests" in body
    assert "join public.recovery_cases" in body
    assert "join public.conversations" in body
    assert "recovery_case.conversation_id = request.conversation_id" in body
    assert "chatwoot_account_id" in body
    assert "chatwoot_inbox_id" in body
    assert "chatwoot_conversation_id" in body
    assert "private_note_body" in body


def test_projection_effect_finalization_is_lease_fenced_and_reconciles_request() -> None:
    body = _function_body(_sql(), "finalize_human_handoff_projection_effect")

    assert "for update" in body
    assert "handoff_projection_lease_fence_rejected" in body
    assert "effect_status = p_outcome" in body
    assert "update public.human_handoff_requests" in body
    assert "status = 'projected'" in body
    assert "status = 'projection_failed'" in body
    assert "status = 'dead_letter'" in body
    assert "lease_owner = null" in body
    assert "v_effect.lease_expires_at <= v_now" in body
    assert "lease_expires_at = null" in body


def test_projection_rpc_acl_is_service_role_only() -> None:
    sql = _sql()

    for function_name in (
        "claim_human_handoff_projection_effects",
        "finalize_human_handoff_projection_effect",
        "get_human_handoff_projection_status",
    ):
        assert f"revoke execute on function public.{function_name}" in sql
        assert f"grant execute on function public.{function_name}" in sql
