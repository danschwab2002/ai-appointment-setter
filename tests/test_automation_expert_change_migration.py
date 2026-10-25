"""La forma de 20261021000100 (automation-expert-change-v1).

Lo que la migracion hace en una base lo prueba PGlite
(tests/sql/followup_engine/validate_automation_expert_change.mjs) y, con la
imagen de Supabase de ATT1, la medicion del 2026-10-21 que cita el
encabezado. Aca se fija lo que tiene que seguir siendo cierto del texto: que
el rol solo recibe sus cuatro funciones, que ninguna va a un rol de la API, y
que no se toca la configuracion del rol.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20261021000100_automation_expert_change_v1.sql"
WRAPPERS = (
    "public.read_flow_store_v1(jsonb)",
    "public.save_flow_change_draft_v1(jsonb,uuid,integer,jsonb,jsonb,text,text)",
    "public.record_flow_change_validation_v1(jsonb,uuid,integer,jsonb,jsonb,jsonb,jsonb,text)",
    "public.get_flow_change_proposal_v1(jsonb,uuid)",
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return re.sub(r"--[^\n]*", "", _sql())


def test_it_is_one_transaction_and_comes_before_the_flow_engine_routes() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in (ROOT / "supabase" / "migrations").glob("*.sql"))
    # La migracion del motor (20261021000200) va despues: concede sus dos
    # lecturas a este rol, detras de to_regrole. El tope de la historia
    # (20261021000300) conserva esos permisos, y la lectura de /flows mas
    # rapida (20261021000400) no los toca. Las lecturas que cuentan lo del
    # motor (20261022000300) parchan por ancla y conservan los permisos.
    assert names[names.index(MIGRATION.name) + 1:] == [
        "20261021000200_flow_engine_routes_v1.sql",
        "20261021000300_flow_engine_history_cap_v1.sql",
        "20261021000400_automation_expert_flows_speed_v1.sql",
        "20261022000100_flow_engine_motor_carrito_v1.sql",
        "20261022000200_flow_engine_bridge_reads_v1.sql",
        "20261022000300_flow_engine_reads_see_motor_v1.sql",
        # El modo «solo guardar» de Hotmart (20261024000100) solo crea una
        # funcion nueva y no redefine ninguna.
        "20261024000100_portable_hotmart_listen_only_capture.sql",
        # El motor nativo (20261024000200) suma funciones y tablas y parcha por
        # ancla, sobre la definicion vigente, las lecturas y guardas del motor.
        "20261024000200_flow_engine_native_v1.sql",
        # F-036 (20261024000400) parcha por ancla la reserva y la autorizacion
        # del link; no toca el motor.
        "20261024000400_opted_out_reply_checkout_issuance.sql",
        # El bridge del motor nativo (20261024000500) suma lecturas y parcha la
        # adopcion de la respuesta; no redefine el motor del carrito.
        "20261024000500_flow_engine_native_bridge_v1.sql",
        # El AE que crea flujos (20261024000600) parcha el ciclo del almacen (el item
        # nuevo) y read_flow_store_v1; no redefine el motor del carrito.
        "20261024000600_flow_change_new_flow_v1.sql",
        # Las lecturas nativas del AE (20261025000100) suman lecturas y parchan el anclaje del AE y prender.
        "20261025000100_automation_expert_native_reads_v1.sql",
        # El btrim del nombre del producto en el pago rechazado (20261025000200, E-087) solo parcha
        # admit_portable_hotmart_payment_failure.
        "20261025000200_payment_failure_product_name_btrim.sql",
        # La lectura del link para la nota privada al derivar (20261025000300, F-034 D8):
        # una funcion nueva, no toca ninguna existente.
        "20261025000300_handoff_link_note_v1.sql",
        # Las conversaciones del comprador para pausar al agente (20261025000400,
        # E-100): una funcion nueva, no toca ninguna existente.
        "20261025000400_purchase_agent_pause_v1.sql",
    ]


def test_the_role_only_gets_its_four_functions_and_the_schema() -> None:
    code = _code()
    grants = re.findall(r"grant\s+(.+?)\s+to\s+automation_expert_change", code)
    assert grants == ["usage on schema public", "execute on function %s"]
    listed = re.search(r"foreach v_function in array array\[(.*?)\]::regprocedure\[\]", code, re.S)
    assert listed is not None
    assert tuple(re.findall(r"'([^']+)'", listed.group(1))) == WRAPPERS


def test_nothing_goes_to_an_api_role_and_the_role_has_no_settings() -> None:
    code = _code()
    assert not re.search(r"grant\s+[^;]*\bto\s+(anon|authenticated|service_role|public)\b", code)
    assert "alter role" not in code
    assert re.search(r"create role automation_expert_change nologin noinherit;", code)
    assert "grant automation_expert_change to authenticator;" in code


def test_the_wrappers_are_definer_with_a_fixed_search_path_and_approve_stays_invoker() -> None:
    code = _code()
    functions = re.findall(r"create or replace function (public\.\w+)\((.*?)\)\s+returns jsonb\s+language plpgsql"
                           r"\s+(\w+)\s+security (\w+)\s+set search_path = pg_catalog, public, pg_temp", code, re.S)
    by_name = {name: (volatility, security) for name, _, volatility, security in functions}
    assert by_name == {
        "public.read_flow_store_v1": ("stable", "definer"),
        "public.save_flow_change_draft_v1": ("volatile", "definer"),
        "public.record_flow_change_validation_v1": ("volatile", "definer"),
        "public.get_flow_change_proposal_v1": ("stable", "definer"),
        "public._flow_change_approve": ("volatile", "invoker"),
    }
    # Los envoltorios llaman a los helpers con la clase del agente, nunca con otra.
    assert code.count("'automation_expert', p_requested_by_ref)") == 2


def test_what_postgrest_answered_with_the_images_of_att1() -> None:
    # Capturado el 2026-10-21 con PostgREST v14.15 y la imagen de Supabase de
    # ATT1 (descartables), la cadena completa aplicada como postgres y un JWT de
    # prueba del rol. Es tambien el fixture de las respuestas de error del
    # cliente del bridge: PostgREST 14 manda P0002 como 500, no como 404.
    import json

    captured = json.loads((ROOT / "tests" / "fixtures" / "automation_expert_change"
                           / "postgrest_responses_automation_expert_change_20261021.json").read_text(encoding="utf-8"))
    responses = captured["responses"]
    assert responses["read_flow_store_ok"]["status"] == 200
    assert responses["read_flow_store_ok"]["body"]["products"] == ["att1"]
    assert responses["publish_denied_42501"]["status"] == 403
    assert responses["publish_denied_42501"]["body"]["code"] == "42501"
    assert (responses["get_proposal_not_found_P0002"]["status"],
            responses["get_proposal_not_found_P0002"]["body"]["message"]) == (500, "flow_change_not_found")
    assert (responses["save_items_invalid_22023"]["status"],
            responses["save_items_invalid_22023"]["body"]["message"]) == (400, "flow_change_items_invalid")
