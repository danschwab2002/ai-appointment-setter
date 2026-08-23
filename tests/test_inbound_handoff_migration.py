from pathlib import Path
import re


MIGRATION = Path(
    "supabase/migrations/20260823000100_inbound_durable_handoff.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def _function_body(sql: str, name: str) -> str:
    match = re.search(
        rf"create\s+or\s+replace\s+function\s+public\.{name}\s*\(.*?"
        rf"as\s+\$function\$(.*?)\$function\$;",
        sql,
        re.DOTALL,
    )
    assert match is not None, f"missing function {name}"
    return match.group(1)


def test_handoff_requests_are_anchored_to_commercial_case_root() -> None:
    sql = _sql()

    assert "add column commercial_case_id uuid" in sql
    assert "update public.human_handoff_requests" in sql
    assert "commercial_case_id = recovery_case_id" in sql
    assert "references public.commercial_cases(id)" in sql
    assert "alter column recovery_case_id drop not null" in sql
    assert "human_handoff_request_aggregate_shape" in sql
    assert "case_kind = 'inbound_sales'" in sql
    assert "recovery_case_id is null" in sql
    assert "commercial_case_id = recovery_case_id" in sql
    assert "human_handoff_requests_one_live_per_commercial_case_idx" in sql


def test_projection_policy_has_exactly_one_supported_scope() -> None:
    sql = _sql()

    assert "add column inbound_scope_key text" in sql
    assert "add column inbound_scope_version integer" in sql
    assert "human_handoff_projection_policy_scope_shape" in sql
    assert "num_nonnulls(scope_key, inbound_scope_key) = 1" in sql
    assert "num_nonnulls(scope_version, inbound_scope_version) = 1" in sql
    assert "references public.inbound_commercial_scope_versions" in sql


def test_inbound_rpc_derives_canonical_scope_and_stops_before_effects() -> None:
    body = _function_body(_sql(), "request_inbound_human_handoff")

    assert "pg_advisory_xact_lock" in body
    assert "from public.commercial_cases" in body
    assert "case_kind = 'inbound_sales'" in body
    assert "from public.inbound_commercial_case_admissions" in body
    assert "from public.inbound_commercial_scope_versions" in body
    assert "from public.channel_identities" in body
    assert "from public.conversations" in body
    assert "conversation.commercial_context = jsonb_build_object" in body
    assert "requested_by" in body and "'agent'" in body
    assert "source_action_id" in body and "null" in body
    assert "source_attempt_id" in body and "null" in body
    assert "status = 'paused'" in body
    assert "automation_status = 'disabled'" in body
    assert "version = commercial_case.version + 1" in body
    assert body.index("update public.commercial_cases") < body.index(
        "insert into public.human_handoff_projection_effects"
    )
    assert "(v_request.id, 'assignment')" in body
    assert "(v_request.id, 'private_note')" in body


def test_inbound_rpc_replays_exactly_and_conflicts_on_changed_inputs() -> None:
    body = _function_body(_sql(), "request_inbound_human_handoff")

    assert "human_handoff_command_conflict" in body
    assert "outcome := 'already_requested'" in body
    assert "projection_policy_key" in body
    assert "projection_policy_version" in body
    assert "primary_reason_code" in body
    assert "commercial_case_id" in body


def test_projection_claim_supports_legacy_and_inbound_aggregates() -> None:
    body = _function_body(_sql(), "claim_human_handoff_projection_effects")

    assert "left join public.recovery_cases" in body
    assert "join public.commercial_cases" in body
    assert "request.recovery_case_id is not null" in body
    assert "request.recovery_case_id is null" in body
    assert "request.inbound_scope_key" in body
    assert "request.scope_key" in body
    assert "for update of effect skip locked" in body


def test_inbound_rpc_is_service_role_only_and_legacy_rpc_is_preserved() -> None:
    sql = _sql()

    assert "drop function public.request_human_handoff" not in sql
    assert "request_human_handoff_legacy_not_replaced" not in sql
    assert "revoke execute on function public.request_inbound_human_handoff" in sql
    assert "from public" in sql
    assert "from anon" in sql
    assert "from authenticated" in sql
    assert "grant execute on function public.request_inbound_human_handoff" in sql
    assert "to service_role" in sql
