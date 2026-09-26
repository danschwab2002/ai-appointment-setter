from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260925000200_slack_handoff_notification_projection_v1.sql"
)


def _sql() -> str:
    return " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())


def test_projection_enqueues_every_handoff_and_seeds_the_preexisting_as_suppressed() -> None:
    sql = _sql()

    assert "create table public.slack_handoff_notification_projection" in sql
    assert "handoff_request_id uuid primary key references public.human_handoff_requests(id)" in sql
    assert "'suppressed'" in sql
    # La siembra y el encolado del claim usan la misma sentencia: todo pedido de
    # derivacion entra una sola vez, sin filtrar por el estado de su proyeccion
    # a Chatwoot (una proyeccion fallida urge mas, no menos).
    enqueue = (
        "from public.human_handoff_requests request "
        "on conflict on constraint slack_handoff_notification_projection_pkey do nothing"
    )
    assert sql.count(enqueue) == 2
    assert "request.status" not in sql
    assert "where request." not in sql
    assert "'suppressed' from public.human_handoff_requests request" in sql
    assert "for update of projection skip locked" in sql
    assert "p_limit is distinct from 1" in sql
    assert "p_lease_seconds < 30" in sql
    assert "p_lease_seconds > 900" in sql
    assert "order by projection.occurred_at, projection.handoff_request_id" in sql


def test_projection_claim_and_finalization_are_lease_fenced() -> None:
    sql = _sql()

    assert "create or replace function public.claim_slack_handoff_notifications" in sql
    assert "claim_token = gen_random_uuid()" in sql
    assert "lease_generation = projection.lease_generation + 1" in sql
    assert "create or replace function public.complete_slack_handoff_notification" in sql
    assert "projection.claim_token = p_claim_token" in sql
    assert "projection.lease_generation = p_lease_generation" in sql
    assert "create or replace function public.release_slack_handoff_notification" in sql
    assert "connector_admission_unknown" in sql
    assert "connector_semantic_conflict" in sql
    assert "connector_rejected" in sql
    assert "connector_unexpected_error" in sql
    assert "least(300" in sql
    assert "projection.attempt_count < 8" in sql


def test_projection_rpc_acl_is_service_role_only() -> None:
    sql = _sql()
    signatures = (
        "public.claim_slack_handoff_notifications(text, integer, integer)",
        "public.complete_slack_handoff_notification(uuid, uuid, bigint, uuid)",
        "public.release_slack_handoff_notification(uuid, uuid, bigint, text)",
    )
    for signature in signatures:
        for role in ("public", "anon", "authenticated"):
            assert f"revoke execute on function {signature} from {role}" in sql
        assert f"grant execute on function {signature} to service_role" in sql
    assert "revoke all on table public.slack_handoff_notification_projection from service_role" in sql
    assert "alter table public.slack_handoff_notification_projection enable row level security" in sql
