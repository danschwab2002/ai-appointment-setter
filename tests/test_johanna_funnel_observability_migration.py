"""Contract tests for Johanna sanitary funnel persistence and dashboard V2."""

from pathlib import Path
import re


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "20260907000200_johanna_funnel_observability_v1.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_migration_creates_closed_append_only_event_ledger() -> None:
    sql = _sql()
    assert "create table public.johanna_funnel_events" in sql
    for column in (
        "event_id text primary key",
        "contract_version text not null",
        "event_type text not null",
        "occurred_at timestamptz not null",
        "anonymous_session_id text not null",
        "landing_ref text not null",
        "offer_ref text not null",
        "utm_source text",
        "utm_medium text",
        "utm_campaign text",
        "utm_content text",
        "utm_term text",
    ):
        assert column in sql
    assert "foreign key (landing_ref, offer_ref)" in sql
    assert "references public.johanna_precheckout_landing_offers" in sql
    assert "before update or delete on public.johanna_funnel_events" in sql
    assert "johanna_funnel_events_append_only" in sql
    assert "enable row level security" in sql
    assert not {"email", "phone", "buyer_name", "fbclid", "payload"} & set(
        re.findall(r"\b[a-z_]+\b", sql.split("create table public.johanna_funnel_events", 1)[1].split(");", 1)[0])
    )


def test_admission_rpc_is_exactly_idempotent_and_conflicts_on_changed_replay() -> None:
    sql = _sql()
    signature = (
        "public.admit_johanna_funnel_event_v1(text,text,text,timestamptz,text,text,"
        "text,text,text,text,text,text)"
    )
    assert "create or replace function public.admit_johanna_funnel_event_v1(" in sql
    assert "'inserted'::text" in sql
    assert "'duplicate'::text" in sql
    assert "'semantic_conflict'::text" in sql
    for field in (
        "contract_version", "event_type", "occurred_at", "anonymous_session_id",
        "landing_ref", "offer_ref", "utm_source", "utm_medium", "utm_campaign",
        "utm_content", "utm_term",
    ):
        assert f"existing.{field} is not distinct from" in sql
    assert f"revoke all on function {signature} from public" in sql
    assert f"revoke all on function {signature} from anon" in sql
    assert f"revoke all on function {signature} from authenticated" in sql
    assert f"grant execute on function {signature} to service_role" in sql


def test_service_role_has_rpc_only_and_no_direct_event_table_dml() -> None:
    sql = _sql()
    for role in ("public", "anon", "authenticated", "service_role"):
        assert f"revoke all on table public.johanna_funnel_events from {role}" in sql
    assert "grant select on table public.johanna_funnel_events" not in sql
    assert "grant insert on table public.johanna_funnel_events" not in sql
    assert "grant update on table public.johanna_funnel_events" not in sql
    assert "grant delete on table public.johanna_funnel_events" not in sql


def test_dashboard_v2_is_scope_fixed_current_snapshot_and_exposes_event_aggregates() -> None:
    sql = _sql()
    assert "create or replace function public.read_johanna_funnel_dashboard_v2(" in sql
    dashboard_sql = sql.split(
        "create or replace function public.read_johanna_funnel_dashboard_v2(", 1
    )[1].split("$function$;", 1)[0]
    assert "p_cutoff" not in dashboard_sql
    assert "p_window_days integer default 7" in dashboard_sql
    assert "v_snapshot_at timestamptz := statement_timestamp()" in dashboard_sql
    assert "row_kind text" in dashboard_sql
    assert "snapshot_at timestamptz" in dashboard_sql
    assert "window_start timestamptz" in dashboard_sql
    assert "'meta'::text" in dashboard_sql
    for scope in (
        "intent.tenant_ref = 'lancemos'",
        "intent.funnel_ref = 'psicologajohanna'",
        "lower(intent.product_ref) = 'f106691755g'",
        "commercial_case.inbound_scope_key = 'libre-de-ansiedad-inbound'",
        "commercial_case.inbound_scope_version = 2",
        "inbound_scope.chatwoot_account_id = 1",
        "inbound_scope.chatwoot_inbox_id = 9",
        "command_row.chatwoot_account_id = 1",
        "command_row.chatwoot_inbox_id = 9",
        "handoff_row.chatwoot_account_id = 1",
        "handoff_row.chatwoot_inbox_id = 9",
        "opt_out_row.canonical_account_id = 1",
        "opt_out_row.canonical_inbox_id = 9",
    ):
        assert scope in sql
    assert dashboard_sql.count("handoff_row.chatwoot_account_id = 1") == 3
    assert dashboard_sql.count("opt_out_row.canonical_account_id = 1") == 3
    assert "submission.external_submission_id = event.anonymous_session_id" in sql
    assert "link.submission_id = submission.id" in sql
    assert "event.landing_ref = intent.landing_ref" in sql
    assert "event.offer_ref = intent.offer_ref" in sql
    for column in (
        "page_view_count bigint",
        "preform_opened_count bigint",
        "preform_submitted_count bigint",
        "checkout_redirected_count bigint",
        "last_funnel_event_at timestamptz",
        "last_funnel_event_type text",
    ):
        assert column in sql
    for fact in (
        "link.attached_at < v_snapshot_at",
        "submission.received_at < v_snapshot_at",
        "event.occurred_at < v_snapshot_at",
        "correlation_row.created_at < v_snapshot_at",
        "admission.created_at < v_snapshot_at",
        "intent.created_at < v_snapshot_at",
    ):
        assert fact in sql
    assert "revoke all on function public.read_johanna_funnel_dashboard_v1(" in sql
    assert "grant execute on function public.read_johanna_funnel_dashboard_v1(" not in sql
    assert "grant execute on function public.read_johanna_funnel_dashboard_v2(" in sql
