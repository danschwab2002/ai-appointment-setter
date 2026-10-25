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


def test_it_is_one_transaction_and_comes_before_the_motor() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    position = names.index(MIGRATION.name)
    assert names[position - 1] == "20261021000300_flow_engine_history_cap_v1.sql"
    # Despues va el camino del motor para el carrito (la T2.3b), que no vuelve
    # a definir read_automation_expert_flows_v1.
    # Y lo que el bridge del motor lee y marca (la T2.4), que tampoco la toca.
    # Las lecturas que cuentan lo del motor (20261022000300) si: le suman las
    # autorizaciones del motor por ancla, sobre esta definicion (lo prueba
    # test_flow_engine_reads_see_motor_migration.py).
    reads = "20261022000300_flow_engine_reads_see_motor_v1.sql"
    assert names[position + 1:] == [
        "20261022000100_flow_engine_motor_carrito_v1.sql",
        "20261022000200_flow_engine_bridge_reads_v1.sql",
        reads,
        # El modo «solo guardar» de Hotmart (20261024000100) solo crea una
        # funcion nueva y no redefine ninguna.
        "20261024000100_portable_hotmart_listen_only_capture.sql",
        # El motor nativo (20261024000200) suma funciones y tablas y parcha por
        # ancla, sobre la definicion vigente, las lecturas y guardas del motor.
        "20261024000200_flow_engine_native_v1.sql",
        # F-036 (20261024000400) parcha por ancla la reserva y la autorizacion
        # del link; no toca el motor.
        "20261024000400_opted_out_reply_checkout_issuance.sql",
        # El bridge del motor nativo (20261024000500) suma lecturas y parcha la
        # adopcion de la respuesta; no redefine el motor del carrito.
        "20261024000500_flow_engine_native_bridge_v1.sql",
        # El AE que crea flujos (20261024000600) parcha el ciclo del almacen (el item
        # nuevo) y read_flow_store_v1; no redefine el motor del carrito.
        "20261024000600_flow_change_new_flow_v1.sql",
        # Las lecturas nativas del AE (20261025000100) suman lecturas y parchan el anclaje del AE y prender.
        "20261025000100_automation_expert_native_reads_v1.sql",
    ]
    for later in names[position + 1:]:
        if later != reads:
            assert "read_automation_expert_flows_v1" not in (MIGRATIONS / later).read_text(encoding="utf-8")


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
