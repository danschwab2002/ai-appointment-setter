"""Los frenos del seguimiento en la base (20261101000300, E-263), leidos como texto.

Lo que hacen lo prueba ``tests/sql/followup_engine/validate_followup_stops.mjs``
con las RPC reales sobre la cadena entera; aca, la forma de la migracion y que
el bridge conoce lo que escribe.
"""

from __future__ import annotations

import re
from pathlib import Path

from bridge.supabase import FOLLOWUP_CLAIM_OUTCOMES

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
MIGRATION = (
    MIGRATIONS / "20261101000300_followup_stops_after_last_touch_and_pending_payment_v1.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_it_is_one_transaction_with_lock_timeout_and_the_last() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    assert names[-1] == MIGRATION.name
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'followup_stops_already_applied'" in sql
    assert "message = 'unexpected_followup_claim_definition'" in sql


def test_every_outcome_it_writes_is_known_to_the_bridge() -> None:
    written = set(re.findall(r"outcome := '([a-z_]+)';", _sql()))
    assert written == {"blocked_after_last_touch", "blocked_pending_payment"}
    assert written <= FOLLOWUP_CLAIM_OUTCOMES


def test_it_patches_only_the_two_claims_and_creates_nothing() -> None:
    code = re.sub(r"--[^\n]*", "", _sql())
    # La unica funcion que crea es el helper temporal de la sesion.
    assert re.findall(r"create (?:or replace )?function ([\w.]+)", code) == [
        "pg_temp.followup_stops_patch"
    ]
    assert "claim_portable_conversation_followup_v1(" in code
    assert "claim_flow_engine_silence_v1(" in code
    assert "claim_conversation_followup_v1(" not in code.replace(
        "claim_portable_conversation_followup_v1(", ""
    )
    for forbidden in ("alter table", "create table", "insert into", "update public.", "delete from"):
        assert forbidden not in code.lower(), forbidden


def test_it_says_that_it_changes_what_0800_said() -> None:
    sql = _sql()
    assert "20261019000800" in sql
    assert "A PROPOSITO" in sql
