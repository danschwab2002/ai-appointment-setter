"""El bridge del primer contacto nativo: el formulario entra al motor nativo (E-252, P20b de M16).

Contrato: ``docs/contracts/flow-engine-v2.md``, seccion 25 (2.13.0). La base es P20a
(``20261101000100_flow_engine_native_intent_v1.sql``, E-247): la RPC
``admit_and_route_portable_lead_precheckout_v1`` admite el envio y decide en la misma
transaccion si es de un flujo nativo de ``intencion`` o del legado. Este PR le pone las
puntas del bridge:

* ``FLOW_ENGINE_NATIVE_INTENT_ENABLED`` (apagado por defecto). **Apagado, el formulario va
  exactamente como hoy**: la misma RPC del legado con los mismos argumentos, o solo la
  admision, y la misma respuesta HTTP en ``/webhooks/lead`` y en el adaptador de GHL. Es el
  camino de ATT1 (unos 125 primeros contactos por semana) y el de Maribel hasta el corte,
  tambien con ``FLOW_ENGINE_ENABLED`` prendido: lo fijan los tests de ``apagado``.
* Prendido, el formulario del producto principal va a la RPC nueva, con el scope del legado
  solo si el flag del legado esta prendido; la respuesta a GHL es la misma, y un error de la
  base es 503 (GHL reintenta; la RPC es idempotente por el id del envio).
* El motor nativo acepta ``intencion`` (``ENGINE_CAPABILITIES_NATIVE``, con ``permiso:
  formulario``): la definicion del primer contacto de Maribel (diseno §2.1) pasa la regla 14 y
  el ciclo. La simulacion cuenta la repeticion como la base (D21: de cualquier flujo nativo de
  ``intencion``, ``ya_disparado`` desde el envio).
* El envio nativo del formulario respeta G3 y el reuso de la conversacion, como la bienvenida.

Datos: el formulario de GHL es la captura real de ATT1
(``tests/fixtures/ghl/ghl_form_webhook_att1_ads_a_20260929.json``); el de ``/webhooks/lead``,
el payload de ATT1 de ``test_lead_precheckout_http``; los dos formularios de Maribel
(``tests/fixtures/ghl_form_maribel_20261031.json``, anonimizados en el servidor) son los que
viajan a la RPC en el test del cliente, y sus 60 minutos medidos son la espera de la definicion.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import test_flow_engine_native_template_shape as shape
from test_flow_engine_native_dispatch import _NativeAuthority, _step
from test_flow_engine_native_quick_replies import NAME
from test_flow_engine_native_quick_replies import maribel_catalog  # noqa: F401 - el fixture del validador
from test_flow_engine_native_send_cuts import (
    LAST_INBOUND_6,
    MARIBEL_WINDOW,
    _assert_blocked,
    _assert_sent,
    _dispatcher,
    _listing,
    _LiveContactChatwoot,
    _run,
)
from test_ghl_precheckout_adapter_http import (
    ATT1_INSTANCE,
    TEST_RISK_ACCEPTANCE,
    TOKEN,
    _ads_a,
    _landing_d,
    _first_contact_off,
    _first_contact_through_ghl,
    _post,
)
from test_instance_wiring import _FirstContactAuthority, _first_contact_app, _first_contact_settings
from test_lead_precheckout_http import _post_att1_form
from test_product_label import _by_product
from test_product_routing import _ghl_manifest

import bridge.app as app_module
from bridge.app import Settings
from bridge.flow_change_api import _simulation_inputs
from bridge.flow_engine import (
    ENGINE_CAPABILITIES_NATIVE,
    NATIVE_PERMISSION_BY_TRIGGER,
    EngineFlow,
    Firing,
    PersonView,
    RunView,
    StartDecision,
    native_not_supported,
    plan_start,
)
from bridge.instance_manifest import InstanceManifest
from bridge.messaging import (
    FIRST_TOUCH_CONTACT_ACTIVE_CONVERSATION,
    FIRST_TOUCH_CONTACT_CONVERSATION_WITH_TEAM,
)
from bridge.native_flows import (
    NATIVE_CART_REFUSED,
    NATIVE_CART_STARTED,
    NATIVE_CART_UNCLAIMED,
    NATIVE_INTENT_TRIGGER,
    native_intent_admission,
)
from bridge.supabase import (
    NATIVE_FLOW_ROW_INVALID,
    PrecheckoutAdmissionResult,
    PrecheckoutNativeFlowOutcome,
    SupabaseClient,
    SupabaseError,
    SupabaseRpcMissing,
)

FIXTURES = Path(__file__).parent / "fixtures"
MARIBEL_FORMS = json.loads((FIXTURES / "ghl_form_maribel_20261031.json").read_text(encoding="utf-8"))
SUBMISSION_ID = "bfc778e7-5c9f-45e6-a910-651f92312157"
INTENT_ID = "1f581f3a-c469-45da-8208-9483d1b26f0b"
FLOW_ID = "5b9c3f0e-7d1a-4c2b-9e8f-0a1b2c3d4e5f"
CASE_ID = "8d2e4f6a-1b3c-4d5e-8f9a-0b1c2d3e4f5a"
FLAG = "FLOW_ENGINE_NATIVE_INTENT_ENABLED"

# El primer contacto de Maribel como flujo nativo, con los valores de hoy (diseno §2.1; la politica
# maribel-primer-contacto-formulario v2 y el scope maribel-primer-contacto v4, medidos en su base el
# 31/10): espera de 60 min, 00:00-23:59 CDMX, vence a 1 d, 150 por dia y 3.000, una por persona y
# otra a las 24 h. El producto es el del runtime de prueba.
FIRST_CONTACT = {
    "esquema": "setter-flujo/v1", "flujo": "primer_contacto", "producto": "att1", "estado": "borrador",
    "nombre": "Primer contacto tras el formulario", "dispara": {"evento": "intencion"},
    "entra": {"permiso": "formulario", "contesto_a": []},
    "pasos": [{"esperar": "60m"}, {"plantilla": NAME, "idioma": "es_MX", "variables": ["nombre", "producto"]}],
    "termina": {"si": ["compra"], "vence": "1d"},
    "horario": {"zona": "America/Mexico_City", "desde": "00:00", "hasta": "23:59"},
    "topes": {"bolsa": "maribel-primer-contacto-nativo", "por_dia": 150, "total": 3000,
              "repeticion": {"una_por": "persona", "otra_despues_de": "24h"}},
    "prioridad": {"le_gana_a": [], "no_arranca_mientras": []},
}


# -- La forma de los fixtures ------------------------------------------------------


def test_the_maribel_forms_are_the_real_ones_and_waited_sixty_minutes() -> None:
    cases = [envio["caso"] for envio in MARIBEL_FORMS["envios"]]
    assert cases == ["primer_contacto_aceptado", "cortado_por_el_carrito"]
    assert all(envio["contract_version"] == "1.1.0" for envio in MARIBEL_FORMS["envios"])
    assert [envio["accion"]["minutos_hasta_el_envio"] for envio in MARIBEL_FORMS["envios"]] == [60, 60]
    # La espera de la definicion es la medida del legado.
    assert FIRST_CONTACT["pasos"][0] == {"esperar": "60m"}


# -- La regla 14 y el ciclo ----------------------------------------------------------


def test_the_native_engine_runs_the_maribel_first_contact() -> None:
    assert native_not_supported(FIRST_CONTACT) == ()
    assert NATIVE_INTENT_TRIGGER == "intencion" in ENGINE_CAPABILITIES_NATIVE.triggers
    assert NATIVE_PERMISSION_BY_TRIGGER["intencion"] == "formulario"


@pytest.mark.parametrize(("change", "code"), [
    ({"entra": {"permiso": "compra"}}, "motor_nativo:permiso"),
    ({"entra": {"permiso": "conversacion"}}, "motor_nativo:permiso"),
    ({"pasos": [{"plantilla": NAME}, {"esperar": "1h"}, {"plantilla": NAME}]}, "engine_not_supported:demasiados_envios"),
    ({"termina": {"si": ["compra", "contesta"], "vence": "1d"}}, "engine_not_supported:salida_contesta"),
    ({"topes": {**FIRST_CONTACT["topes"], "repeticion": {"una_por": "conversacion", "otra_despues_de": "24h"}}},
     "motor_nativo:repeticion"),
])
def test_what_the_native_engine_does_not_run_with_the_form(change: dict, code: str) -> None:
    assert code in native_not_supported({**copy.deepcopy(FIRST_CONTACT), **change})


def test_without_intencion_the_form_definition_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    # El rojo de este PR: sin intencion en las capacidades, la definicion de Maribel no pasa.
    import bridge.flow_engine as engine

    monkeypatch.setattr(engine, "ENGINE_CAPABILITIES_NATIVE", replace(
        ENGINE_CAPABILITIES_NATIVE, triggers=ENGINE_CAPABILITIES_NATIVE.triggers - {"intencion"}))
    monkeypatch.setattr(engine, "NATIVE_PERMISSION_BY_TRIGGER",
                        {k: v for k, v in NATIVE_PERMISSION_BY_TRIGGER.items() if k != "intencion"})
    assert "motor_nativo:disparador" in engine.native_not_supported(FIRST_CONTACT)


def test_the_cycle_validates_the_maribel_first_contact(maribel_catalog: None,  # noqa: F811
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    def item(pasos: list) -> dict:
        definition = {key: copy.deepcopy(FIRST_CONTACT[key])
                      for key in ("nombre", "dispara", "entra", "termina", "horario", "topes")}
        return {"producto": "att1", "flujo": "primer_contacto", "tipo": "nuevo", "prender": "cohorte",
                "definicion": {**definition, "pasos": pasos}}

    monkeypatch.setattr(shape, "_item", item)
    _, result = shape._propose_and_validate(copy.deepcopy(FIRST_CONTACT["pasos"]))

    assert result["blocks"] == [], result["blocks"]
    assert result["status"] == "validada"


def test_the_simulation_of_a_new_form_flow_counts_the_forms() -> None:
    # flow_change_api pasa el disparador tal cual: con intencion, la base cuenta los formularios
    # (bloque 8 de P20a). Sin este PR, la validacion ya frenaba antes de llegar aca.
    asked: list[tuple[str, str]] = []

    class _Store:
        async def read_new_flow_counts(self, instance: object, product_ref: str, trigger: str,
                                       since: datetime) -> dict[str, object]:
            asked.append((product_ref, trigger))
            return {"contract": "flow-engine-v2", "trigger": trigger, "product_ref": product_ref,
                    "since": since.isoformat(), "until": since.isoformat(), "events": 86, "people": 80}

    items = [{"producto": "att1", "flujo": "primer_contacto", "tipo": "nuevo", "prender": "cohorte",
              "definicion": copy.deepcopy(FIRST_CONTACT)}]
    inputs = asyncio.run(_simulation_inputs(_Store(), {"tenant_ref": "lancemos", "ally_ref": "att1"}, items))

    assert asked == [("att1", "intencion")]
    assert inputs["new_flow_counts"]["events"] == 86


# -- La simulacion cuenta la repeticion como la base (D21) ---------------------------

T0 = datetime(2026, 10, 31, 21, 18, tzinfo=UTC)


def _flow(key: str, *, legacy_kind: str | None = None, again: str = "24h") -> EngineFlow:
    definition = copy.deepcopy(FIRST_CONTACT)
    definition["topes"]["repeticion"]["otra_despues_de"] = again
    return EngineFlow(flow_id=f"id-{key}", product_ref="att1", flow_key=key, version=1, definition=definition,
                      legacy_kind=legacy_kind, fixed_zone=None, pool="maribel-primer-contacto-nativo",
                      exceptions=frozenset())


def _fire(at: datetime) -> Firing:
    return Firing(source_ref=f"form-{at.isoformat()}", event="intencion", at=at, received_at=at,
                  product_ref="att1", offer_ref="u8ikyx78", contact_ref="contact-1",
                  intent_refs=frozenset({"intent-1"}), permission="formulario")


def _run_of(flow: EngineFlow, started: datetime, *, sends: tuple[datetime, ...], closed: datetime | None) -> RunView:
    return RunView(run_ref=f"run-{flow.flow_key}", flow=flow.ref, version=1, firing=_fire(started),
                   started_at=started + timedelta(minutes=60), sends=sends, closed_at=closed)


NATIVE = _flow("primer_contacto")
OTHER_NATIVE = _flow("primer_contacto_b")
LEGACY = _flow("precheckout", legacy_kind="precheckout")
FLOWS = {flow.ref: flow for flow in (NATIVE, OTHER_NATIVE, LEGACY)}


def test_a_form_five_hours_after_a_cut_without_a_send_starts() -> None:
    # El carrito corto el primero a los 60 min sin mandar: el segundo formulario arranca (D21).
    cut = _run_of(NATIVE, T0, sends=(), closed=T0 + timedelta(minutes=60))
    decision = plan_start(NATIVE, _fire(T0 + timedelta(hours=5)), PersonView((cut,), ()), flows=FLOWS)
    assert decision == StartDecision(True, None, None)


def test_a_form_five_hours_after_a_send_is_ya_disparado_like_the_base() -> None:
    sent = _run_of(NATIVE, T0, sends=(T0 + timedelta(minutes=60),), closed=T0 + timedelta(minutes=61))
    decision = plan_start(NATIVE, _fire(T0 + timedelta(hours=5)), PersonView((sent,), ()), flows=FLOWS)
    assert decision == StartDecision(False, "ya_disparado", None)


def test_the_window_counts_from_the_send_not_from_the_start() -> None:
    # Arranco 30 h antes y mando hace 23 h (a las 7 h de esperar): frena. A las 25 h del envio, no.
    sent = _run_of(NATIVE, T0, sends=(T0 + timedelta(hours=7),), closed=T0 + timedelta(hours=7))
    assert plan_start(NATIVE, _fire(T0 + timedelta(hours=30)), PersonView((sent,), ()), flows=FLOWS) \
        == StartDecision(False, "ya_disparado", None)
    assert plan_start(NATIVE, _fire(T0 + timedelta(hours=32)), PersonView((sent,), ()), flows=FLOWS) \
        == StartDecision(True, None, None)


def test_another_native_form_flow_counts_too() -> None:
    open_run = _run_of(OTHER_NATIVE, T0, sends=(), closed=None)
    sent = _run_of(OTHER_NATIVE, T0, sends=(T0 + timedelta(minutes=60),), closed=T0 + timedelta(minutes=61))
    assert plan_start(NATIVE, _fire(T0 + timedelta(minutes=30)), PersonView((open_run,), ()), flows=FLOWS) \
        == StartDecision(False, "ya_en_curso", None)
    assert plan_start(NATIVE, _fire(T0 + timedelta(hours=2)), PersonView((sent,), ()), flows=FLOWS) \
        == StartDecision(False, "ya_disparado", None)


def test_nunca_stops_after_any_send_and_not_after_a_cut() -> None:
    never = _flow("primer_contacto", again="nunca")
    flows = {**FLOWS, never.ref: never}
    sent = _run_of(never, T0, sends=(T0 + timedelta(minutes=60),), closed=T0 + timedelta(minutes=61))
    cut = _run_of(never, T0, sends=(), closed=T0 + timedelta(minutes=60))
    assert plan_start(never, _fire(T0 + timedelta(days=30)), PersonView((sent,), ()), flows=flows) \
        == StartDecision(False, "ya_disparado", None)
    assert plan_start(never, _fire(T0 + timedelta(days=30)), PersonView((cut,), ()), flows=flows) \
        == StartDecision(True, None, None)


def test_persona_y_oferta_counts_only_the_same_offer() -> None:
    by_offer = _flow("primer_contacto")
    by_offer.definition["topes"]["repeticion"]["una_por"] = "persona_y_oferta"  # type: ignore[index]
    flows = {**FLOWS, by_offer.ref: by_offer}
    sent = _run_of(by_offer, T0, sends=(T0 + timedelta(minutes=60),), closed=T0 + timedelta(minutes=61))
    other_offer = replace(_fire(T0 + timedelta(hours=5)), offer_ref="mmliigg5")
    assert plan_start(by_offer, other_offer, PersonView((sent,), ()), flows=flows) == StartDecision(True, None, None)
    assert plan_start(by_offer, _fire(T0 + timedelta(hours=5)), PersonView((sent,), ()), flows=flows) \
        == StartDecision(False, "ya_disparado", None)


def test_the_legacy_first_contact_keeps_its_rule() -> None:
    # El legado (legacy_kind precheckout) sigue con la regla generica: ya_en_curso, solo su flujo,
    # y un corte sin envio dentro de la ventana no frena por envio (no tiene envios).
    sent = _run_of(LEGACY, T0, sends=(T0 + timedelta(minutes=60),), closed=T0 + timedelta(minutes=61))
    assert plan_start(LEGACY, _fire(T0 + timedelta(hours=5)), PersonView((sent,), ()), flows=FLOWS) \
        == StartDecision(False, "ya_en_curso", None)
    native_sent = _run_of(NATIVE, T0, sends=(T0 + timedelta(minutes=60),), closed=T0 + timedelta(minutes=61))
    assert plan_start(LEGACY, _fire(T0 + timedelta(hours=5)), PersonView((native_sent,), ()), flows=FLOWS) \
        == StartDecision(True, None, None)


# -- Lo que la base contesto, leido como el carrito ------------------------------------


def _row(outcome: str, reason: str | None = None, case: str | None = None) -> PrecheckoutNativeFlowOutcome:
    return PrecheckoutNativeFlowOutcome(flow_id=FLOW_ID, version=1, outcome=outcome, reason=reason,
                                        recovery_case_id=case)


def test_the_route_reads_like_the_cart() -> None:
    assert native_intent_admission("nativo", (_row("motor_arranco", case=CASE_ID),)).outcome == NATIVE_CART_STARTED
    refused = native_intent_admission("nativo", (_row("motor_no_arranca", "superseded_by_provider_event"),))
    assert (refused.outcome, refused.reason) == (NATIVE_CART_REFUSED, "superseded_by_provider_event")
    unclaimed = native_intent_admission("legado", (_row("motor_no_arranca", "fuera_de_cohorte"),))
    assert (unclaimed.outcome, unclaimed.reason) == (NATIVE_CART_UNCLAIMED, "fuera_de_cohorte")
    assert native_intent_admission("legado", ()).outcome == NATIVE_CART_UNCLAIMED
    # Sin ruta (una respuesta que el cliente nunca deja pasar) no es del nativo.
    assert native_intent_admission(None, (_row("motor_arranco", case=CASE_ID),)).outcome == NATIVE_CART_UNCLAIMED
    # Arranco sin caso no es un recorrido: es del nativo y no arranco.
    assert native_intent_admission("nativo", (_row("motor_arranco"),)).outcome == NATIVE_CART_REFUSED


# -- El cliente de la RPC --------------------------------------------------------------


def _recording_client(rows: list[dict[str, object]], *, status: int = 200) -> tuple[SupabaseClient, list]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=rows)

    client = SupabaseClient(base_url="https://supabase.example.test", service_role_key="service-role",
                            transport=httpx.MockTransport(handler))
    return client, requests


ATT1_CONFIG = InstanceManifest.from_toml_file(FIXTURES / "instances" / "att1" / "instancia.toml") \
    .to_commercial_ally_config()


def _route(client: SupabaseClient, envio: dict[str, Any], scope: tuple[str | None, int | None]) -> object:
    return asyncio.run(client.admit_and_route_portable_lead_precheckout(
        config=ATT1_CONFIG, external_submission_id=envio["canonical"]["external_submission_id"],
        raw_payload=envio["raw"], canonical_payload=envio["canonical"],
        legacy_scope_key=scope[0], legacy_scope_version=scope[1]))


def _answer(**overrides: object) -> dict[str, object]:
    return {"outcome": "inserted", "submission_id": SUBMISSION_ID, "purchase_intent_id": INTENT_ID,
            "plan_outcome": None, "plan_reason": None, "ruta": "nativo",
            "flujos": [{"flow_id": FLOW_ID, "version": 1, "outcome": "motor_arranco", "reason": None,
                        "recovery_case_id": CASE_ID}], **overrides}


def test_the_client_sends_the_maribel_form_as_is_with_the_legacy_scope() -> None:
    [accepted, _] = MARIBEL_FORMS["envios"]
    client, requests = _recording_client([_answer()])

    result = _route(client, accepted, ("maribel-primer-contacto", 4))

    [request] = requests
    assert request.url.path == "/rest/v1/rpc/admit_and_route_portable_lead_precheckout_v1"
    body = json.loads(request.content)
    assert body == {
        "p_tenant_ref": ATT1_CONFIG.tenant_ref, "p_funnel_ref": ATT1_CONFIG.funnel_ref,
        "p_binding_version": ATT1_CONFIG.binding_version,
        "p_external_submission_id": accepted["canonical"]["external_submission_id"],
        "p_raw_payload": accepted["raw"], "p_canonical_payload": accepted["canonical"],
        "p_legacy_scope_key": "maribel-primer-contacto", "p_legacy_scope_version": 4,
    }
    assert result == PrecheckoutAdmissionResult(
        outcome="inserted", submission_id=SUBMISSION_ID, purchase_intent_id=INTENT_ID,
        route="nativo", native_flows=(_row("motor_arranco", case=CASE_ID),))


def test_without_the_legacy_flag_both_scope_values_go_null() -> None:
    [_, cut] = MARIBEL_FORMS["envios"]
    client, requests = _recording_client([_answer(
        ruta="nativo", flujos=[{"flow_id": FLOW_ID, "version": 1, "outcome": "motor_no_arranca",
                                "reason": "superseded_by_provider_event", "recovery_case_id": None}])])

    result = _route(client, cut, (None, None))

    body = json.loads(requests[0].content)
    assert (body["p_legacy_scope_key"], body["p_legacy_scope_version"]) == (None, None)
    assert result.native_flows == (_row("motor_no_arranca", "superseded_by_provider_event"),)  # type: ignore[attr-defined]


def test_the_legacy_route_carries_the_legacy_plan() -> None:
    [accepted, _] = MARIBEL_FORMS["envios"]
    client, _ = _recording_client([_answer(ruta="legado", plan_outcome="planned",
                                           plan_reason="first_contact_scheduled", flujos=[])])
    result = _route(client, accepted, ("maribel-primer-contacto", 4))
    assert (result.route, result.plan_outcome, result.plan_reason) == (  # type: ignore[attr-defined]
        "legado", "planned", "first_contact_scheduled")


def test_a_legacy_route_without_its_plan_is_invalid_like_the_legacy_rpc() -> None:
    [accepted, _] = MARIBEL_FORMS["envios"]
    client, _ = _recording_client([_answer(ruta="legado", flujos=[])])
    with pytest.raises(SupabaseError, match="_invalid_plan"):
        _route(client, accepted, ("maribel-primer-contacto", 4))


INVALID_ROW = PrecheckoutNativeFlowOutcome(
    flow_id="-", version=None, outcome=NATIVE_FLOW_ROW_INVALID, reason=None, recovery_case_id=None)
GOOD_ROW = {"flow_id": FLOW_ID, "version": 1, "outcome": "motor_arranco", "reason": None,
            "recovery_case_id": CASE_ID}


@pytest.mark.parametrize("row", [
    {"flow_id": "persona-1@example.test", "outcome": "x"},
    {**GOOD_ROW, "reason": "Persona Anonima +521555000001"},
    {**GOOD_ROW, "recovery_case_id": "x"},
    {**GOOD_ROW, "version": 0},
    {**GOOD_ROW, "version": "1"},
    {**GOOD_ROW, "outcome": "Motor Arranco"},
    "persona-1@example.test",
], ids=["flow_id", "reason", "caso", "version 0", "version texto", "outcome", "no es un objeto"])
def test_an_odd_flow_row_never_fails_the_admission_and_carries_no_value(row: object) -> None:
    # El envio ya quedo admitido: una fila rara no vuelve 503 (GHL leeria la misma para siempre), y
    # ningun valor de ella sale del cliente.
    [accepted, _] = MARIBEL_FORMS["envios"]
    client, _ = _recording_client([_answer(flujos=[row, GOOD_ROW])])
    result = _route(client, accepted, (None, None))
    assert result.native_flows == (INVALID_ROW, _row("motor_arranco", case=CASE_ID))  # type: ignore[attr-defined]


@pytest.mark.parametrize("flujos", [None, {}, "[]"])
def test_a_flujos_that_is_not_a_list_is_one_invalid_row(flujos: object) -> None:
    [accepted, _] = MARIBEL_FORMS["envios"]
    client, _ = _recording_client([_answer(flujos=flujos)])
    assert _route(client, accepted, (None, None)).native_flows == (INVALID_ROW,)  # type: ignore[attr-defined]


def test_a_legacy_route_without_the_legacy_scope_is_only_admitted() -> None:
    # El flag nuevo prendido y el del legado apagado, sin flujo nativo: la base solo admite y el plan
    # vuelve nulo. Exigirlo seria un 503 para cada formulario.
    [accepted, _] = MARIBEL_FORMS["envios"]
    client, _ = _recording_client([_answer(ruta="legado", flujos=[])])
    result = _route(client, accepted, (None, None))
    assert (result.route, result.plan_outcome, result.plan_reason) == ("legado", None, None)  # type: ignore[attr-defined]


def test_a_native_route_ignores_a_plan_it_should_not_carry() -> None:
    [accepted, _] = MARIBEL_FORMS["envios"]
    client, _ = _recording_client([_answer(plan_outcome="planned", plan_reason="first_contact_scheduled")])
    result = _route(client, accepted, ("maribel-primer-contacto", 4))
    assert (result.plan_outcome, result.plan_reason) == (None, None)  # type: ignore[attr-defined]


def test_a_missing_rpc_is_its_own_error() -> None:
    # La base sin 20261101000100: PostgREST da 404 y el bridge cae al camino de hoy (abajo).
    [accepted, _] = MARIBEL_FORMS["envios"]
    client, _ = _recording_client([], status=404)
    with pytest.raises(SupabaseRpcMissing):
        _route(client, accepted, ("maribel-primer-contacto", 4))


@pytest.mark.parametrize("answer", [
    {"ruta": "otra"},
    {"ruta": None},
    {"outcome": "planned"},
    {"submission_id": None},
    {"purchase_intent_id": None},
])
def test_a_wrong_essential_column_is_an_error(answer: dict[str, object]) -> None:
    [accepted, _] = MARIBEL_FORMS["envios"]
    client, _ = _recording_client([_answer(**answer)])
    with pytest.raises(SupabaseError):
        _route(client, accepted, (None, None))


def test_two_rows_are_an_error() -> None:
    [accepted, _] = MARIBEL_FORMS["envios"]
    client, _ = _recording_client([_answer(), _answer()])
    with pytest.raises(SupabaseError, match="_invalid_shape"):
        _route(client, accepted, (None, None))


def test_a_base_error_is_a_supabase_error() -> None:
    [accepted, _] = MARIBEL_FORMS["envios"]
    client, _ = _recording_client([], status=503)
    with pytest.raises(SupabaseError, match="HTTP 503"):
        _route(client, accepted, ("maribel-primer-contacto", 4))


def test_the_scope_key_and_version_go_together() -> None:
    [accepted, _] = MARIBEL_FORMS["envios"]
    client, requests = _recording_client([_answer()])
    with pytest.raises(ValueError):
        _route(client, accepted, ("maribel-primer-contacto", None))
    assert requests == []


# -- El flag y el arranque ---------------------------------------------------------------


def test_the_flag_is_off_by_default_and_portable() -> None:
    assert Settings.__dataclass_fields__["flow_engine_native_intent_enabled"].default is False
    assert "flow_engine_native_intent_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


def test_from_env_reads_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # El minimo para leer el entorno, el mismo de test_flow_engine_bridge_settings.
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(FLAG, "true")
    assert Settings.from_env().flow_engine_native_intent_enabled is True
    monkeypatch.setenv(FLAG, "TRUE")
    assert Settings.from_env().flow_engine_native_intent_enabled is True
    # Como todos los flags del bridge: solo «true» lo prende.
    for value in ("1", "yes", "on", ""):
        monkeypatch.setenv(FLAG, value)
        assert Settings.from_env().flow_engine_native_intent_enabled is False, value
    monkeypatch.delenv(FLAG)
    assert Settings.from_env().flow_engine_native_intent_enabled is False


@pytest.fixture
def att1_instance(tmp_path: Path) -> Path:
    target = tmp_path / "instancia"
    shutil.copytree(ATT1_INSTANCE, target)
    return target


def _engine(settings: Settings, **overrides: object) -> Settings:
    """El mismo runtime con el motor prendido (como ATT1 y Maribel hoy)."""
    values: dict[str, object] = {
        "flow_engine_enabled": True,
        "worker_enabled": True,
        "followup_policy_key": "att1-carrito",
        "followup_policy_version": 1,
    }
    values.update(overrides)
    return replace(settings, **values)


def _native(settings: Settings) -> Settings:
    return _engine(settings, flow_engine_native_intent_enabled=True)


def _without_legacy(settings: Settings) -> Settings:
    """Sin el flag del legado, con lo que el motor necesita igual: el modo directo y la salida durable."""
    return replace(_first_contact_off(settings), dispatcher_approved_template_direct_enabled=True,
                   dispatcher_outbound_enabled=True)


@pytest.mark.parametrize(("overrides", "message"), [
    ({"instance_manifest": None}, f"{FLAG} requires an instance manifest"),
    ({"flow_engine_enabled": False}, f"{FLAG} requires FLOW_ENGINE_ENABLED"),
    ({"lead_precheckout_enabled": False, "ghl_precheckout_adapter_enabled": False},
     f"{FLAG} requires LEAD_PRECHECKOUT_ENABLED or GHL_PRECHECKOUT_ADAPTER_ENABLED"),
    ({"pilot_boundary_enabled": False}, f"{FLAG} requires LANCEMOS_PILOT_BOUNDARY_ENABLED"),
    ({"dispatcher_enabled": False}, f"{FLAG} requires DURABLE_DISPATCHER_ENABLED"),
    ({"dispatcher_approved_template_direct_enabled": False},
     f"{FLAG} requires DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED"),
    ({"portable_hotmart_purchase_stop_enabled": False}, f"{FLAG} requires PORTABLE_HOTMART_PURCHASE_STOP_ENABLED"),
    ({"hotmart_hottok": None}, f"{FLAG} requires HOTMART_HOTTOK"),
    ({"chatwoot_scoped_inbound_senders_enabled": False}, f"{FLAG} requires CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED"),
])
def test_an_incomplete_wiring_does_not_start(att1_instance: Path, overrides: dict, message: str) -> None:
    settings = replace(_native(_first_contact_settings(att1_instance)), **overrides)
    with pytest.raises(ValueError, match=f"^{message}$"):
        app_module._validate_flow_engine_native_intent(settings)


def test_the_gate_does_not_need_the_legacy_flag(att1_instance: Path) -> None:
    # Nada se ata al flag del legado: con el apagado, el nativo arranca igual y tiene destinatario.
    settings = _native(_without_legacy(_first_contact_settings(att1_instance)))
    app_module._validate_flow_engine_native_intent(settings)
    app = _first_contact_app(settings, _RouteAuthority())
    runtime = app.state.resolution_worker._flow_engine
    assert runtime.readiness()["flow_engine_native_intent"] == "on"


def test_the_startup_runs_the_gate(att1_instance: Path) -> None:
    # Sin el flag del legado, para que lo frene esta puerta y no la del legado.
    settings = replace(_native(_without_legacy(_first_contact_settings(att1_instance))),
                       chatwoot_scoped_inbound_senders_enabled=False)
    with pytest.raises(ValueError, match=f"^{FLAG} requires CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED$"):
        _first_contact_app(settings, _RouteAuthority())


def test_ready_says_off_with_the_engine_and_without_the_flag(att1_instance: Path) -> None:
    app = _first_contact_app(_engine(_first_contact_settings(att1_instance)), _RouteAuthority())
    assert app.state.resolution_worker._flow_engine.readiness()["flow_engine_native_intent"] == "off"


# -- La entrada: apagado, exactamente lo de hoy ----------------------------------------------


class _RouteAuthority(_FirstContactAuthority):
    """La base del formulario, mas la RPC nueva que lo rutea."""

    def __init__(self, answer: dict[str, object] | None = None, *, fail: bool = False) -> None:
        super().__init__()
        self.route_calls: list[dict[str, object]] = []
        self.fail = fail
        self.answer = answer or {"route": "nativo", "native_flows": (_row("motor_arranco", case=CASE_ID),)}

    async def admit_and_route_portable_lead_precheckout(self, **kwargs: object) -> object:
        self.route_calls.append(kwargs)
        if self.fail:
            raise SupabaseError("portable_lead_precheckout_admission_and_route_failed: HTTP 503")
        return PrecheckoutAdmissionResult(outcome="inserted", submission_id=SUBMISSION_ID,
                                          purchase_intent_id=INTENT_ID, **self.answer)  # type: ignore[arg-type]


@pytest.mark.parametrize("engine", [False, True], ids=["motor apagado", "motor prendido (ATT1 hoy)"])
def test_off_the_ghl_form_plans_the_legacy_in_its_rpc_as_today(att1_instance: Path, engine: bool) -> None:
    settings = _first_contact_through_ghl(att1_instance)
    if engine:
        settings = _engine(settings)
    authority = _RouteAuthority()

    response = _post(_first_contact_app(settings, authority), _ads_a())

    assert response.status_code == 200
    assert authority.route_calls == [] and authority.admission_calls == []
    [call] = authority.plan_calls
    assert set(call) == {"config", "external_submission_id", "raw_payload", "canonical_payload",
                         "scope_key", "scope_version"}
    assert (call["scope_key"], call["scope_version"]) == ("att1-primer-contacto", 1)


def test_off_without_the_legacy_flag_the_ghl_form_is_only_admitted(att1_instance: Path) -> None:
    settings = _first_contact_off(_first_contact_through_ghl(att1_instance))
    authority = _RouteAuthority()

    response = _post(_first_contact_app(settings, authority), _ads_a())

    assert response.status_code == 200
    assert authority.route_calls == [] and authority.plan_calls == []
    [call] = authority.admission_calls
    assert set(call) == {"config", "external_submission_id", "raw_payload", "canonical_payload"}


def test_off_the_lead_webhook_plans_the_legacy_as_today(att1_instance: Path) -> None:
    authority = _RouteAuthority()
    response = _post_att1_form(_first_contact_app(_engine(_first_contact_settings(att1_instance)), authority))
    assert response.status_code == 200
    assert authority.route_calls == []
    [call] = authority.plan_calls
    assert (call["scope_key"], call["scope_version"]) == ("att1-primer-contacto", 1)


# -- La entrada: prendido, la RPC que rutea ---------------------------------------------------


def test_on_the_ghl_form_goes_to_the_route_rpc_with_the_legacy_scope(att1_instance: Path) -> None:
    settings = _native(_first_contact_through_ghl(att1_instance))
    authority = _RouteAuthority()

    response = _post(_first_contact_app(settings, authority), _ads_a())

    assert response.status_code == 200
    assert authority.plan_calls == [] and authority.admission_calls == []
    [call] = authority.route_calls
    assert set(call) == {"config", "external_submission_id", "raw_payload", "canonical_payload",
                         "legacy_scope_key", "legacy_scope_version"}
    assert call["config"] == settings.commercial_ally_config
    assert (call["legacy_scope_key"], call["legacy_scope_version"]) == ("att1-primer-contacto", 1)


def test_on_and_off_the_ghl_and_lead_payloads_are_the_same(att1_instance: Path) -> None:
    # Lo que llega a la base es el mismo evento traducido, y la respuesta HTTP es la misma byte a byte.
    off_authority, on_authority = _RouteAuthority(), _RouteAuthority()
    body = _ads_a()
    off = _post(_first_contact_app(_first_contact_through_ghl(att1_instance), off_authority), copy.deepcopy(body))
    on = _post(_first_contact_app(_native(_first_contact_through_ghl(att1_instance)), on_authority),
               copy.deepcopy(body))

    assert (off.status_code, on.status_code) == (200, 200)
    [old], [new] = off_authority.plan_calls, on_authority.route_calls
    # El id y la hora de la traduccion se generan en cada pedido; todo lo demas es igual.
    assert old["config"] == new["config"]
    for key in ("raw_payload", "canonical_payload"):
        assert set(old[key]) == set(new[key]), key  # type: ignore[arg-type]
    assert old["raw_payload"]["data"] == new["raw_payload"]["data"]  # type: ignore[index]
    for key in ("lead", "consent", "commerce", "source"):
        assert old["canonical_payload"][key] == new["canonical_payload"][key], key  # type: ignore[index]
    off_json, on_json = off.json(), on.json()
    assert off_json.pop("delivery_id") and on_json.pop("delivery_id")
    assert off_json == on_json == {"status": "received", "purchase_intent_id": INTENT_ID,
                                   "activation_authorized": False, "contact_authorized": False}


def test_on_without_the_legacy_flag_the_scope_goes_null(att1_instance: Path) -> None:
    settings = _native(_without_legacy(_first_contact_through_ghl(att1_instance)))
    authority = _RouteAuthority({"route": "legado", "native_flows": ()})

    response = _post(_first_contact_app(settings, authority), _ads_a())

    assert response.status_code == 200
    [call] = authority.route_calls
    assert (call["legacy_scope_key"], call["legacy_scope_version"]) == (None, None)


def test_on_the_lead_webhook_goes_to_the_route_rpc(att1_instance: Path) -> None:
    authority = _RouteAuthority()
    response = _post_att1_form(_first_contact_app(_native(_first_contact_settings(att1_instance)), authority))
    assert response.status_code == 200
    assert authority.plan_calls == [] and authority.admission_calls == []
    [call] = authority.route_calls
    assert call["external_submission_id"] == "01K3F8QW7N2VYB4M6X9CDPTZRA"


@pytest.mark.parametrize(("post", "detail"), [
    ("ghl", "ghl_precheckout_persist_unavailable"),
    ("lead", "lead_precheckout_persist_unavailable"),
])
def test_on_an_unavailable_route_is_a_503_so_the_form_is_retried(att1_instance: Path, post: str, detail: str) -> None:
    # El formulario no tiene cola propia: el 503 hace que GHL (o el relay) lo vuelva a mandar.
    authority = _RouteAuthority(fail=True)
    if post == "ghl":
        response = _post(_first_contact_app(_native(_first_contact_through_ghl(att1_instance)), authority), _ads_a())
    else:
        response = _post_att1_form(_first_contact_app(_native(_first_contact_settings(att1_instance)), authority))
    assert response.status_code == 503
    assert response.json()["detail"] == detail
    assert len(authority.route_calls) == 1 and authority.plan_calls == []


@pytest.mark.parametrize(("answer", "native", "level"), [
    ({"route": "nativo", "native_flows": (_row("motor_arranco", case=CASE_ID),)}, "arranco", logging.INFO),
    ({"route": "nativo", "native_flows": (_row("motor_no_arranca", "superseded_by_provider_event"),)},
     "no_arranca", logging.INFO),
    ({"route": "legado", "plan_outcome": "planned", "plan_reason": "first_contact_scheduled",
      "native_flows": (_row("motor_no_arranca", "fuera_de_cohorte"),)}, "sin_reclamo", logging.INFO),
    ({"route": "legado", "plan_outcome": "planned", "plan_reason": "first_contact_scheduled",
      "native_flows": (_row("motor_no_arranca", "error_interno:p0001"),)}, "sin_reclamo", logging.WARNING),
    ({"route": "legado", "plan_outcome": "plan_failed", "plan_reason": "channel_identity_inbox_mismatch",
      "native_flows": ()}, "sin_reclamo", logging.WARNING),
    ({"route": "legado", "plan_outcome": "planned", "plan_reason": "first_contact_scheduled",
      "native_flows": (_row("motor_no_arranca", "motor_nativo:forma"),)}, "sin_reclamo", logging.WARNING),
    ({"route": "nativo", "native_flows": (INVALID_ROW,)}, "no_arranca", logging.WARNING),
])
def test_ghl_gets_the_same_200_and_the_log_carries_only_codes(
    att1_instance: Path, caplog: pytest.LogCaptureFixture, answer: dict, native: str, level: int,
) -> None:
    authority = _RouteAuthority(answer)
    body = _ads_a()
    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(_first_contact_app(_native(_first_contact_through_ghl(att1_instance)), authority), body)

    assert response.status_code == 200 and response.json()["status"] == "received"
    [record] = [r for r in caplog.records if r.getMessage().startswith("flow_engine_native_intent_admission ")]
    assert record.levelno == level
    message = record.getMessage()
    assert f"route={answer['route']} native={native} " in message
    flows = ",".join(f"{r.flow_id}:{r.outcome}:{r.reason or '-'}" for r in answer["native_flows"]) or "-"
    assert f" flows={flows} " in message
    assert message.endswith(f"submission_id={SUBMISSION_ID}")
    everything = "\n".join(r.getMessage() for r in caplog.records)
    for value in (body["email"], body["phone"], body["full_name"], body["contact_id"], TOKEN):
        assert value not in everything


def test_on_a_form_of_another_product_is_only_admitted_in_its_funnel(att1_instance: Path) -> None:
    # Como el legado (paso 2c-2): un producto que no es el principal no tiene primer contacto, tampoco
    # con el flag. La landing D movida al segundo producto de prueba (test_product_routing).
    base = _native(_first_contact_through_ghl(att1_instance))
    assert base.instance_manifest is not None
    # Los flujos del manifiesto del primer contacto (con precheckout), con los dos productos.
    manifest = replace(_ghl_manifest(), ghl_risk_acceptance=TEST_RISK_ACCEPTANCE,
                       flows=base.instance_manifest.flows)
    settings = replace(base, instance_manifest=manifest,
                       commercial_ally_config=manifest.to_commercial_ally_config(),
                       commercial_knowledge=_by_product("att1", "att2"))
    authority = _RouteAuthority()

    response = _post(_first_contact_app(settings, authority), _landing_d())

    assert response.status_code == 200
    assert authority.route_calls == [] and authority.plan_calls == []
    [call] = authority.admission_calls
    assert call["config"].funnel_ref == "att2"  # type: ignore[attr-defined]


class _NoRouteRpc(_RouteAuthority):
    async def admit_and_route_portable_lead_precheckout(self, **kwargs: object) -> object:
        self.route_calls.append(kwargs)
        raise SupabaseRpcMissing("portable_lead_precheckout_admission_and_route_failed: HTTP 404")


def test_on_without_the_migration_the_form_goes_the_way_of_today(
    att1_instance: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    # El flag prendido antes de aplicar 20261101000100: un 503 por formulario los perderia cuando GHL
    # deje de reintentar. Va el camino de hoy (el plan del legado con su scope), con un warning.
    authority = _NoRouteRpc()
    with caplog.at_level(logging.INFO, logger="bridge.app"):
        response = _post(_first_contact_app(_native(_first_contact_through_ghl(att1_instance)), authority), _ads_a())

    assert response.status_code == 200
    assert len(authority.route_calls) == 1 and authority.admission_calls == []
    [call] = authority.plan_calls
    assert (call["scope_key"], call["scope_version"]) == ("att1-primer-contacto", 1)
    [record] = [r for r in caplog.records if r.getMessage().startswith("flow_engine_native_intent_rpc_missing ")]
    assert record.levelno == logging.WARNING and "fallback=legacy_plan" in record.getMessage()


def test_on_without_the_migration_and_the_legacy_flag_the_form_is_only_admitted(att1_instance: Path) -> None:
    authority = _NoRouteRpc()
    response = _post(_first_contact_app(_native(_without_legacy(_first_contact_through_ghl(att1_instance))),
                                        authority), _ads_a())
    assert response.status_code == 200
    assert authority.plan_calls == []
    [call] = authority.admission_calls
    assert set(call) == {"config", "external_submission_id", "raw_payload", "canonical_payload"}


# -- El envio: G3 y el reuso de la conversacion, como la bienvenida -------------------------
# El paso nativo del formulario sale por el mismo sender (``send_first_touch``) que la bienvenida, con
# la plantilla del paso y permiso formulario (MARKETING tambien). Los cortes son del sender, no del ancla:
# estas pruebas caracterizan (pasan tambien sin este PR) y fijan que el paso de intencion no se los saltea.


def _form_authority() -> _NativeAuthority:
    return _NativeAuthority(step=_step(entry_permission="formulario", flow_key="primer_contacto"))


@pytest.mark.parametrize("guard_on", [True, False], ids=["guarda prendida", "guarda apagada"])
def test_the_native_first_contact_stops_when_the_person_wrote_within_two_hours(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, guard_on: bool
) -> None:
    authority = _form_authority()
    chatwoot = _LiveContactChatwoot(_listing())
    with caplog.at_level("INFO", logger="bridge"):
        _run(_dispatcher(authority, chatwoot, tmp_path, guard=MARIBEL_WINDOW if guard_on else 0, reuse=False))
    if guard_on:
        _assert_blocked(authority, chatwoot)
        assert f"reason={FIRST_TOUCH_CONTACT_ACTIVE_CONVERSATION}" in caplog.text
    else:
        _assert_sent(authority, chatwoot)


@pytest.mark.parametrize("reuse_on", [True, False], ids=["reuso prendido", "reuso apagado"])
def test_the_native_first_contact_stops_when_the_team_has_the_open_conversation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, reuse_on: bool
) -> None:
    authority = _form_authority()
    chatwoot = _LiveContactChatwoot(_listing(lambda c: c["meta"].update(team={"id": 2})))
    with caplog.at_level("INFO", logger="bridge"):
        _run(_dispatcher(authority, chatwoot, tmp_path, guard=MARIBEL_WINDOW, reuse=reuse_on,
                         now=LAST_INBOUND_6 + MARIBEL_WINDOW + 1))
    if reuse_on:
        _assert_blocked(authority, chatwoot)
        assert f"reason={FIRST_TOUCH_CONTACT_CONVERSATION_WITH_TEAM}" in caplog.text
    else:
        _assert_sent(authority, chatwoot)
