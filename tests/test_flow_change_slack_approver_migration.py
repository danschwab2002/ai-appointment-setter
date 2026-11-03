"""La migracion de la pagina de aprobacion (20261028000100, A′ PR 2), leida como texto.

Lo que corre se prueba en PGlite (tests/sql/followup_engine/validate_flow_change_slack_approver.mjs):
aca se fija la forma, para que un cambio de la migracion no pase sin que alguien lo mire.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20261028000100_flow_change_slack_approver_v1.sql"
PACKAGE = ROOT / "tests/sql/followup_engine/package.json"

WRAPPERS = (
    "begin_flow_approval_oidc_v1(jsonb,text,text,text,timestamptz)",
    "consume_flow_approval_oidc_v1(jsonb,text)",
    "create_flow_approval_session_v1(jsonb,text,text,text,text,timestamptz)",
    "validate_flow_approval_session_v1(jsonb,text)",
    "revoke_flow_approval_session_v1(jsonb,text)",
    "approver_get_flow_change_proposal_v1(jsonb,text,uuid)",
    "approver_read_flow_store_v1(jsonb,text)",
    "approver_read_open_flow_changes_v1(jsonb,text,uuid)",
    "approver_approve_flow_change_v1(jsonb,text,uuid,text)",
    "approver_publish_flow_change_v1(jsonb,text,uuid,text,jsonb)",
    "approver_pause_flow_v1(jsonb,text,text,text,bigint,text)",
    "approver_discard_flow_change_v1(jsonb,text,uuid,text)",
    "approver_flow_cohort_v1(jsonb,text,text,text,text,text)",
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return re.sub(r"--[^\n]*", "", _sql())


def test_it_is_one_transaction_and_comes_before_the_native_cart() -> None:
    sql = _sql()
    assert sql.count("\nbegin;\n") == 1
    assert sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATION.parent.glob("*.sql"))
    # Despues van el carrito nativo (20261028000200, E-161) y sumar ofertas (20261028000300, E-163),
    # que no tocan lo de esta.
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
    assert names[-1] == "20261103001300_flow_engine_native_segment_label_v1.sql"
    assert names[-2] == "20261103001000_handoff_wait_alerts_v1.sql"
    assert names[-3] == "20261103000900_flow_engine_native_payment_failure_branches_v1.sql"
    assert names[-4] == "20261103000700_customer_status_v1.sql"
    assert names[-5] == "20261103000600_flow_approval_history_v1.sql"
    assert names[-6] == "20261103000500_meta_delivery_rejections_v1.sql"
    assert names[-7] == "20261102000400_identity_recycle_v1.sql"
    assert names[-8] == "20261102000300_flow_engine_native_silence_sweep_v1.sql"
    assert names[-9] == "20261102000200_meta_template_requests_v1.sql"
    assert names[-10] == "20261102000100_recovery_coupon_by_reply_v1.sql"
    assert names[-11] == "20261101000300_followup_stops_after_last_touch_and_pending_payment_v1.sql"
    assert names[-12] == "20261101000200_flow_engine_native_silence_v1.sql"
    # Antes, el primer contacto nativo (20261101000100, E-247): el disparador intencion.
    assert names[-13] == "20261101000100_flow_engine_native_intent_v1.sql"
    assert names[-14] == "20261030000400_portable_purchase_stops_see_our_link_v1.sql"
    assert names[-15] == "20261030000300_flow_change_list_v1.sql"
    assert names[-16] == "20261030000200_flow_engine_native_payment_failure_v1.sql"
    assert names[-17] == "20261030000100_flow_engine_native_legacy_double_case_v1.sql"
    assert names[-18] == "20261028000400_purchase_by_our_link_counts_v1.sql"
    assert names[-19] == "20261028000300_pilot_scope_offer_append_v1.sql"
    assert names[-20] == "20261028000200_flow_engine_native_cart_v1.sql"
    assert names[-21] == MIGRATION.name
    assert names[-22] == "20261026000200_flow_engine_native_offer_in_context_v1.sql"
    # Correrla dos veces aborta, y necesita el almacen y la cohorte del operador.
    assert "flow_change_slack_approver_already_applied" in sql
    assert "flow_change_store_missing" in sql


def test_the_validator_is_in_the_npm_chain() -> None:
    chain = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    assert chain.rstrip().endswith(
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
        " && node validate_meta_template_requests.mjs --check && node validate_flow_engine_native_silence_sweep.mjs"
        " && node validate_identity_recycle.mjs && node validate_meta_delivery_rejections.mjs && node validate_flow_approval_history.mjs && node validate_customer_status.mjs && node validate_flow_engine_native_payment_failure_branches.mjs --check && node validate_handoff_wait_alerts.mjs && node validate_flow_engine_native_segment_label.mjs"
    )


def test_only_the_role_executes_its_wrappers_and_nobody_the_internals() -> None:
    code = _code()
    acl = code.split("do $acl$", 1)[1]
    for signature in WRAPPERS:
        assert f"'public.{signature}'" in acl, signature
    assert "'public._flow_approval_session_actor(text,text,text)'" in acl
    assert "'public._flow_cohort_change(text,text,text,text,text,text,text,text,text)'" in acl
    # Un solo grant de funciones, al rol, y los dos internos excluidos.
    assert acl.count("grant execute on function") == 1
    assert "to flow_change_approver" in acl
    assert "service_role" not in acl.replace("'anon', 'authenticated', 'service_role'", "")
    assert re.search(r"grant\s+[a-z, ]+\s+on\s+table", code) is None


def test_the_wrappers_are_definer_with_a_fixed_search_path_and_the_actor_comes_from_the_session() -> None:
    code = _code()
    for name in (signature.split("(", 1)[0] for signature in WRAPPERS):
        body = code.split(f"create function public.{name}(", 1)[1].split("$function$;", 1)[0]
        assert "security definer" in body, name
        assert "set search_path = pg_catalog, public, pg_temp" in body, name
        if name.startswith("approver_"):
            # El ID de Slack sale de la fila de la sesion, nunca de un parametro.
            assert "_flow_approval_session_actor(" in body, name
            assert "p_slack_user_id" not in body, name
            assert "p_approver_ref" not in body, name


def test_the_session_tables_keep_only_hashes() -> None:
    code = _code()
    for table in ("flow_approval_oidc_states", "flow_approval_sessions"):
        body = code.split(f"create table public.{table} (", 1)[1].split(");", 1)[0]
        assert "token" not in body
        assert "code " not in body
        assert "id_token" not in body
    assert "interval '10 minutes'" in code
    assert "interval '8 hours'" in code


def test_the_page_only_adds_to_the_cohort() -> None:
    code = _code()
    page = code.split("create function public.approver_flow_cohort_v1(", 1)[1].split("$function$;", 1)[0]
    assert "'agregar', p_phone, 'slack', v_actor" in page
    helper = code.split("create function public._flow_cohort_change(", 1)[1].split("$function$;", 1)[0]
    assert "(p_actor_kind = 'slack' and p_action = 'agregar')" in helper
