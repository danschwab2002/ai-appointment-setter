from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260911000400_payment_link_attribution.sql"
)


def test_payment_link_migration_defines_durable_binding_and_send_command() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "create table public.payment_link_bindings" in sql
    assert "create table public.payment_link_send_commands" in sql
    assert "source_reevaluation_id uuid not null unique" in sql
    assert "source_submission_id uuid not null unique" in sql
    assert "sequence_origin_event_ulid text not null unique" in sql
    assert "checkout_url_original text not null" in sql
    assert "checkout_url_final text not null" in sql
    assert "tracking_field text not null" in sql
    assert "tracking_value text not null unique" in sql
    assert "tracking_prefix text not null" in sql
    assert "tracking_value = tracking_prefix || sequence_origin_event_ulid" in sql
    assert "foreign key (purchase_intent_id, source_submission_id)" in sql
    assert "trigger_external_message_id text not null" in sql


def test_payment_link_migration_defines_atomic_prepare_and_finalize_rpcs() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "function public.prepare_chatwoot_payment_link_send" in sql
    assert "function public.finalize_chatwoot_payment_link_send" in sql
    assert "for update" in sql
    assert "source_reevaluation_id" in sql
    assert "source_submission_id" in sql
    assert "external_submission_id" in sql
    assert "canonical_payload #>> '{commerce,checkout_url}'" in sql
    assert "make_interval(secs => p_max_age_seconds)" in sql
    assert "lifecycle_state <> 'waiting_for_purchase'" in sql
    assert "cmd.status = 'accepted_by_chatwoot'" in sql
    assert "array['src', 'xcod']" in sql
    assert "r.id = v_command.source_reevaluation_id" in sql
    assert "precheckout_sequence_not_latest" in sql
    assert "has_chatwoot_opt_out_stop" in sql


def test_each_new_precheckout_supersedes_the_old_sequence_without_mutating_it() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "'superseded_by_newer_precheckout'" in sql
    assert "public.schedule_precheckout_first_touch_reevaluation(uuid,uuid)" in sql
    assert "set status = 'completed'" in sql
    assert "outcome = 'superseded_by_newer_precheckout'" in sql
    replacement = sql.split("v_new text := $new$", 1)[1].split("$new$;", 1)[0]
    assert "set source_submission_id" not in replacement
    assert "remove_mutable_sequence_identity" in sql


def test_payment_link_migration_is_service_role_only_and_protects_rows() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "enable row level security" in sql
    assert "payment_link_binding_immutable" in sql
    assert "payment_link_send_command_immutable" in sql
    assert "revoke all on function public.prepare_chatwoot_payment_link_send" in sql
    assert "grant execute on function public.prepare_chatwoot_payment_link_send" in sql
    assert "to service_role" in sql
    assert "revoke all on public.payment_link_bindings" in sql
    assert "revoke all on public.payment_link_send_commands" in sql
