"""El motor nativo en la base (migracion 20261024000200, el PR 2 del hito 4).

Lo que se puede leer en el texto de la migracion. El comportamiento lo prueba
tests/sql/followup_engine/validate_flow_engine_native.mjs contra la cadena
completa (PGlite), con un rojo a proposito por pieza.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261024000200_flow_engine_native_v1.sql"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"

# Las funciones vigentes que la migracion parcha por ancla, y su marca.
PATCHED = {
    "public.validate_flow_engine_route_insert()": "flow_engine_native_route: begin",
    "public.validate_flow_engine_route_update()": "flow_engine_native_route: begin",
    "public.operator_route_flow_v1(jsonb,text,text,text,bigint,text,text,jsonb)": "flow_engine_native_route: begin",
    "public.validate_flow_control_change()": "flow_engine_native_cohort: begin",
    "public._flow_engine_compile_policy_core(jsonb,text,integer,text,text)": "flow_engine_native_shape",
    "public._flow_engine_authorize_start(uuid,uuid,timestamptz,boolean)": "flow_engine_native_start: begin",
    "public.flow_engine_execution_context_v1(uuid)": "flow_engine_native_steps",
    "public.read_flow_engine_v1(jsonb,text)": "flow_engine_native_excluded",
    "public.read_flow_engine_routes_v1(jsonb)": "flow_engine_native_excluded",
    "public.read_flow_engine_history_v1(jsonb,text,timestamptz)": "flow_engine_native_excluded",
}


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return re.sub(r"--[^\n]*", "", _sql())


def test_it_is_one_transaction_and_the_last() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    # Despues van F-036 (20261024000400), que parcha el link del agente, el
    # bridge del motor nativo (20261024000500) y el AE que crea flujos
    # (20261024000600), que llama a la apertura de esta sin redefinirla.
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
    assert names[-1] == "20261101000300_followup_stops_after_last_touch_and_pending_payment_v1.sql"
    assert names[-2] == "20261101000200_flow_engine_native_silence_v1.sql"
    # Antes, el primer contacto nativo (20261101000100, E-247): el disparador intencion.
    assert names[-3] == "20261101000100_flow_engine_native_intent_v1.sql"
    assert names[-4] == "20261030000400_portable_purchase_stops_see_our_link_v1.sql"
    assert names[-5] == "20261030000300_flow_change_list_v1.sql"
    assert names[-6] == "20261030000200_flow_engine_native_payment_failure_v1.sql"
    assert names[-7] == "20261030000100_flow_engine_native_legacy_double_case_v1.sql"
    assert names[-8] == "20261028000400_purchase_by_our_link_counts_v1.sql"
    assert names[-9] == "20261028000300_pilot_scope_offer_append_v1.sql"
    assert names[-10] == "20261028000200_flow_engine_native_cart_v1.sql"
    assert names[-11] == "20261028000100_flow_change_slack_approver_v1.sql"
    assert names[-12] == "20261026000200_flow_engine_native_offer_in_context_v1.sql"
    assert names[-13] == "20261026000100_flow_engine_native_template_shape_v1.sql"
    assert names[-14] == "20261025000400_purchase_agent_pause_v1.sql"
    assert names[-15] == "20261025000300_handoff_link_note_v1.sql"
    assert names[-16] == "20261025000200_payment_failure_product_name_btrim.sql"
    assert names[-21] == MIGRATION.name
    assert names[-20] == "20261024000400_opted_out_reply_checkout_issuance.sql"
    assert names[-19] == "20261024000500_flow_engine_native_bridge_v1.sql"
    assert names[-18] == "20261024000600_flow_change_new_flow_v1.sql"
    assert names[-17] == "20261025000100_automation_expert_native_reads_v1.sql"
    assert names[-22] == "20261024000100_portable_hotmart_listen_only_capture.sql"
    # Correrla dos veces aborta, y necesita el camino del motor del carrito.
    assert "flow_engine_native_already_applied" in sql
    assert "flow_engine_motor_missing" in sql


def test_the_patches_go_by_anchor_on_the_current_definition() -> None:
    code = _code()
    # Ninguna funcion vigente se reescribe a mano: los reemplazos son por
    # ancla sobre pg_get_functiondef, con el duenio verificado.
    assert "create or replace function" not in code.lower()
    assert "pg_get_functiondef" in code
    for reason in ("flow_engine_native_owner_mismatch", "unexpected_flow_engine_native_definition",
                   "flow_engine_native_definition_not_replaced", "flow_engine_native_function_missing"):
        assert reason in code
    for signature, marker in PATCHED.items():
        assert f"'{signature}'" in code, signature
        assert marker in code, marker
    # La compra que dispara un flujo nativo no se frena sin tocar el freno: lo
    # mide el validador de PGlite.
    assert "_portable_case_purchase_stop_reason(uuid)'" not in code


def test_only_the_admission_is_granted_and_the_tables_are_closed() -> None:
    code = _code()
    grants = re.findall(r"grant execute on function ([^\s]+\([^)]*\)) to (\w+)", code)
    assert grants == [("public.admit_flow_engine_event_v1(jsonb, jsonb)", "service_role")]
    for table in ("flow_engine_pools", "flow_cohort_members", "flow_engine_native_runs"):
        assert f"create table public.{table}" in code
        assert f"alter table public.{table} enable row level security" in code
        assert f"public.{table}" in code.split("revoke all on table", 1)[1]


def test_the_native_route_and_the_pool_rule_are_in_the_base() -> None:
    code = _code()
    assert "flow_engine_routes_native_shape" in code
    assert "alter column legacy_kind drop not null" in code
    assert "'apagado', 'prendido', 'pausado', 'cohorte'" in code
    # Una bolsa nativa no se llama como un scope, y un scope no se llama como
    # una bolsa: los dos lados.
    assert "flow_engine_pool_is_legacy_scope" in code
    assert "pilot_scope_is_native_pool" in code
    assert "before insert on public.pilot_scope_versions" in code
    # La ruta nativa no se mueve.
    assert code.count("flow_route_native_cannot_move") >= 2


def test_the_native_shape_is_one_send_and_the_legacy_shape_is_untouched() -> None:
    code = _code()
    shape = re.search(r"when 'nativo:compra' then\s*'(\[.*?\])'::jsonb", code, re.S)
    assert shape is not None
    steps = json.loads(shape.group(1))
    assert [step["step_key"] for step in steps] == ["first_contact"]
    assert steps[0]["variables"] == ["nombre", "producto"]
    # La forma del legado (_flow_engine_legacy_steps) no se redefine: la del
    # nativo va con un coalesce y da null para todo legacy_kind.
    assert "_flow_engine_legacy_steps(text)'" not in code
    assert "coalesce(public._flow_engine_legacy_steps(p_legacy_kind)" in code


def test_the_validator_is_in_the_npm_chain() -> None:
    chain = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.rstrip().endswith(
        "node validate_flow_engine_native.mjs"
        " && node validate_opted_out_reply_issuance.mjs"
        " && node validate_flow_engine_native_bridge.mjs --emit-bridge-fixtures --check"
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
        " && node validate_flow_engine_native_silence.mjs"
        " && node validate_followup_stops.mjs"
    )
