"""La base del primer contacto nativo (20261101000100, E-247, P20a de M16), leida como texto.

El comportamiento lo prueba tests/sql/followup_engine/validate_flow_engine_native_intent.mjs sobre
PGlite: el formulario capturado de Maribel abre el recorrido nativo en cohorte, los frenos del legado
cortan en la admision, la reevaluacion y el arranque, y sin un flujo nativo de intencion los tres
validadores del primer contacto del legado corren enteros con la entrada nueva. Aca se fija lo que
no tiene que cambiar sin que alguien lo lea: que no redefine el legado, el orden, los permisos y los
errores que se relanzan.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
MIGRATION = MIGRATIONS / "20261101000100_flow_engine_native_intent_v1.sql"
VALIDATOR = ROOT / "tests" / "sql" / "followup_engine" / "validate_flow_engine_native_intent.mjs"
PACKAGE = ROOT / "tests" / "sql" / "followup_engine" / "package.json"
FIXTURE = ROOT / "tests" / "fixtures" / "ghl_form_maribel_20261031.json"
ROUTE = "public.admit_and_route_portable_lead_precheckout_v1(text,text,integer,text,jsonb,jsonb,text,integer)"
STOP = "public._flow_engine_native_intent_stop_reason(uuid)"

# Las funciones del primer contacto del legado: el camino de ATT1, que no se redefine.
LEGACY = (
    "admit_and_plan_portable_lead_precheckout",
    "admit_portable_observed_lead_precheckout",
    "_portable_precheckout_stop_reason",
    "_find_portable_precheckout_contact",
    "_ensure_portable_precheckout_contact",
    "_plan_portable_precheckout_first_contact",
    "reevaluate_portable_precheckout_action",
    "mark_portable_precheckout_request_started",
    "_portable_consented_intent_reason",
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    return re.sub(r"--[^\n]*", "", _sql())


def _patched() -> list[str]:
    return re.findall(r"c_\w+ constant text :=\s*'public\.(\w+)\(", _sql()) + re.findall(
        r"flow_engine_native_intent_patch\(\s*'public\.(\w+)\(", _sql()
    )


def test_it_is_one_transaction_and_the_last() -> None:
    sql = _sql()
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))

    assert "\nbegin;\n" in sql and sql.rstrip().endswith("commit;")
    # Despues va el seguimiento a 24 h nativo (20261101000200, E-251).
    assert names[-1] == "20261101000200_flow_engine_native_silence_v1.sql"
    assert names[-2] == MIGRATION.name
    assert names[-3] == "20261030000400_portable_purchase_stops_see_our_link_v1.sql"
    # R9: el bloque 1 valida flow_engine_native_runs con ACCESS EXCLUSIVE.
    assert "set local lock_timeout = '5s';" in sql
    # El ALTER de flow_engine_native_runs va al final: retiene el ACCESS EXCLUSIVE lo menos posible.
    assert sql.index("alter table public.flow_engine_native_runs") > sql.index("$patch$;")
    # Correrla dos veces aborta, y necesita el pago rechazado nativo y el freno con nuestro link.
    assert "flow_engine_native_intent_already_applied" in sql
    assert "flow_engine_native_intent_missing" in sql
    assert "_flow_engine_native_payment_failure_audience(uuid,uuid,text,text,text)" in sql
    assert "_portable_checkout_link_purchase_exists(text,text,uuid,text,text)" in sql


def test_it_creates_four_functions_and_one_table() -> None:
    code = _code()

    assert re.findall(r"create function public\.(\w+)\(", code) == [
        "_flow_engine_native_intent_audience",
        "_flow_engine_native_intent_unclaimed",
        "admit_and_route_portable_lead_precheckout_v1",
        "_flow_engine_native_intent_stop_reason",
    ]
    # Reemplazadas enteras, solo las dos de una linea (como 20261030000200).
    assert re.findall(r"create or replace function public\.(\w+)\(", code) == [
        "_flow_engine_native_trigger",
        "_flow_engine_native_steps",
    ]
    assert re.findall(r"create table public\.(\w+)", code) == ["flow_engine_native_intent_admissions"]
    assert "alter table public.flow_engine_native_intent_admissions enable row level security;" in code
    assert "update public." not in code.lower() and "delete from" not in code.lower()


def test_it_never_redefines_the_legacy_first_contact() -> None:
    code = _code()
    patched = _patched()

    for name in LEGACY:
        assert name not in patched, name
        assert f"function public.{name}(" not in code, name
    # Lo que si se parchea por ancla contada.
    assert sorted(set(patched)) == sorted(
        {
            "_flow_engine_admit_native_flow",
            "_flow_engine_authorize_native_tail",
            "reevaluate_followup_action",
            "read_flow_engine_native_trigger_counts_v1",
            "admit_portable_inbound_commercial_case_v1",
            "read_automation_expert_native_history_v1",
        }
    )
    assert "unexpected_flow_engine_native_intent_definition" in code
    assert "flow_engine_native_intent_definition_not_replaced" in code
    assert "flow_engine_native_intent_owner_mismatch" in code


def test_without_a_native_flow_the_entry_is_the_legacy_one() -> None:
    body = _code().split("create function public.admit_and_route_portable_lead_precheckout_v1(", 1)[1]
    body = body.split("$function$;", 1)[0]

    # La misma firma que admit_and_plan_portable_lead_precheckout y sus cinco columnas primero.
    assert body.startswith(
        "\n    p_tenant_ref text, p_funnel_ref text, p_binding_version integer, p_external_submission_id text,\n"
        "    p_raw_payload jsonb, p_canonical_payload jsonb, p_legacy_scope_key text, p_legacy_scope_version integer)\n"
        "returns table(outcome text, submission_id uuid, purchase_intent_id uuid, plan_outcome text, plan_reason text,\n"
    )
    assert "security definer" in body
    # Sin flujo, el legado tal cual (o solo la admision, sin scope).
    assert "v_to_legacy := coalesce(cardinality(v_flow_ids), 0) = 0\n" in body
    # ... y sin ningun recorrido nativo de intencion en la base (ATT1 hoy).
    assert "where native_any.trigger_event = 'intencion');" in body
    # El toque nativo de 24 h que el D21 del legado no ve (el ancla flow_engine_event).
    assert "and touched.anchor_type = 'flow_engine_event'" in body
    assert "> clock_timestamp() - interval '24 hours'" in body
    assert "from public.admit_and_plan_portable_lead_precheckout(" in body
    assert body.count("from public.admit_portable_observed_lead_precheckout(") == 2
    # Decide la base, por los flujos de intencion del funnel del binding (no el cache del worker).
    assert "d.definition #>> '{dispara,evento}' = 'intencion'" in body
    assert "c.product_ref = v_binding.funnel_ref" in body


def test_the_entry_keeps_the_legacy_order_and_never_loses_a_form() -> None:
    body = _code().split("create function public.admit_and_route_portable_lead_precheckout_v1(", 1)[1]
    body = body.split("$function$;", 1)[0]

    # (a) control -> flujos -> intencion y contacto.
    control = body.index("from public.pilot_runtime_controls control")
    routes = body.index("from public.flow_engine_routes r where r.flow_id = any(v_flow_ids)")
    admission = body.index("from public.admit_portable_observed_lead_precheckout(")
    assert control < routes < admission
    # (c) los frenos sin contacto antes de crearlo (d).
    stop_without = body.index("public._portable_precheckout_stop_reason(v_intent_id, null)")
    ensure = body.index("public._ensure_portable_precheckout_contact(v_intent_id, v_submission_id)")
    stop_with = body.index("public._portable_precheckout_stop_reason(v_intent_id, v_contact_id)")
    anchor = body.index("insert into public.webhook_events (")
    assert stop_without < ensure < stop_with < anchor
    # (g) los transitorios se relanzan, en el flujo y en el bloque entero.
    transient = (
        "if v_sqlstate like '40%' or v_sqlstate like '53%' or v_sqlstate like '57%'\n"
    )
    assert body.count(transient) == 2
    assert body.count("or v_sqlstate like '08%' or v_sqlstate = '55P03' then\n") == 2
    # (i) los triggers diferidos corren adentro del flujo.
    assert "set constraints all immediate;" in body
    assert body.count("set constraints all deferred;") == 2
    # El ancla del nativo es distinta de la del legado.
    assert "'flow-engine-intent:' || v_submission_id::text" in body
    assert "precheckout-submission:" not in body


def test_the_permissions() -> None:
    code = _code()

    assert "'public._flow_engine_native_intent_audience(uuid,uuid,uuid,text,text,text)'," in code
    assert "revoke all on table public.flow_engine_native_intent_admissions from public;" in code
    grants = re.findall(r"grant execute on function public\.(\w+)\(", code)
    assert grants == ["admit_and_route_portable_lead_precheckout_v1", "_flow_engine_native_intent_stop_reason"]
    stop = code.split("create function public._flow_engine_native_intent_stop_reason(", 1)[1]
    stop = stop.split("$function$;", 1)[0]
    assert "security definer" in stop and "stable" in stop


def test_the_shared_branches_ask_for_intencion() -> None:
    sql = _sql()

    # La reevaluacion de todo (carrito, pago, compra de ATT1): el wrapper solo con intencion, una vez.
    assert (
        "    if v_case.context ->> ''flow_engine_trigger'' = ''intencion'' then\\n'\n"
        "        || E'        v_native_intent_stop := public._flow_engine_native_intent_stop_reason(v_case.id);\\n'"
    ) in sql
    assert sql.count("public._flow_engine_native_intent_stop_reason(v_case.id)") == 1
    assert "or (v_case.context ->> ''flow_engine_trigger'' = ''intencion''" in sql
    # La adopcion: solo el ancla nativa con intencion.
    assert "and recovery.context ->> ''flow_engine_trigger'' = ''intencion''" in sql
    # El arranque: los locks de opt-out de las dos formas.
    assert "''chatwoot-opt-out-user''" in sql
    assert "if v_native.trigger_event = ''intencion'' then" in sql


def test_the_validator_and_the_captured_form() -> None:
    validator = VALIDATOR.read_text(encoding="utf-8")
    scripts = json.loads(PACKAGE.read_text(encoding="utf-8"))["scripts"]["test"]
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # Despues va el validador del seguimiento a 24 h nativo (20261101000200, E-251).
    assert scripts.rstrip().endswith(
        "node validate_flow_engine_native_intent.mjs && node validate_flow_engine_native_silence.mjs")
    assert MIGRATION.name in validator
    for legacy in (
        "validate_portable_precheckout_first_contact.mjs",
        "validate_att1_portable_chain.mjs",
        "validate_portable_inbound_template_adoption.mjs",
    ):
        assert legacy in validator
    assert "ghl_form_maribel_20261031.json" in validator
    # El fixture: solo lectura, anonimizado, sin datos de la persona.
    assert "solo lectura" in fixture["origen"]
    text = FIXTURE.read_text(encoding="utf-8")
    assert re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text) == [
        "persona-1@example.test",
        "persona-1@example.test",
        "persona-2@example.test",
        "persona-2@example.test",
    ]
    # Los telefonos: solo los sinteticos (52155500000N y sus formas).
    phones = set(re.findall(r"(?<![A-Z\d])\d{9,}", re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z?", "", text)))
    assert phones <= {"521555000001", "521555000002", "1555000001", "1555000002"}, phones
    assert {envio["accion"]["terminal_reason"] for envio in fixture["envios"]} == {
        "accepted_by_chatwoot",
        "superseded_by_provider_event",
    }
