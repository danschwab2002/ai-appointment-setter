"""El motor nativo en la base (migracion 20261024000200, el PR 2 del hito 4).

Lo que se puede leer en el texto de la migracion. El comportamiento lo prueba
tests/sql/followup_engine/validate_flow_engine_native.mjs contra la cadena
completa (PGlite), con un rojo a proposito por pieza.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261024000200_flow_engine_native_v1.sql"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"

# Las funciones vigentes que la migracion parcha por ancla, y su marca.
PATCHED = {
    "public.validate_flow_engine_route_insert()": "flow_engine_native_route: begin",
    "public.validate_flow_engine_route_update()": "flow_engine_native_route: begin",
    "public.operator_route_flow_v1(jsonb,text,text,text,bigint,text,text,jsonb)": "flow_engine_native_route: begin",
    "public.validate_flow_control_change()": "flow_engine_native_cohort: begin",
    "public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)": "flow_engine_native_shape",
    "public._flow_engine_authorize_start(uuid,uuid,timestamptz,boolean)": "flow_engine_native_start: begin",
    "public.flow_engine_execution_context_v1(uuid)": "flow_engine_native_steps",
    "public.read_flow_engine_v1(jsonb,text)": "flow_engine_native_excluded",
    "public.read_flow_engine_routes_v1(jsonb)": "flow_engine_native_excluded",
    "public.read_flow_engine_history_v1(jsonb,text,timestamptz)": "flow_engine_native_excluded",
}


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return re.sub(r"--[^\n]*", "", _sql())


def test_it_is_one_transaction_and_the_last() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # Despues va solo F-036 (20261024000400), que parcha el link del agente.
    assert names[-2] == MIGRATION.name
    assert names[-1] == "20261024000400_opted_out_reply_checkout_issuance.sql"
    assert names[-3] == "20261024000100_portable_hotmart_listen_only_capture.sql"
    # Correrla dos veces aborta, y necesita el camino del motor del carrito.
    assert "flow_engine_native_already_applied" in sql
    assert "flow_engine_motor_missing" in sql


def test_the_patches_go_by_anchor_on_the_current_definition() -> None:
    code = _code()
    # Ninguna funcion vigente se reescribe a mano: los reemplazos son por
    # ancla sobre pg_get_functiondef, con el duenio verificado.
    assert "create or replace function" not in code.lower()
    assert "pg_get_functiondef" in code
    for reason in ("flow_engine_native_owner_mismatch", "unexpected_flow_engine_native_definition",
                   "flow_engine_native_definition_not_replaced", "flow_engine_native_function_missing"):
        assert reason in code
    for signature, marker in PATCHED.items():
        assert f"'{signature}'" in code, signature
        assert marker in code, marker
    # La compra que dispara un flujo nativo no se frena sin tocar el freno: lo
    # mide el validador de PGlite.
    assert "_portable_case_purchase_stop_reason(uuid)'" not in code


def test_only_the_admission_is_granted_and_the_tables_are_closed() -> None:
    code = _code()
    grants = re.findall(r"grant execute on function ([^\s]+\([^)]*\)) to (\w+)", code)
    assert grants == [("public.admit_flow_engine_event_v1(jsonb, jsonb)", "service_role")]
    for table in ("flow_engine_pools", "flow_cohort_members", "flow_engine_native_runs"):
        assert f"create table public.{table}" in code
        assert f"alter table public.{table} enable row level security" in code
        assert f"public.{table}" in code.split("revoke all on table", 1)[1]


def test_the_native_route_and_the_pool_rule_are_in_the_base() -> None:
    code = _code()
    assert "flow_engine_routes_native_shape" in code
    assert "alter column legacy_kind drop not null" in code
    assert "'apagado', 'prendido', 'pausado', 'cohorte'" in code
    # Una bolsa nativa no se llama como un scope, y un scope no se llama como
    # una bolsa: los dos lados.
    assert "flow_engine_pool_is_legacy_scope" in code
    assert "pilot_scope_is_native_pool" in code
    assert "before insert on public.pilot_scope_versions" in code
    # La ruta nativa no se mueve.
    assert code.count("flow_route_native_cannot_move") >= 2


def test_the_native_shape_is_one_send_and_the_legacy_shape_is_untouched() -> None:
    code = _code()
    shape = re.search(r"when 'nativo:compra' then\s*'(\[.*?\])'::jsonb", code, re.S)
    assert shape is not None
    steps = json.loads(shape.group(1))
    assert [step["step_key"] for step in steps] == ["first_contact"]
    assert steps[0]["variables"] == ["nombre", "producto"]
    # La forma del legado (_flow_engine_legacy_steps) no se redefine: la del
    # nativo va con un coalesce y da null para todo legacy_kind.
    assert "_flow_engine_legacy_steps(text)'" not in code
    assert "coalesce(public._flow_engine_legacy_steps(p_legacy_kind)" in code


def test_the_validator_is_in_the_npm_chain() -> None:
    chain = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.rstrip().endswith(
        "node validate_flow_engine_native.mjs"
        " && node validate_opted_out_reply_issuance.mjs"
    )
