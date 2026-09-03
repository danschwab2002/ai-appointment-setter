from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    PROJECT_ROOT
    / "supabase"
    / "migrations"
    / "20260903000100_commercial_ally_portable_recovery.sql"
)
PAYMENT_FAILURE_MIGRATION = (
    PROJECT_ROOT
    / "supabase"
    / "migrations"
    / "20260903000300_commercial_ally_payment_failure_recovery.sql"
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
    assert "create table public.commercial_ally_hotmart_event_bindings" in sql
    assert "scope_id uuid not null" in sql
    assert "references public.hotmart_purchase_intent_scopes(id) on delete restrict" in sql
    assert "v_provenance.scope_id is distinct from v_scope.id" in sql
    assert "portable_hotmart_cart_replay_binding_mismatch" in sql
    assert "v_provenance.tenant_ref is distinct from v_binding.tenant_ref" in sql
    assert "v_provenance.funnel_ref is distinct from v_binding.funnel_ref" in sql
    assert (
        "v_provenance.binding_version is distinct from v_binding.binding_version"
        in sql
    )
    assert "v_provenance.hotmart_product_id is distinct from v_scope.hotmart_product_id" in sql
    assert "is distinct from v_scope.purchase_intent_product_ref" in sql
    assert "v_provenance.offer_ref is distinct from v_scope.offer_ref" in sql
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
    for role in ("public", "anon", "authenticated", "service_role"):
        assert (
            "revoke all on table public.commercial_ally_hotmart_event_bindings "
            f"from {role}"
        ) in compact
        assert (
            "revoke all on function "
            "public.protect_commercial_ally_hotmart_event_binding() "
            f"from {role}"
        ) in compact
    assert "commercial_ally_hotmart_event_bindings_append_only" in compact
    assert "commercial_ally_hotmart_event_binding_immutable" in compact


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


def test_portable_payment_failure_migration_admits_and_plans_atomically() -> None:
    sql = PAYMENT_FAILURE_MIGRATION.read_text(encoding="utf-8").lower()

    assert "function public.admit_portable_hotmart_payment_failure" in sql
    assert "event_type = 'purchase_canceled'" in sql
    assert "'payment_failure'" in sql
    assert "function public.plan_portable_payment_failure_recovery" in sql
    assert "payment_failure_first_contact" in sql
    assert "insert into public.recovery_cases" in sql
    assert "insert into public.recovery_case_events" in sql
    assert "insert into public.followup_sequences" in sql
    assert "insert into public.scheduled_actions" in sql
    assert "purpose = 'cart_recovery'" in sql


def test_portable_payment_failure_migration_expands_event_role_fail_closed() -> None:
    sql = PAYMENT_FAILURE_MIGRATION.read_text(encoding="utf-8").lower()
    compact = " ".join(sql.split())

    assert "drop constraint recovery_case_events_event_role_check" in compact
    assert "drop constraint pilot_scope_versions_tenant_key_check" in compact
    assert "tenant_key = 'lancemos'" not in compact
    assert (
        "check (event_role in ('cart_abandonment', 'payment_failure'))"
        in compact
    )
    assert "security invoker" in sql
    assert "set search_path = pg_catalog, public, pg_temp" in sql
    for function_name in (
        "admit_portable_hotmart_payment_failure",
        "plan_portable_payment_failure_recovery",
    ):
        assert f"revoke all on function public.{function_name}" in compact
        assert f"grant execute on function public.{function_name}" in compact
