from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/20260825000500_johanna_mvp_full_activation.sql"
)
ANY_REASON_MIGRATION = Path(
    "supabase/migrations/20260827000100_hotmart_canceled_any_reason.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_payment_failure_admission_is_exact_and_durable() -> None:
    sql = _sql()

    assert "create table public.johanna_payment_failure_cases" in sql
    assert "create or replace function public.admit_johanna_payment_failure(" in sql
    assert "security definer" in sql
    assert "set search_path = pg_catalog, public, pg_temp" in sql
    assert "PURCHASE_CANCELED" in sql
    assert "CANCELLED" in sql
    assert "NO_FUNDS" in sql
    assert "8104005" in sql
    assert "bxjge6zq" in sql
    assert "intent.tenant_ref = 'lancemos'" in sql
    assert "intent.funnel_ref = 'psicologajohanna'" in sql
    assert "intent.landing_ref = 'ads-a'" in sql
    assert "lower(intent.product_ref) = 'f106691755g'" in sql
    assert "pending_human_review" in sql
    assert "payment_failure_supported" in sql
    assert "semantic_conflict" in sql
    assert (
        "revoke all on table public.johanna_payment_failure_cases from service_role"
        in sql
    )


def test_payment_failure_rpc_and_tables_are_service_role_only() -> None:
    sql = _sql()

    assert "revoke all on table public.johanna_payment_failure_cases from public" in sql
    assert "revoke all on table public.johanna_payment_failure_cases from anon" in sql
    assert "revoke all on table public.johanna_payment_failure_cases from authenticated" in sql
    assert "revoke all on function public.admit_johanna_payment_failure(text, jsonb, text, text) from public" in sql
    assert "grant execute on function public.admit_johanna_payment_failure(text, jsonb, text, text) to service_role" in sql


def test_handoff_claim_returns_the_canonical_external_user() -> None:
    sql = _sql()

    assert "drop function public.claim_human_handoff_projection_effects(text, integer, integer, timestamptz)" in sql
    assert "external_user_id text" in sql
    assert "identity.external_user_id" in sql
    assert "grant execute on function public.claim_human_handoff_projection_effects(text, integer, integer, timestamptz) to service_role" in sql


def test_payment_failure_begin_serializes_with_durable_opt_out() -> None:
    sql = _sql()

    user_lock = "'chatwoot-opt-out-user'"
    budget_lock = "'johanna-recovery-budget:'"
    stop_read = "from public.contact_opt_out_events stop"
    command_insert = "insert into public.johanna_abandonment_one_shot_commands"

    assert user_lock in sql
    assert "from public.channel_identities identity" in sql
    assert "for update of identity" in sql
    assert "for update of point, owner" in sql
    assert sql.index(user_lock) < sql.index(budget_lock)
    assert sql.index(user_lock) < sql.index(stop_read) < sql.index(command_insert)


def test_payment_failure_any_reason_migration_preserves_all_other_gates() -> None:
    sql = ANY_REASON_MIGRATION.read_text(encoding="utf-8")

    assert "p_payload #>> '{event}' is distinct from 'PURCHASE_CANCELED'" in sql
    assert "p_payload #>> '{data,purchase,status}' is distinct from 'CANCELED'" in sql
    assert "failure_case.purchase_status <> 'CANCELED'" in sql
    assert "failure_case.refusal_reason <>" not in sql
    assert "alter column refusal_reason drop not null" in sql
    assert "intent.tenant_ref = 'lancemos'" in sql
    assert "intent.funnel_ref = 'psicologajohanna'" in sql
    assert "intent.landing_ref = 'ads-a'" in sql
    assert "lower(intent.product_ref) = 'f106691755g'" in sql
    assert "v_product_ref is distinct from '8104005'" in sql
    assert "v_offer_ref is distinct from 'bxjge6zq'" in sql
    assert "from public.contact_opt_out_events stop" in sql
    assert "'johanna-recovery-budget:'" in sql
