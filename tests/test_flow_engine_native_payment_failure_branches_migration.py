"""Las cuatro ramas del pago rechazado nativo en la base (20261103000900, E-306, flow-engine-v2 §27), leidas como texto.

El comportamiento lo prueba ``tests/sql/followup_engine/validate_flow_engine_native_payment_failure_branches.mjs``
con la cadena entera en PGlite. Aca se fija lo que no tiene que romperse en silencio:

* es una sola transaccion con ``lock_timeout``, la ultima de la cadena, y aborta si ya corrio;
* cada funcion viva se cambia con un reemplazo contado, con su marcador, y falla con 55000 si el texto no
  esta exactamente una vez;
* lo que el bridge lee (los resultados del registro, los datos y sus valores) es lo que conoce el cliente;
* la tabla de las elecciones nace con RLS y sin roles de la API, y las entradas son solo de ``service_role``;
* el validador de PGlite es el ultimo de la cadena de npm.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bridge.supabase import NATIVE_BRANCH_RECORD_OUTCOMES, NATIVE_PAYMENT_FAILURE_DATA_VALUES

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261103000900_flow_engine_native_payment_failure_branches_v1.sql"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(line for line in _sql().splitlines() if not line.lstrip().startswith("--"))


def test_it_is_one_transaction_with_lock_timeout_and_the_last() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    assert names[-1] == MIGRATION.name
    assert names[-2] == "20261103000700_customer_status_v1.sql"
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'flow_engine_native_payment_failure_branches_already_applied'" in sql


def test_every_live_function_is_patched_with_a_counted_replacement() -> None:
    code = " ".join(_code().split())
    for message in ("unexpected_flow_engine_native_unsupported_definition", "unexpected_flow_engine_native_steps_definition",
                    "unexpected_flow_engine_compile_definition", "unexpected_flow_engine_sends_definition",
                    "unexpected_recovery_checkout_issuance_definition", "unexpected_flow_sales_definition",
                    "unexpected_flow_engine_execution_context_definition"):
        assert f"message = '{message}'" in code, message
    assert code.count("length(v_text) - length(replace(v_text, ") >= 9
    # El contexto se vuelve a crear desde la definicion que conoce (20261028000200).
    assert "drop function public.flow_engine_execution_context_v1(uuid);" in code
    assert "position('flow_engine_native_cart_permission' in v_definition) = 0" in code


def test_what_the_base_writes_is_what_the_bridge_knows() -> None:
    sql = _sql()
    assert set(re.findall(r"select '([a-z_]+)'::text, v_(?:record\.)?template", sql)) == set(NATIVE_BRANCH_RECORD_OUTCOMES)
    values = json.loads(re.search(r"c_values constant jsonb :=\s*'(\{.*?\})';", sql, re.S).group(1))
    assert {datum: frozenset(listed) for datum, listed in values.items()} == NATIVE_PAYMENT_FAILURE_DATA_VALUES
    for datum, listed in NATIVE_PAYMENT_FAILURE_DATA_VALUES.items():
        assert f"{datum} text not null check ({datum} in (" in sql
        for value in listed:
            assert f"'{value}'" in sql


def test_the_choices_table_and_the_acl() -> None:
    code = _code()
    assert "alter table public.flow_engine_native_branch_choices enable row level security;" in code
    assert "execute function public.reject_flow_engine_native_mutation();" in code
    assert "revoke all on table public.flow_engine_native_branch_choices from %I" in code
    assert "execute format('grant execute on function %s to service_role', v_function);" in code
    entry = code[code.index("v_entrypoints constant regprocedure[]"):code.index("v_private constant")]
    assert "get_flow_engine_native_payment_failure_context_v1" in entry
    assert "record_flow_engine_native_branch_v1" in entry
    assert "flow_engine_execution_context_v1" in entry


def test_the_validator_is_the_last_of_the_npm_chain() -> None:
    chain = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.rstrip().endswith(
        " && node validate_customer_status.mjs && node validate_flow_engine_native_payment_failure_branches.mjs --check")
