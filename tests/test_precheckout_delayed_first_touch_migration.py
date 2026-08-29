from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / (
    "20260829000200_precheckout_delayed_first_touch_timer.sql"
)


def migration_sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_generalizes_existing_timer_without_creating_parallel_effect_tables() -> None:
    sql = migration_sql()

    assert "alter table public.hotmart_abandonment_reevaluations" in sql
    assert "source_kind" in sql
    assert "source_submission_id" in sql
    assert "precheckout_intent" in sql
    assert "create table" not in sql
    assert "scheduled_actions" not in sql
    assert "delivery_attempts" not in sql


def test_precheckout_timer_is_exactly_one_hour_and_reuses_policy_snapshot() -> None:
    sql = migration_sql()

    assert "schedule_precheckout_first_touch_reevaluation" in sql
    assert "v_delay_seconds <> 3600" in sql
    assert "due_at" in sql
    assert "make_interval(secs => v_delay_seconds)" in sql
    assert "precheckout-first-touch:" in sql
    assert "on conflict (source_submission_id)" in sql


def test_scheduler_revalidates_v1_1_consent_identity_and_scope() -> None:
    sql = migration_sql()

    for marker in (
        "contract_version is distinct from '1.1.0'",
        "{consent,marketing_optin}",
        "{consent,whatsapp_contact}",
        "johanna-precheckout-whatsapp-disclosure-v1",
        "identity_conflict",
        "waiting_for_purchase",
        "f106691755g",
        "bxjge6zq",
    ):
        assert marker in sql


def test_admission_calls_scheduler_in_same_transaction_for_v1_1_only() -> None:
    sql = migration_sql()

    assert "pg_get_functiondef" in sql
    assert "v_contract_version = '1.1.0'" in sql
    assert "perform public.schedule_precheckout_first_touch_reevaluation(" in sql
    assert "observed_precheckout_timer_hook_marker_mismatch" in sql


def test_precheckout_timers_remain_inert_until_effect_task() -> None:
    sql = migration_sql()

    assert "reevaluation.source_kind = 'hotmart_event'" in sql
    assert "precheckout_first_touch_not_active" in sql
    assert "johanna_abandonment_one_shot_commands" not in sql
    assert "chatwoot" not in sql


def test_precheckout_source_has_a_separate_default_off_gate() -> None:
    sql = migration_sql()

    assert "precheckout_first_touch_enabled boolean not null default false" in sql
    assert "not v_binding.precheckout_first_touch_enabled" in sql


def test_new_scheduler_is_service_role_only() -> None:
    sql = migration_sql()

    signature = (
        "public.schedule_precheckout_first_touch_reevaluation(uuid, uuid)"
    )
    assert f"revoke all on function {signature} from public" in sql
    assert f"grant execute on function {signature}" in sql
    assert "to service_role" in sql
