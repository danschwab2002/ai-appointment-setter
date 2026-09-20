from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260914000100_johanna_checkout_issuance_v2.sql"


def _sql() -> str:
    assert MIGRATION.exists(), "checkout issuance V2 migration is missing"
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_catalog_pins_one_active_default_offer() -> None:
    sql = _sql()
    assert "create table public.checkout_offer_catalog" in sql
    assert "checkout_offer_catalog_one_active_default_idx" in sql
    assert "tenant_ref, scope_key, scope_version, lower(product_ref)" in sql
    assert "offer.scope_key = v_case.inbound_scope_key" in sql
    assert "offer.scope_version = v_case.inbound_scope_version" in sql
    assert "where status = 'active' and default_for_inbound" in sql
    assert "'libre-de-ansiedad-inbound', 2" in sql
    assert "'ads-a'" in sql
    assert "'bxjge6zq'" in sql
    assert "'https://pay.hotmart.com/f106691755g'" in sql


def test_issuance_stores_durable_context_and_original_sck() -> None:
    sql = _sql()
    assert "create table public.checkout_link_issuances" in sql
    for field in (
        "issuance_ulid",
        "commercial_case_id",
        "purchase_intent_id",
        "contact_id",
        "channel_identity_id",
        "chatwoot_conversation_id",
        "source_kind",
        "source_submission_id",
        "original_sck",
        "offer_catalog_id",
        "checkout_url_final",
        "sck_value",
    ):
        assert field in sql
    assert "source_kind in ('inbound_request', 'precheckout_request')" in sql
    assert "raw_payload #>> '{data,attribution,sck}'" in sql
    assert "sck_value = 'hermes|v1|' || issuance_ulid" in sql
    assert "checkout_link_issuance_immutable" in sql


def test_reserve_and_authorize_rpcs_are_idempotent_and_reauthorize() -> None:
    sql = _sql()
    assert "create function public.reserve_chatwoot_checkout_issuance_v2" in sql
    assert "create function public.authorize_chatwoot_checkout_issuance_v2" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "has_chatwoot_opt_out_stop" in sql
    assert "human_takeover" in sql
    assert "for update" in sql
    assert "trigger_external_message_id" in sql
    assert "on conflict" in sql
    assert "purchase_already_approved" in sql


def test_hotmart_sck_correlation_uses_persisted_equality() -> None:
    sql = _sql()
    assert "create function public.correlate_hotmart_checkout_issuance_v2" in sql
    assert "create function public.admit_and_correlate_hotmart_checkout_issuance_v2" in sql
    assert "_admit_hotmart_purchase_approved_base" in sql
    assert "where issuance.sck_value = p_sck_value" in sql
    assert "hotmart_webhook_event_id" in sql
    assert "purchase_matched" in sql
    assert "invalid_hermes_sck" in sql
    assert "cancel_hotmart_abandonment_reevaluations_for_purchase" in sql


def test_tables_are_rls_closed_and_only_rpc_execute_is_granted() -> None:
    sql = _sql()
    for table in ("checkout_offer_catalog", "checkout_link_issuances"):
        assert f"alter table public.{table} enable row level security" in sql
        assert f"revoke all on table public.{table} from service_role" in sql
    assert "security definer" in sql
    assert "set search_path = pg_catalog, public, pg_temp" in sql
    assert "grant execute on function public.reserve_chatwoot_checkout_issuance_v2" in sql
    assert "grant execute on function public.authorize_chatwoot_checkout_issuance_v2" in sql
    assert "revoke all on function public.correlate_hotmart_checkout_issuance_v2" in sql
    assert "grant execute on function public.correlate_hotmart_checkout_issuance_v2" not in sql
