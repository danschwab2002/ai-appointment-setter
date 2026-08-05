"""Contract tests for durable WhatsApp identity binding."""

from __future__ import annotations

import re
from pathlib import Path


MIGRATION = (
    Path(__file__).parents[1]
    / "supabase"
    / "migrations"
    / "20260804000200_followup_identity_binding.sql"
)


def _sql() -> str:
    assert MIGRATION.exists(), "missing follow-up identity binding migration"
    return re.sub(
        r"\s+", " ", MIGRATION.read_text(encoding="utf-8").lower()
    ).strip()


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value)


def test_identity_binding_is_transactional_fenced_and_idempotent() -> None:
    sql = _sql()

    assert sql.startswith("begin;") and sql.endswith("commit;")
    assert (
        "create or replace function public.plan_cart_recovery_with_identity("
        in sql
    )
    assert "p_chatwoot_account_id bigint" in sql
    assert "p_chatwoot_inbox_id bigint" in sql
    assert "p_external_user_id text" in sql
    assert "p_abandoned_at timestamptz" in sql
    assert "external_user_id ~ '^[0-9]+$'" in sql
    assert "'chatwoot:' || p_chatwoot_account_id::text" in sql
    assert "jsonb_build_object('inbox_id', p_chatwoot_inbox_id)" in sql
    assert "exception when unique_violation" in sql

    contact_lock = sql.index("from public.contacts c")
    case_lock = sql.index("from public.recovery_cases rc")
    assert contact_lock < case_lock
    assert "for update" in sql[contact_lock:case_lock]
    assert "for update" in sql[case_lock:]

    assert "channel_identity_contact_mismatch" in sql
    assert "channel_identity_not_active" in sql
    assert "channel_identity_inbox_mismatch" in sql
    assert "selected_channel_identity_id = v_identity.id" in sql
    assert "identity_resolution_status = 'resolved'" in sql


def test_identity_binding_is_private_to_the_service_role() -> None:
    sql = _sql()
    signature = (
        "public.plan_cart_recovery_with_identity("
        "uuid, uuid, text, text, text, text, integer, timestamptz, "
        "bigint, bigint, text)"
    )

    compact_sql = _compact(sql)
    assert _compact(f"revoke execute on function {signature} from public") in compact_sql
    for role in ("anon", "authenticated"):
        assert f"rolname = '{role}'" in sql
        assert _compact(
            f"revoke execute on function {signature} from {role}"
        ) in compact_sql
    assert "rolname = 'service_role'" in sql
    assert _compact(
        f"grant execute on function {signature} to service_role"
    ) in compact_sql
