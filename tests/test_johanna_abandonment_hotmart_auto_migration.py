"""Contract tests for Johanna's default-off Hotmart automatic single touch."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260825000400_johanna_waba_single_touch_policy.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_auto_v2_has_one_separate_budget_bound_to_exact_hotmart_event() -> None:
    sql = _sql()

    assert "hotmart_webhook_event_id uuid" in sql
    assert "johanna-abandonment-template-e2e-v2" in sql
    assert "function public.begin_johanna_abandonment_hotmart_auto" in sql
    assert "correlation.webhook_event_id = p_hotmart_webhook_event_id" in sql
    assert "correlation.event_type = 'purchase_out_of_shopping_cart'" in sql
    assert "correlation.outcome = 'resolved'" in sql
    assert "correlation.purchase_intent_id = p_purchase_intent_id" in sql
    assert "correlation.candidate_count = 1" in sql
    assert "not correlation.manual_handoff_required" in sql
    assert "intent.current_classification <> 'confirmed_abandonment'" in sql
    assert "external_product_id = '8104005'" in sql
    assert "offer_code = 'bxjge6zq'" in sql
    assert "activate_lancemos_pilot_scope_version" in sql
    assert "'inactive'" in sql
    assert "p_expected_generation is distinct from 1" in sql
    assert "runtime_generation = 1" in sql
    assert "unique index johanna_abandonment_one_shot_commands_target_phone_idx" in sql
    assert "where cmd.target_phone = p_allowed_external_user_id" in sql
    assert "'budget_consumed'::text" in sql
    assert "johanna_carrito_abandonado_01" in sql
    assert "johanna_compra_fallida_01" not in sql


def test_auto_rpc_is_service_role_only_and_manual_v1_is_preserved() -> None:
    sql = _sql()
    signature = (
        "public.begin_johanna_abandonment_hotmart_auto"
        "(text,uuid,uuid,text,bigint,bigint,text,integer,bigint)"
    )

    assert f"revoke all on function {signature} from public" in sql
    assert f"revoke all on function {signature} from anon" in sql
    assert f"revoke all on function {signature} from authenticated" in sql
    assert f"grant execute on function {signature} to service_role" in sql
    assert "begin_johanna_abandonment_one_shot" not in sql
