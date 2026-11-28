"""4.3, confirmacion de registro a un evento: la segunda parte (la base, el freno por salud y la capacidad).

Lo que se prueba:

* la capacidad ``intencion:registro`` del motor nativo (D-43.7, D-43.10): con los cuatro flags del registro, un
  flujo ``dispara: {evento: intencion, registro: <clave>}`` de una sola plantilla lo ejecuta el nativo; sin
  ellos, ``motor_nativo:disparador`` como en la primera parte; con dos plantillas, nunca;
* el runtime que publica ``/ready`` (``flow_change_runtime``): con el registro apagado, byte a byte el de hoy;
  prendido, ``native_registration_enabled`` y el operador lo acepta;
* el freno por salud de Meta (D-43.9): el veredicto del disparo por audiencia (E-484), fail-closed;
* el bridge (D-43.9, D-43.11): con los cuatro flags llama a la entrada nueva con la clave del evento y el freno,
  sin el archivo de la salud frena, y una base sin la entrada vuelve a lo de la primera parte;
* el cliente de la base: la forma de la respuesta de
  ``admit_and_route_registration_lead_precheckout_v1`` (20261127500000).

Lo que la base hace de verdad lo prueba ``tests/sql/followup_engine/validate_own_forms_registration_confirmation.mjs``
(PGlite, la cadena entera) y el Postgres 17 real.

Dato real: el envio es el de la primera parte (``tests/fixtures/own_forms/att1_own_form_submission_shape_20261126.json``,
la forma del envio de prueba de ``att1-org-b`` del 26/11, sin datos personales). La lectura de la salud es la forma
que deja ``meta_salud.py`` en ``estado.json`` (``{"<instancia>": {"calidad", "leido_en", ...}}``), un archivo
nuestro y no un borde externo: la misma forma que usan ``test_flow_audience_sweeper`` y ``test_flow_audience_bridge``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_flow_engine_native_intent import ATT1_INSTANCE, FIRST_CONTACT, _engine, _RouteAuthority, _row
from test_instance_wiring import _first_contact_app, _first_contact_settings
from test_own_forms import BUYER

import bridge.app as app_module
from bridge.flow_engine import (
    ENGINE_CAPABILITIES_NATIVE_REGISTRATION,
    NATIVE_CAPABILITIES_BY_TRIGGER,
    REGISTRATION_TRIGGER,
    is_registration_trigger,
    native_not_supported,
    native_trigger_key,
)
from bridge.flow_validator import NativeRuntime, runtime_runs_registration
from bridge.instance_manifest import OWN_FORM_DEFAULT_DISCLAIMER, OWN_FORM_KIND_REGISTRATION, OwnForm
from bridge.own_forms import registration_health_brake
from bridge.supabase import PrecheckoutAdmissionResult, SupabaseClient, SupabaseError, SupabaseRpcMissing

EVENT = "clase-28-11"
BASE = "https://setter-att1.example"
THANKS = "https://site.metodoraizana.com.mx/gracias-clase"
NOW = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
SUBMISSION_ID = "00000000-0000-4000-8000-00000000a431"
INTENT_ID = "00000000-0000-4000-8000-00000000b431"
CASE_ID = "00000000-0000-4000-8000-00000000c431"
FLOW_ID = "00000000-0000-4000-8000-00000000d431"


def _confirmation(**changes: object) -> dict:
    flow = copy.deepcopy(FIRST_CONTACT)
    flow.update({"flujo": "confirmacion_registro", "dispara": {"evento": "intencion", "registro": EVENT},
                 # La confirmacion sale apenas llega el formulario: sin la espera del primer contacto.
                 "pasos": [copy.deepcopy(FIRST_CONTACT["pasos"][1])],
                 "topes": {**FIRST_CONTACT["topes"], "bolsa": "att1-registro-nativo",
                           "repeticion": {"una_por": "persona", "otra_despues_de": "nunca"}}})
    flow.update(changes)
    return flow


# -- La capacidad intencion:registro (D-43.7, D-43.10) ------------------------------------------------------


@pytest.mark.parametrize(("trigger", "expected"), [
    ({"evento": "intencion", "registro": EVENT}, True),
    ({"evento": "intencion"}, False),
    ({"evento": "carrito", "registro": EVENT}, False),
    ({"evento": "intencion", "registro": "Clase 28"}, False),
    ({"evento": "intencion", "registro": "ab"}, False),
    ({"evento": "intencion", "registro": 28}, False),
    ({"evento": "intencion", "registro": EVENT, "otra": "x"}, False),
])
def test_the_registration_trigger_is_the_shape_of_the_base(trigger: dict, expected: bool) -> None:
    # La misma forma que reconoce _flow_engine_native_trigger en 20261127500000.
    assert is_registration_trigger(trigger) is expected


def test_without_the_flags_the_registration_is_not_a_native_trigger() -> None:
    flow = _confirmation()
    assert native_trigger_key(flow) is None
    assert "motor_nativo:disparador" in native_not_supported(flow)


def test_with_the_flags_the_confirmation_runs_natively() -> None:
    flow = _confirmation()
    assert native_trigger_key(flow, registration=True) == REGISTRATION_TRIGGER
    assert NATIVE_CAPABILITIES_BY_TRIGGER[REGISTRATION_TRIGGER] is ENGINE_CAPABILITIES_NATIVE_REGISTRATION
    assert native_not_supported(flow, registration=True) == ()
    # El primer contacto comercial no cambia con el flag.
    assert native_not_supported(FIRST_CONTACT) == native_not_supported(FIRST_CONTACT, registration=True) == ()


@pytest.mark.parametrize(("change", "code"), [
    # Una sola confirmacion (D-43.7): un recordatorio despues no entra aunque el bridge mande varios toques.
    ({"pasos": [FIRST_CONTACT["pasos"][1], {"esperar": "1d"}, FIRST_CONTACT["pasos"][1]],
      "termina": {"si": ["compra", "contesta"], "vence": "2d"}}, "engine_not_supported:demasiados_envios"),
    # Sin espera despues del envio.
    ({"pasos": [FIRST_CONTACT["pasos"][1], {"esperar": "1h"}]}, "engine_not_supported:espera_despues_del_envio"),
    # Entra con el formulario, como el primer contacto.
    ({"entra": {"permiso": "compra", "contesto_a": []}}, "motor_nativo:permiso"),
    # Variables, de nombre y producto.
    ({"pasos": [{**FIRST_CONTACT["pasos"][1], "variables": ["nombre", "precio"]}]},
     "engine_not_supported:variable_precio"),
])
def test_what_the_confirmation_still_cannot_do(change: dict, code: str) -> None:
    assert code in native_not_supported(_confirmation(**change), registration=True)


def test_the_ae_publishes_the_confirmation_only_with_the_registration_running() -> None:
    import test_flow_validator as tfv

    from bridge.flow_definition import InstanceView

    def findings(on: bool) -> set[str]:
        runtime = NativeRuntime(engine_enabled=True, native_intent_enabled=True, native_silence_sweeper=True,
                                cart_touches=True, main_product="att1", native_registration=on)
        instance = InstanceView("lancemos", "att1", frozenset({"att1"}), "America/Mexico_City", runtime=runtime)
        return {finding.code for finding in tfv.run_new(_confirmation(), "confirmacion_registro",
                                                        actor="automation_expert", instance=instance)}

    assert "motor_nativo:disparador" in findings(False)
    assert not {code for code in findings(True)
                if code.startswith(("motor_nativo:", "engine_not_supported:"))}


# -- El runtime que publica /ready ------------------------------------------------------------------------


def _runtime(on: bool) -> NativeRuntime:
    return NativeRuntime(engine_enabled=True, native_intent_enabled=True, native_silence_sweeper=True,
                         cart_touches=True, main_product="att1", native_registration=on)


def test_the_runtime_carries_the_registration_only_when_it_runs() -> None:
    from bridge.flow_change_operator import instance_from_input

    off, on = _runtime(False), _runtime(True)
    # Apagado: la forma de hoy, sin la clave nueva.
    assert "native_registration_enabled" not in off.to_json()
    assert set(off.to_json()) == set(NativeRuntime(engine_enabled=True, native_intent_enabled=True,
                                                   native_silence_sweeper=True, cart_touches=True,
                                                   main_product="att1").to_json())
    assert on.to_json()["native_registration_enabled"] is True
    base = {"tenant_ref": "lancemos", "ally_ref": "att1", "products": ["att1"], "time_zone": "America/Mexico_City"}
    assert instance_from_input({**base, "runtime": on.to_json()}).runtime == on
    assert instance_from_input({**base, "runtime": off.to_json()}).runtime == off
    assert runtime_runs_registration(instance_from_input({**base, "runtime": on.to_json()}).runtime) is True
    with pytest.raises(Exception):
        instance_from_input({**base, "runtime": {**on.to_json(), "native_registration_enabled": "si"}})


def test_the_operator_script_accepts_the_runtime_with_and_without_the_registration() -> None:
    from test_flow_change_operator_script import script as module

    assert module._runtime_ok(_runtime(False).to_json())  # noqa: SLF001
    assert module._runtime_ok(_runtime(True).to_json())  # noqa: SLF001
    assert not module._runtime_ok({**_runtime(True).to_json(), "native_registration_enabled": "si"})  # noqa: SLF001
    assert not module._runtime_ok({**_runtime(False).to_json(), "otra": True})  # noqa: SLF001


# -- El freno por salud de Meta (D-43.9) -------------------------------------------------------------------


GREEN = {"calidad": "GREEN", "leido_en": (NOW - timedelta(hours=2)).isoformat()}


@pytest.mark.parametrize(("reading", "brake"), [
    (GREEN, None),
    ({**GREEN, "puede_enviar": "LIMITED"}, None),
    ({**GREEN, "calidad": "RED"}, "meta_salud:red"),
    ({**GREEN, "calidad": "YELLOW"}, "meta_salud:yellow"),
    ({**GREEN, "puede_enviar": "BLOCKED"}, "meta_salud:blocked"),
    ({**GREEN, "limite": "TIER_2K"}, "meta_salud:limite_bajo"),
    ({**GREEN, "leido_en": (NOW - timedelta(hours=27)).isoformat()}, "meta_salud:vieja"),
    ({"calidad": "GREEN"}, "meta_salud:sin_lectura"),
    (None, "meta_salud:sin_lectura"),
])
def test_the_registration_brakes_like_the_audience(reading: dict | None, brake: str | None) -> None:
    assert registration_health_brake(reading, now=NOW) == brake


# -- El bridge ---------------------------------------------------------------------------------------------


class _RegistrationAuthority(_RouteAuthority):
    """La base del formulario con la entrada del registro (20261127500000)."""

    def __init__(self, *, missing: bool = False) -> None:
        super().__init__()
        self.registration_calls: list[dict[str, object]] = []
        self.missing = missing

    async def admit_and_route_registration_lead_precheckout(self, **kwargs: object) -> object:
        self.registration_calls.append(kwargs)
        if self.missing:
            raise SupabaseRpcMissing("registration_lead_precheckout_admission_and_route_failed: HTTP 404")
        return PrecheckoutAdmissionResult(
            outcome="inserted", submission_id=SUBMISSION_ID, purchase_intent_id=INTENT_ID, route="nativo",
            native_flows=(_row("motor_arranco", case=CASE_ID),))


@pytest.fixture
def att1_instance(tmp_path: Path) -> Path:
    target = tmp_path / "instancia"
    shutil.copytree(ATT1_INSTANCE, target)
    return target


def _settings(att1_instance: Path, *, health: Path | None) -> object:
    settings = _first_contact_settings(att1_instance)
    landing = settings.commercial_ally_config.offer_landings[0].landing_id  # type: ignore[union-attr]
    forms = (
        OwnForm(key="att1-clase-28-11", landing_id=landing, disclaimer=OWN_FORM_DEFAULT_DISCLAIMER,
                thanks_url=THANKS, use_core=False, kind=OWN_FORM_KIND_REGISTRATION, registration_event=EVENT),
        OwnForm(key="att1-org-b", landing_id=landing, disclaimer=OWN_FORM_DEFAULT_DISCLAIMER),
    )
    manifest = replace(settings.instance_manifest, own_forms=forms)  # type: ignore[arg-type]
    settings = replace(settings, instance_manifest=manifest, own_forms_enabled=True, own_forms_public_base_url=BASE,
                       own_forms_registration_flow_enabled=True,
                       own_forms_registration_meta_health_path=str(health) if health else None,
                       own_forms_registration_meta_health_key="att1" if health else None)
    return _engine(settings, flow_engine_native_intent_enabled=True)


def _health(tmp_path: Path, reading: dict) -> Path:
    # El estado.json de meta_salud.py: una lectura por instancia.
    path = tmp_path / "estado.json"
    path.write_text(json.dumps({"att1": reading, "maribel": {**reading, "calidad": "RED"}}), encoding="utf-8")
    return path


def _send(app: object, form_key: str) -> httpx.Response:
    body = {"nombre": BUYER["name"], "email": BUYER["email"], "telefono": BUYER["phone"],
            "pagina": f"{BASE}/f/{form_key}", "referrer": "", "website": "", "fbc": "", "fbp": ""}

    async def post() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
            return await client.post(f"/f/{form_key}/enviar", content=json.dumps(body),
                                     headers={"Content-Type": "application/json", "Origin": BASE})

    return asyncio.run(post())


def _ready(app: object) -> dict:
    async def get() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/ready")

    return asyncio.run(get()).json()


def _fresh() -> dict:
    return {"calidad": "GREEN", "leido_en": datetime.now(UTC).isoformat()}


def test_the_registration_form_goes_to_the_confirmation_with_its_event(
    att1_instance: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(att1_instance, health=_health(tmp_path, _fresh()))
    authority = _RegistrationAuthority()

    with caplog.at_level("INFO", logger="bridge.app"):
        response = _send(_first_contact_app(settings, authority), "att1-clase-28-11")

    assert response.status_code == 200, response.text
    assert response.json()["gracias_url"] == THANKS
    [call] = authority.registration_calls
    assert set(call) == {"config", "external_submission_id", "raw_payload", "canonical_payload",
                         "registration_event", "health_brake"}
    assert (call["registration_event"], call["health_brake"]) == (EVENT, None)
    # Ni el legado ni el formulario comercial ni la admision sola.
    assert (len(authority.route_calls), len(authority.plan_calls), len(authority.admission_calls)) == (0, 0, 0)
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("own_form_registration_admitted")]
    assert f"evento={EVENT}" in line and "route=nativo" in line and "brake=-" in line
    assert BUYER["email"] not in line and BUYER["phone"] not in line

    # El formulario comercial de la misma instancia sigue yendo al primer contacto, como hoy.
    authority = _RegistrationAuthority()
    assert _send(_first_contact_app(settings, authority), "att1-org-b").status_code == 200
    assert (len(authority.registration_calls), len(authority.route_calls)) == (0, 1)


@pytest.mark.parametrize(("reading", "brake"), [
    ({"calidad": "RED"}, "meta_salud:red"),
    ({"calidad": "GREEN", "limite": "TIER_250"}, "meta_salud:limite_bajo"),
])
def test_a_bad_health_brakes_the_confirmation(
    att1_instance: Path, tmp_path: Path, reading: dict, brake: str
) -> None:
    settings = _settings(att1_instance, health=_health(tmp_path, {**_fresh(), **reading}))
    authority = _RegistrationAuthority()
    assert _send(_first_contact_app(settings, authority), "att1-clase-28-11").status_code == 200
    [call] = authority.registration_calls
    assert call["health_brake"] == brake


def test_without_the_health_file_nothing_goes_out(att1_instance: Path) -> None:
    # Fail-closed: el registro prendido sin FLOW_AUDIENCE_META_HEALTH_PATH frena todas las confirmaciones.
    settings = _settings(att1_instance, health=None)
    authority = _RegistrationAuthority()
    assert _send(_first_contact_app(settings, authority), "att1-clase-28-11").status_code == 200
    [call] = authority.registration_calls
    assert call["health_brake"] == "meta_salud:sin_lectura"
    assert '"own_forms_registration": "confirma:1:sin_salud"' in json.dumps(
        _ready(_first_contact_app(settings, _RegistrationAuthority())))


def test_a_base_without_the_entry_goes_back_to_the_first_part(att1_instance: Path, tmp_path: Path) -> None:
    settings = _settings(att1_instance, health=_health(tmp_path, _fresh()))
    authority = _RegistrationAuthority(missing=True)
    response = _send(_first_contact_app(settings, authority), "att1-clase-28-11")
    assert response.status_code == 200
    assert len(authority.registration_calls) == 1
    # Solo la admision, ningun mensaje: lo de la primera parte.
    assert (len(authority.admission_calls), len(authority.route_calls), len(authority.plan_calls)) == (1, 0, 0)


def test_ready_says_it_confirms(att1_instance: Path, tmp_path: Path) -> None:
    settings = _settings(att1_instance, health=_health(tmp_path, _fresh()))
    ready = _ready(_first_contact_app(settings, _RegistrationAuthority()))
    assert '"own_forms_registration": "confirma:1"' in json.dumps(ready)
    # Lo que lee el AE para no publicar sin efecto (E-353): la confirmacion corre.
    assert json.loads(ready["flow_change_runtime"])["native_registration_enabled"] is True


def test_the_settings_read_the_audience_health(monkeypatch: pytest.MonkeyPatch) -> None:
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
    off = app_module.Settings.from_env()
    assert (off.own_forms_registration_meta_health_path, off.own_forms_registration_meta_health_key) == (None, None)
    monkeypatch.setenv("FLOW_AUDIENCE_META_HEALTH_PATH", "/var/lib/meta-salud/estado.json")
    monkeypatch.setenv("FLOW_AUDIENCE_META_HEALTH_KEY", "att1")
    on = app_module.Settings.from_env()
    assert (on.own_forms_registration_meta_health_path, on.own_forms_registration_meta_health_key) == (
        "/var/lib/meta-salud/estado.json", "att1")


# -- El cliente de la base ---------------------------------------------------------------------------------


def _client(handler: object) -> SupabaseClient:
    return SupabaseClient(base_url="https://supabase.example.test", service_role_key="test-service-role",
                          transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


def _call(client: SupabaseClient, instance: Path) -> PrecheckoutAdmissionResult:
    # Una copia de la instancia: _first_contact_settings aprueba el conocimiento en el lugar.
    config = _first_contact_settings(instance).commercial_ally_config
    return asyncio.run(client.admit_and_route_registration_lead_precheckout(
        config=config, external_submission_id="own-form-z2", raw_payload={"id": "own-form-z2"},  # type: ignore[arg-type]
        canonical_payload={"external_submission_id": "own-form-z2"}, registration_event=EVENT,
        health_brake="meta_salud:red"))


def test_the_client_sends_the_event_and_the_brake_and_reads_the_route(att1_instance: Path) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx.Response(200, json=[{"outcome": "inserted", "submission_id": SUBMISSION_ID,
                                          "purchase_intent_id": INTENT_ID, "plan_outcome": None, "plan_reason": None,
                                          "ruta": "nativo", "flujos": [{"flow_id": FLOW_ID, "version": 1,
                                                                        "outcome": "motor_no_arranca",
                                                                        "reason": "meta_salud:red",
                                                                        "recovery_case_id": None}]}])

    result = _call(_client(handler), att1_instance)
    [request] = seen
    assert request["path"] == "/rest/v1/rpc/admit_and_route_registration_lead_precheckout_v1"
    assert (request["body"]["p_registration_event"], request["body"]["p_health_brake"]) == (EVENT, "meta_salud:red")
    assert result.route == "nativo" and result.native_flows[0].reason == "meta_salud:red"


def test_the_client_refuses_another_route_and_says_when_the_entry_is_missing(att1_instance: Path) -> None:
    def legacy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"outcome": "inserted", "submission_id": SUBMISSION_ID,
                                          "purchase_intent_id": INTENT_ID, "ruta": "legado", "flujos": []}])

    with pytest.raises(SupabaseError, match="invalid_route"):
        _call(_client(legacy), att1_instance)
    with pytest.raises(SupabaseRpcMissing):
        _call(_client(lambda request: httpx.Response(404, json={"code": "PGRST202"})), att1_instance)


# -- La simulacion de la vista previa (deuda de #532) ----------------------------------------------------------


def _new_confirmation_item() -> dict:
    definition = {key: value for key, value in _confirmation().items()
                  if key not in ("esquema", "flujo", "producto", "estado")}
    return {"producto": "att1", "flujo": "confirmacion_registro", "tipo": "nuevo", "definicion": definition,
            "prender": "cohorte"}


class _AllForms:
    """La lectura de conteos de hoy: con intencion cuenta TODOS los formularios consentidos del producto."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def read_new_flow_counts(self, instance: object, product: str, trigger: str, since: object) -> dict:
        self.asked.append(trigger)
        return {"contract": "flow-engine-v2", "trigger": trigger, "product_ref": product,
                "since": "2026-11-20T00:00:00Z", "until": "2026-11-27T00:00:00Z", "events": 1020, "people": 1001}


