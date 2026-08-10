"""Contract tests for authoritative cart-abandonment admission."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260810000200_hotmart_cart_abandonment_authoritative.sql"
)


def _sql() -> str:
    assert MIGRATION.exists(), "missing authoritative cart-abandonment migration"
    return MIGRATION.read_text().lower()


def test_admission_distinguishes_exact_replay_from_semantic_conflict() -> None:
    sql = _sql()

    assert "create table public.hotmart_cart_abandonment_semantic_conflicts" in sql
    assert "function public.admit_hotmart_cart_abandonment" in sql
    assert "function public.hotmart_cart_abandonment_payload_is_processable" in sql
    assert "function public.hotmart_cart_abandonment_semantic_tuple" in sql
    assert "outcome := 'duplicate'" in sql
    assert "outcome := 'semantic_conflict'" in sql
    assert "incoming_payload" in sql


def test_admission_requires_a_positive_integer_product_id() -> None:
    sql = _sql()

    assert "jsonb_typeof(p_payload #> '{data,product,id}') = 'number'" in sql
    assert "p_payload #>> '{data,product,id}' ~ '^[0-9]+$'" in sql
    assert "(p_payload #>> '{data,product,id}')::numeric > 0" in sql


def test_unresolved_semantic_conflict_blocks_request_start() -> None:
    sql = _sql()

    assert sql.count("pg_advisory_xact_lock(7275726368617365)") >= 2
    assert "unresolved_cart_abandonment_semantic_conflict" in sql
    assert "before insert or update of phase" in sql


def test_plan_binding_trigger_checks_canonical_event_fields() -> None:
    sql = _sql()

    assert "trigger recovery_case_events_validate_hotmart_abandonment" in sql
    assert "before insert or update of recovery_case_id" in sql
    assert "new.event_role <> 'cart_abandonment'" in sql
    assert "cart_abandonment_product_mismatch" in sql
    assert "cart_abandonment_product_name_mismatch" in sql
    assert "cart_abandonment_offer_mismatch" in sql
    assert "cart_abandonment_contact_mismatch" in sql
    assert "cart_abandonment_timestamp_mismatch" in sql
    assert "cart_abandonment_event_not_authoritative" in sql
    assert "v_email_contact_count <> 1" in sql
    assert "v_phone_contact_count <> 1" in sql
    assert "'{data,buyer,checkout_phone}'" in sql
    assert "trigger recovery_case_events_protect_hotmart_abandonment_mutation" in sql
    assert "trigger webhook_events_protect_hotmart_cart_source_update" in sql
    assert "trigger webhook_events_protect_hotmart_cart_source_delete" in sql
    assert "cart_abandonment_source_immutable" in sql
    assert "trigger recovery_cases_protect_hotmart_cart_binding" in sql
    assert "cart_abandonment_binding_immutable" in sql


def test_only_admission_entrypoint_is_executable_by_service_role() -> None:
    sql = _sql()

    for role in ("public", "anon", "authenticated"):
        assert role in sql
    assert "grant execute on function public.admit_hotmart_cart_abandonment" in sql
    assert "to service_role" in sql
    assert "validate_hotmart_cart_recovery_binding() from service_role" in sql
