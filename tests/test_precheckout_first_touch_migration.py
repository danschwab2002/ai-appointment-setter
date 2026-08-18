from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/20260818000100_precheckout_test_first_touch.sql"
)


def test_first_touch_migration_is_one_shot_and_test_only() -> None:
    sql = MIGRATION.read_text()

    assert "precheckout_test_first_touch_commands" in sql
    assert "max_messages = 1 and followups_allowed = 0" in sql
    assert "test_only and not generalizable" in sql
    assert "purchase_intent_id uuid not null unique" in sql
    assert "libre_ansiedad_test_first_touch_v1" in sql
    assert "libre-ansiedad-precheckout-first-touch-v1" in sql
    assert "delivery_unknown" in sql
    assert "current_classification" not in sql
    assert "scheduled_actions" not in sql
    assert "followup_sequences" not in sql


def test_first_touch_rpcs_are_hardened_and_service_role_only() -> None:
    sql = MIGRATION.read_text()

    assert sql.count("security definer") == 2
    assert sql.count("set search_path = pg_catalog, public, pg_temp") == 2
    assert "grant execute on function public.begin_precheckout_test_first_touch" in sql
    assert "grant execute on function public.finish_precheckout_test_first_touch" in sql
    assert "revoke all on table public.precheckout_test_first_touch_commands from service_role" in sql
    assert "grant select" not in sql


def test_first_touch_begin_revalidates_identity_and_stops() -> None:
    sql = MIGRATION.read_text()

    assert "v_intent.normalized_phone is distinct from p_allowed_external_user_id" in sql
    assert "ci.external_user_id = p_allowed_external_user_id" in sql
    assert "ci.account_id = 'chatwoot:' || p_chatwoot_account_id::text" in sql
    assert "ci.metadata ->> 'inbox_id' = p_chatwoot_inbox_id::text" in sql
    assert "'chatwoot_conversation_id', v_external_conversation_id" in sql
    assert "ci.identity_status = 'active'" in sql
    assert "c.contact_permission not in ('opted_out', 'blocked', 'restricted')" in sql
    assert "c.lifecycle_status <> 'do_not_contact'" in sql
    assert "not conv.human_takeover" in sql
    assert "rollout_scope text not null" in sql
    assert "precheckout_first_touch_rollout_consumed" in sql
    assert "precheckout_first_touch_contact_changed" in sql
    assert "for update of conv" in sql
    assert "set status = p_outcome" in sql
    assert "conv.status not in ('paused_human', 'closed', 'blocked')" in sql
    assert "v_intent.lifecycle_state <> 'waiting_for_purchase'" in sql
