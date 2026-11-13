"""La renovacion por respuesta y el cupon del turno en la base (20261102000100, E-267), leidos como texto.

Lo que hacen lo prueba ``tests/sql/followup_engine/validate_recovery_coupon_by_reply.mjs``
con las RPC reales sobre la cadena entera; aca, la forma de la migracion y que
el bridge conoce lo que contesta.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from bridge.supabase import CART_RENEWAL_BY_REPLY_OUTCOMES, RECOVERY_COUPON_TURN_STATES

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261102000100_recovery_coupon_by_reply_v1.sql"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_it_is_one_transaction_with_lock_timeout_and_the_last() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # Despues va solo la de las solicitudes de plantilla de Meta (20261102000200, E-268).
    # El barrido del seguimiento a 24 h nativo (20261102000300, E-269).
    # El reciclado de un telefono de prueba (20261102000400, E-270, F-147 PR 1).
    # Los rechazos de Meta y el unico reintento del 131049 (20261103000500, E-284).
    # El historial de la pagina de aprobacion (20261103000600, E-295).
    # Si la persona ya es cliente (20261103000700, E-297).
    # Las cuatro ramas del pago rechazado nativo (20261103000900, E-306).
    # El aviso de derivaciones sin atender (20261103001000, E-309).
    # La etiqueta de un segmento en un flujo nativo (20261103001300, E-316): un reemplazo contado en la forma nativa.
    # El rechazo de la cuenta espera a que la cuenta vuelva (20261107000100, E-348): dos tablas y ocho RPC de service_role.
    assert names[-9] == "20261107000100_meta_account_hold_retry_v1.sql"
    # El almacen dice quien ejecuta cada flujo (20261108000100, E-353): una clave mas en read_flow_store_v1.
    # Las campanas del Automation Expert como dato (20261108005000, E-355): dos tablas y doce RPC.
    # El sondeo de Meta y el aviso en el hilo (20261109000100, E-361): dos columnas, una tabla y cuatro RPC.
    # La oferta de la campana en el link del agente (20261110000500, E-410): una reserva, una columna y el trigger.
    # El caso se cierra cuando su ultimo toque termina sin salir (20261111000100, E-423): una funcion privada y el envoltorio.
    # Un pago pendiente que se cancela no clasifica la intencion (20261111000200, E-423): una rama del correlador portable.
    assert names[-2] == "20261111000200_pending_cancel_keeps_first_contact_v1.sql"
    # Los numeros del tablero (20261112200000, E-507): una lectura nueva.
    assert names[-1] == "20261112200000_read_results_v1.sql"
    assert names[-3] == "20261111000100_close_recovery_case_without_pending_touch_v1.sql"
    assert names[-4] == "20261110000500_campaign_offer_checkout_issuance_v1.sql"
    assert names[-5] == "20261109000100_meta_template_request_notices_v1.sql"
    assert names[-6] == "20261108005000_agent_campaigns_v1.sql"
    # La base no publica una version nativa que el motor no ejecuta (20261108004100, E-357).
    assert names[-7] == "20261108004100_flow_change_publish_native_version_guard_v1.sql"
    assert names[-8] == "20261108000100_flow_store_route_executor_v1.sql"
    # El cierre del caso por el cupon de una campana (20261106000100, E-339): una RPC de service_role.
    assert names[-10] == "20261106000100_recovery_case_segment_coupon_close_v1.sql"
    # El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
    assert names[-11] == "20261103001700_flow_engine_native_campaign_origin_v1.sql"
    # La entrada a una campana: lo que contesto cada conversacion a la pregunta (20261103001500, E-317).
    assert names[-12] == "20261103001500_segment_entry_answers_v1.sql"
    assert names[-13] == "20261103001300_flow_engine_native_segment_label_v1.sql"
    assert names[-14] == "20261103001000_handoff_wait_alerts_v1.sql"
    assert names[-15] == "20261103000900_flow_engine_native_payment_failure_branches_v1.sql"
    assert names[-16] == "20261103000700_customer_status_v1.sql"
    assert names[-17] == "20261103000600_flow_approval_history_v1.sql"
    assert names[-18] == "20261103000500_meta_delivery_rejections_v1.sql"
    assert names[-19] == "20261102000400_identity_recycle_v1.sql"
    assert names[-20] == "20261102000300_flow_engine_native_silence_sweep_v1.sql"
    assert names[-21] == "20261102000200_meta_template_requests_v1.sql"
    assert names[-22] == MIGRATION.name
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'recovery_coupon_by_reply_already_applied'" in sql
    assert "message = 'unexpected_request_recovery_renewal_definition'" in sql


def test_it_only_creates_the_two_functions() -> None:
    code = re.sub(r"--[^\n]*", "", _sql())
    assert re.findall(r"create (?:or replace )?function ([\w.]+)", code) == [
        "public.request_recovery_renewal_by_reply_v1",
        "public.get_recovery_coupon_for_conversation_v1",
    ]
    # Ninguna sentencia que escriba (lo que la guarda busca en la definicion
    # vigente va entre comillas y no cuenta).
    statements = code.lower()
    for forbidden in ("create or replace", "alter table", "create table", "execute replace("):
        assert forbidden not in statements, forbidden
    assert re.findall(r"\n\s*(insert into|update public\.|delete from)", statements) == []


def test_the_renewal_delegates_in_the_one_of_the_button() -> None:
    code = re.sub(r"--[^\n]*", "", _sql())
    body = code.split("function public.request_recovery_renewal_by_reply_v1(", 1)[1].split("$function$;", 1)[0]
    assert "from public.request_recovery_renewal_v1(" in body
    assert "action.step_key = 'cart_last_touch'" in body
    assert "action.created_at > p_occurred_at" in body


def test_every_outcome_and_state_is_known_to_the_bridge() -> None:
    sql = _sql()
    assert set(re.findall(r"select '([a-z_]+)'::text;", sql)) <= CART_RENEWAL_BY_REPLY_OUTCOMES
    assert "reply_before_last_touch" in CART_RENEWAL_BY_REPLY_OUTCOMES
    # Lo que la delegada contesta (la del boton) tambien.
    assert {"advanced", "already_requested", "no_pending_last_touch"} <= CART_RENEWAL_BY_REPLY_OUTCOMES
    assert set(re.findall(r"select '([a-z_]+)'::text, ", sql)) == RECOVERY_COUPON_TURN_STATES


def test_it_never_seeds_a_third_coupon() -> None:
    code = re.sub(r"--[^\n]*", "", _sql()).lower()
    assert "recovery_coupons (" not in code
    assert "claim_due_recovery_coupons" not in code


def test_the_validator_is_the_last_of_the_npm_chain() -> None:
    chain = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.rstrip().endswith(
        " && node validate_followup_stops.mjs && node validate_recovery_coupon_by_reply.mjs"
        " && node validate_meta_template_requests.mjs --check && node validate_flow_engine_native_silence_sweep.mjs"
        " && node validate_identity_recycle.mjs && node validate_meta_delivery_rejections.mjs && node validate_flow_approval_history.mjs && node validate_customer_status.mjs && node validate_flow_engine_native_payment_failure_branches.mjs --check && node validate_handoff_wait_alerts.mjs && node validate_flow_engine_native_segment_label.mjs && node validate_segment_entry_answers.mjs && node validate_flow_engine_native_campaign_origin.mjs --check && node validate_recovery_case_segment_coupon_close.mjs && node validate_meta_account_hold_retry.mjs && node validate_flow_store_route_executor.mjs --check && node validate_flow_change_publish_native_version_guard.mjs && node validate_agent_campaigns.mjs && node validate_meta_template_request_notices.mjs && node validate_recovery_case_no_touch_left_close.mjs"
    )
