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
    assert names[-1] == "20261028000300_pilot_scope_offer_append_v1.sql"
    assert names[-2] == "20261028000200_flow_engine_native_cart_v1.sql"
    assert names[-3] == MIGRATION.name
    assert names[-4] == "20261026000200_flow_engine_native_offer_in_context_v1.sql"
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
