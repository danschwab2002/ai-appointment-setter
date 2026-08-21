from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260821000100_hotmart_abandonment_timer.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_timer_schema_physically_snapshots_variable_policy_delay() -> None:
    sql = _sql()

    assert "create table public.hotmart_abandonment_timer_policy_bindings" in sql
    assert "create table public.hotmart_abandonment_timer_policy_binding_events" in sql
    assert "create table public.hotmart_abandonment_reevaluations" in sql
    assert "delay_seconds_snapshot integer not null" in sql
    assert "delay_seconds_snapshot between 60 and 2592000" in sql
    assert "due_at = observed_at + make_interval(secs => delay_seconds_snapshot)" in sql
    assert "foreign key (" in sql
    assert "policy_binding_generation" in sql
    assert "followup_policy_versions(policy_key, version)" in sql
    assert "where status = 'scheduled'" in sql
    assert "source_webhook_event_id uuid not null unique" in sql
    assert "hotmart_abandonment_reevaluation_events_append_only" in sql
    assert "hotmart_abandonment_reevaluation_event_append_only" in sql


def test_correlated_wrappers_schedule_cart_and_cancel_on_purchase() -> None:
    sql = _sql()

    assert (
        "create or replace function public.schedule_hotmart_abandonment_reevaluation"
        in sql
    )
    assert "v_correlation.outcome <> 'resolved'" in sql
    assert "v_correlation.event_type <> 'purchase_out_of_shopping_cart'" in sql
    assert "v_intent.current_classification <> 'confirmed_abandonment'" in sql
    assert "when binding.product_ref is not null" in sql
    assert "end desc" in sql
    assert "into v_binding, v_specificity" not in sql
    assert "binding.enabled" in sql
    assert "policy.grace_period" in sql
    assert "hotmart-abandonment:" in sql
    assert (
        "perform public.schedule_hotmart_abandonment_reevaluation(v_event_id)" in sql
    )
    assert (
        "create or replace function public.cancel_hotmart_abandonment_reevaluations_for_purchase"
        in sql
    )
    assert "outcome = 'cancelled_purchased'" in sql
    assert (
        "perform public.cancel_hotmart_abandonment_reevaluations_for_purchase(" in sql
    )
    assert "insert into public.scheduled_actions" not in sql
    assert "insert into public.followup_delivery_attempts" not in sql


def test_due_reevaluation_is_internal_idempotent_and_service_role_only() -> None:
    sql = _sql()

    assert (
        "create or replace function public.list_due_hotmart_abandonment_reevaluations"
        in sql
    )
    assert "p_batch_size between 1 and 100" in sql
    assert "reevaluation.due_at <= p_now" in sql
    assert (
        "create or replace function public.reevaluate_hotmart_abandonment_timer" in sql
    )
    assert "from public.purchase_intents intent" in sql
    assert "for update;" in sql
    assert "blocked_not_authorized" in sql
    assert "blocked_contact_binding_missing" in sql
    assert "cancelled_intent_changed" in sql
    assert "old.outcome is distinct from 'cancelled_purchased'" in sql
    assert "grant execute on function public.list_due_hotmart_abandonment_reevaluations" in sql
    assert "grant execute on function public.reevaluate_hotmart_abandonment_timer" in sql
    assert "grant select on table public.hotmart_abandonment_reevaluations" not in sql
    assert "chatwoot" not in sql
    assert "insert into public.messages" not in sql
    assert "insert into public.followup_delivery_attempts" not in sql
