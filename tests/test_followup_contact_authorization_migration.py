from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "20260805000200_followup_contact_authorization_grant.sql"
)


def _sql() -> str:
    assert MIGRATION.exists(), "missing follow-up contact authorization grant migration"
    return MIGRATION.read_text()


def test_planning_grants_whatsapp_cart_recovery_authorization() -> None:
    sql = _sql().lower()
    # The grant lives inside the atomic planning RPC, not a separate best-effort write.
    assert "create or replace function public.plan_cart_recovery_with_identity" in sql
    assert "insert into public.contact_authorizations" in sql
    assert "'allowed'" in sql
    assert "'hotmart'" in sql
    assert "'cart_recovery'" in sql
    assert "'whatsapp'" in sql


def test_grant_is_idempotent_and_respects_optout() -> None:
    sql = _sql().lower()
    # Only grant when no active authorization row already exists for the tuple,
    # which makes replay idempotent and never overrides an active denial/restriction.
    assert "not exists" in sql
    assert "authorization_status" in sql
    assert "valid_until is null or ca.valid_until" in sql
    assert "ca.valid_from <= " in sql


def test_grant_records_cart_abandonment_evidence() -> None:
    sql = _sql().lower()
    assert "cart_abandonment" in sql
    assert "p_webhook_event_id" in sql
