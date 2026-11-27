"""4.3, confirmacion de registro a un evento: la primera parte (own-forms-v1 «Registro a un evento»).

Lo que se prueba: ``[[formularios_propios]]`` con ``tipo = "registro"`` (D-43.1, D-43.5); la admision de
un formulario de registro con OWN_FORMS_REGISTRATION_FLOW_ENABLED y el motor (D-43.3: se admite y no se
planifica el primer contacto comercial, ni del legado ni nativo); que con cualquier flag apagado va
exactamente como hoy (D-43.4); ``/ready``; y ``dispara.registro`` en el esquema del flujo (D-43.2: la
regla 1 lo acepta con ``intencion`` y la regla 14 lo marca «todavia no», asi que no se puede publicar).

Dato real: ``tests/fixtures/own_forms/att1_own_form_submission_shape_20261126.json``, la forma del envio
de prueba del formulario propio ``att1-org-b`` del 26/11 15:28Z en att1-db (solo lectura, sin datos
personales). El comprador de los envios es el sintetico de ``test_own_forms``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import test_flow_engine_native_template_shape as shape
from test_flow_engine_native_intent import ATT1_INSTANCE, FIRST_CONTACT, _RouteAuthority, _engine
from test_flow_engine_native_quick_replies import maribel_catalog  # noqa: F401 - el fixture del validador
from test_instance_wiring import _first_contact_app, _first_contact_settings
from test_own_forms import BUYER, CONFIG, LANDING, NOW, att1_payload, submission

import bridge.app as app_module
from bridge.app import Settings
from bridge.flow_definition import check_definition
from bridge.supabase import SupabaseRpcMissing
from bridge.flow_engine import REGISTRATION_TRIGGER, native_not_supported, not_supported
from bridge.instance_manifest import (
    OWN_FORM_DEFAULT_DISCLAIMER,
    OWN_FORM_KIND_PRECHECKOUT,
    OWN_FORM_KIND_REGISTRATION,
    InstanceManifest,
    ManifestError,
    OwnForm,
)
from bridge.own_forms import (
    OWN_FORM_ADMISSION_REGISTRATION,
    OWN_FORM_ADMISSION_TODAY,
    REGISTRATION_FLOW_FLAGS,
    own_form_admission,
    registration_flow_missing,
    translate_own_form_submission,
)

FIXTURES = Path(__file__).parent / "fixtures"
SHAPE = json.loads(
    (FIXTURES / "own_forms" / "att1_own_form_submission_shape_20261126.json").read_text(encoding="utf-8")
)
FLAG = "OWN_FORMS_REGISTRATION_FLOW_ENABLED"
BASE = "https://setter-att1.example"
THANKS = "https://site.metodoraizana.com.mx/gracias-clase"
REGISTRATION = {
    "clave": "att1-clase-28-11",
    "landing_id": "alimenta-tu-tiroides",
    "tipo": "registro",
    "evento": "clase-28-11",
    "gracias_url": THANKS,
    "usar_core": False,
}
SALES = {"clave": "att1-org-b", "landing_id": "alimenta-tu-tiroides"}


def _manifest(forms: list[dict]) -> InstanceManifest:
    payload = att1_payload()
    payload["formularios_propios"] = forms
    return InstanceManifest.from_mapping(payload)


# -- El manifiesto (D-43.1, D-43.5) ----------------------------------------------------------


def test_a_registration_form_is_read_with_its_event() -> None:
    registration, sales = _manifest([REGISTRATION, SALES]).own_forms
    assert (registration.kind, registration.registration_event, registration.is_registration) == (
        OWN_FORM_KIND_REGISTRATION, "clase-28-11", True)
    assert (registration.thanks_url, registration.use_core) == (THANKS, False)
    # Sin tipo, el de hoy: el formulario de la VSL antes del checkout.
    assert (sales.kind, sales.registration_event, sales.is_registration) == (
        OWN_FORM_KIND_PRECHECKOUT, None, False)


@pytest.mark.parametrize(("change", "message"), [
    ({"tipo": "inscripcion"}, r"tipo debe ser uno de precheckout, registro"),
    ({"evento": None}, r"evento"),
    ({"evento": "Clase 28"}, r"evento debe ser un slug"),
    ({"evento": "ab"}, r"evento debe ser un slug"),
    ({"gracias_url": None}, r"un formulario de registro exige gracias_url"),
    ({"usar_core": None}, r"un formulario de registro exige usar_core = false"),
    ({"usar_core": True}, r"un formulario de registro exige usar_core = false"),
])
def test_a_bad_registration_form_does_not_load(change: dict, message: str) -> None:
    form = {**REGISTRATION, **change}
    form = {key: value for key, value in form.items() if value is not None}
    with pytest.raises(ManifestError, match=message):
        _manifest([form])


def test_an_event_without_registro_does_not_load() -> None:
    with pytest.raises(ManifestError, match=r"evento solo vale con tipo = \"registro\""):
        _manifest([{**SALES, "evento": "clase-28-11"}])


def test_the_real_payload_carries_no_form_key() -> None:
    # El dato real: la base ve la landing y la oferta, no el formulario. Un envio de registro arma el
    # mismo evento (la misma landing, la misma forma), asi que la clave del evento no viaja en el envio:
    # la tiene que mandar el bridge aparte cuando la base rutee el registro (own-forms-v1).
    event = translate_own_form_submission(submission(), config=CONFIG, landing=LANDING, now=NOW).event
    assert sorted(event) == SHAPE["raw_keys"]
    assert sorted(event["data"]) == SHAPE["raw_data_keys"]
    assert sorted(event["data"]["buyer"]) == SHAPE["buyer_keys"]
    assert sorted(event["data"]["attribution"]) == SHAPE["attribution_keys"]
    assert event["source"] == SHAPE["raw_source"]
    assert event["data"]["offer"] == SHAPE["raw_offer"]
    assert event["data"]["consent"] == SHAPE["raw_consent"]
    flat = json.dumps(event)
    assert "att1-org-b" not in flat and "registro" not in flat


# -- La decision pura (D-43.3, D-43.4) -------------------------------------------------------


def test_the_registration_needs_its_flag_and_the_engine() -> None:
    assert REGISTRATION_FLOW_FLAGS == (
        FLAG, "OWN_FORMS_ENABLED", "FLOW_ENGINE_ENABLED", "FLOW_ENGINE_NATIVE_INTENT_ENABLED")
    assert registration_flow_missing(registration_enabled=True, own_forms_enabled=True,
                                     flow_engine_enabled=True, native_intent_enabled=True) == ()
    assert registration_flow_missing(registration_enabled=True, own_forms_enabled=True,
                                     flow_engine_enabled=True, native_intent_enabled=False) == (
        "FLOW_ENGINE_NATIVE_INTENT_ENABLED",)
    assert registration_flow_missing(registration_enabled=False, own_forms_enabled=True,
                                     flow_engine_enabled=False, native_intent_enabled=False) == (
        FLAG, "FLOW_ENGINE_ENABLED", "FLOW_ENGINE_NATIVE_INTENT_ENABLED")


def test_only_a_registration_form_with_the_registration_active_changes() -> None:
    registration, sales = _manifest([REGISTRATION, SALES]).own_forms
    assert own_form_admission(registration, registration_active=True) == OWN_FORM_ADMISSION_REGISTRATION
    assert own_form_admission(registration, registration_active=False) == OWN_FORM_ADMISSION_TODAY
    assert own_form_admission(sales, registration_active=True) == OWN_FORM_ADMISSION_TODAY
    assert own_form_admission(object(), registration_active=True) == OWN_FORM_ADMISSION_TODAY


# -- El esquema del flujo (D-43.2) -----------------------------------------------------------


def _registration_flow(**dispara: object) -> dict:
    flow = copy.deepcopy(FIRST_CONTACT)
    flow["flujo"] = "confirmacion_registro"
    flow["dispara"] = {"evento": "intencion", "registro": "clase-28-11", **dispara}
    # La confirmacion sale apenas llega el formulario: sin la espera del primer contacto.
    flow["pasos"] = [copy.deepcopy(FIRST_CONTACT["pasos"][1])]
    return flow


def _codes(findings: object) -> list[tuple[str, str]]:
    return [(item.code, item.path) for item in findings]  # type: ignore[attr-defined]


def test_rule_1_accepts_registro_with_intencion() -> None:
    assert [c for c in _codes(check_definition(_registration_flow())) if "registro" in c[1]] == []
    # Sin registro, lo de hoy.
    assert [c for c in _codes(check_definition(FIRST_CONTACT)) if c[1].startswith("/dispara")] == []


@pytest.mark.parametrize("dispara", [
    {"evento": "carrito", "registro": "clase-28-11"},
    {"evento": "intencion", "registro": "Clase 28"},
    {"evento": "intencion", "registro": 28},
])
def test_rule_1_refuses_a_bad_registro(dispara: dict) -> None:
    flow = {**_registration_flow(), "dispara": dispara}
    assert ("invalid_value", "/dispara/registro") in _codes(check_definition(flow))


def test_rule_14_says_not_yet_in_every_engine() -> None:
    flow = _registration_flow()
    assert "engine_not_supported:disparo_registro" in not_supported(flow)
    assert "motor_nativo:disparador" in native_not_supported(flow)
    # El primer contacto comercial, sin registro, no cambia.
    assert native_not_supported(FIRST_CONTACT) == ()
    assert "engine_not_supported:disparo_registro" not in not_supported(FIRST_CONTACT)


def test_the_capability_is_the_switch_of_rule_14() -> None:
    # Cuando un motor declare intencion:registro, la regla 14 deja de marcarlo.
    from bridge.flow_engine import ENGINE_CAPABILITIES_V1

    caps = replace(ENGINE_CAPABILITIES_V1, triggers=ENGINE_CAPABILITIES_V1.triggers | {REGISTRATION_TRIGGER})
    assert "engine_not_supported:disparo_registro" not in not_supported(_registration_flow(), caps)


def test_the_cycle_does_not_publish_a_registration_flow(maribel_catalog: None,  # noqa: F811
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    flow = _registration_flow()

    def item(pasos: list) -> dict:
        definition = {key: copy.deepcopy(flow[key])
                      for key in ("nombre", "dispara", "entra", "termina", "horario", "topes")}
        return {"producto": "att1", "flujo": "confirmacion_registro", "tipo": "nuevo", "prender": "cohorte",
                "definicion": {**definition, "pasos": pasos}}

    monkeypatch.setattr(shape, "_item", item)
    _, result = shape._propose_and_validate(copy.deepcopy(flow["pasos"]))

    assert result["status"] != "validada"
    codes = {block["code"] for block in result["blocks"]}
    # Lo que la frena es el disparador del registro (y lo que el nativo deriva de no reconocerlo: el
    # permiso y la forma de los pasos); el resto es el primer contacto que ya publica.
    assert "motor_nativo:disparador" in codes, result["blocks"]
    assert codes <= {"motor_nativo:disparador", "motor_nativo:permiso", "motor_nativo:forma",
                     "engine_not_supported:disparo_registro"}, result["blocks"]
    assert {block["kind"] for block in result["blocks"]} == {"todavia_no"}


# -- El flag, el arranque y /ready ------------------------------------------------------------


def test_the_flag_is_off_by_default_and_portable() -> None:
    assert Settings.__dataclass_fields__["own_forms_registration_flow_enabled"].default is False
    assert "own_forms_registration_flow_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


def test_from_env_reads_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert Settings.from_env().own_forms_registration_flow_enabled is False
    monkeypatch.setenv(FLAG, "true")
    assert Settings.from_env().own_forms_registration_flow_enabled is True
    monkeypatch.setenv(FLAG, "1")
    assert Settings.from_env().own_forms_registration_flow_enabled is False


@pytest.fixture
def att1_instance(tmp_path: Path) -> Path:
    target = tmp_path / "instancia"
    shutil.copytree(ATT1_INSTANCE, target)
    return target


def _forms(settings: Settings) -> tuple[OwnForm, ...]:
    landing = settings.commercial_ally_config.offer_landings[0].landing_id  # type: ignore[union-attr]
    return (
        OwnForm(key="att1-clase-28-11", landing_id=landing, disclaimer=OWN_FORM_DEFAULT_DISCLAIMER,
                thanks_url=THANKS, use_core=False, kind=OWN_FORM_KIND_REGISTRATION,
                registration_event="clase-28-11"),
        OwnForm(key="att1-org-b", landing_id=landing, disclaimer=OWN_FORM_DEFAULT_DISCLAIMER),
    )


def _settings(att1_instance: Path, *, registration: bool, engine: bool, native: bool) -> Settings:
    settings = _first_contact_settings(att1_instance)
    manifest = replace(settings.instance_manifest, own_forms=_forms(settings))  # type: ignore[arg-type]
    settings = replace(settings, instance_manifest=manifest, own_forms_enabled=True,
                       own_forms_public_base_url=BASE, own_forms_registration_flow_enabled=registration)
    if engine:
        settings = _engine(settings, flow_engine_native_intent_enabled=native)
    return settings


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


def _calls(authority: _RouteAuthority) -> tuple[int, int, int]:
    return len(authority.route_calls), len(authority.plan_calls), len(authority.admission_calls)


# Con cualquiera de los flags apagado, el formulario de registro es un formulario mas: lo de hoy.
# (registro, motor, intencion) -> (rutea el nativo, planifica el legado, solo admite)
TODAY = [
    pytest.param(False, True, True, (1, 0, 0), id="registro apagado, motor nativo prendido"),
    pytest.param(False, False, False, (0, 1, 0), id="todo apagado: el legado de ATT1"),
    pytest.param(True, False, False, (0, 1, 0), id="registro prendido sin el motor"),
    pytest.param(True, True, False, (0, 1, 0), id="registro prendido sin la intencion nativa"),
]


@pytest.mark.parametrize(("registration", "engine", "native", "expected"), TODAY)
def test_with_a_flag_off_the_registration_form_goes_as_today(
    att1_instance: Path, registration: bool, engine: bool, native: bool, expected: tuple[int, int, int]
) -> None:
    settings = _settings(att1_instance, registration=registration, engine=engine, native=native)
    for form_key in ("att1-clase-28-11", "att1-org-b"):
        authority = _RouteAuthority()
        response = _send(_first_contact_app(settings, authority), form_key)
        assert response.status_code == 200, response.text
        assert _calls(authority) == expected, form_key
        assert response.json()["status"] == "received"


def test_with_all_flags_the_registration_form_is_only_admitted(
    att1_instance: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Una base sin la entrada del registro (sin 20261127500000, la segunda parte): lo de la primera parte,
    # solo la admision. Con la entrada, test_own_forms_registration_confirmation.py.
    settings = _settings(att1_instance, registration=True, engine=True, native=True)

    class _WithoutTheRegistrationRpc(_RouteAuthority):
        async def admit_and_route_registration_lead_precheckout(self, **kwargs: object) -> object:
            raise SupabaseRpcMissing("registration_lead_precheckout_admission_and_route_failed: HTTP 404")

    authority = _WithoutTheRegistrationRpc()
    app = _first_contact_app(settings, authority)

    with caplog.at_level("INFO", logger="bridge.app"):
        response = _send(app, "att1-clase-28-11")

    assert response.status_code == 200, response.text
    # Ni el legado ni el motor nativo de intencion: solo la admision. No se planifica ningun mensaje.
    assert _calls(authority) == (0, 0, 1)
    assert response.json() == {"status": "received", "checkout_url": response.json()["checkout_url"],
                               "gracias_url": THANKS}
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("own_form_registration_admitted")]
    assert "form=att1-clase-28-11 evento=clase-28-11" in line and "route=sin_ruteo_de_la_base" in line
    assert BUYER["email"] not in line and BUYER["phone"] not in line

    # El formulario comercial de la misma instancia sigue yendo al motor, como hoy.
    authority = _RouteAuthority()
    response = _send(_first_contact_app(settings, authority), "att1-org-b")
    assert response.status_code == 200
    assert _calls(authority) == (1, 0, 0)


def test_ready_reports_the_registration_only_with_its_flag(att1_instance: Path) -> None:
    off = _ready(_first_contact_app(_settings(att1_instance, registration=False, engine=True, native=True),
                                    _RouteAuthority()))
    assert "own_forms_registration" not in json.dumps(off)
    inert = _ready(_first_contact_app(_settings(att1_instance, registration=True, engine=True, native=False),
                                      _RouteAuthority()))
    assert "inerte:FLOW_ENGINE_NATIVE_INTENT_ENABLED" in json.dumps(inert)
    on = _ready(_first_contact_app(_settings(att1_instance, registration=True, engine=True, native=True),
                                   _RouteAuthority()))
    # Desde la segunda parte (D-43.11): confirma, y sin el archivo de la salud de Meta, sin_salud.
    assert '"own_forms_registration": "confirma:1:sin_salud"' in json.dumps(on)
