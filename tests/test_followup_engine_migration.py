"""Contract tests for the durable follow-up engine SQL migration."""

from __future__ import annotations

import re
from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "20260803000100_followup_engine_v1.sql"
)
PERMISSIONS_HOTFIX = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "20260804000100_followup_engine_permissions_hotfix.sql"
)
BASELINE = (
    Path(__file__).parents[1]
    / "supabase"
    / "baseline"
    / "20260803_public_schema.sql"
)


def _migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def _function_body(sql: str, function_name: str) -> str:
    match = re.search(
        rf"create or replace function public\.{function_name}\b(?P<body>.*?)\$function\$;",
        sql,
        flags=re.DOTALL,
    )
    assert match is not None, f"missing function {function_name}"
    return match.group("body")


def test_plan_cart_recovery_atomically_materializes_first_action() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "plan_cart_recovery")

    assert "insert into public.recovery_cases" in body
    assert "insert into public.followup_sequences" in body
    assert "insert into public.scheduled_actions" in body
    assert "insert into public.conversation_events" in body
    assert "first_contact_review" in body
    assert "cart_abandonment" in body
    assert "for update" in body


def test_repeated_abandonment_preserves_one_case_without_resurrecting_pause() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "plan_cart_recovery")

    assert "into v_sequence_id, v_sequence_status" in body
    assert "if v_case_status = 'paused' or v_sequence_status = 'paused'" in body
    assert "expected_case_version = v_case_version" in body
    assert "'cart_abandonment_aggregated'" in body


def test_claim_due_followup_actions_uses_recoverable_leases() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "claim_due_followup_actions")

    assert "for update skip locked" in body
    assert "coalesce(sa.next_attempt_at, sa.due_at) <= p_now" in body
    assert "sa.status in ('pending', 'deferred', 'retryable_failed')" in body
    assert "sa.lease_expires_at is null or sa.lease_expires_at <= p_now" in body
    assert "lease_generation = due.lease_generation + 1" in body
    assert "lease_owner = p_worker_id" in body
    assert "lease_expires_at = p_now + p_lease_duration" in body
    assert "delivery_unknown" not in body


def test_claim_does_not_reclaim_an_in_flight_external_request() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "claim_due_followup_actions")

    assert "not exists" in body
    assert "fda.action_id = sa.id" in body
    assert "fda.phase = 'request_started'" in body


def test_reevaluation_is_fenced_fail_closed_and_audited() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "reevaluate_followup_action")

    assert "v_action.lease_owner = p_worker_id" in body
    assert "v_action.lease_generation = p_lease_generation" in body
    assert "v_action.lease_expires_at > p_now" in body
    assert "v_contact.contact_permission in ('opted_out', 'blocked', 'restricted')" in body
    assert body.index("contact_blocked") < body.index("purchase_detected")
    assert "v_case.purchase_event_id is not null or v_case.status = 'won'" in body
    assert "c.human_takeover or c.status = 'paused_human'" in body
    assert "authorization_status in ('denied', 'restricted')" in body
    assert "v_policy.status <> 'published' or v_step is null" in body
    assert "v_step ->> 'mode' <> 'freeform'" in body
    assert "v_decision := 'defer'" in body
    assert "v_reason := 'business_window_closed'" in body
    assert "v_action.action_type = 'first_contact_review'" in body
    assert "ci.external_conversation_id is null" in body
    assert "v_reason := 'chatwoot_authority_unavailable'" in body
    assert "v_action.action_type = 'no_reply_review'" in body
    assert "v_reason := 'eligible_for_execution'" in body
    assert "ce.data ->> 'decision' <> 'execute'" in body
    assert "authorization_status = 'allowed'" in body
    assert "v_case.identity_resolution_status <> 'resolved'" in body
    assert "followup_action_reevaluated" in body
    assert "'worker_id', p_worker_id" in body
    assert "followup_action_reevaluated" in body[:body.index("current_action_lease_not_found")]
    assert "revoke execute on function public.reevaluate_followup_action" in sql
    assert "grant execute on function public.reevaluate_followup_action" in sql
    assert "create or replace function public.get_followup_chatwoot_context" in sql
    assert "external_conversation_id" in body
    assert "p_chatwoot_inbound_after_anchor" in body
    assert "chatwoot_anchor_not_found" in body
    assert "prospect_replied" in body
    assert "grant execute on function public.get_followup_chatwoot_context" in sql


