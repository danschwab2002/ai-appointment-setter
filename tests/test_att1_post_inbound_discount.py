from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / (
    "20260905000100_commercial_ally_post_inbound_discount.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_post_inbound_discount_is_one_shot_and_inbound_anchored() -> None:
    sql = _sql()

    assert "recovery_case_id uuid primary key" in sql
    assert "action_type, status, due_at, expires_at, next_attempt_at" in sql
    assert "'inbound_reply_offer', 'deferred'" in sql
    assert "'infinity'::timestamptz, 'infinity'::timestamptz" in sql
    assert "'payment_failure_discount_offer'" in sql
    assert "'inbound_message'" in sql
    assert "p_chatwoot_message_id" in sql
    assert "p_inbound_received_at <= v_initial_message.occurred_at" in sql


def test_post_inbound_discount_requires_exact_safe_policy() -> None:
    sql = _sql()

    for marker in (
        "policy.discount_value = 10",
        "policy.offer_expiration_mode = 'indefinite'",
        "policy.presentation_stage = 'later_step'",
        "policy.requires_inbound_reply_after_initial_template",
        "policy.coupon_delivery_mode = 'meta_template_variable'",
        "not policy.urgency_copy_allowed",
        "policy.channel_provider = 'waba'",
        "policy.delivery_mode = 'approved_template'",
    ):
        assert marker in sql


def test_post_inbound_discount_boundary_is_default_off_and_append_only() -> None:
    sql = _sql()
    env = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "CHATWOOT_POST_INBOUND_DISCOUNT_PLANNING_ENABLED=false" in env
    assert "set search_path = pg_catalog, public, pg_temp" in sql
    assert "count(*) into v_initial_action_count" in sql
    assert "v_initial_action_count <> 1" in sql
    assert "action.conversation_id = v_conversation.id" in sql
    assert "count(*) into v_initial_message_count" in sql
    assert "v_initial_message_count <> 1" in sql
    assert "canonical_channel_identity_id uuid not null" in sql
    assert "canonical_external_user_id text not null" in sql
    assert sql.index("select binding.* into v_existing") < sql.index(
        "select binding.* into v_runtime"
    )
    assert "commercial_ally_post_inbound_discount_binding_immutable" in sql
    assert "enable row level security" in sql
    assert "revoke all on table public.commercial_ally_post_inbound_discount_bindings" in sql
    assert "to_regrole('anon') is not null" in sql
    assert "to_regrole('authenticated') is not null" in sql
    assert "to_regrole('service_role') is not null" in sql
    assert "grant execute on function public.plan_commercial_ally_post_inbound_discount" in sql
    assert "effect_authorized', false" in sql