def test_a_new_registration_flow_is_not_simulated_with_the_commercial_forms() -> None:
    from bridge import flow_change_api
    from bridge.flow_change_simulation import build_new_flows_simulation
    from bridge.flow_definition import FlowWorld, WorldFlow, parse_items

    store = _AllForms()
    data = asyncio.run(flow_change_api._new_flow_input(  # noqa: SLF001
        store, {"tenant_ref": "lancemos", "ally_ref": "att1"}, "att1", _new_confirmation_item()))
    # Antes: pedia los conteos de intencion (1.020 formularios comerciales de ATT1) y decia que la confirmacion
    # le habria llegado a 1.001 personas.
    assert store.asked == [] and data == {"new_flow_uncounted": True}
    commercial = {key: value for key, value in FIRST_CONTACT.items()
                  if key not in ("esquema", "flujo", "producto", "estado")}
    world = FlowWorld(flows=(WorldFlow(flow_id=FLOW_ID, product_ref="att1", flow_key="primer_contacto",
                                       state="prendido", active_version=1, versions={1: commercial},
                                       native=True),))
    built = build_new_flows_simulation([data], parse_items([_new_confirmation_item()]), world=world)
    assert built["status"] == "built", built
    summary = built["summary"]
    assert (summary["disparador"], summary["clave"], summary["alcance_al_nacer"]) == ("registro", EVENT, 0)
    assert "personas" not in summary and "mensajes_si_fuera_para_todos" not in summary
    # D-43.8: el primer contacto comercial no toma un registro, asi que no comparte el evento.
    assert "comparte_el_evento_con" not in summary