def test_automatic_message_limit_terminalizes_the_sequence_without_orphaning_it() -> None:
    body = _function_body(_migration_sql(), "reevaluate_followup_action")

    assert "v_reason = 'automatic_message_limit_reached'" in body
    assert "completion_reason = v_reason" in body
    assert "set status = 'sequence_exhausted'" in body


def test_reconciliation_action_cannot_enter_the_outbound_execute_path() -> None:
    body = _function_body(_migration_sql(), "reevaluate_followup_action")

    reconcile_guard = "v_action.action_type = 'reconcile_delivery'"
    assert reconcile_guard in body
    assert "v_reason := 'reconciliation_requires_dedicated_worker'" in body
    assert body.index(reconcile_guard) < body.index(
        "v_action.action_type = 'first_contact_review'"
    )


def test_reservation_requires_matching_durable_execute_authorization() -> None:
    body = _function_body(_migration_sql(), "reserve_followup_delivery_attempt")

    assert "followup_execute_authorization_not_found" in body
    assert "ce.event_type = 'followup_action_reevaluated'" in body
    assert "ce.data ->> 'decision' = 'execute'" in body
    assert "ce.data ->> 'worker_id' = p_worker_id" in body
    assert "ce.data ->> 'lease_generation' = p_lease_generation::text" in body
    assert "ce.data ->> 'case_version' = p_expected_case_version::text" in body
    assert "ce.data ->> 'sequence_revision' = p_expected_sequence_revision::text" in body
    assert "v_action.action_type not in ('first_contact_review', 'no_reply_review')" in body


def test_execution_context_is_fenced_and_exposes_only_required_case_fields() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "get_followup_execution_context")

    assert "v_action.lease_owner = p_worker_id" in body
    assert "v_action.lease_generation = p_lease_generation" in body
    assert "v_action.lease_expires_at > p_now" in body
    assert "from public.recovery_cases rc" in body
    assert "join public.contacts c" in body
    assert "c.full_name" in body
    assert "c.email" in body
    assert "c.phone" in body
    assert "rc.product_name" in body
    assert "rc.offer_code" in body
    assert "grant execute on function public.get_followup_execution_context" in sql


def test_claim_sweeps_actions_expired_after_a_worker_crash() -> None:
    body = _function_body(_migration_sql(), "claim_due_followup_actions")
    assert "expired_after_worker_crash" in body
    assert body.index("from public.contacts c") < body.index(
        "from public.recovery_cases rc where rc.id = v_case_id for update"
    )
    assert "set status = 'expired'" in body
    assert "set status = 'completed', completion_reason = 'expired'" in body


def test_reevaluation_locks_case_sequence_action_in_global_order() -> None:
    body = _function_body(_migration_sql(), "reevaluate_followup_action")
    contact_lock = body.index("select c.* into strict v_contact")
    case_lock = body.index("select rc.* into strict v_case")
    sequence_lock = body.index("select fs.* into strict v_sequence")
    action_lock = body.index("where sa.id = p_action_id\n    for update")
    assert contact_lock < case_lock < sequence_lock < action_lock


def test_reevaluation_does_not_leave_active_aggregate_without_action() -> None:
    body = _function_body(_migration_sql(), "reevaluate_followup_action")
    assert "completion_reason = 'expired'" in body
    assert "status = 'expired', closed_at = p_now" in body
    assert "authoritative_state_changed" in body
    assert "status = 'paused', revision = revision + 1" in body


