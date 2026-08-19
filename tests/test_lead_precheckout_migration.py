"""Contract tests for observed lead.precheckout durable admission."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260818000200_observed_lead_precheckout.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_observed_admission_is_separate_idempotent_and_effect_free() -> None:
    sql = _sql()

    assert "function public.admit_observed_lead_precheckout" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "semantic_conflict" in sql
    assert "observed_precheckout_conflict_fingerprint_collision" in sql
    assert "'1.0.0'" in sql
    assert "provider_observed" in sql
    assert "activation_authorized" in sql
    assert "scheduled_actions" not in sql
    assert "recovery_cases" not in sql


def test_observed_admission_requires_email_but_allows_invalid_phone_as_null() -> None:
    sql = _sql()

    assert "alter column normalized_phone drop not null" in sql
    assert "normalized_phone is null or normalized_phone ~" in sql
    assert "v_email is null" in sql
    assert "v_phone is not null and v_phone !~" in sql


def test_observed_admission_revalidates_no_contact_authority_and_assurance() -> None:
    sql = _sql()

    assert "{consent,marketing_optin}" in sql
    assert "{consent,whatsapp_contact}" in sql
    assert "is distinct from 'false'" in sql
    assert "{assurance,provider_observed}" in sql
    assert "is distinct from 'true'" in sql
    assert "{assurance,activation_authorized}" in sql


def test_observed_admission_is_pinned_to_confirmed_pilot_scope() -> None:
    sql = _sql()

    for value in ("lancemos", "psicologajohanna", "ads-a", "f106691755g", "bxjge6zq"):
        assert value in sql
    assert "{commerce,price}" in sql
    assert "is distinct from '49'" in sql
    assert "{commerce,currency}" in sql
    assert "is distinct from 'usd'" in sql


def test_observed_rpc_is_service_role_only() -> None:
    sql = _sql()

    assert "security definer\nset search_path = pg_catalog, public, pg_temp" in sql
    assert "revoke all on function public.admit_observed_lead_precheckout" in sql
    assert "grant execute on function public.admit_observed_lead_precheckout" in sql
    assert "to service_role" in sql


def test_observed_admission_correlates_email_or_phone_without_duplicate_intents() -> None:
    sql = _sql()

    assert "v_email_intent_id" in sql
    assert "v_phone_intent_id" in sql
    assert "pi.normalized_email = v_email" in sql
    assert "pi.normalized_phone = v_phone" in sql
    assert "v_email_intent_id is not null" in sql
    assert "v_phone_intent_id is not null" in sql
    assert "current_classification = 'identity_conflict'" in sql
    assert "'email'," in sql
    assert "'phone'," in sql
    assert "order by pi.id\n    for update" in sql
    assert "normalized_phone = case" in sql
    assert "when normalized_phone is null" in sql
    assert "and v_phone_intent_id is null" in sql
