from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260824000100_operator_correlation_review_read.sql"
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