def test_reserve_delivery_attempt_requires_current_fencing_generation() -> None:
    sql = _migration_sql()
    assert "create table public.followup_delivery_attempts" in sql
    body = _function_body(sql, "reserve_followup_delivery_attempt")

    assert "v_action.lease_owner = p_worker_id" in body
    assert "v_action.lease_generation = p_lease_generation" in body
    assert "v_action.lease_expires_at > p_now" in body


def test_reserve_replay_rejects_incompatible_durable_parameters() -> None:
    body = _function_body(_migration_sql(), "reserve_followup_delivery_attempt")

    assert "v_existing.channel is distinct from p_channel" in body
    assert "v_existing.mode is distinct from p_mode" in body
    assert "v_existing.expected_case_version is distinct from p_expected_case_version" in body
    assert "delivery_attempt_already_reserved_differently" in body
    assert "for update" in body
    assert "insert into public.followup_delivery_attempts" in body
    assert "v_action.idempotency_key" in body
    assert "execution_attempt_count = execution_attempt_count + 1" in body


def test_reserve_rechecks_case_and_sequence_revisions_under_lock() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "reserve_followup_delivery_attempt")

    assert "v_case.version = p_expected_case_version" in body
    assert "v_action.expected_case_version = p_expected_case_version" in body
    assert "v_sequence.revision = p_expected_sequence_revision" in body
    assert "for update of sa, rc, fs" not in body

    case_lock = body.index("select rc.* into strict v_case")
    sequence_lock = body.index("select fs.* into strict v_sequence")
    action_lock = body.index("where sa.id = p_action_id\n    for update")
    assert case_lock < sequence_lock < action_lock


def test_finalize_records_acceptance_but_suppresses_successor_after_concurrent_change() -> None:
    sql = _migration_sql()
    reserve_body = _function_body(sql, "reserve_followup_delivery_attempt")
    finalize_body = _function_body(sql, "_finalize_followup_delivery_attempt")

    assert "expected_case_version bigint not null" in sql
    assert "expected_sequence_revision bigint not null" in sql
    assert "p_expected_case_version" in reserve_body
    assert "p_expected_sequence_revision" in reserve_body
    assert "v_attempt.expected_case_version" in finalize_body
    assert "v_attempt.expected_sequence_revision" in finalize_body
    assert "v_case.version = v_attempt.expected_case_version" in finalize_body
    assert "v_sequence.revision = v_attempt.expected_sequence_revision" in finalize_body
    assert "authoritative_state_changed_after_reservation" in finalize_body
    assert "v_from_status := v_action.status" in finalize_body
    assert "'from_status', v_from_status" in finalize_body
    assert "'from_status', 'execution_reserved'" not in finalize_body


def test_late_rejection_cannot_resurrect_a_terminal_action() -> None:
    body = _function_body(_migration_sql(), "_finalize_followup_delivery_attempt")

    assert "p_outcome = 'rejected' and not v_authoritative_current" in body
    assert "rejected_after_authoritative_state_change" in body
    assert body.index("p_outcome = 'rejected' and not v_authoritative_current") < body.index(
        "elsif p_outcome in ('failed_before_request', 'rejected')"
    )


def test_late_delivery_unknown_preserves_terminal_action_state() -> None:
    body = _function_body(_migration_sql(), "_finalize_followup_delivery_attempt")

    assert "p_outcome = 'delivery_unknown' and not v_authoritative_current" in body
    assert "delivery_unknown_after_authoritative_state_change" in body
    assert body.index("p_outcome = 'delivery_unknown' and not v_authoritative_current") < body.index(
        "elsif p_outcome = 'delivery_unknown' then"
    )


def test_finalize_locks_authoritative_state_before_action_and_attempt() -> None:
    body = _function_body(_migration_sql(), "_finalize_followup_delivery_attempt")

    case_lock = body.index("select rc.* into strict v_case")
    sequence_lock = body.index("select fs.* into strict v_sequence")
    action_lock = body.index("where sa.id = p_action_id\n    for update")
    attempt_lock = body.index("select fda.* into strict v_attempt")

    assert case_lock < sequence_lock < action_lock < attempt_lock


