from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / (
    "20260829000300_precheckout_delayed_one_shot_reservation.sql"
)


def sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_reuses_existing_command_ledger_and_recipient_budget() -> None:
    migration = sql()

    assert "alter table public.johanna_abandonment_one_shot_commands" in migration
    assert "source_reevaluation_id" in migration
    assert "johanna_abandonment_one_shot_commands_target_phone_idx" in migration
    assert "insert into public.johanna_abandonment_one_shot_commands" in migration
    assert "create table" not in migration


def test_precheckout_route_has_exact_template_and_source_binding() -> None:
    migration = sql()

    for marker in (
        "johanna-precheckout-delayed-first-touch-v1",
        "johanna_interes_precheckout_01",
        "johanna-precheckout-delayed-first-touch-v1",
        "source_reevaluation_id is not null",
        "hotmart_webhook_event_id is null",
        "payment_failure_case_id is null",
        "max_messages",
        "followups_allowed",
    ):
        assert marker in migration


def test_reevaluation_rechecks_all_authoritative_stops() -> None:
    migration = sql()

    for marker in (
        "cancelled_purchased",
        "superseded_by_provider_event",
        "blocked_not_authorized",
        "blocked_contact",
        "blocked_identity",
        "blocked_handoff",
        "contact_opt_out_events",
        "hotmart_purchase_intent_correlation_candidates",
        "johanna_payment_failure_cases",
        "human_takeover",
        "do_not_contact",
    ):
        assert marker in migration


def test_provider_and_precheckout_share_same_global_lock() -> None:
    migration = sql()

    assert "hashtextextended('johanna-abandonment-template-e2e-v2', 0)" in migration
    assert "'johanna-recovery-budget:' || v_intent.normalized_phone" in migration
    assert "where cmd.target_phone = v_intent.normalized_phone" in migration
    assert "budget_consumed" in migration


def test_reevaluation_reserves_command_and_completes_timer_atomically() -> None:
    migration = sql()

    assert "command_reserved" in migration
    assert "reserved" in migration
    assert "precheckout-delayed:" in migration
    assert "update public.hotmart_abandonment_reevaluations" in migration
    assert "_reevaluate_precheckout_delayed_first_touch" in migration
    assert "reevaluate_hotmart_abandonment_timer" in migration


def test_scope_must_be_published_but_is_not_created_by_this_task() -> None:
    migration = sql()

    assert "johanna-precheckout-delayed-first-touch" in migration
    assert "scope.status = 'published'" in migration
    assert "scope.source = 'landing'" in migration
    assert "check (source in ('hotmart', 'landing'))" in migration
    assert "v_outcome := 'blocked_contact_binding_missing'" in migration
    assert "insert into public.pilot_scope_versions" not in migration


def test_task_has_no_sender_or_external_effect() -> None:
    migration = sql()

    assert "chatwootmessage" not in migration
    assert "http" not in migration
    assert "send_first_message" not in migration
    assert "followup_delivery_attempts" not in migration
    assert "scheduled_actions" not in migration


def test_internal_helper_is_not_api_executable() -> None:
    migration = sql()
    signature = "public._reevaluate_precheckout_delayed_first_touch(uuid,timestamptz)"

    assert f"revoke all on function {signature} from public" in migration
    assert f"revoke all on function {signature} from service_role" in migration
