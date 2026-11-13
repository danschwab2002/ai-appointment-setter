"""La etiqueta de un segmento en un flujo nativo (20261103001300, E-316), leida como texto.

El comportamiento lo prueba ``tests/sql/followup_engine/validate_flow_engine_native_segment_label.mjs`` con
la cadena entera en PGlite (antes y despues). Aca se fija lo que no tiene que romperse en silencio:

* es una sola transaccion con ``lock_timeout``, la ultima de la cadena, y aborta si ya corrio;
* cambia una sola funcion viva, ``_flow_engine_native_unsupported``, con un reemplazo contado que falla con
  55000 si el texto no esta exactamente una vez, y no crea ni borra nada ni toca permisos;
* el texto que reemplaza es el de la definicion vigente (20261101000200, el bloque de etiquetar);
* el validador de PGlite es el ultimo de la cadena de npm.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261103001300_flow_engine_native_segment_label_v1.sql"
SILENCE = MIGRATIONS / "20261101000200_flow_engine_native_silence_v1.sql"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return "\n".join(line for line in _sql().splitlines() if not line.lstrip().startswith("--"))


def test_it_is_one_transaction_with_lock_timeout_and_the_last() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
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
    assert names[-21] == MIGRATION.name
    assert names[-22] == "20261103001000_handoff_wait_alerts_v1.sql"
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'flow_engine_native_segment_label_already_applied'" in sql


def test_it_patches_only_the_native_shape_with_a_counted_replacement() -> None:
    code = " ".join(_code().split())
    assert "message = 'unexpected_flow_engine_native_unsupported_definition'" in code
    assert code.count("length(v_text) - length(replace(v_text, c_old, '')) <> length(c_old)") == 1
    assert "execute replace(v_text, c_old, c_new);" in code
    lowered = _code().lower()
    for forbidden in ("create function", "create or replace function", "drop ", "grant ", "revoke ",
                      "create table", "alter table", "insert ", "update ", "delete "):
        assert forbidden not in lowered, forbidden


def _literal(name: str) -> str:
    """El texto de una constante ``E'...' || E'...'`` del bloque, como lo ve Postgres."""
    block = re.search(rf"{name} constant text :=\s*(.*?);\n", _sql(), re.S)
    assert block is not None, name
    parts = re.findall(r"E'((?:[^']|'')*)'", block.group(1))
    return "".join(part.replace("''", "'").replace("\\n", "\n").replace("\\\\", "\\") for part in parts)


def test_the_replaced_text_is_the_live_definition() -> None:
    old = _literal("c_old")
    assert SILENCE.read_text(encoding="utf-8").count(old) == 1
    new = _literal("c_new")
    assert "flow_engine_native_segment_label" in new
    assert "step.value ->> 'etiquetar' = 'cliente'" in new
    assert "like 'automation\\_%'" in new
    # Lo de antes queda: solo se suma la condicion de la etiqueta.
    assert new.replace(
        "\n                                and (jsonb_typeof(step.value -> 'etiquetar') is distinct from 'string'"
        "\n                                     or step.value ->> 'etiquetar' = 'cliente'"
        "\n                                     or step.value ->> 'etiquetar' like 'automation\\_%')))", "))"
    ).replace(
        "             -- flow_engine_native_segment_label: la etiqueta de un segmento va con cualquier\n"
        "             -- disparador; cliente, la de la automatizacion o una que no es texto, solo con la\n"
        "             -- compra (20261103001300).\n", "") == old


def test_the_validator_is_the_last_of_the_npm_chain() -> None:
    chain = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.rstrip().endswith(
        " && node validate_handoff_wait_alerts.mjs && node validate_flow_engine_native_segment_label.mjs && node validate_segment_entry_answers.mjs && node validate_flow_engine_native_campaign_origin.mjs --check && node validate_recovery_case_segment_coupon_close.mjs && node validate_meta_account_hold_retry.mjs && node validate_flow_store_route_executor.mjs --check && node validate_flow_change_publish_native_version_guard.mjs && node validate_agent_campaigns.mjs && node validate_meta_template_request_notices.mjs && node validate_recovery_case_no_touch_left_close.mjs && node validate_e483_native_multitouch.mjs && node validate_e483_native_cancel_in_flight.mjs && node validate_e483_native_priority_wins.mjs && node validate_e483_native_condition_after_send.mjs --check && node validate_e483_payment_failure_by_phone.mjs && node validate_e483_native_personal_coupon.mjs --check"
        " && node validate_e483_native_audience_source.mjs && node validate_flow_audience_triggers.mjs && node validate_flow_audience_native_e2e.mjs"
        " && node validate_e483_native_legacy_double_case_touches.mjs --check")