def test_finalize_delivery_attempt_preserves_external_uncertainty() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "_finalize_followup_delivery_attempt")

    assert "v_action.lease_owner = p_worker_id" in body
    assert "v_action.lease_generation = p_lease_generation" in body
    assert "fda.lease_generation = p_lease_generation" in body
    assert "p_outcome = 'accepted_by_chatwoot'" in body
    assert "automatic_messages_accepted = automatic_messages_accepted + 1" in body
    assert "p_outcome = 'delivery_unknown'" in body
    assert "status = 'delivery_unknown'" in body
    assert "p_outcome in ('failed_before_request', 'rejected')" in body
    assert "status = 'retryable_failed'" in body
    assert "next_attempt_at = p_next_attempt_at" in body


def test_accepted_delivery_materializes_only_the_next_review() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "_finalize_followup_delivery_attempt")

    assert "p_accepted_message_id is null" in body
    assert "insert into public.scheduled_actions" in body
    assert "'no_reply_review'" in body
    assert "p_now + v_next_delay" in body
    assert "'accepted_outbound_message'" in body
    assert "p_accepted_message_id" in body
    assert "p_now + v_next_delay < v_action.expires_at" in body
    assert "next_step_outside_expiration" in body
    assert "status = 'completed'" in body
    assert "'policy_exhausted'" in body
    assert "completion_reason = v_completion_reason" in body


def test_finalize_delivery_attempt_is_idempotent_after_commit() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "_finalize_followup_delivery_attempt")

    completed_guard = body.index("if v_attempt.phase = 'completed'")
    active_lease_guard = body.index("v_action.lease_owner = p_worker_id")
    assert completed_guard < active_lease_guard
    assert "v_attempt.outcome is distinct from p_outcome" in body
    assert "return next v_action" in body[completed_guard:active_lease_guard]


def test_published_policy_versions_are_immutable() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "protect_published_followup_policy")

    assert "old.status = 'published'" in body
    assert "raise exception" in body
    assert "before update or delete on public.followup_policy_versions" in sql


def test_contact_authorization_is_scoped_by_channel_and_purpose() -> None:
    sql = _migration_sql()

    assert "create table public.contact_authorizations" in sql
    assert "contact_id uuid not null" in sql
    assert "channel text not null" in sql
    assert "purpose text not null" in sql
    assert "authorization_status text not null" in sql
    assert "authorization_source text not null" in sql
    assert "valid_from timestamptz not null" in sql
    assert "valid_until timestamptz" in sql
    assert "authorization_status = any (array['allowed', 'denied', 'restricted', 'unknown'])" in sql


def test_request_start_is_persisted_before_the_external_post() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "mark_followup_request_started")

    assert "v_action.lease_owner = p_worker_id" in body
    assert "v_action.lease_generation = p_lease_generation" in body
    assert "fda.phase = 'reserved'" in body
    assert "phase = 'request_started'" in body
    assert "v_effective_now timestamptz := clock_timestamp()" in body
    assert "request_started_at = v_effective_now" in body


def test_request_start_revalidates_authoritative_state_under_lock() -> None:
    body = _function_body(_migration_sql(), "mark_followup_request_started")

    assert "v_case.version = v_attempt.expected_case_version" in body
    assert "v_sequence.revision = v_attempt.expected_sequence_revision" in body
    assert "v_case.status in ('grace_period', 'active')" in body
    assert "v_sequence.status = 'active'" in body
    assert "authoritative_state_changed_before_request" in body

    case_lock = body.index("select rc.* into strict v_case")
    sequence_lock = body.index("select fs.* into strict v_sequence")
    action_lock = body.index("where sa.id = p_action_id\n    for update")
    attempt_lock = body.index("select fda.* into strict v_attempt", action_lock)
    assert case_lock < sequence_lock < action_lock < attempt_lock


