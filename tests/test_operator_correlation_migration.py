from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260824000100_operator_correlation_review_read.sql"
)
RESOLUTION_MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260824000200_operator_correlation_manual_resolution.sql"
)
CASEFOLD_MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260828000100_operator_correlation_product_casefold.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_operator_correlation_read_rpcs_are_narrow_and_effect_free() -> None:
    sql = _sql()
    compact = " ".join(sql.split())

    assert "create or replace function public.list_operator_unresolved_correlations" in sql
    assert "create or replace function public.get_operator_unresolved_correlation" in sql
    assert compact.count("security definer") == 2
    assert compact.count("set search_path = pg_catalog, public, pg_temp") == 2
    assert "manual_handoff_required" in sql
    assert "'unmatched', 'ambiguous', 'conflict'" in sql
    assert "scope.tenant_ref = p_tenant_ref" in sql
    assert "scope.funnel_ref = p_funnel_ref" in sql
    for dimension in (
        "intent.tenant_ref = scope.tenant_ref",
        "intent.funnel_ref = scope.funnel_ref",
        "intent.product_ref = scope.purchase_intent_product_ref",
        "intent.offer_ref = scope.offer_ref",
    ):
        assert dimension in sql
    assert sql.count("then '***@'") == 2
    assert "masked_email" in sql
    assert "masked_phone" in sql
    for mutation in ("insert into", "update public.", "delete from"):
        assert mutation not in sql
    assert "grant select" not in sql
    assert "grant all on table" not in sql


def test_operator_correlation_read_rpcs_are_service_role_only() -> None:
    sql = _sql()
    compact = " ".join(sql.split())
    signatures = (
        "public.list_operator_unresolved_correlations(text, text, integer, uuid)",
        "public.get_operator_unresolved_correlation(text, text, uuid)",
    )
    for signature in signatures:
        for role in ("public", "anon", "authenticated"):
            assert f"revoke execute on function {signature} from {role}" in compact
        assert f"grant execute on function {signature} to service_role" in compact


def test_manual_resolution_is_separate_immutable_and_effect_free() -> None:
    sql = RESOLUTION_MIGRATION.read_text(encoding="utf-8").lower()
    compact = " ".join(sql.split())

    assert "create table public.operator_correlation_resolution_commands" in sql
    assert "create table public.operator_correlation_resolutions" in sql
    assert "create or replace function public.prepare_operator_correlation_resolution" in sql
    assert "create or replace function public.confirm_operator_correlation_resolution" in sql
    assert compact.count("security definer") == 3
    assert compact.count("set search_path = pg_catalog, public, pg_temp") >= 3
    assert "candidate_snapshot jsonb not null" in sql
    assert "idempotency_key uuid not null unique" in sql
    assert "request_fingerprint jsonb not null" in sql
    assert "operator_correlation_idempotency_conflict" in sql
    assert "order by intent.id for share of intent" in compact
    assert "unique (webhook_event_id)" in sql
    assert "operator_correlation_resolution_rows_are_immutable" in sql
    assert "validate_operator_correlation_resolution_command_insert" in sql
    assert "validate_operator_correlation_command_before_insert" in sql
    assert "operator_correlation_resolution_command_invalid" in sql
    assert "not exists" in sql
    assert "public.operator_correlation_resolutions" in sql
    for forbidden_effect in (
        "update public.purchase_intents",
        "insert into public.hotmart_abandonment_reevaluations",
        "insert into public.followup_actions",
        "activation_authorized = true",
    ):
        assert forbidden_effect not in compact


def test_manual_resolution_rpcs_are_service_role_only() -> None:
    compact = " ".join(
        RESOLUTION_MIGRATION.read_text(encoding="utf-8").lower().split()
    )
    signatures = (
        "public.prepare_operator_correlation_resolution(text, text, text, uuid, text, uuid, text, uuid)",
        "public.confirm_operator_correlation_resolution(text, text, text, uuid, text, uuid)",
    )
    for signature in signatures:
        for role in ("public", "anon", "authenticated"):
            assert f"revoke execute on function {signature} from {role}" in compact
        assert f"grant execute on function {signature} to service_role" in compact


def test_operator_correlation_product_scope_matches_case_insensitively() -> None:
    sql = CASEFOLD_MIGRATION.read_text(encoding="utf-8").lower()
    compact = " ".join(sql.split())

    for signature in (
        "public.validate_operator_correlation_resolution_command_insert()",
        "public.prepare_operator_correlation_resolution(text,text,text,uuid,text,uuid,text,uuid)",
        "public.confirm_operator_correlation_resolution(text,text,text,uuid,text,uuid)",
        "public.list_operator_unresolved_correlations(text,text,integer,uuid)",
    ):
        assert signature in compact
    assert "v_expected_occurrences integer[] := array[2, 1, 2, 1]" in compact
    assert (
        "v_expected_security_definer boolean[] := array[false, true, true, true]"
        in compact
    )
    assert (
        "lower(intent.product_ref) = lower(v_scope.purchase_intent_product_ref)"
        in compact
    )
    assert (
        "lower(intent.product_ref) = lower(scope.purchase_intent_product_ref)"
        in compact
    )
    assert "pg_get_functiondef(v_function)" in compact
    assert "operator_correlation_casefold_definition_mismatch" in compact
    assert "grant " not in compact
    assert "revoke " not in compact
