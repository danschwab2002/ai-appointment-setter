"""El camino del motor para el carrito (20261022000100, la T2.3b del motor de flujos).

Lo que se prueba aca sin base: que sea una transaccion y la ultima migracion,
que las funciones parchadas se toquen solo por ancla (nunca con create or
replace), que cada ancla venga de la definicion vigente y aparezca con su
marca, que los permisos sean solo las cuatro de service_role, y que la
admision verifique la audiencia con el mismo helper que el envoltorio. El
comportamiento lo prueban validate_flow_engine_motor_v1.mjs y
validate_flow_engine_motor_parity.mjs (PGlite) y real_postgres_flow_engine.py.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261022000100_flow_engine_motor_carrito_v1.sql"

PATCHED = {
    # funcion: (marcas, la migracion que trae el texto del ancla)
    "_flow_engine_legacy_guard": (("flow_engine_motor_guard",), "20261021000200"),
    "operator_create_flow_route_v1": (("flow_engine_route_flow_on",), "20261021000200"),
    "plan_lancemos_pilot_cart_recovery": (("flow_engine_motor_delegation",), "20261021000200"),
    "mark_lancemos_pilot_request_started": (("flow_engine_motor_delegation",), "20261021000200"),
    "mark_followup_request_started": (("flow_engine_motor_authorization",), "20260810000300"),
    "reevaluate_followup_action": (("flow_engine_turned_off", "flow_engine_turned_off_close"), "20261019000300"),
}
SERVICE_ROLE = (
    "public._flow_engine_run_turned_off(uuid)",
    "public.admit_flow_engine_hotmart_v1(",
    "public.mark_flow_engine_request_started_v1(uuid, uuid, text, bigint, timestamptz)",
    "public.flow_engine_execution_context_v1(uuid)",
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return re.sub(r"--[^\n]*", "", _sql())


def test_it_is_one_transaction_and_comes_before_the_bridge_reads() -> None:
    sql = _sql()
    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    position = names.index(MIGRATION.name)
    # Va despues de las rutas de la T2.3a, del tope de la historia y de la
    # lectura de /flows mas rapida.
    assert names[position - 1] == "20261021000400_automation_expert_flows_speed_v1.sql"
    assert names[position - 2] == "20261021000300_flow_engine_history_cap_v1.sql"
    assert names[position - 3] == "20261021000200_flow_engine_routes_v1.sql"
    # Despues va lo que el bridge del motor lee y marca (la T2.4), que solo
    # crea tres funciones nuevas y no redefine ninguna de esta. Y las lecturas
    # que cuentan lo del motor (20261022000300), que parchan tres lecturas por
    # ancla y no redefinen ninguna funcion de esta.
    assert names[position + 1:] == [
        "20261022000200_flow_engine_bridge_reads_v1.sql",
        "20261022000300_flow_engine_reads_see_motor_v1.sql",
    ]
    later = (MIGRATIONS / names[position + 1]).read_text(encoding="utf-8")
    assert "create or replace" not in later.lower()
    reads = re.sub(r"--[^\n]*", "", (MIGRATIONS / names[position + 2]).read_text(encoding="utf-8")).lower()
    assert "create or replace" not in reads and "create function" not in reads
    # Correrla dos veces aborta.
    assert "flow_engine_motor_already_applied" in sql


def test_the_patched_functions_are_only_touched_by_anchor() -> None:
    code = _code().lower()
    for name in PATCHED:
        assert f"create or replace function public.{name}(" not in code, name
        assert f"create function public.{name}(" not in code, name
        assert f"'public.{name}(" in code, name
    # El unico reemplazo entero es la capacidad del motor.
    replaced = re.findall(r"create or replace function public\.([a-z_0-9]+)\(", code)
    assert replaced == ["_flow_engine_route_kinds"]
    assert "select array['carrito']::text[]" in code
    # El metodo de 20261007000100: la definicion vigente, el ancla una sola
    # vez, la marca ausente, el duenio, y la marca presente despues.
    assert code.count("pg_get_functiondef(") >= 8
    assert "unexpected_flow_engine_motor_definition" in code
    assert "flow_engine_motor_owner_mismatch" in code
    assert "flow_engine_motor_definition_not_replaced" in code


def test_every_marker_has_its_begin_and_end_and_its_anchor_comes_from_the_live_definition() -> None:
    sql = _sql()
    for name, (markers, origin) in PATCHED.items():
        for marker in markers:
            uses = sum(marker in other for other, _ in PATCHED.values())
            assert sql.count(f"-- {marker}: begin") == uses, marker
            assert sql.count(f"-- {marker}: end") == uses, marker
        origin_sql = next(MIGRATIONS.glob(f"{origin}_*.sql")).read_text(encoding="utf-8")
        assert name in origin_sql, (name, origin)
    anchors = {
        "    if v_route.executor = 'motor' then\n": "20261021000200",
        "    insert into public.flow_engine_routes (flow_id, executor, legacy_kind, legacy_scope_key, generation,\n":
            "20261021000200",
        "    -- flow_engine_legacy_guard: begin\n": "20261021000200",
        "        from public.pilot_outbound_request_authorizations authrow\n"
        "        where authrow.action_id = p_action_id\n"
        "          and authrow.attempt_id = p_attempt_id\n": "20260810000300",
        "    if v_decision = 'defer' then\n": "20260805000300",
        "            'purchase_detected_any_offer'\n        ) then\n": "20261019000300",
    }
    for anchor, origin in anchors.items():
        origin_sql = next(MIGRATIONS.glob(f"{origin}_*.sql")).read_text(encoding="utf-8")
        # En la migracion de origen, el texto esta tal cual (el cuerpo de la
        # funcion) o como literal E'' (un ancla de un parche anterior).
        lines = [line for line in anchor.split("\n") if line.strip()]
        assert anchor in origin_sql or all(line.replace("'", "''") in origin_sql for line in lines), (anchor, origin)
        # En esta migracion, el ancla va como literal E'', por partes.
        assert all(line.replace("'", "''") in sql for line in anchor.split("\n") if line.strip()), anchor


def test_only_the_four_of_service_role_are_granted() -> None:
    code = _code()
    grants = re.findall(r"grant execute on function (public\.[a-z_0-9]+\()", code)
    assert sorted(grants) == sorted({signature.split("(")[0] + "(" for signature in SERVICE_ROLE})
    assert len(re.findall(r"\bgrant\b", code)) == 4
    assert code.count("if to_regrole('service_role') is not null then") == 4
    # Toda funcion nueva se revoca a public y a cada rol de la API.
    created = set(re.findall(r"create function public\.([a-z_0-9]+)\(", code))
    acl = "".join(code.split("do $acl_")[1:])
    revoked = set(re.findall(r"public\.([a-z_0-9]+)\(", acl))
    assert created <= revoked, created - revoked
    assert len(created) == 15
    assert "anon" in acl and "authenticated" in acl


def test_the_admission_binds_the_audience_with_the_wrapper_helper() -> None:
    code = _code()
    helper = code.split("create function public._portable_cart_audience_checks(", 1)[1].split("$function$;", 1)[0]
    assert "public._lancemos_pilot_audience_intent(" in helper
    assert "correlation.outcome = 'resolved'" in helper
    # La admision no llama a la evaluacion del scope: la regla de la audiencia
    # (validate_pilot_scope_audience_mode.mjs) queda como esta.
    assert "evaluate_lancemos_pilot_scope(" not in code
    # Y no redefine la historia (el tope de 20261021000300 queda).
    assert "read_flow_engine_history_v1" not in code


def test_the_reasons_for_the_bridge_of_today_have_no_colon_nor_digits() -> None:
    code = _code()
    # El plan: el bridge de hoy descarta un motivo con dos puntos o digitos.
    assert "translate(v_flow_engine_admission.engine_reason, '':0123456789'', ''_'')" in code
    # El arranque: sin dos puntos.
    assert "translate(v_flow_engine_auth.engine_reason, '':'', ''_'')" in code
    # La admision y el arranque del motor nunca devuelven otra forma de motivo
    # que la de las tablas del motor.
    assert "'^[a-z0-9_:]{1,80}$'" in code


def test_the_turned_off_run_closes_the_attempt_as_failed_before_request() -> None:
    code = _code()
    block = code.split("elsif v_outcome = 'terminado' then", 1)[1].split("end if;\n\n    return query", 1)[0]
    # El intento reservado no tiene request_started_at: el CHECK de
    # followup_delivery_attempts solo deja failed_before_request.
    assert "outcome = 'failed_before_request'" in block
    assert "reason_code = 'flujo_apagado'" in block
    assert "for update of contact_row" in block
    assert "current_action_lease_not_found" in block
