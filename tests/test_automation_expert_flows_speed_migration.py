"""La lectura de /flows del Automation Expert sin timeout (20261021000400).

read_automation_expert_flows_v1 leia conversation_events entera por cada
accion en tres lugares (10,5 s en att1-db el 22/10, sobre un statement_timeout
de 8 s en el bridge). La migracion la reemplaza con un CTE que lee las
reevaluaciones de la ventana una sola vez. Estas pruebas fijan la forma: que
solo cambia la consulta de lo observado, y nada mas de la funcion. La salida
igual a la de antes la verifica validate_automation_expert_read.mjs en PGlite,
que recrea la version anterior y compara las dos sobre los mismos datos.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261021000400_automation_expert_flows_speed_v1.sql"
ORIGINAL = MIGRATIONS / "20261015000200_automation_expert_read_v1.sql"

START = "create or replace function public.read_automation_expert_flows_v1("
END = "$function$;"
OBSERVED_START = "    -- Lo observado en la ventana, solo conteos y medianas.\n"
OBSERVED_END = "      into v_observed;\n"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _function(sql: str) -> str:
    start = sql.index(START)
    return sql[start:sql.index(END, start) + len(END)]


def _outside_observed(function: str) -> str:
    start = function.index(OBSERVED_START)
    return function[:start] + function[function.index(OBSERVED_END, start):]


def test_it_is_one_transaction_and_the_last_migration() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    assert names[-1] == MIGRATION.name
    # Va antes de la reserva de la T2.3b (20261022000100): si esta entra
    # primero, el orden de los archivos y el de aplicacion en ATT1 coinciden.
    assert MIGRATION.name < "20261022000100"


def test_it_only_replaces_the_flows_function() -> None:
    code = re.sub(r"--[^\n]*", "", _sql())
    assert code.count("create or replace function") == 1
    assert "create function" not in code
    assert not re.search(r"\b(grant|revoke|alter|drop|insert|update|delete)\b", code, re.I)


def test_only_the_observed_query_changes() -> None:
    before = _function(ORIGINAL.read_text(encoding="utf-8"))
    after = _function(_sql())
    assert _outside_observed(after) == _outside_observed(before)


def test_the_reevaluations_are_read_once() -> None:
    after = _function(_sql())
    observed = after[after.index(OBSERVED_START):after.index(OBSERVED_END)]
    assert observed.count("from public.conversation_events") == 1
    assert "    reevaluated as materialized (\n" in observed
    for name in ("authorized", "deferred", "stopped", "superseded"):
        assert f"    {name} as materialized (\n" in observed, name
    # Los mismos filtros que antes, ahora sobre las columnas del CTE.
    assert "where event.event_type = 'followup_action_reevaluated'" in observed
    assert "and event.created_at >= v_window_from and event.created_at < v_window_to" in observed
    assert "where event.decision = 'defer'" in observed
    assert "where event.decision in ('cancel', 'pause', 'escalate')" in observed
    assert "and event.reason_code = 'superseded_by_provider_event'" in observed


def test_it_checks_the_owner_and_the_permissions() -> None:
    sql = _sql()
    assert sql.index("automation_expert_flows_owner_mismatch") < sql.index(START)
    assert sql.index(START) < sql.index("automation_expert_flows_acl_changed")
    for role in ("'public'", "'anon'", "'authenticated'", "'service_role'"):
        assert f"has_function_privilege({role}, 'public.read_automation_expert_flows_v1(jsonb)'" in sql