def _registration_world_flow(product: str = "att1", flow_key: str = "confirmacion_registro",
                             flow_id: str = FLOW_ID, state: str = "prendido", event: str = EVENT):
    from bridge.flow_definition import WorldFlow

    definition = {key: value for key, value in _confirmation(dispara={"evento": "intencion", "registro": event}).items()
                  if key not in ("esquema", "flujo", "producto", "estado")}
    return WorldFlow(flow_id=flow_id, product_ref=product, flow_key=flow_key, state=state, active_version=1,
                     versions={1: definition}, native=True)


def test_a_new_commercial_flow_does_not_share_the_event_with_a_registration_flow() -> None:
    from bridge.flow_change_simulation import build_new_flow_simulation
    from bridge.flow_definition import FlowWorld, parse_items

    commercial = {key: value for key, value in FIRST_CONTACT.items()
                  if key not in ("esquema", "flujo", "producto", "estado")}
    item = {"producto": "att1", "flujo": "otro_contacto", "tipo": "nuevo", "definicion": commercial,
            "prender": "cohorte"}
    counts = {"contract": "flow-engine-v2", "trigger": "intencion", "product_ref": "att1",
              "since": "2026-11-20T00:00:00Z", "until": "2026-11-27T00:00:00Z", "events": 1020, "people": 1001}
    world = FlowWorld(flows=(_registration_world_flow(),))
    built = build_new_flow_simulation(counts, parse_items([item]), world=world)
    assert built["status"] == "built", built
    # D-43.8: el registro prendido no arranca con el formulario comercial.
    assert "att1/confirmacion_registro" not in [
        entry["flujo"] for entry in built["summary"].get("comparte_el_evento_con") or []]


