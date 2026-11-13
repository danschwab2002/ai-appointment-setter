"""Las solicitudes de plantilla de Meta en la base (20261102000200, E-268, F-146 partes 1 y 2), leidas como texto.

Lo que hacen lo prueba ``tests/sql/followup_engine/validate_meta_template_requests.mjs``
con las RPC reales sobre la cadena entera; aca, la forma del archivo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261102000200_meta_template_requests_v1.sql"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_it_is_one_transaction_with_lock_timeout_the_last_and_does_not_run_twice() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # El barrido del seguimiento a 24 h nativo (20261102000300, E-269).
    # El reciclado de un telefono de prueba (20261102000400, E-270, F-147 PR 1).
    # Los rechazos de Meta y el unico reintento del 131049 (20261103000500, E-284).
    # El historial de la pagina de aprobacion (20261103000600, E-295).
    # Si la persona ya es cliente (20261103000700, E-297).
    # Las cuatro ramas del pago rechazado nativo (20261103000900, E-306).
    # El aviso de derivaciones sin atender (20261103001000, E-309).
    # La etiqueta de un segmento en un flujo nativo (20261103001300, E-316): un reemplazo contado en la forma nativa.
    # El rechazo de la cuenta espera a que la cuenta vuelva (20261107000100, E-348): dos tablas y ocho RPC de service_role.
    assert names[-17] == "20261107000100_meta_account_hold_retry_v1.sql"
    # El almacen dice quien ejecuta cada flujo (20261108000100, E-353): una clave mas en read_flow_store_v1.
    # Las campanas del Automation Expert como dato (20261108005000, E-355): dos tablas y doce RPC.
    # El sondeo de Meta y el aviso en el hilo (20261109000100, E-361): dos columnas, una tabla y cuatro RPC.
    # La oferta de la campana en el link del agente (20261110000500, E-410): una reserva, una columna y el trigger.
    # El caso se cierra cuando su ultimo toque termina sin salir (20261111000100, E-423): una funcion privada y el envoltorio.
    # Un pago pendiente que se cancela no clasifica la intencion (20261111000200, E-423): una rama del correlador portable.
    assert names[-10] == "20261111000200_pending_cancel_keeps_first_contact_v1.sql"
    # Los numeros del tablero (20261112200000, E-507): una lectura nueva.
    assert names[-9] == "20261112200000_read_results_v1.sql"
    assert names[-11] == "20261111000100_close_recovery_case_without_pending_touch_v1.sql"
    assert names[-12] == "20261110000500_campaign_offer_checkout_issuance_v1.sql"
    assert names[-13] == "20261109000100_meta_template_request_notices_v1.sql"
    assert names[-14] == "20261108005000_agent_campaigns_v1.sql"
    # El motor nativo de varios toques (20261112210000, E-483): las dos funciones de la forma nativa.
    # en_curso: cancelar en el nativo (20261112210100, E-483, C8).
    # prioridad.le_gana_a en el nativo (20261112210200, E-483, C7).
    # El pago rechazado completo en el nativo: B1 por motivo y B2 o Z (20261112210300, E-483, C3).
    # El pago rechazado sin formulario de una oferta de campana (20261112210400, E-483, pieza PF).
    # El evento de un disparo por audiencia en el nativo (20261112210600, E-483, para E-484).
    assert names[-1] == "20261112220000_flow_audience_triggers_v1.sql"
    assert names[-2] == "20261112210600_e483_native_audience_source_v1.sql"
    assert names[-3] == "20261112210500_e483_native_personal_coupon_v1.sql"
    assert names[-4] == "20261112210400_e483_payment_failure_by_phone_v1.sql"
    assert names[-5] == "20261112210300_e483_native_condition_after_send_v1.sql"
    assert names[-6] == "20261112210200_e483_native_priority_wins_v1.sql"
    assert names[-7] == "20261112210100_e483_native_cancel_in_flight_v1.sql"
    assert names[-8] == "20261112210000_e483_native_multitouch_v1.sql"
    # La base no publica una version nativa que el motor no ejecuta (20261108004100, E-357).
    assert names[-15] == "20261108004100_flow_change_publish_native_version_guard_v1.sql"
    assert names[-16] == "20261108000100_flow_store_route_executor_v1.sql"
    # El cierre del caso por el cupon de una campana (20261106000100, E-339): una RPC de service_role.
    assert names[-18] == "20261106000100_recovery_case_segment_coupon_close_v1.sql"
    # El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
    assert names[-19] == "20261103001700_flow_engine_native_campaign_origin_v1.sql"
    # La entrada a una campana: lo que contesto cada conversacion a la pregunta (20261103001500, E-317).
    assert names[-20] == "20261103001500_segment_entry_answers_v1.sql"
    assert names[-21] == "20261103001300_flow_engine_native_segment_label_v1.sql"
    assert names[-22] == "20261103001000_handoff_wait_alerts_v1.sql"
    assert names[-23] == "20261103000900_flow_engine_native_payment_failure_branches_v1.sql"
    assert names[-24] == "20261103000700_customer_status_v1.sql"
    assert names[-25] == "20261103000600_flow_approval_history_v1.sql"
    assert names[-26] == "20261103000500_meta_delivery_rejections_v1.sql"
    assert names[-27] == "20261102000400_identity_recycle_v1.sql"
    assert names[-28] == "20261102000300_flow_engine_native_silence_sweep_v1.sql"
    assert names[-29] == MIGRATION.name
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'meta_template_requests_already_applied'" in sql
    assert "message = 'meta_template_requests_prerequisite_missing'" in sql


def test_it_only_adds_never_replaces_or_drops() -> None:
    sql = _sql().lower()
    assert "create or replace" not in sql
    assert "drop " not in sql
    assert "alter role" not in sql and "create role" not in sql
    assert "alter default privileges" not in sql
    created = re.findall(r"^create function public\.([a-z0-9_]+)\(", _sql(), re.M)
    assert len(created) == 19 and len(set(created)) == 19
    assert re.findall(r"^create table public\.([a-z_]+) \(", _sql(), re.M) == [
        "meta_template_requests", "meta_template_request_events"]


def test_each_role_gets_only_its_functions() -> None:
    sql = _sql()
    acl = sql[sql.index("do $acl$"):]
    agent = acl[acl.index("to_regrole('automation_expert_change')"):acl.index("to_regrole('flow_change_approver')")]
    page = acl[acl.index("to_regrole('flow_change_approver')"):acl.index("to_regrole('service_role')")]
    service = acl[acl.index("to_regrole('service_role')"):]
    assert re.findall(r"'public\.([a-z_0-9]+)\(", agent) == [
        "save_meta_template_request_draft_v1", "record_meta_template_request_validation_v1",
        "get_meta_template_request_v1", "list_meta_template_requests_v1"]
    assert re.findall(r"'public\.([a-z_0-9]+)\(", page) == [
        "approver_get_meta_template_request_v1", "approver_approve_meta_template_request_v1",
        "approver_discard_meta_template_request_v1", "approver_begin_meta_template_send_v1",
        "approver_record_meta_template_sent_v1"]
    assert re.findall(r"function public\.([a-z_0-9]+)\(", service) == ["record_meta_template_status_v1"]
    # Nada para anon ni authenticated, y las tablas para nadie de la API.
    assert "to anon" not in sql and "to authenticated" not in sql
    assert "grant select" not in sql.lower() and "grant all" not in sql.lower()


def test_nothing_in_it_calls_meta_or_reads_a_token() -> None:
    sql = _sql().lower()
    for word in ("graph.facebook.com", "http", "access_token", "provider_config", "net."):
        assert word not in sql.replace("https://", ""), word


def test_the_validator_is_the_last_of_the_npm_chain() -> None:
    chain = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.rstrip().endswith(" && node validate_meta_template_requests.mjs --check && node validate_flow_engine_native_silence_sweep.mjs && node validate_identity_recycle.mjs && node validate_meta_delivery_rejections.mjs && node validate_flow_approval_history.mjs && node validate_customer_status.mjs && node validate_flow_engine_native_payment_failure_branches.mjs --check && node validate_handoff_wait_alerts.mjs && node validate_flow_engine_native_segment_label.mjs && node validate_segment_entry_answers.mjs && node validate_flow_engine_native_campaign_origin.mjs --check && node validate_recovery_case_segment_coupon_close.mjs && node validate_meta_account_hold_retry.mjs && node validate_flow_store_route_executor.mjs --check && node validate_flow_change_publish_native_version_guard.mjs && node validate_agent_campaigns.mjs && node validate_meta_template_request_notices.mjs && node validate_recovery_case_no_touch_left_close.mjs && node validate_e483_native_multitouch.mjs && node validate_e483_native_cancel_in_flight.mjs && node validate_e483_native_priority_wins.mjs && node validate_e483_native_condition_after_send.mjs --check && node validate_e483_payment_failure_by_phone.mjs && node validate_e483_native_personal_coupon.mjs --check"
        " && node validate_e483_native_audience_source.mjs && node validate_flow_audience_triggers.mjs")
