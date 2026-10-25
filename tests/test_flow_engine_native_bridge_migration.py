"""La migracion del bridge del motor nativo (20261024000500, E-065), leida.

Lo que hace en una base lo prueba ``validate_flow_engine_native_bridge.mjs``
(PGlite, en la cadena de ``npm test``). Aca se fija lo que se ve en el texto.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261024000500_flow_engine_native_bridge_v1.sql"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return re.sub(r"--[^\n]*", "", _sql())


def test_it_is_one_transaction_and_the_last() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # Despues va solo el AE que crea flujos (20261024000600), que no redefine nada de esta.
    # Despues va la del btrim del nombre del producto en el pago rechazado (20261025000200, E-087).
    assert names[-1] == "20261025000200_payment_failure_product_name_btrim.sql"
    assert names[-2] == "20261025000100_automation_expert_native_reads_v1.sql"
    assert names[-3] == "20261024000600_flow_change_new_flow_v1.sql"
    assert names[-4] == MIGRATION.name
    assert names[-5] == "20261024000400_opted_out_reply_checkout_issuance.sql"
    assert names[-6] == "20261024000200_flow_engine_native_v1.sql"


def test_the_validator_is_in_the_npm_chain() -> None:
    package = json.loads((ROOT / "tests" / "sql" / "followup_engine" / "package.json").read_text())
    chain = package["scripts"]["test"]
    assert chain.rstrip().endswith("node validate_flow_engine_native_bridge.mjs --emit-bridge-fixtures --check"
                                   " && node validate_flow_change_new_flow.mjs"
                                   " && node validate_automation_expert_native_reads.mjs"
                                   " && node validate_payment_failure_product_name_btrim.mjs")


def test_only_service_role_executes_the_seven_entrypoints_and_no_helper() -> None:
    code = _code()
    grants = re.findall(r"grant execute on function public\.([a-z0-9_]+)\(", code)
    assert sorted(grants) == sorted([
        "read_flow_engine_native_flows_v1",
        "enqueue_flow_engine_native_event_v1",
        "read_flow_engine_native_event_queue_v1",
        "finish_flow_engine_native_event_v1",
        "read_flow_engine_start_rejections_v1",
        "mark_flow_engine_start_rejection_notified_v1",
        "flow_engine_native_step_tail_v1",
    ])
    assert re.findall(r"to (\w+);", code.split("if to_regrole('service_role') is not null then", 1)[1]) == [
        "service_role"
    ] * 7
    assert "grant execute on function public._flow_engine_rejection" not in code


def test_the_adoption_patch_only_adds_the_native_case() -> None:
    code = _sql()
    assert "'public.admit_portable_inbound_commercial_case_v1(text,integer,bigint,text)'" in code
    assert "left join public.pilot_recovery_case_bindings binding" in code
    assert "binding.recovery_case_id is not null" in code
    assert "public.flow_engine_native_runs native_run" in code
    # Si la definicion viva no es la esperada, aborta en vez de derivar a ciegas.
    assert "unexpected_flow_engine_native_bridge_definition" in code
    assert "flow_engine_native_bridge_definition_not_replaced" in code


def test_mot_002_only_tells_what_the_engine_did_wrong() -> None:
    [body] = re.findall(
        r"create function public\._flow_engine_rejection_is_alertable\(p_reason text\).*?\$function\$(.*?)\$function\$",
        _sql(), flags=re.S)
    for reason in ("flow_engine_pool_unknown", "error\\_interno:", "politica\\_compilada:", "motor\\_nativo:"):
        assert reason in body
    for person in ("baja", "fuera_de_cohorte", "flujo_pausado", "ya_en_curso", "ya_disparado", "no_entra"):
        assert person not in body


def test_the_queue_only_gives_purchases_already_applied() -> None:
    code = _code()
    assert "e.processing_status <> 'received'" in code
    assert "event_type is distinct from 'PURCHASE_APPROVED'" in code
