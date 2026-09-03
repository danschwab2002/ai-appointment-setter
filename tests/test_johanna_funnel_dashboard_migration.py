from pathlib import Path
import re


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "20260831000100_johanna_funnel_dashboard_read.sql"
)
CONTAINMENT = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "20260831000200_disable_johanna_funnel_dashboard_read.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_containment_revokes_dashboard_rpc_from_every_api_role() -> None:
    lowered = CONTAINMENT.read_text(encoding="utf-8").lower()

    assert "begin;" in lowered
    assert lowered.count("revoke all on function public.read_johanna_funnel_dashboard_v1(") == 4
    for role in ("public", "anon", "authenticated", "service_role"):
        assert f") from {role};" in lowered
    assert "grant execute" not in lowered
    assert "commit;" in lowered


def test_dashboard_read_rpc_is_stable_sanitary_and_service_role_only() -> None:
    sql = _sql()
    lowered = sql.lower()

    assert "create or replace function public.read_johanna_funnel_dashboard_v1(" in lowered
    assert "returns table (" in lowered
    assert "language plpgsql" in lowered
    assert "stable" in lowered
    assert "security definer" in lowered
    assert "set search_path = pg_catalog, public, pg_temp" in lowered
    assert "p_window_days between 1 and 31" in lowered
    assert "revoke all on function public.read_johanna_funnel_dashboard_v1(" in lowered
    assert "from public" in lowered
    assert "from anon" in lowered
    assert "from authenticated" in lowered
    assert "grant execute on function public.read_johanna_funnel_dashboard_v1(" in lowered
    assert "to service_role" in lowered

    forbidden = {
        "raw_payload",
        "canonical_payload",
        "payload",
        "normalized_email",
        "normalized_phone",
        "target_phone",
        "buyer_name",
        "external_user_id",
        "private_note_body",
        "chatwoot_message_id",
    }
    function_body = re.search(
        r"create or replace function public\.read_johanna_funnel_dashboard_v1\(.+?\$function\$;",
        lowered,
        flags=re.DOTALL,
    )
    assert function_body is not None
    assert not forbidden & set(re.findall(r"[a-z_]+", function_body.group()))


def test_dashboard_read_rpc_returns_closed_case_shape() -> None:
    lowered = _sql().lower()
    for column in (
        "case_id uuid",
        "case_type text",
        "provenance text",
        "stage text",
        "commercial_outcome text",
        "control_outcomes text[]",
        "created_at timestamptz",
        "updated_at timestamptz",
        "conversation_id uuid",
        "chatwoot_conversation_id bigint",
        "chatwoot_status text",
        "attention_reasons text[]",
    ):
        assert column in lowered
    assert "customer_production" not in lowered
    assert "controlled_test" in lowered
    assert "simulator" in lowered
    assert "unknown" in lowered


def test_recovery_origins_use_the_three_approved_disjoint_names() -> None:
    lowered = _sql().lower()
    assert "then 'both'" in lowered
    assert "then 'precheckout_only'" in lowered
    assert "else 'hotmart_only'" in lowered
    assert "precheckout_and_hotmart" not in lowered
    assert "then 'precheckout'" not in lowered
    assert "else 'cart_recovery'" not in lowered


def test_handoff_and_opt_out_are_independent_for_all_three_case_roots() -> None:
    lowered = _sql().lower()
    assert lowered.count("then 'handoff_' || handoff.status") == 3
    assert lowered.count("case when opt_out.has_opt_out then 'opt_out' end") == 3
    assert lowered.count("then 'handoff_pending'") == 3
