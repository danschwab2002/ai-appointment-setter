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
    assert names[-1] == "20261101000200_flow_engine_native_silence_v1.sql"
    # Antes, el primer contacto nativo (20261101000100, E-247): el disparador intencion.
    assert names[-2] == "20261101000100_flow_engine_native_intent_v1.sql"
    assert names[-3] == "20261030000400_portable_purchase_stops_see_our_link_v1.sql"
    assert names[-4] == "20261030000300_flow_change_list_v1.sql"
    assert names[-5] == "20261030000200_flow_engine_native_payment_failure_v1.sql"
    assert names[-6] == "20261030000100_flow_engine_native_legacy_double_case_v1.sql"
    assert names[-7] == "20261028000400_purchase_by_our_link_counts_v1.sql"
    assert names[-8] == "20261028000300_pilot_scope_offer_append_v1.sql"
    assert names[-9] == "20261028000200_flow_engine_native_cart_v1.sql"
    assert names[-10] == "20261028000100_flow_change_slack_approver_v1.sql"
    assert names[-11] == "20261026000200_flow_engine_native_offer_in_context_v1.sql"
    assert names[-12] == "20261026000100_flow_engine_native_template_shape_v1.sql"
    assert names[-13] == "20261025000400_purchase_agent_pause_v1.sql"
    assert names[-14] == "20261025000300_handoff_link_note_v1.sql"
    assert names[-15] == "20261025000200_payment_failure_product_name_btrim.sql"
    assert names[-16] == "20261025000100_automation_expert_native_reads_v1.sql"
    assert names[-17] == "20261024000600_flow_change_new_flow_v1.sql"
    assert names[-18] == MIGRATION.name
    assert names[-19] == "20261024000400_opted_out_reply_checkout_issuance.sql"
    assert names[-20] == "20261024000200_flow_engine_native_v1.sql"


def test_the_validator_is_in_the_npm_chain() -> None:
    package = json.loads((ROOT / "tests" / "sql" / "followup_engine" / "package.json").read_text())
    chain = package["scripts"]["test"]
    assert chain.rstrip().endswith("node validate_flow_engine_native_bridge.mjs --emit-bridge-fixtures --check"
                                   " && node validate_flow_change_new_flow.mjs"
                                   " && node validate_automation_expert_native_reads.mjs"
                                   " && node validate_payment_failure_product_name_btrim.mjs"
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
        " && node validate_flow_engine_native_silence.mjs")


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
