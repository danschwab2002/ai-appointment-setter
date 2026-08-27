from pathlib import Path


MIGRATION = Path(
    "supabase/migrations/20260827000200_chatwoot_invalid_contact_retry.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_invalid_contact_retry_is_bounded_and_exact() -> None:
    sql = _sql()

    assert "add column invalid_contact_retry_count integer not null default 0" in sql
    assert "check (invalid_contact_retry_count between 0 and 1)" in sql
    assert (
        "create or replace function "
        "public.prepare_johanna_payment_failure_invalid_contact_retry(" in sql
    )
    assert "command.failure_code is distinct from 'invalid_contact_id'" in sql
    assert "command.chatwoot_conversation_id is not null" in sql
    assert "command.chatwoot_message_id is not null" in sql
    assert "command.invalid_contact_retry_count <> 0" in sql
    assert "set invalid_contact_retry_count = 1" in sql
    assert "set case_status = 'outbound_started'" in sql
    assert "'not_retryable'::text" in sql
    assert "'retry_started'::text" in sql


def test_invalid_contact_retry_rpc_is_hardened_and_service_role_only() -> None:
    sql = _sql()
    signature = (
        "public.prepare_johanna_payment_failure_invalid_contact_retry"
        "(text,uuid,bigint,bigint)"
    )

    assert "security definer" in sql
    assert "set search_path = pg_catalog, public, pg_temp" in sql
    assert f"revoke all on function {signature} from public" in sql
    assert f"revoke all on function {signature} from anon" in sql
    assert f"revoke all on function {signature} from authenticated" in sql
    assert f"grant execute on function {signature} to service_role" in sql
