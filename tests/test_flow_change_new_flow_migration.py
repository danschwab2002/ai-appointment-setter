"""La migracion del AE que crea flujos (20261024000600, E-070), leida.

Lo que hace en una base lo prueba ``validate_flow_change_new_flow.mjs`` (PGlite,
al final de ``npm test``): la forma de la migracion, la conversacion de prueba
publicada en cohorte, lo que no se crea, la cohorte del operador, la admision y
la simulacion. Aca se fija lo que se lee sin base: una transaccion, la ultima
de la cadena, en la cadena de npm, y quien ejecuta que.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261024000600_flow_change_new_flow_v1.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return re.sub(r"--[^\n]*", "", _sql())


def test_it_is_one_transaction_and_the_last() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # Despues va solo la de las lecturas nativas del AE (20261025000100), que parcha prender sin tocar lo de esta.
    assert names[-1] == "20261025000100_automation_expert_native_reads_v1.sql"
    assert names[-2] == MIGRATION.name
    assert names[-3] == "20261024000500_flow_engine_native_bridge_v1.sql"


def test_the_validator_is_in_the_npm_chain() -> None:
    package = json.loads((ROOT / "tests" / "sql" / "followup_engine" / "package.json").read_text())
    assert package["scripts"]["test"].rstrip().endswith(
        "node validate_flow_change_new_flow.mjs && node validate_automation_expert_native_reads.mjs")


def test_only_the_simulation_read_is_granted_and_only_to_service_role_and_the_agent() -> None:
    grants = re.findall(r"grant execute on function public\.([a-z0-9_]+)\([^;]*\)\s+to ([a-z_]+);", _code())
    assert sorted(grants) == [
        ("read_flow_engine_native_trigger_counts_v1", "automation_expert_change"),
        ("read_flow_engine_native_trigger_counts_v1", "service_role"),
    ]
    assert "to anon" not in _code() and "to authenticated" not in _code()


def test_it_patches_by_anchor_and_does_not_create_roles_or_rows() -> None:
    code = _code()
    assert "create role" not in code
    for statement in ("insert into public.flow_controls", "insert into public.flow_definitions"):
        # Solo dentro del parche de publicar (texto de la funcion), nunca como sentencia de la migracion.
        assert statement in code
        assert all(line.startswith("        || E'") for line in code.splitlines() if statement in line)
    for signature in (
        "public._flow_change_items_are_valid(jsonb)", "public._flow_change_item_flow_ids(jsonb)",
        "public._flow_change_save_draft(text,text,uuid,integer,jsonb,jsonb,text,text,text)",
        "public._flow_change_publish(text,text,uuid,text,jsonb)",
        "public.operator_publish_flow_change_v1(jsonb,uuid,text,jsonb)", "public.read_flow_store_v1(jsonb)",
    ):
        assert f"'{signature}'" in code, signature


def test_the_phone_of_the_cohort_never_leaves_the_function() -> None:
    body = _sql().split("create function public.operator_flow_cohort_v1", 1)[1].split("$function$;", 1)[0]
    returns = re.findall(r"return jsonb_build_object\(([^;]*)\);", body)
    assert returns and all("p_phone" not in value and "v_canonical" not in value and "v_contacts" not in value
                           for value in returns)
    event = body.split("'flow_cohort_changed'", 1)[1].split(");", 1)[0]
    assert "p_phone" not in event and "v_contacts" not in event
