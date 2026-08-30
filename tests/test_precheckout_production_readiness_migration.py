from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/20260829000500_precheckout_production_readiness.sql"
)


def _sql() -> str:
    return MIGRATION.read_text().lower()


def test_migration_publishes_exact_default_off_precheckout_scope() -> None:
    sql = _sql()

    assert "'johanna-precheckout-delayed-first-touch', 1, 'published'" in sql
    assert "'landing', 'precheckout_form_submitted'" in sql
    assert "'f106691755g', 'bxjge6zq'" in sql
    assert "'johanna-abandonment-single-touch-e2e', 2" in sql
    assert "'utc', 1, 1, 1" in sql
    assert "'johanna-precheckout-delayed-first-touch', 1, 'inactive', 0" in sql
    assert "set precheckout_first_touch_enabled = true" not in sql
    assert "set enabled = true" not in sql


def test_migration_exposes_sanitized_readiness_snapshot() -> None:
    sql = _sql()

    signature = "public.get_precheckout_delayed_first_touch_readiness()"
    assert f"create or replace function {signature}" in sql
    assert "migration_tracking_complete boolean" in sql
    assert "scope_configured boolean" in sql
    assert "first_touch_binding_enabled boolean" in sql
    assert "due_count bigint" in sql
    assert "reserved_count bigint" in sql
    assert "request_started_count bigint" in sql
    assert "delivery_unknown_count bigint" in sql
    assert "reason_code text" in sql
    assert "security definer" in sql
    assert "set search_path = pg_catalog, public, pg_temp" in sql
    for version in (
        "20260829000200",
        "20260829000300",
        "20260829000400",
        "20260829000500",
    ):
        assert version in sql


def test_readiness_rpc_is_service_role_only() -> None:
    sql = _sql()
    signature = "public.get_precheckout_delayed_first_touch_readiness()"

    assert f"revoke all on function {signature} from public" in sql
    assert f"revoke all on function {signature} from anon" in sql
    assert f"revoke all on function {signature} from authenticated" in sql
    assert f"grant execute on function {signature} to service_role" in sql
    assert "precheckout_production_readiness_postflight_failed" in sql


def test_sql_package_runs_production_readiness_behavior_probe() -> None:
    package = Path("tests/sql/followup_engine/package.json").read_text()

    assert "node validate_precheckout_production_readiness.mjs" in package