def test_a_registration_shares_only_with_the_same_event_of_the_same_product() -> None:
    from bridge.flow_change_simulation import build_new_flows_simulation, build_native_uncounted_simulation
    from bridge.flow_definition import FlowWorld, parse_items

    world = FlowWorld(flows=(
        _registration_world_flow(),
        _registration_world_flow(product="maribel", flow_key="confirmacion_maribel",
                                 flow_id="00000000-0000-4000-8000-00000000d432"),
        _registration_world_flow(flow_key="otra_clase", flow_id="00000000-0000-4000-8000-00000000d433",
                                 event="clase-30-11"),
    ))
    built = build_new_flows_simulation([{"new_flow_uncounted": True}], parse_items([_new_confirmation_item()]),
                                       world=world)
    assert built["summary"]["comparte_el_evento_con"] == [{"flujo": "att1/confirmacion_registro",
                                                           "estado": "prendido"}]
    raw = {"producto": "att1", "flujo": "confirmacion_registro", "tipo": "apagar", "flow_id": FLOW_ID}
    native = build_native_uncounted_simulation(parse_items([raw]), world=world)
    assert native["status"] == "built", native
    summary = native["summary"]
    assert (summary["disparador"], summary["clave"], summary["queda_en"]) == ("registro", EVENT, "apagado")
    # El flujo no se nombra a si mismo, y el registro de otro producto o de otro evento no comparte.
    assert "comparte_el_evento_con" not in summary


def test_two_new_registrations_to_the_same_event_name_each_other_only_in_the_same_product() -> None:
    from bridge.flow_change_simulation import build_new_flows_simulation
    from bridge.flow_definition import FlowWorld, parse_items

    second = {**_new_confirmation_item(), "flujo": "confirmacion_dos"}
    built = build_new_flows_simulation([{"new_flow_uncounted": True}] * 2,
                                       parse_items([_new_confirmation_item(), second]), world=FlowWorld(flows=()))
    assert built["status"] == "built", built
    first, two = built["summary"]["flujos"]
    assert first["comparte_el_evento_con"] == [{"flujo": "att1/confirmacion_dos", "estado": "nuevo"}]
    assert two["comparte_el_evento_con"] == [{"flujo": "att1/confirmacion_registro", "estado": "nuevo"}]
