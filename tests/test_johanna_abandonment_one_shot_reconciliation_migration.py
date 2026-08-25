"""Contract tests for reconciling an observed Johanna one-shot delivery."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260825000300_reconcile_johanna_abandonment_one_shot.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_reconciliation_promotes_only_delivery_unknown_without_resend() -> None:
    sql = _sql()

    assert "function public.reconcile_johanna_abandonment_one_shot" in sql
    assert "where cmd.command_key = p_command_key" in sql
    assert "command.status <> 'delivery_unknown'" in sql
    assert "status = 'accepted_by_chatwoot'" in sql
    assert "chatwoot_conversation_id = p_chatwoot_conversation_id" in sql
    assert "chatwoot_message_id = p_chatwoot_message_id" in sql
    assert "failure_code = null" in sql
    assert "insert into" not in sql


def test_reconciliation_updates_through_an_explicit_trigger_exception() -> None:
    sql = _sql()

    assert "current_setting('app.johanna_one_shot_reconciliation', true)" in sql
    assert "set_config('app.johanna_one_shot_reconciliation', 'on', true)" in sql
    assert "old.status = 'delivery_unknown'" in sql
    assert "new.status = 'accepted_by_chatwoot'" in sql


def test_reconciliation_rpc_is_service_role_only() -> None:
    sql = _sql()
    signature = "public.reconcile_johanna_abandonment_one_shot(text,bigint,bigint)"

    assert "security definer\nset search_path = pg_catalog, public, pg_temp" in sql
    assert f"revoke all on function {signature} from public" in sql
    assert f"revoke all on function {signature} from anon" in sql
    assert f"revoke all on function {signature} from authenticated" in sql
    assert f"grant execute on function {signature} to service_role" in sql
