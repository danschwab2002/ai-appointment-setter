from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260826000100_johanna_dynamic_recipients.sql"
)


def test_dynamic_abandonment_rpc_derives_recipient_from_durable_intent() -> None:
    sql = MIGRATION.read_text().lower()

    signature = "begin_johanna_abandonment_hotmart_auto_v2(\n    p_command_key text,\n    p_hotmart_webhook_event_id uuid,\n    p_purchase_intent_id uuid,"
    assert signature in sql
    function_body = sql.split("as $function$", 1)[1].split("$function$", 1)[0]
    assert "p_allowed_external_user_id" not in function_body
    assert "select intent.normalized_phone" in function_body
    assert "where intent.id = p_purchase_intent_id" in function_body
    assert "v_target_phone !~ '^[1-9][0-9]{7,14}$'" in function_body
    assert "public.begin_johanna_abandonment_hotmart_auto(" in function_body
    assert "v_target_phone" in function_body


def test_dynamic_abandonment_rpc_is_service_role_only() -> None:
    sql = MIGRATION.read_text().lower()
    signature = (
        "public.begin_johanna_abandonment_hotmart_auto_v2("
        "text, uuid, uuid, bigint, bigint, text, integer, bigint)"
    )

    assert f"revoke all on function {signature} from public" in sql
    assert f"revoke all on function {signature} from anon" in sql
    assert f"revoke all on function {signature} from authenticated" in sql
    assert f"grant execute on function {signature} to service_role" in sql