def test_delivery_outcome_matches_the_persisted_request_phase() -> None:
    sql = _migration_sql()

    assert "outcome = 'failed_before_request' and request_started_at is null" in sql
    assert "outcome in ('accepted_by_chatwoot', 'rejected', 'delivery_unknown')" in sql
    assert "request_started_at is not null" in sql
    assert "and finalized_next_attempt_at is not null" not in sql


def test_claim_reserve_and_request_start_fail_closed_after_expiration() -> None:
    sql = _migration_sql()
    claim = _function_body(sql, "claim_due_followup_actions")
    reserve = _function_body(sql, "reserve_followup_delivery_attempt")
    request_start = _function_body(sql, "mark_followup_request_started")

    assert "sa.expires_at > p_now" in claim
    assert "v_action.expires_at > p_now" in reserve
    assert "v_action.expires_at > v_effective_now" in request_start
    assert "v_action.lease_expires_at > v_effective_now" in request_start


def test_delivery_unknown_has_explicit_bounded_reconciliation_rpc() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "reconcile_followup_delivery_attempt")
    canonical = _function_body(sql, "record_and_finalize_followup_acceptance")

    assert "p_resolution = 'accepted_by_chatwoot'" in body
    assert "canonical_acceptance_required" in body
    assert "p_resolution not in ('not_applied', 'escalated')" in body
    assert "v_attempt.outcome = 'delivery_unknown'" in canonical
    assert "v_attempt.reconciliation_deadline" in canonical
    assert "v_reconciling := true" in canonical
    assert "reconciliation_resolution = 'accepted_by_chatwoot'" in canonical
    assert "reconciliation_resolution" in body
    assert "reconciled_at = p_now" in body
    assert "public.finalize_followup_delivery_attempt" in body
    assert "p_now < v_attempt.reconciliation_deadline" in body
    assert "reconciliation_window_not_expired" in body
    assert "p_now >= v_attempt.reconciliation_deadline" in body
    assert "reconciliation_window_expired" in body
    assert "followup_delivery_reconciled" in body
    assert "revoke execute on function public.reconcile_followup_delivery_attempt" in sql

    case_lock = body.index("select rc.* into strict v_case")
    sequence_lock = body.index("select fs.* into strict v_sequence")
    action_lock = body.index("where sa.id = p_action_id\n    for update")
    attempt_lock = body.index("select fda.* into strict v_attempt", action_lock)
    assert case_lock < sequence_lock < action_lock < attempt_lock


def test_delivery_unknown_ledger_requires_deadline_and_durable_reconciliation_inputs() -> None:
    sql = _migration_sql()
    finalize = _function_body(sql, "_finalize_followup_delivery_attempt")

    assert "outcome is distinct from 'delivery_unknown'\n        or reconciliation_deadline is not null" in sql
    assert "p_reconciliation_deadline <= p_now" in finalize
    assert "future_reconciliation_deadline_required" in finalize
    assert "accepted_message_id uuid" in sql
    assert "reconciliation_next_attempt_at timestamptz" in sql
    assert "and accepted_message_id is not null" in sql


def test_acceptance_idempotency_compares_the_durable_anchor() -> None:
    sql = _migration_sql()
    finalize = _function_body(sql, "_finalize_followup_delivery_attempt")
    reconcile = _function_body(sql, "reconcile_followup_delivery_attempt")

    expected = "accepted_message_id is distinct from p_accepted_message_id"
    assert expected in finalize
    assert expected in reconcile


def test_not_applied_can_only_restore_the_same_action_as_retryable() -> None:
    body = _function_body(_migration_sql(), "reconcile_followup_delivery_attempt")

    assert "p_next_attempt_at >= v_action.expires_at" in body
    assert "v_action.execution_attempt_count > v_action.max_execution_retries" in body
    assert "not_applied_retry_not_permitted" in body
    assert "reconciliation_next_attempt_at" in body


