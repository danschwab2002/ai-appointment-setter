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
    # Si la persona ya es cliente (20261103000700, E-297).
    # Las cuatro ramas del pago rechazado nativo (20261103000900, E-306).
    # El aviso de derivaciones sin atender (20261103001000, E-309).
    # La etiqueta de un segmento en un flujo nativo (20261103001300, E-316): un reemplazo contado en la forma nativa.
    # El rechazo de la cuenta espera a que la cuenta vuelva (20261107000100, E-348): dos tablas y ocho RPC de service_role.
    assert names[-4] == "20261107000100_meta_account_hold_retry_v1.sql"
    # El almacen dice quien ejecuta cada flujo (20261108000100, E-353): una clave mas en read_flow_store_v1.
    # Las campanas del Automation Expert como dato (20261108005000, E-355): dos tablas y doce RPC.
    assert names[-1] == "20261108005000_agent_campaigns_v1.sql"
    # La base no publica una version nativa que el motor no ejecuta (20261108004100, E-357).
    assert names[-2] == "20261108004100_flow_change_publish_native_version_guard_v1.sql"
    assert names[-3] == "20261108000100_flow_store_route_executor_v1.sql"
    # El cierre del caso por el cupon de una campana (20261106000100, E-339): una RPC de service_role.
    assert names[-5] == "20261106000100_recovery_case_segment_coupon_close_v1.sql"
    # El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
    assert names[-6] == "20261103001700_flow_engine_native_campaign_origin_v1.sql"
    # La entrada a una campana: lo que contesto cada conversacion a la pregunta (20261103001500, E-317).
    assert names[-7] == "20261103001500_segment_entry_answers_v1.sql"
    assert names[-8] == "20261103001300_flow_engine_native_segment_label_v1.sql"
    assert names[-9] == "20261103001000_handoff_wait_alerts_v1.sql"
    assert names[-10] == "20261103000900_flow_engine_native_payment_failure_branches_v1.sql"
    assert names[-11] == "20261103000700_customer_status_v1.sql"
    assert names[-12] == "20261103000600_flow_approval_history_v1.sql"
    assert names[-13] == "20261103000500_meta_delivery_rejections_v1.sql"
    assert names[-14] == MIGRATION.name
    assert names[-15] == "20261102000300_flow_engine_native_silence_sweep_v1.sql"
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
                                   " && node validate_identity_recycle.mjs && node validate_meta_delivery_rejections.mjs && node validate_flow_approval_history.mjs && node validate_customer_status.mjs && node validate_flow_engine_native_payment_failure_branches.mjs --check && node validate_handoff_wait_alerts.mjs && node validate_flow_engine_native_segment_label.mjs && node validate_segment_entry_answers.mjs && node validate_flow_engine_native_campaign_origin.mjs --check && node validate_recovery_case_segment_coupon_close.mjs && node validate_meta_account_hold_retry.mjs && node validate_flow_store_route_executor.mjs --check && node validate_flow_change_publish_native_version_guard.mjs && node validate_agent_campaigns.mjs")
