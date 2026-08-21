"""Contract tests for versioned observed WhatsApp consent authorization."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260821000200_lead_whatsapp_consent_authorization.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_migration_replaces_observed_admission_with_versioned_contract() -> None:
    sql = _sql()

    assert "create or replace function public.admit_observed_lead_precheckout" in sql
    assert "array['1.0.0', '1.1.0']" in sql
    assert "v_contract_version is null" in sql
    assert "johanna-precheckout-whatsapp-disclosure-v1" in sql
    assert "observed_precheckout_consent_mismatch" in sql


def test_v1_1_authority_is_derived_from_exact_canonical_evidence() -> None:
    sql = _sql()

    assert "{consent,marketing_optin}" in sql
    assert "{consent,whatsapp_contact}" in sql
    assert "{consent,copy_version}" in sql
    assert "{identity,phone_valid}" in sql
    assert "{assurance,activation_authorized}" in sql
    assert "v_contact_authorized" in sql
    assert "v_activation_authorized" in sql


def test_signed_raw_identity_is_bound_to_the_canonical_identity() -> None:
    sql = _sql()

    assert "{created_at}" in sql
    assert "{source,site}" in sql
    assert "{source,landing_id}" in sql
    assert "{data,buyer,email}" in sql
    assert "{data,buyer,phone_country_code}" in sql
    assert "{data,buyer,phone_national}" in sql
    assert "{data,product,hotlink}" in sql
    assert "{data,offer,code}" in sql
    assert "{data,checkout_url}" in sql
    assert "{dedupe_key}" in sql
    assert "observed_precheckout_raw_canonical_mismatch" in sql
    assert "observed_precheckout_identity_mismatch" in sql


def test_authorized_admission_persists_version_and_promotes_consistent_intent() -> None:
    sql = _sql()

    assert "p_canonical_payload #>> '{contract_version}'" in sql
    assert "v_contract_version" in sql
    assert "v_contact_authorized" in sql
    assert "v_activation_authorized" in sql
    assert "whatsapp_contact_authorized = case" in sql
    assert "activation_authorized = case" in sql
    assert "current_classification = 'identity_conflict'" in sql


def test_resolved_abandonment_preserves_prior_v1_1_activation_authority() -> None:
    sql = _sql()

    assert "preserve_resolved_abandonment_authority" in sql
    assert "hotmart_intent_correlator_missing" in sql
    assert "hotmart_resolved_abandonment_authority_marker_mismatch" in sql
    assert "v_occurrences <> 1" in sql
    assert "execute replace(v_definition, v_old, v_new)" in sql


def test_migration_preserves_closed_rpc_acl_and_has_no_commercial_effects() -> None:
    sql = _sql()

    assert "security definer" in sql
    assert "set search_path = pg_catalog, public, pg_temp" in sql
    assert "revoke all on function public.admit_observed_lead_precheckout" in sql
    assert "grant execute on function public.admit_observed_lead_precheckout" in sql
    for forbidden in (
        "scheduled_actions",
        "followup_delivery_attempts",
        "messages",
        "recovery_cases",
        "chatwoot",
    ):
        assert forbidden not in sql
