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
    # Despues, el aviso de derivaciones sin atender (20261103001000, E-309).
    # La etiqueta de un segmento en un flujo nativo (20261103001300, E-316): un reemplazo contado en la forma nativa.
    # El rechazo de la cuenta espera a que la cuenta vuelva (20261107000100, E-348): dos tablas y ocho RPC de service_role.
    assert names[-18] == "20261107000100_meta_account_hold_retry_v1.sql"
    # El almacen dice quien ejecuta cada flujo (20261108000100, E-353): una clave mas en read_flow_store_v1.
    # Las campanas del Automation Expert como dato (20261108005000, E-355): dos tablas y doce RPC.
    # El sondeo de Meta y el aviso en el hilo (20261109000100, E-361): dos columnas, una tabla y cuatro RPC.
    # La oferta de la campana en el link del agente (20261110000500, E-410): una reserva, una columna y el trigger.
    # El caso se cierra cuando su ultimo toque termina sin salir (20261111000100, E-423): una funcion privada y el envoltorio.
    # Un pago pendiente que se cancela no clasifica la intencion (20261111000200, E-423): una rama del correlador portable.
    assert names[-11] == "20261111000200_pending_cancel_keeps_first_contact_v1.sql"
    # Los numeros del tablero (20261112200000, E-507): una lectura nueva.
    assert names[-10] == "20261112200000_read_results_v1.sql"
    assert names[-12] == "20261111000100_close_recovery_case_without_pending_touch_v1.sql"
    assert names[-13] == "20261110000500_campaign_offer_checkout_issuance_v1.sql"
    assert names[-14] == "20261109000100_meta_template_request_notices_v1.sql"
    assert names[-15] == "20261108005000_agent_campaigns_v1.sql"
    # El motor nativo de varios toques (20261112210000, E-483): las dos funciones de la forma nativa.
    # en_curso: cancelar en el nativo (20261112210100, E-483, C8).
    # prioridad.le_gana_a en el nativo (20261112210200, E-483, C7).
    # El pago rechazado completo en el nativo: B1 por motivo y B2 o Z (20261112210300, E-483, C3).
    # El pago rechazado sin formulario de una oferta de campana (20261112210400, E-483, pieza PF).
    # El evento de un disparo por audiencia en el nativo (20261112210600, E-483, para E-484).
    assert names[-1] == "20261112230000_e483_native_person_cap_v1.sql"
    assert names[-2] == "20261112220000_flow_audience_triggers_v1.sql"
    assert names[-3] == "20261112210600_e483_native_audience_source_v1.sql"
    assert names[-4] == "20261112210500_e483_native_personal_coupon_v1.sql"
    assert names[-5] == "20261112210400_e483_payment_failure_by_phone_v1.sql"
    assert names[-6] == "20261112210300_e483_native_condition_after_send_v1.sql"
    assert names[-7] == "20261112210200_e483_native_priority_wins_v1.sql"
    assert names[-8] == "20261112210100_e483_native_cancel_in_flight_v1.sql"
    assert names[-9] == "20261112210000_e483_native_multitouch_v1.sql"
    # La base no publica una version nativa que el motor no ejecuta (20261108004100, E-357).
    assert names[-16] == "20261108004100_flow_change_publish_native_version_guard_v1.sql"
    assert names[-17] == "20261108000100_flow_store_route_executor_v1.sql"
    # El cierre del caso por el cupon de una campana (20261106000100, E-339): una RPC de service_role.
    assert names[-19] == "20261106000100_recovery_case_segment_coupon_close_v1.sql"
    # El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
    assert names[-20] == "20261103001700_flow_engine_native_campaign_origin_v1.sql"
    # La entrada a una campana: lo que contesto cada conversacion a la pregunta (20261103001500, E-317).
    assert names[-21] == "20261103001500_segment_entry_answers_v1.sql"
    assert names[-22] == "20261103001300_flow_engine_native_segment_label_v1.sql"
    assert names[-23] == "20261103001000_handoff_wait_alerts_v1.sql"
    assert names[-24] == MIGRATION.name
    assert names[-25] == "20261103000700_customer_status_v1.sql"
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
        " && node validate_customer_status.mjs && node validate_flow_engine_native_payment_failure_branches.mjs --check && node validate_handoff_wait_alerts.mjs && node validate_flow_engine_native_segment_label.mjs && node validate_segment_entry_answers.mjs && node validate_flow_engine_native_campaign_origin.mjs --check && node validate_recovery_case_segment_coupon_close.mjs && node validate_meta_account_hold_retry.mjs && node validate_flow_store_route_executor.mjs --check && node validate_flow_change_publish_native_version_guard.mjs && node validate_agent_campaigns.mjs && node validate_meta_template_request_notices.mjs && node validate_recovery_case_no_touch_left_close.mjs && node validate_e483_native_multitouch.mjs && node validate_e483_native_cancel_in_flight.mjs && node validate_e483_native_priority_wins.mjs && node validate_e483_native_condition_after_send.mjs --check && node validate_e483_payment_failure_by_phone.mjs && node validate_e483_native_personal_coupon.mjs --check"
        " && node validate_e483_native_audience_source.mjs && node validate_flow_audience_triggers.mjs && node validate_flow_audience_native_e2e.mjs"
        " && node validate_e483_native_legacy_double_case_touches.mjs --check && node validate_e483_native_person_cap.mjs")
