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
    # Despues, sumar ofertas a un scope publicado (20261028000300, E-163).
    # Despues, la compra con nuestro link cuenta siempre (20261028000400, E-169).
    # El doble caso del carrito (20261030000100, E-204): la admision nativa y el planificador del carrito.
    # El pago rechazado nativo (20261030000200, E-207): el disparador pago_fallido del motor nativo.
    # La lista de las propuestas del AE (20261030000300, E-208): una lectura nueva.
    # Los frenos de la compra ven nuestro link (20261030000400, E-215): la tercera fuente.
    # El seguimiento a 24 h nativo (20261101000200, E-251): la base del silencio.
    # Los frenos del seguimiento despues de Z y con una ficha vigente (20261101000300, E-263).
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
    assert names[-21] == "20261107000100_meta_account_hold_retry_v1.sql"
    # El almacen dice quien ejecuta cada flujo (20261108000100, E-353): una clave mas en read_flow_store_v1.
    # Las campanas del Automation Expert como dato (20261108005000, E-355): dos tablas y doce RPC.
    # El sondeo de Meta y el aviso en el hilo (20261109000100, E-361): dos columnas, una tabla y cuatro RPC.
    # La oferta de la campana en el link del agente (20261110000500, E-410): una reserva, una columna y el trigger.
    # El caso se cierra cuando su ultimo toque termina sin salir (20261111000100, E-423): una funcion privada y el envoltorio.
    # Un pago pendiente que se cancela no clasifica la intencion (20261111000200, E-423): una rama del correlador portable.
    assert names[-14] == "20261111000200_pending_cancel_keeps_first_contact_v1.sql"
    # Los numeros del tablero (20261112200000, E-507): una lectura nueva.
    assert names[-13] == "20261112200000_read_results_v1.sql"
    assert names[-15] == "20261111000100_close_recovery_case_without_pending_touch_v1.sql"
    assert names[-16] == "20261110000500_campaign_offer_checkout_issuance_v1.sql"
    assert names[-17] == "20261109000100_meta_template_request_notices_v1.sql"
    assert names[-18] == "20261108005000_agent_campaigns_v1.sql"
    # El motor nativo de varios toques (20261112210000, E-483): las dos funciones de la forma nativa.
    # en_curso: cancelar en el nativo (20261112210100, E-483, C8).
    # prioridad.le_gana_a en el nativo (20261112210200, E-483, C7).
    # El pago rechazado completo en el nativo: B1 por motivo y B2 o Z (20261112210300, E-483, C3).
    # El pago rechazado sin formulario de una oferta de campana (20261112210400, E-483, pieza PF).
    # El evento de un disparo por audiencia en el nativo (20261112210600, E-483, para E-484).
    assert names[-3] == "20261113200000_e528_native_payment_slip_v1.sql"
    assert names[-4] == "20261112230000_e483_native_person_cap_v1.sql"
    assert names[-5] == "20261112220000_flow_audience_triggers_v1.sql"
    assert names[-6] == "20261112210600_e483_native_audience_source_v1.sql"
    assert names[-7] == "20261112210500_e483_native_personal_coupon_v1.sql"
    assert names[-8] == "20261112210400_e483_payment_failure_by_phone_v1.sql"
    assert names[-9] == "20261112210300_e483_native_condition_after_send_v1.sql"
    assert names[-10] == "20261112210200_e483_native_priority_wins_v1.sql"
    assert names[-11] == "20261112210100_e483_native_cancel_in_flight_v1.sql"
    assert names[-12] == "20261112210000_e483_native_multitouch_v1.sql"
    # La base no publica una version nativa que el motor no ejecuta (20261108004100, E-357).
    assert names[-19] == "20261108004100_flow_change_publish_native_version_guard_v1.sql"
    assert names[-20] == "20261108000100_flow_store_route_executor_v1.sql"
    # El cierre del caso por el cupon de una campana (20261106000100, E-339): una RPC de service_role.
    assert names[-22] == "20261106000100_recovery_case_segment_coupon_close_v1.sql"
    # El origen de un recorrido nativo para la campana por landing (20261103001700, E-321): una lectura de service_role.
    assert names[-23] == "20261103001700_flow_engine_native_campaign_origin_v1.sql"
    # La entrada a una campana: lo que contesto cada conversacion a la pregunta (20261103001500, E-317).
    assert names[-24] == "20261103001500_segment_entry_answers_v1.sql"
    assert names[-25] == "20261103001300_flow_engine_native_segment_label_v1.sql"
    assert names[-26] == "20261103001000_handoff_wait_alerts_v1.sql"
    assert names[-27] == "20261103000900_flow_engine_native_payment_failure_branches_v1.sql"
    assert names[-28] == "20261103000700_customer_status_v1.sql"
    assert names[-29] == "20261103000600_flow_approval_history_v1.sql"
    assert names[-30] == "20261103000500_meta_delivery_rejections_v1.sql"
    assert names[-31] == "20261102000400_identity_recycle_v1.sql"
    assert names[-32] == "20261102000300_flow_engine_native_silence_sweep_v1.sql"
    assert names[-33] == "20261102000200_meta_template_requests_v1.sql"
    assert names[-34] == "20261102000100_recovery_coupon_by_reply_v1.sql"
    assert names[-35] == "20261101000300_followup_stops_after_last_touch_and_pending_payment_v1.sql"
    assert names[-36] == "20261101000200_flow_engine_native_silence_v1.sql"
    # Antes, el primer contacto nativo (20261101000100, E-247): el disparador intencion.
    assert names[-37] == "20261101000100_flow_engine_native_intent_v1.sql"
    assert names[-38] == "20261030000400_portable_purchase_stops_see_our_link_v1.sql"
    assert names[-39] == "20261030000300_flow_change_list_v1.sql"
    assert names[-40] == "20261030000200_flow_engine_native_payment_failure_v1.sql"
    assert names[-41] == "20261030000100_flow_engine_native_legacy_double_case_v1.sql"
    assert names[-42] == "20261028000400_purchase_by_our_link_counts_v1.sql"
    assert names[-43] == "20261028000300_pilot_scope_offer_append_v1.sql"
    assert names[-44] == "20261028000200_flow_engine_native_cart_v1.sql"
    assert names[-45] == "20261028000100_flow_change_slack_approver_v1.sql"
    assert names[-46] == "20261026000200_flow_engine_native_offer_in_context_v1.sql"
    assert names[-47] == "20261026000100_flow_engine_native_template_shape_v1.sql"
    assert names[-48] == "20261025000400_purchase_agent_pause_v1.sql"
    assert names[-49] == "20261025000300_handoff_link_note_v1.sql"
    assert names[-50] == "20261025000200_payment_failure_product_name_btrim.sql"
    assert names[-51] == MIGRATION.name
    assert names[-52] == "20261024000600_flow_change_new_flow_v1.sql"
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
        " && node validate_flow_engine_native_cart.mjs --check"
        " && node validate_pilot_scope_offer_append.mjs"
        " && node validate_purchase_by_our_link_counts.mjs"
        " && node validate_flow_engine_native_legacy_double_case.mjs --check"
        " && node validate_flow_engine_native_payment_failure.mjs --check"
        " && node validate_flow_change_list.mjs --check"
        " && node validate_flow_engine_native_intent.mjs"
        " && node validate_flow_engine_native_silence.mjs"
        " && node validate_followup_stops.mjs && node validate_recovery_coupon_by_reply.mjs"
        " && node validate_meta_template_requests.mjs --check && node validate_flow_engine_native_silence_sweep.mjs && node validate_identity_recycle.mjs && node validate_meta_delivery_rejections.mjs && node validate_flow_approval_history.mjs && node validate_customer_status.mjs && node validate_flow_engine_native_payment_failure_branches.mjs --check && node validate_handoff_wait_alerts.mjs && node validate_flow_engine_native_segment_label.mjs && node validate_segment_entry_answers.mjs && node validate_flow_engine_native_campaign_origin.mjs --check && node validate_recovery_case_segment_coupon_close.mjs && node validate_meta_account_hold_retry.mjs && node validate_flow_store_route_executor.mjs --check && node validate_flow_change_publish_native_version_guard.mjs && node validate_agent_campaigns.mjs && node validate_meta_template_request_notices.mjs && node validate_recovery_case_no_touch_left_close.mjs && node validate_e483_native_multitouch.mjs && node validate_e483_native_cancel_in_flight.mjs && node validate_e483_native_priority_wins.mjs && node validate_e483_native_condition_after_send.mjs --check && node validate_e483_payment_failure_by_phone.mjs && node validate_e483_native_personal_coupon.mjs --check"
        " && node validate_e483_native_audience_source.mjs && node validate_flow_audience_triggers.mjs && node validate_flow_audience_native_e2e.mjs"
        " && node validate_e483_native_legacy_double_case_touches.mjs --check && node validate_e483_native_person_cap.mjs"
        " && node validate_e528_native_payment_slip.mjs && node validate_identity_link_decider.mjs")


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
