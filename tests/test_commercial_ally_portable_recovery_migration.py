from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    PROJECT_ROOT
    / "supabase"
    / "migrations"
    / "20260903000100_commercial_ally_portable_recovery.sql"
)


def test_portable_recovery_adds_binding_fenced_cart_admission() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    portable_body = sql.split(
        "create function public.admit_johanna_hotmart_cart_abandonment",
        maxsplit=1,
    )[0]

    assert "function public.admit_portable_hotmart_cart_abandonment" in sql
    assert "p_tenant_ref text" in sql
    assert "p_funnel_ref text" in sql
    assert "p_binding_version integer" in sql
    assert "p_external_event_id text" in sql
    assert "p_payload jsonb" in sql
    assert "b.status = 'active'" in sql
    assert "for update" in sql
    assert "hotmart_purchase_intent_scopes" in sql
    assert "scope.active" in sql
    assert "public._admit_hotmart_cart_abandonment_base(" in sql
    assert "public._admit_hotmart_purchase_intent_identity(" in sql
    assert "public.correlate_hotmart_purchase_intent(" in sql
    assert "public.admit_and_correlate_hotmart_cart_abandonment(" not in portable_body
    assert "public.schedule_hotmart_abandonment_reevaluation(" not in portable_body
    assert "security definer" in sql
    assert "set search_path = pg_catalog, public, pg_temp" in sql


def test_portable_recovery_is_unseeded_and_has_no_effect_surface() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "insert into public.commercial_ally_runtime_bindings" not in sql
    assert "insert into public.hotmart_purchase_intent_scopes" not in sql
    for forbidden in (
        "scheduled_actions",
        "followup_delivery_attempts",
        "request_started",
        "chatwoot",
        "meta",
        "messages",
    ):
        assert forbidden not in sql


def test_portable_recovery_rpc_is_service_role_execute_only() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    compact = " ".join(sql.split())
    signature = (
        "public.admit_portable_hotmart_cart_abandonment("
        " text, text, integer, text, jsonb, text, text )"
    )

    assert f"revoke all on function {signature} from public" in compact
    assert f"revoke all on function {signature} from anon" in compact
    assert f"revoke all on function {signature} from authenticated" in compact
    assert f"grant execute on function {signature} to service_role" in compact


def test_recovery_migration_closes_legacy_unscoped_rpc() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    compact = " ".join(sql.split())
    legacy = (
        "public.admit_and_correlate_hotmart_cart_abandonment("
        " text, jsonb, text, text )"
    )
    wrapper = (
        "public.admit_johanna_hotmart_cart_abandonment("
        " text, jsonb, text, text )"
    )

    assert "create function public.admit_johanna_hotmart_cart_abandonment" in compact
    assert f"revoke all on function {legacy} from service_role" in compact
    assert f"grant execute on function {wrapper} to service_role" in compact
