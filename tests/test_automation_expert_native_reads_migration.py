"""La migracion de las lecturas nativas del AE (20261025000100, el PR 5 del hito 4), leida como texto.

Lo que corre de verdad lo prueba
tests/sql/followup_engine/validate_automation_expert_native_reads.mjs sobre la
cadena completa en PGlite.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261025000100_automation_expert_native_reads_v1.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return re.sub(r"--[^\n]*", "", _sql())


def test_it_is_one_transaction_and_the_last() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # Despues va la del btrim del nombre del producto en el pago rechazado (20261025000200, E-087).
    # Despues va la lectura del link para la nota al derivar (20261025000300, F-034 D8).
    # Y despues, las conversaciones del comprador para pausar al agente (20261025000400, E-100).
    # Y despues, la forma nativa con las variables del paso (20261026000100, E-115).
    # Y despues, la oferta del evento en el caso nativo (20261026000200, E-122).
    # Y despues, la pagina de aprobacion (20261028000100, A′ PR 2).
    # Y despues, el carrito nativo (20261028000200, E-161).
    assert names[-1] == "20261028000200_flow_engine_native_cart_v1.sql"
    assert names[-2] == "20261028000100_flow_change_slack_approver_v1.sql"
    assert names[-3] == "20261026000200_flow_engine_native_offer_in_context_v1.sql"
    assert names[-4] == "20261026000100_flow_engine_native_template_shape_v1.sql"
    assert names[-5] == "20261025000400_purchase_agent_pause_v1.sql"
    assert names[-6] == "20261025000300_handoff_link_note_v1.sql"
    assert names[-7] == "20261025000200_payment_failure_product_name_btrim.sql"
    assert names[-8] == MIGRATION.name
    assert names[-9] == "20261024000600_flow_change_new_flow_v1.sql"
    # Correrla dos veces aborta, y necesita el AE que crea flujos.
    assert "automation_expert_native_reads_already_applied" in sql
    assert "flow_change_new_flow_missing" in sql


def test_the_validator_is_in_the_npm_chain() -> None:
    package = json.loads((ROOT / "tests" / "sql" / "followup_engine" / "package.json").read_text())
    assert package["scripts"]["test"].rstrip().endswith(
        "node validate_automation_expert_native_reads.mjs && node validate_payment_failure_product_name_btrim.mjs"
        " && node validate_handoff_link_note.mjs && node validate_purchase_agent_pause.mjs"
        " && node validate_flow_engine_native_template_shape.mjs"
        " && node validate_flow_engine_native_offer_in_context.mjs --check"
        " && node validate_flow_change_slack_approver.mjs"
        " && node validate_flow_engine_native_cart.mjs --check")


def test_only_service_role_executes_the_three_reads() -> None:
    code = _code()
    grants = re.findall(r"grant execute on function public\.([a-z0-9_]+)\([^;]*\)\s+to ([a-z_]+);", code)
    assert sorted(grants) == [
        ("read_automation_expert_native_flows_v1", "service_role"),
        ("read_automation_expert_native_history_v1", "service_role"),
        ("read_flow_sales_v1", "service_role"),
    ]
    assert "to anon" not in code and "to authenticated" not in code


def test_it_creates_no_table_no_role_and_writes_no_row() -> None:
    code = _code().lower()
    assert "create table" not in code
    assert "create role" not in code
    assert not re.search(r"\b(insert into|update public\.|delete from)\b", code)
    # Las tres lecturas son stable y security definer con search_path fijo.
    for name in ("read_automation_expert_native_flows_v1", "read_automation_expert_native_history_v1",
                 "read_flow_sales_v1"):
        body = code.split(f"create function public.{name}(", 1)[1].split("$function$;", 1)[0]
        assert "stable" in body and "security definer" in body
        assert "set search_path = pg_catalog, public, pg_temp" in body


def test_each_patch_carries_its_marker_and_its_expected_count() -> None:
    code = _code()
    patches = re.findall(r"select pg_temp\.automation_expert_native_patch\(\s*'([^']+)'.*?'([a-z_0-9]+)', (\d)\);",
                         code, flags=re.S)
    assert patches == [
        ("public.read_automation_expert_lead_history_v1(jsonb,uuid)", "automation_expert_native_anchor_v1", "1"),
        ("public.find_automation_expert_lead_v1(jsonb,bigint,text,text)", "automation_expert_native_anchor_v1", "1"),
        ("public._flow_change_items_are_valid(jsonb)", "flow_change_prender_a_cohorte_v1", "1"),
        ("public._flow_change_publish(text,text,uuid,text,jsonb)", "flow_change_prender_a_cohorte_v1", "2"),
    ]


def test_the_reads_return_nothing_of_a_person() -> None:
    code = _code()
    for name in ("read_automation_expert_native_flows_v1", "read_flow_sales_v1"):
        body = code.split(f"create function public.{name}(", 1)[1].split("$function$;", 1)[0]
        built = " ".join(re.findall(r"jsonb_build_object\((.*?)\)", body, flags=re.S))
        for private in ("'contact_id'", "'person_ref'", "'email'", "'phone'", "'destino'"):
            assert private not in built, (name, private)
