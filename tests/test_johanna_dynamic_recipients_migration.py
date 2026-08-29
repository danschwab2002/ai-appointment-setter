from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260826000100_johanna_dynamic_recipients.sql"
)
OPERATOR_RESOLUTION_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260829000100_johanna_operator_resolution_one_shot.sql"
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


def test_operator_resolution_authorizes_only_the_exact_conflict_candidate() -> None:
    sql = OPERATOR_RESOLUTION_MIGRATION.read_text().lower()

    assert "operator_correlation_resolutions" in sql
    assert "resolution.resolution_outcome = ''linked_candidate''" in sql
    assert "resolution.effective_purchase_intent_id = p_purchase_intent_id" in sql
    assert "resolution.deterministic_outcome = ''conflict''" in sql
    assert "correlation.reason_code = ''email_phone_conflict''" in sql
    assert "correlation.candidate_count = 1" in sql
    assert "correlation.manual_handoff_required" in sql
    assert "intent.current_classification = ''identity_conflict''" in sql
    assert "not intent.whatsapp_contact_authorized" in sql
    assert "contact_opt_out_events" in sql
