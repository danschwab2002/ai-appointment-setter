from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260908000100_slack_correlation_notification_projection.sql"
)


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_projection_is_scoped_durable_and_excludes_manual_resolution() -> None:
    sql = _sql()

    assert "create table public.slack_correlation_notification_projection" in sql
    assert "references public.hotmart_purchase_intent_correlations(webhook_event_id)" in sql
    assert "scope.tenant_ref = p_tenant_ref" in sql
    assert "scope.funnel_ref = p_funnel_ref" in sql
    assert "correlation.outcome in ('unmatched', 'ambiguous', 'conflict')" in sql
    assert "not exists ( select 1 from public.operator_correlation_resolutions" in sql
    assert "for update of projection skip locked" in sql
    assert "p_limit is distinct from 1" in sql
    assert "p_lease_seconds < 30" in sql
    assert "p_lease_seconds > 900" in sql
    assert "binding.binding_version = p_binding_version" in sql
    assert "binding.status = 'active'" in sql
    assert "commercial_ally_binding_not_active" in sql
    assert "order by projection.occurred_at, projection.source_event_id" in sql


def test_projection_claim_and_finalization_are_lease_fenced() -> None:
    sql = _sql()

    assert "create or replace function public.claim_slack_correlation_notifications" in sql
    assert "claim_token = gen_random_uuid()" in sql
    assert "lease_generation = projection.lease_generation + 1" in sql
    assert "create or replace function public.complete_slack_correlation_notification" in sql
    assert "projection.claim_token = p_claim_token" in sql
    assert "projection.lease_generation = p_lease_generation" in sql
    assert "create or replace function public.release_slack_correlation_notification" in sql
    assert "connector_admission_unknown" in sql
    assert "connector_semantic_conflict" in sql
    assert "connector_rejected" in sql
    assert "least(300" in sql
    assert "projection.attempt_count < 8" in sql


def test_projection_rpc_acl_is_service_role_only() -> None:
    sql = _sql()
    signatures = (
        "public.claim_slack_correlation_notifications(text, text, text, integer, integer, integer)",
        "public.complete_slack_correlation_notification(uuid, uuid, bigint, uuid)",
        "public.release_slack_correlation_notification(uuid, uuid, bigint, text)",
    )
    for signature in signatures:
        for role in ("public", "anon", "authenticated"):
            assert f"revoke execute on function {signature} from {role}" in sql
        assert f"grant execute on function {signature} to service_role" in sql
    assert "revoke all on table public.slack_correlation_notification_projection from service_role" in sql
