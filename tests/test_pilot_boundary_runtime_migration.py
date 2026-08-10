from pathlib import Path
import re


MIGRATION = Path(
    "supabase/migrations/20260810000300_lancemos_pilot_boundary_runtime.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def _function_body(sql: str, name: str) -> str:
    pattern = re.compile(
        rf"create\s+or\s+replace\s+function\s+public\.{name}\s*\(.*?"
        rf"as\s+\$function\$(.*?)\$function\$;",
        re.DOTALL,
    )
    match = pattern.search(sql)
    assert match is not None, f"missing function {name}"
    return match.group(1)


def test_runtime_migration_adds_atomic_pilot_planning_rpc() -> None:
    sql = _sql()
    body = _function_body(sql, "plan_lancemos_pilot_cart_recovery")

    assert "from public.evaluate_lancemos_pilot_scope" in body
    assert "from public.plan_cart_recovery_with_identity" in body
    assert "insert into public.pilot_recovery_case_bindings" in body
    assert body.index("evaluate_lancemos_pilot_scope") < body.index(
        "plan_cart_recovery_with_identity"
    )
    assert "pilot_scope_rejected" in body
    assert "for update" in body


def test_runtime_migration_adds_atomic_request_start_rpc() -> None:
    sql = _sql()
    body = _function_body(sql, "mark_lancemos_pilot_request_started")

    assert "from public.authorize_lancemos_pilot_request_start" in body
    assert "from public.mark_followup_request_started" in body
    assert "join public.pilot_recovery_case_bindings" in body
    assert body.index("authorize_lancemos_pilot_request_start") < body.index(
        "mark_followup_request_started"
    )
    assert "pilot_authorization_without_request_start" in body
    assert "pilot_delivery_mode_mismatch" in body
    assert "approved_template" in body


def test_legacy_request_start_requires_existing_pilot_authorization() -> None:
    sql = _sql()
    body = _function_body(sql, "mark_followup_request_started")

    assert "pilot_outbound_request_authorizations" in body
    assert "pilot_request_authorization_required" in body
    assert "_mark_followup_request_started_without_pilot_guard" in body


def test_runtime_rpcs_are_service_role_only() -> None:
    sql = _sql()

    for signature in (
        "plan_lancemos_pilot_cart_recovery",
        "mark_lancemos_pilot_request_started",
    ):
        assert f"revoke execute on function public.{signature}" in sql
        assert f"grant execute on function public.{signature}" in sql

    assert "'anon', 'authenticated', 'service_role'" in sql
    assert "v_role" in sql
    assert (
        "revoke execute on function "
        "public._mark_followup_request_started_without_pilot_guard"
    ) in sql
    assert (
        "grant execute on function public.mark_followup_request_started"
        not in sql
    )
