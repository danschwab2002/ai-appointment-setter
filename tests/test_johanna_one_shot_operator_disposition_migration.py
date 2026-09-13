from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260913000100_johanna_one_shot_operator_disposition.sql"
PACKAGE = ROOT / "tests/sql/followup_engine/package.json"
ACL = ROOT / "scripts/supabase_acl_inventory.sql"
SCHEMA = ROOT / "scripts/supabase_schema_inventory.sql"


def _sql() -> str:
    assert MIGRATION.exists(), "operator-disposition migration is missing"
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_disposition_is_append_only_and_does_not_rewrite_delivery_history() -> None:
    sql = _sql()

    assert "create table public.johanna_one_shot_operator_dispositions" in sql
    assert "command_id uuid not null unique" in sql
    assert "references public.johanna_abandonment_one_shot_commands(id) on delete restrict" in sql
    assert "disposition_code = 'unverifiable_external_contact_deleted'" in sql
    assert "evidence_code = 'operator_confirmed_chatwoot_contact_deleted'" in sql
    assert "semantic_fingerprint" in sql
    assert "before update or delete on public.johanna_one_shot_operator_dispositions" in sql
    assert "operator_disposition_immutable" in sql
    assert "update public.johanna_abandonment_one_shot_commands" not in sql


def test_rpc_fails_closed_on_any_command_that_could_still_have_provider_evidence() -> None:
    sql = _sql()

    signature = "public.resolve_johanna_one_shot_unverifiable_contact_deleted(uuid, text)"
    assert f"function {signature}" in sql
    assert "command.status <> 'delivery_unknown'" in sql
    assert "command.failure_code is distinct from 'chatwoot_http_error'" in sql
    assert "command.chatwoot_conversation_id is not null" in sql
    assert "command.chatwoot_message_id is not null" in sql
    assert "command.finalized_at > clock_timestamp() - interval '24 hours'" in sql
    assert "command.timer_source_kind <> 'precheckout_intent'" in sql
    assert "command.timer_status <> 'completed'" in sql
    assert "command.timer_outcome <> 'command_reserved'" in sql
    assert "operator_ref !~ '^[a-z0-9][a-z0-9:_-]{2,63}$'" in sql
    assert "johanna_one_shot_operator_disposition_ineligible" in sql


def test_exact_replay_is_idempotent_and_changed_operator_conflicts() -> None:
    sql = _sql()

    assert "pg_advisory_xact_lock" in sql
    assert "for update of candidate, timer" in sql
    assert "existing.semantic_fingerprint is distinct from fingerprint" in sql
    assert "johanna_one_shot_operator_disposition_conflict" in sql
    assert "'recorded'::text" in sql
    assert "'replay'::text" in sql


def test_disposed_command_is_frozen_before_historical_exceptions() -> None:
    sql = _sql()

    protector = sql.index(
        "create or replace function public.protect_johanna_abandonment_one_shot_command()"
    )
    disposed_guard = sql.index("johanna_abandonment_one_shot_disposed_immutable")
    retry_exception = sql.index("app.johanna_one_shot_invalid_contact_retry")
    reconciliation_exception = sql.index("app.johanna_one_shot_reconciliation")
    assert protector < disposed_guard < retry_exception < reconciliation_exception
    assert "if old.status = 'reserved'" in sql
    assert "and new.status = 'request_started'" in sql
    assert "if old.status in ('reserved', 'request_started')" in sql


def test_readiness_counts_only_undisposed_delivery_unknown_rows() -> None:
    sql = _sql()

    assert "create or replace function public.get_precheckout_delayed_first_touch_readiness()" in sql
    assert "not exists" in sql
    assert "from public.johanna_one_shot_operator_dispositions disposition" in sql
    assert "disposition.command_id = command.id" in sql
    assert "select count(*) = 6" in sql


def test_rpc_and_table_are_closed_to_direct_api_access() -> None:
    sql = _sql()
    signature = "public.resolve_johanna_one_shot_unverifiable_contact_deleted(uuid, text)"

    assert "alter table public.johanna_one_shot_operator_dispositions enable row level security" in sql
    assert "revoke all on table public.johanna_one_shot_operator_dispositions from public" in sql
    assert "revoke all on table public.johanna_one_shot_operator_dispositions from anon" in sql
    assert "revoke all on table public.johanna_one_shot_operator_dispositions from authenticated" in sql
    assert "revoke all on table public.johanna_one_shot_operator_dispositions from service_role" in sql
    assert f"revoke all on function {signature} from public" in sql
    assert f"revoke all on function {signature} from anon" in sql
    assert f"revoke all on function {signature} from authenticated" in sql
    assert f"grant execute on function {signature} to service_role" in sql
    assert "security definer" in sql
    assert "set search_path = pg_catalog, public, pg_temp" in sql


def test_validator_and_inventories_register_the_new_boundary() -> None:
    validator = "validate_johanna_one_shot_operator_disposition.mjs"
    signature = "resolve_johanna_one_shot_unverifiable_contact_deleted"

    assert validator in PACKAGE.read_text(encoding="utf-8")
    assert signature in ACL.read_text(encoding="utf-8")
    assert "20260913000100_johanna_one_shot_operator_disposition.sql" in SCHEMA.read_text(
        encoding="utf-8"
    )
