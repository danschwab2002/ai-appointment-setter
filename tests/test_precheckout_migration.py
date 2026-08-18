"""Contract tests for provisional pre-checkout purchase-intent admission."""

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1]
    / "supabase/migrations/20260814000200_precheckout_purchase_intents.sql"
)


def test_precheckout_admission_is_atomic_idempotent_and_effect_free() -> None:
    sql = MIGRATION.read_text().lower()

    assert "create table public.precheckout_submissions" in sql
    assert "create table public.purchase_intents" in sql
    assert "function public.admit_precheckout_form_submission" in sql
    assert "pg_advisory_xact_lock" in sql
    assert "semantic_conflict" in sql
    assert "waiting_for_purchase" in sql
    assert "provider_observed" in sql
    assert "activation_authorized" in sql
    assert "scheduled_actions" not in sql
    assert "recovery_cases" not in sql


def test_precheckout_rpc_is_service_role_only() -> None:
    sql = MIGRATION.read_text().lower()

    assert "security definer\nset search_path = pg_catalog, public, pg_temp" in sql
    for role in ("public", "anon", "authenticated"):
        assert (
            "revoke all on function public.admit_precheckout_form_submission"
            in sql
        )
        assert role in sql
    assert (
        "grant execute on function public.admit_precheckout_form_submission"
        in sql
    )
    assert "to service_role" in sql
    for table in (
        "precheckout_submissions",
        "purchase_intents",
        "purchase_intent_submissions",
        "precheckout_submission_conflicts",
    ):
        assert f"revoke all on table public.{table} from service_role" in sql


def test_precheckout_acl_is_portable_when_supabase_roles_are_absent() -> None:
    sql = MIGRATION.read_text().lower()

    for role in ("anon", "authenticated", "service_role"):
        assert f"where rolname = '{role}'" in sql


def test_precheckout_rpc_revalidates_consent_and_durable_canonical_fields() -> None:
    sql = MIGRATION.read_text().lower()

    assert "jsonb_typeof(p_canonical_payload #> '{consent,whatsapp_contact}')" in sql
    assert "is distinct from 'boolean'" in sql
    for field in ("terms_accepted", "privacy_accepted", "whatsapp_contact"):
        assert (
            f"p_canonical_payload #>> '{{consent,{field}}}' is distinct from 'false'"
            in sql
        )


def test_precheckout_intent_uses_phone_as_the_required_identity() -> None:
    sql = MIGRATION.read_text().lower()

    assert "normalized_email text," in sql
    index = sql.split("create unique index purchase_intents_one_live_identity_idx", 1)[1]
    index = index.split("where lifecycle_state", 1)[0]
    assert "normalized_phone" in index
    assert "normalized_email" not in index
    assert "{consent,terms_accepted}" in sql
    assert "{consent,privacy_accepted}" in sql
    assert "{consent,copy_version}" in sql
    assert "{lead,full_name}" in sql


def test_repeated_submissions_attach_to_one_live_intent() -> None:
    sql = MIGRATION.read_text().lower()

    assert "create table public.purchase_intent_submissions" in sql
    assert "purchase_intents_one_live_identity_idx" in sql
    assert "where lifecycle_state = 'waiting_for_purchase'" in sql
    assert "insert into public.purchase_intent_submissions" in sql
    assert "select pi.id into v_purchase_intent_id" in sql
    assert (
        "whatsapp_contact_authorized = whatsapp_contact_authorized and"
        in sql
    )


def test_conflicting_replay_has_stable_fingerprint_and_collision_check() -> None:
    sql = MIGRATION.read_text().lower()

    assert "content_fingerprint text not null" in sql
    assert "unique (existing_submission_id, content_fingerprint)" in sql
    assert "precheckout_conflict_fingerprint_collision" in sql