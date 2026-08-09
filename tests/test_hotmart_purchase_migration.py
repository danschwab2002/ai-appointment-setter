"""Contract tests for the Hotmart approved-purchase migration."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260808000100_hotmart_purchase_approved.sql"
)
ORDERING_GUARD_MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260808000200_hotmart_purchase_ordering_guard.sql"
)
ORDERING_GUARD_PRIVILEGES_MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260808000300_hotmart_purchase_ordering_guard_privileges.sql"
)
SEMANTIC_CONFLICT_MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260808000500_hotmart_purchase_semantic_conflicts.sql"
)


def test_purchase_rpc_atomically_closes_exact_recovery_case() -> None:
    sql = MIGRATION.read_text().lower()

    assert "function public.apply_hotmart_purchase_approved" in sql
    assert "v_event.event_type <> 'purchase_approved'" in sql
    assert "for update" in sql
    assert "set status = 'won'" in sql
    assert "purchase_event_id = p_webhook_event_id" in sql
    assert "completion_reason = 'purchase_detected'" in sql
    assert "terminal_reason = 'purchase_detected'" in sql
    assert "status in ('pending', 'deferred', 'retryable_failed')" in sql
    assert "phase = 'request_started'" in sql
    assert "status = 'delivery_unknown'" in sql
    assert "is not distinct from p_offer_code" in sql
    assert "policy.expires_after" in sql
    assert "lock table public.contacts" in sql
    assert "purchase_rpc_payload_mismatch" in sql
    assert "v_payload_email is distinct from v_buyer_email" in sql
    assert "v_payload_phone is distinct from v_buyer_phone" in sql
    assert "webhook_events_hotmart_purchase_transaction_unique_idx" in sql


def test_purchase_rpc_never_selects_first_ambiguous_candidate() -> None:
    sql = MIGRATION.read_text().lower()

    assert "limit 1" not in sql
    assert "purchase_correlation_contact_ambiguous" in sql
    assert "purchase_correlation_case_ambiguous" in sql
    assert "terminal_reason = 'purchase_correlation_ambiguous'" in sql
    assert "set status = 'paused'" in sql


def test_purchase_rpc_distinguishes_not_found_and_idempotent_replay() -> None:
    sql = MIGRATION.read_text().lower()

    assert "purchase_correlation_contact_not_found" in sql
    assert "purchase_correlation_case_not_found" in sql
    assert "'not_found'::text" in sql
    assert "'already_applied'::text" in sql
    assert "processed_purchase_without_recovery_case" in sql


def test_late_abandonment_is_cancelled_by_already_known_purchase() -> None:
    sql = ORDERING_GUARD_MIGRATION.read_text().lower()

    assert "function public.stop_cart_recovery_for_known_purchase" in sql
    assert "trigger scheduled_actions_stop_for_known_purchase" in sql
    assert "after insert on public.scheduled_actions" in sql
    assert "for update of we skip locked" in sql
    assert "count(distinct identity_match.contact_id) = 1" in sql
    assert "count(*) = 1" in sql
    assert "purchase_correlation_case_not_found" in sql
    assert "'known_purchase_before_recovery_plan'" in sql
    assert "purchase_event_id = v_purchase_event_id" in sql


def test_ordering_guard_trigger_function_has_no_direct_api_execute_surface() -> None:
    sql = ORDERING_GUARD_PRIVILEGES_MIGRATION.read_text().lower()

    assert "stop_cart_recovery_for_known_purchase()" in sql
    assert "revoke all" in sql
    for role in ("public", "anon", "authenticated", "service_role"):
        assert role in sql
    assert "grant execute" not in sql


def test_purchase_admission_distinguishes_exact_replay_from_semantic_conflict() -> None:
    sql = SEMANTIC_CONFLICT_MIGRATION.read_text().lower()

    assert "create table public.hotmart_purchase_semantic_conflicts" in sql
    assert "function public.admit_hotmart_purchase_approved" in sql
    assert "function public.hotmart_purchase_semantic_tuple" in sql
    assert "lock table public.webhook_events" in sql
    assert "outcome := 'duplicate'" in sql
    assert "outcome := 'semantic_conflict'" in sql
    assert "incoming_payload" in sql
    assert "resolved_at" in sql


def test_unresolved_purchase_semantic_conflict_blocks_request_start() -> None:
    sql = SEMANTIC_CONFLICT_MIGRATION.read_text().lower()

    assert sql.count("pg_advisory_xact_lock(7275726368617365)") >= 2
    assert "function public.guard_purchase_semantic_conflict_request_start" in sql
    assert "new.phase <> 'request_started'" in sql
    assert "unresolved_purchase_semantic_conflict" in sql
    assert "trigger followup_attempts_guard_purchase_semantic_conflict" in sql
    assert "before insert or update of phase" in sql


def test_purchase_admission_privileges_expose_only_the_entrypoint() -> None:
    sql = SEMANTIC_CONFLICT_MIGRATION.read_text().lower()

    for role in ("public", "anon", "authenticated"):
        assert role in sql
    assert "grant execute on function public.admit_hotmart_purchase_approved" in sql
    assert "to service_role" in sql
    assert "hotmart_purchase_semantic_tuple(jsonb) from service_role" in sql
    assert "guard_purchase_semantic_conflict_request_start() from service_role" in sql
