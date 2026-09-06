from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/20260831000300_johanna_six_landing_precheckout.sql"
)

EXPECTED_PAIRS = {
    ("ads-a", "bxjge6zq"),
    ("ads-b", "mgbgpp19"),
    ("ads-c", "s1qfxm7m"),
    ("org-a", "jtt6fcsm"),
    ("org-b", "ecyu87q0"),
    ("org-c", "ulhzpw9a"),
}


def _sql() -> str:
    return MIGRATION.read_text().lower()


def test_migration_declares_the_exact_six_landing_offer_pairs() -> None:
    sql = _sql()

    for landing_ref, offer_ref in EXPECTED_PAIRS:
        assert f"('{landing_ref}', '{offer_ref}')" in sql

    assert "johanna_precheckout_landing_offers" in sql
    assert "primary key (landing_ref, offer_ref)" in sql
    assert "published_johanna_precheckout_pair_is_immutable" in sql


def test_migration_expands_correlation_and_timer_bindings_without_wildcards() -> None:
    sql = _sql()

    assert "insert into public.hotmart_purchase_intent_scopes" in sql
    assert "insert into public.hotmart_abandonment_timer_policy_bindings" in sql
    assert "offer_ref is null" not in sql
    assert "offer_ref = '*'" not in sql
    assert "active = true" in sql
    assert "precheckout_first_touch_enabled = true" in sql
    assert "precheckout_first_touch_enabled is distinct from true" in sql


def test_migration_replaces_all_precheckout_effect_gates_with_pair_authority() -> None:
    sql = _sql()

    assert "create or replace function public.is_johanna_precheckout_pair" in sql
    assert "create or replace function public.admit_observed_lead_precheckout" in sql
    assert "create or replace function public.schedule_precheckout_first_touch_reevaluation" in sql
    assert "create or replace function public._reevaluate_precheckout_delayed_first_touch" in sql
    assert "create or replace function public.get_precheckout_delayed_one_shot_command" in sql
    assert sql.count("public.is_johanna_precheckout_pair(") >= 5


def test_migration_keeps_identity_consent_and_stop_guards() -> None:
    sql = _sql()

    required_guards = (
        "identity_conflict",
        "activation_authorized",
        "whatsapp_contact_authorized",
        "contact_opt_out_events",
        "human_takeover",
        "request_started",
        "delivery_unknown",
    )
    for guard in required_guards:
        assert guard in sql


def test_migration_admits_invalid_phone_without_granting_effect_authority() -> None:
    sql = _sql()

    assert "v_phone_contact_authorized" in sql
    assert "p_raw_payload #>> '{data,consent,whatsapp_contact}' is distinct from 'true'" in sql
    assert "v_phone_contact_authorized := (v_phone is not null)" in sql
    assert "v_contact_authorized := v_phone_contact_authorized" in sql
    assert "v_activation_authorized := v_phone_contact_authorized" in sql


def test_readiness_covers_all_exact_rows_and_preserves_inactive_zero_contract() -> None:
    sql = _sql()

    assert "create or replace function public.get_precheckout_delayed_first_touch_readiness" in sql
    assert "count(*) = 6" in sql
    assert "bool_and(binding.enabled)" in sql
    assert "bool_and(binding.precheckout_first_touch_enabled)" in sql
    assert "v_runtime_state is distinct from 'inactive'" in sql
    assert "v_runtime_generation is distinct from 0" in sql
    assert "max_cohort_contacts = 1000000" in sql


def test_migration_does_not_backfill_or_resend_historical_effects() -> None:
    sql = _sql()

    assert "insert into public.johanna_abandonment_one_shot_commands" not in sql
    assert "insert into public.outbound_attempts" not in sql
    assert "update public.hotmart_abandonment_reevaluations set status = 'scheduled'" not in sql