def test_scheduled_action_identity_is_immutable() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "protect_scheduled_action_identity")

    for column in (
        "recovery_case_id",
        "followup_sequence_id",
        "policy_key",
        "policy_version",
        "step_key",
        "action_type",
        "idempotency_key",
    ):
        assert f"new.{column} is distinct from old.{column}" in body
    assert "scheduled_action_identity_is_immutable" in body
    assert "create trigger scheduled_actions_protect_identity" in sql


def test_migration_aborts_instead_of_guessing_legacy_scheduler_backfills() -> None:
    sql = _migration_sql()

    assert "from public.recovery_cases" in sql
    assert "from public.followup_sequences" in sql
    assert "from public.scheduled_actions" in sql
    assert "followup_engine_requires_empty_legacy_scheduler_tables" in sql


def test_effect_ledger_and_action_relationships_preserve_integrity() -> None:
    sql = _migration_sql()

    assert "references public.scheduled_actions(id) on delete restrict" in sql
    assert "unique (id, recovery_case_id, policy_key, policy_version)" in sql
    assert "foreign key (followup_sequence_id, recovery_case_id, policy_key, policy_version)" in sql
    assert "alter column policy_key set not null" in sql
    assert "alter column policy_version set not null" in sql


def test_every_claim_is_append_only_audited() -> None:
    body = _function_body(_migration_sql(), "claim_due_followup_actions")

    assert "'followup_action_claimed'" in body
    assert "'worker_id', p_worker_id" in body
    assert "'lease_generation', claimed.lease_generation" in body
    assert "'claimed_at', p_now" in body
    assert "join audited on audited.related_action_id = claimed.id" in body


def test_only_one_active_sequence_and_due_before_expiry_are_allowed() -> None:
    sql = _migration_sql()
    baseline = BASELINE.read_text(encoding="utf-8").lower()

    assert "followup_sequences_one_active_per_case_idx" in baseline
    assert "where status = 'active'" in baseline
    assert "drop index public.followup_sequences_one_active_per_case_idx" not in sql
    assert "scheduled_actions_due_before_expiry_check" in sql
    assert "expires_at is null or due_at <= expires_at" in sql
    assert "alter column expires_at set not null" in sql


def test_retry_beyond_expiration_becomes_expired() -> None:
    sql = _migration_sql()
    body = _function_body(sql, "_finalize_followup_delivery_attempt")

    assert "p_next_attempt_at < v_action.expires_at" in body
    assert "status = 'expired'" in body
    assert "retry_beyond_expiration" in body


def test_accepted_finalization_surface_exposes_only_the_canonical_rpc() -> None:
    sql = _migration_sql()
    wrapper = _function_body(sql, "finalize_followup_delivery_attempt")

    assert "p_outcome = 'accepted_by_chatwoot'" in wrapper
    assert "canonical_acceptance_required" in wrapper
    assert "revoke execute on function public._finalize_followup_delivery_attempt" in sql
    assert "grant execute on function public._finalize_followup_delivery_attempt" not in sql
    assert "grant execute on function public.record_and_finalize_followup_acceptance" in sql


def test_followup_engine_objects_are_not_publicly_executable() -> None:
    sql = _migration_sql()

    assert "revoke all on public.followup_policy_versions from public" in sql
    assert "revoke all on public.contact_authorizations from public" in sql
    assert "revoke execute on function public.plan_cart_recovery" in sql
    assert "grant execute on function public.claim_due_followup_actions" in sql
    assert "to service_role" in sql


def test_internal_finalizer_is_explicitly_revoked_from_supabase_api_roles() -> None:
    assert PERMISSIONS_HOTFIX.exists(), "missing permissions hotfix migration"
    sql = re.sub(
        r"\s+", " ", PERMISSIONS_HOTFIX.read_text(encoding="utf-8").lower()
    )
    signature = (
        "public._finalize_followup_delivery_attempt("
        "uuid, uuid, text, bigint, text, text, uuid, text, timestamptz, "
        "timestamptz, timestamptz)"
    )

    for role in ("public", "anon", "authenticated", "service_role"):
        assert f"revoke execute on function {signature} from {role}" in sql
