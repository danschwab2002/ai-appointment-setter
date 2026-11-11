"""Los frenos del seguimiento en la base (20261101000300, E-263), leidos como texto.

Lo que hacen lo prueba ``tests/sql/followup_engine/validate_followup_stops.mjs``
con las RPC reales sobre la cadena entera; aca, la forma de la migracion y que
el bridge conoce lo que escribe.
"""

from __future__ import annotations

import re
from pathlib import Path

from bridge.supabase import FOLLOWUP_CLAIM_OUTCOMES

MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
MIGRATION = (
    MIGRATIONS / "20261101000300_followup_stops_after_last_touch_and_pending_payment_v1.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_it_is_one_transaction_with_lock_timeout_and_the_last() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # La renovacion por respuesta y el cupon del turno (20261102000100, E-267).
    # Las solicitudes de plantilla de Meta (20261102000200, E-268, F-146 partes 1 y 2).
    # El barrido del seguimiento a 24 h nativo (20261102000300, E-269).
    # El reciclado de un telefono de prueba (20261102000400, E-270, F-147 PR 1).
    # Los rechazos de Meta y el unico reintento del 131049 (20261103000500, E-284).
    # El historial de la pagina de aprobacion (20261103000600, E-295).
    # Si la persona ya es cliente (20261103000700, E-297).
    # Las cuatro ramas del pago rechazado nativo (20261103000900, E-306).
    # El aviso de derivaciones sin atender (20261103001000, E-309).
    # La etiqueta de un segmento en un flujo nativo (20261103001300, E-316): un reemplazo contado en la forma nativa.
    # El rechazo de la cuenta espera a que la cuenta vuelva (20261107000100, E-348): dos tablas y ocho RPC de service_role.
    assert names[-8] == "20261107000100_meta_account_hold_retry_v1.sql"
    # El almacen dice quien ejecuta cada flujo (20261108000100, E-353): una clave mas en read_flow_store_v1.
    # Las campanas del Automation Expert como dato (20261108005000, E-355): dos tablas y doce RPC.
    # El sondeo de Meta y el aviso en el hilo (20261109000100, E-361): dos columnas, una tabla y cuatro RPC.
    # La oferta de la campana en el link del agente (20261110000500, E-410): una reserva, una columna y el trigger.
    # El caso se cierra cuando su ultimo toque termina sin salir (20261111000100, E-423): una funcion privada y el envoltorio.
    # Un pago pendiente que se cancela no clasifica la intencion (20261111000200, E-423): una rama del correlador portable.
    assert names[-1] == "20261111000200_pending_cancel_keeps_first_contact_v1.sql"
    assert names[-2] == "20261111000100_close_recovery_case_without_pending_touch_v1.sql"
    assert names[-3] == "20261110000500_campaign_offer_checkout_issuance_v1.sql"
    assert names[-4] == "20261109000100_meta_template_request_notices_v1.sql"
    assert names[-5] == "20261108005000_agent_campaigns_v1.sql"
    # La base no publica una version nativa que el motor no ejecuta (20261108004100, E-357).
    assert names[-6] == "20261108004100_flow_change_publish_native_version_guard_v1.sql"
    assert names[-7] == "20261108000100_flow_store_route_executor_v1.sql"
    # El cierre del caso por el cupon de una campana (20261106000100, E-339): una RPC de service_role.
    assert names[-9] == "20261106000100_recovery_case_segment_coupon_close_v1.sql"
    # El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
    assert names[-10] == "20261103001700_flow_engine_native_campaign_origin_v1.sql"
    # La entrada a una campana: lo que contesto cada conversacion a la pregunta (20261103001500, E-317).
    assert names[-11] == "20261103001500_segment_entry_answers_v1.sql"
    assert names[-12] == "20261103001300_flow_engine_native_segment_label_v1.sql"
    assert names[-13] == "20261103001000_handoff_wait_alerts_v1.sql"
    assert names[-14] == "20261103000900_flow_engine_native_payment_failure_branches_v1.sql"
    assert names[-15] == "20261103000700_customer_status_v1.sql"
    assert names[-16] == "20261103000600_flow_approval_history_v1.sql"
    assert names[-17] == "20261103000500_meta_delivery_rejections_v1.sql"
    assert names[-18] == "20261102000400_identity_recycle_v1.sql"
    assert names[-19] == "20261102000300_flow_engine_native_silence_sweep_v1.sql"
    assert names[-20] == "20261102000200_meta_template_requests_v1.sql"
    assert names[-21] == "20261102000100_recovery_coupon_by_reply_v1.sql"
    assert names[-22] == MIGRATION.name
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'followup_stops_already_applied'" in sql
    assert "message = 'unexpected_followup_claim_definition'" in sql


def test_every_outcome_it_writes_is_known_to_the_bridge() -> None:
    written = set(re.findall(r"outcome := '([a-z_]+)';", _sql()))
    assert written == {"blocked_after_last_touch", "blocked_pending_payment"}
    assert written <= FOLLOWUP_CLAIM_OUTCOMES


def test_it_patches_only_the_two_claims_and_creates_nothing() -> None:
    code = re.sub(r"--[^\n]*", "", _sql())
    # La unica funcion que crea es el helper temporal de la sesion.
    assert re.findall(r"create (?:or replace )?function ([\w.]+)", code) == [
        "pg_temp.followup_stops_patch"
    ]
    assert "claim_portable_conversation_followup_v1(" in code
    assert "claim_flow_engine_silence_v1(" in code
    assert "claim_conversation_followup_v1(" not in code.replace(
        "claim_portable_conversation_followup_v1(", ""
    )
    for forbidden in ("alter table", "create table", "insert into", "update public.", "delete from"):
        assert forbidden not in code.lower(), forbidden


def test_it_says_that_it_changes_what_0800_said() -> None:
    sql = _sql()
    assert "20261019000800" in sql
    assert "A PROPOSITO" in sql
