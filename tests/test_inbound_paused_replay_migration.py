from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260826000200_inbound_paused_replay_guard.sql"
)


def test_inbound_replay_v2_revalidates_locked_durable_aggregate() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    body = sql.split(
        "create function public.admit_inbound_commercial_case_v2(", 1
    )[1].split("$function$;", 1)[0]

    assert "from public.admit_inbound_commercial_case_base(" in body
    assert "from public.commercial_cases commercial_case" in body
    assert "from public.conversations conversation" in body
    assert body.count("for update") >= 2
    assert "v_case.status = 'active'" in body
    assert "v_case.automation_status = 'draft_only'" in body
    assert "v_conversation.status in" in body
    assert "'paused_human'" not in body.split("v_conversation.status in", 1)[1]
    assert "v_conversation.automation_status = 'draft_only'" in body
    assert "not v_conversation.human_takeover" in body
    assert "outcome := 'blocked'" in body
    assert "automation_status := 'disabled'" in body


def test_legacy_inbound_rpc_maps_blocked_to_fail_closed_conflict() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    legacy = sql.split(
        "create function public.admit_inbound_commercial_case(\n", 1
    )[1].split("$function$;", 1)[0]

    assert "from public.admit_inbound_commercial_case_v2(" in legacy
    assert "when result.outcome = 'blocked'" in legacy
    assert "then 'evidence_conflict'" in legacy


def test_inbound_replay_rpc_acl_exposes_wrappers_not_base() -> None:
    compact = " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())

    base = "public.admit_inbound_commercial_case_base( text, integer, bigint, text )"
    v2 = "public.admit_inbound_commercial_case_v2( text, integer, bigint, text )"
    legacy = "public.admit_inbound_commercial_case( text, integer, bigint, text )"
    assert f"revoke all on function {base} from public" in compact
    assert f"revoke all on function {base} from service_role" in compact
    assert f"grant execute on function {v2} to service_role" in compact
    assert f"grant execute on function {legacy} to service_role" in compact
    for role in ("public", "anon", "authenticated"):
        assert f"revoke all on function {v2} from {role}" in compact
        assert f"revoke all on function {legacy} from {role}" in compact
