"""Contract tests for the operator-owned proactive lead bootstrap."""

from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/20260825000100_proactive_lead_identity_bootstrap.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_bootstrap_requires_authorized_v1_1_intent_and_quiescent_runtime() -> None:
    sql = _sql()

    assert "create or replace function public.bootstrap_proactive_lead_identity" in sql
    assert "contract_version = '1.1.0'" in sql
    assert "whatsapp_contact_authorized" in sql
    assert "activation_authorized" in sql
    assert "provider_observed" in sql
    assert "runtime_state not in ('inactive', 'paused')" in sql


def test_bootstrap_reuses_exact_waba_owner_and_rejects_conflicts() -> None:
    sql = _sql()

    assert "create table public.proactive_lead_bootstrap_targets" in sql
    assert "p_channel_identity_id" not in sql
    assert "identity.external_user_id is distinct from intent.normalized_phone" in sql
    assert "identity.account_id is distinct from 'chatwoot:' || scope.chatwoot_account_id::text" in sql
    assert "identity.metadata ->> 'inbox_id'" in sql
    assert "proactive_bootstrap_channel_identity_mismatch" in sql
    assert "proactive_bootstrap_phone_owner_mismatch" in sql
    assert "proactive_bootstrap_phone_ambiguous" in sql
    assert "contact_permission in ('opted_out', 'blocked', 'restricted')" in sql
    assert "lifecycle_status = 'do_not_contact'" in sql


def test_bootstrap_attaches_phone_and_enrols_one_contact() -> None:
    sql = _sql()

    assert "insert into public.contact_points" in sql
    assert "source, verification_status" in sql
    assert "'system', 'verified'" in sql
    assert "from public.set_lancemos_pilot_cohort_member" in sql
    assert "p_target_status" not in sql
    assert "'active'" in sql


def test_bootstrap_is_command_idempotent_and_effect_free() -> None:
    sql = _sql()

    assert "create table public.proactive_lead_identity_bootstrap_commands" in sql
    assert "command_key text primary key" in sql
    assert "semantic_fingerprint" in sql
    assert "proactive_bootstrap_command_conflict" in sql
    assert "proactive_bootstrap_completed" in sql
    for forbidden in (
        "insert into public.recovery_cases",
        "insert into public.scheduled_actions",
        "insert into public.followup_delivery_attempts",
        "insert into public.messages",
        "request_started",
    ):
        assert forbidden not in sql


def test_bootstrap_rpc_is_service_role_only() -> None:
    sql = _sql()

    assert "security definer" in sql
    assert "set search_path = pg_catalog, public, pg_temp" in sql
    assert "revoke all on function public.bootstrap_proactive_lead_identity" in sql
    assert "if exists (select 1 from pg_roles where rolname = 'anon')" in sql
    assert "if exists (select 1 from pg_roles where rolname = 'authenticated')" in sql
    assert ") from anon, authenticated;" not in sql
    assert "revoke all on function public.protect_proactive_lead_identity_bootstrap_command()" in sql
    assert "from service_role" in sql
    assert "grant execute on function public.bootstrap_proactive_lead_identity" in sql
