"""El reciclado de un telefono de prueba en la base (20261102000400, E-270, F-147 PR 1), leido como texto.

Lo que hace lo prueba ``tests/sql/followup_engine/validate_identity_recycle.mjs`` con las RPC reales sobre
la cadena entera; aca, la forma del archivo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261102000400_identity_recycle_v1.sql"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_it_is_one_transaction_with_lock_timeout_the_last_and_does_not_run_twice() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # Despues, los rechazos de Meta (20261103000500, E-284).
    # El historial de la pagina de aprobacion (20261103000600, E-295).
    assert names[-1] == "20261103000600_flow_approval_history_v1.sql"
    assert names[-2] == "20261103000500_meta_delivery_rejections_v1.sql"
    assert names[-3] == MIGRATION.name
    assert names[-4] == "20261102000300_flow_engine_native_silence_sweep_v1.sql"
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'identity_recycle_already_applied'" in sql
    assert "message = 'identity_recycle_prerequisite_missing'" in sql


def test_it_never_turns_triggers_off_and_never_deletes() -> None:
    code = re.sub(r"--[^\n]*", "", _sql()).lower()
    assert "session_replication_role" not in code
    assert "disable trigger" not in code
    assert re.search(r"\bdelete\s+from\b", code) is None
    assert "drop " not in code and "alter role" not in code and "create role" not in code


def test_the_two_immutability_functions_are_patched_by_counted_replacement() -> None:
    sql = _sql()
    assert "create or replace function public.protect_" not in sql
    assert "pg_temp.identity_recycle_patch(\n        'public.protect_hotmart_purchase_intent_correlation()'" in sql
    assert "pg_temp.identity_recycle_patch(\n        'public.protect_hotmart_cart_source_event()'" in sql
    assert "unexpected_immutability_definition" in sql
    # Solo la tabla de identidades del evento, no las correlaciones que comparten la funcion.
    assert "tg_table_name = ''hotmart_purchase_intent_event_identities''" in sql


def test_each_role_gets_only_its_functions_and_never_the_agent() -> None:
    sql = _sql()
    acl = sql[sql.index("do $acl$"):]
    page = acl[acl.index("to_regrole('flow_change_approver') is not null"):acl.index("to_regrole('service_role') is not null")]
    service = acl[acl.index("to_regrole('service_role') is not null"):]
    assert re.findall(r"'public\.([a-z_0-9]+)\(", page) == [
        "approver_identity_recycle_preview_v1", "approver_identity_recycle_apply_v1",
        "approver_get_identity_recycle_v1", "approver_identity_recycle_undo_v1"]
    assert re.findall(r"function public\.([a-z_0-9]+)\(", service) == ["purge_identity_recycles_v1"]
    assert "to automation_expert_change" not in sql
    assert "to anon" not in sql and "to authenticated" not in sql


def test_the_validator_is_the_last_of_the_npm_chain() -> None:
    chain = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.rstrip().endswith(" && node validate_flow_engine_native_silence_sweep.mjs"
                                   " && node validate_identity_recycle.mjs && node validate_meta_delivery_rejections.mjs && node validate_flow_approval_history.mjs")
