from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260913000200_slack_correlation_event_context.sql"
)


def test_event_context_migration_adds_rolling_safe_v2_claim_rpc() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "create or replace function public.claim_slack_correlation_notifications_v2" in sql
    assert "source_event_type text" in sql
    assert "notification_contract_version integer" in sql
    assert "projection.notification_contract_version is null" in sql
    assert "projection.notification_contract_version, 1" in sql
    assert "projection.notification_contract_version, 2" in sql
    assert "join public.webhook_events" in sql
    assert "event.event_type" in sql
    assert "event.event_type = correlation.event_type" in sql
    assert "create or replace function public.claim_slack_correlation_notifications(" in sql


def test_event_context_v2_is_service_role_only() -> None:
    sql = " ".join(MIGRATION.read_text(encoding="utf-8").lower().split())
    sql = sql.replace("( ", "(").replace(" )", ")")

    signature = (
        "public.claim_slack_correlation_notifications_v2"
        "(text, text, text, integer, integer, integer)"
    )
    assert f"revoke all on function {signature} from public" in sql
    assert f"grant execute on function {signature} to service_role" in sql
    assert "security definer" in sql
    assert "set search_path = pg_catalog, public, pg_temp" in sql
