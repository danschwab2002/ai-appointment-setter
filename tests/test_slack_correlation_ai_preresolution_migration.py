from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260916000100_operator_correlation_ai_preresolution.sql"
)


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split()).replace(
        "( ", "("
    ).replace(" )", ")")


def test_migration_keeps_unmatched_internal_and_gates_new_slack_projection() -> None:
    sql = _sql()

    assert "create table public.operator_correlation_preresolutions" in sql
    assert "'suppressed_unmatched'" in sql
    assert "create trigger slack_correlation_preresolution_gate" in sql
    assert "new.projection_status := 'suppressed'" in sql
    assert "projection.attempt_count > 0" in sql
    assert "projection.notification_contract_version in (1, 2)" in sql
    assert "notification_contract_version = 3" in sql
    assert "pre.status = 'recommended'" in sql
    assert "pre.outcome in ('unmatched', 'ambiguous', 'conflict')" not in sql


def test_migration_builds_non_pii_bounded_evidence() -> None:
    sql = _sql()

    assert "get_operator_correlation_preresolution_evidence" in sql
    assert "'candidate_id', ranked.purchase_intent_id" in sql
    assert "'label', 'persona ' || ranked.ordinal::text" in sql
    assert "'precheckout_time_proximity_minutes'" in sql
    assert "'discriminating', facts.discriminating" in sql
    assert "evidence_snapshot jsonb" in sql
    assert "evidence_fingerprint text" in sql
    assert "sha256(convert_to(v_evidence::text, 'utf8'))" in sql
    assert "ranked.next_gap_minutes - ranked.gap_minutes >= 5" in sql
    assert "normalized_email" not in sql
    assert "normalized_phone" not in sql
    assert "payload ->" not in sql


def test_completion_is_fenced_and_only_recommends_an_existing_candidate() -> None:
    sql = _sql()

    assert "operator_correlation_preresolution_lease_lost" in sql
    assert "pre.claim_token is distinct from p_claim_token" in sql
    assert "pre.lease_generation is distinct from p_lease_generation" in sql
    assert "candidate.purchase_intent_id = p_recommended_purchase_intent_id" in sql
    assert "fact.value ->> 'candidate_id' = p_recommended_purchase_intent_id::text" in sql
    assert "(fact.value ->> 'discriminating')::boolean" in sql
    assert "jsonb_array_length(p_supporting_evidence) > 5" in sql
    assert "on conflict (source_event_id) do update" in sql
    assert "slack_correlation_notification_projection.attempt_count = 0" in sql
    assert "slack_correlation_notification_projection.notification_id is null" in sql


def test_v3_claim_requires_a_durable_recommendation_and_preserves_v1_v2() -> None:
    sql = _sql()

    assert "check (notification_contract_version in (1, 2, 3))" in sql
    assert "create or replace function public.claim_slack_correlation_notifications_v3" in sql
    assert "recommendation_data jsonb" in sql
    assert "pre.status = 'recommended'" in sql
    assert "projection.notification_contract_version = 3" in sql
    assert "recommendation_ref" in sql
    assert "model_name" in sql
    assert "prompt_version" in sql


def test_ai_preresolution_rpcs_are_service_role_only() -> None:
    sql = _sql()
    signatures = (
        "public.claim_operator_correlation_preresolutions(text, text, text, integer, integer)",
        "public.get_operator_correlation_preresolution_evidence(text, text, uuid, uuid, bigint)",
        "public.complete_operator_correlation_preresolution(uuid, uuid, bigint, text, uuid, jsonb, text, text)",
        "public.release_operator_correlation_preresolution(uuid, uuid, bigint, text)",
        "public.claim_slack_correlation_notifications_v3(text, text, text, integer, integer, integer)",
    )
    for signature in signatures:
        assert f"revoke all on function {signature} from public" in sql
        assert f"grant execute on function {signature} to service_role" in sql

    assert sql.count("security definer") >= 6
    assert sql.count("set search_path = pg_catalog, public, pg_temp") >= 6
    assert "grant execute" in sql
    assert "to anon" not in sql
    assert "to authenticated" not in sql
