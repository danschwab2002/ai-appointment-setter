"""El motor nativo manda plantillas con boton de URL fija y con sus propias variables (E-115).

Datos: la bienvenida real de ATT1, ``att1_bienvenida_01`` (APPROVED, UTILITY,
es_MX), leida del catalogo de la bandeja 11 el 2026-10-26 por E-113:
``tests/fixtures/chatwoot_message_templates_inbox_11_bienvenida_20261026.json``.
Tiene un encabezado de texto fijo («Notificación»), un cuerpo con un solo
``{{1}}`` (el producto, segun su ejemplo) y un boton URL fijo
(«Completar mi perfil», sin variable). En el mismo fixture van la ficha de
efectivo (boton URL con ``{{1}}`` y respuesta rapida), que un flujo nativo
sigue sin poder mandar, y el carrito (tres respuestas rapidas, MARKETING): desde
flow-engine-v2 2.6.0 (E-158) sus botones pasan, y lo frena la categoria con
``permiso: compra`` (regla 17). La plantilla de Maribel, con respuestas rapidas,
esta en ``test_flow_engine_native_quick_replies.py``.

La prueba de aceptacion es la prueba en seco de E-113: el mismo item que
mandaria el Automation Expert, por el doble del bridge
(``test_flow_change_api_app``), sale ``validada``; y el envio que arma el
bridge para ese paso es el que Meta espera: un parametro de cuerpo, cero de
boton.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
import test_flow_change_api_app as app_tests
from test_flow_change_api_app import AUTH, PREFIX, SLACK, PostgrestEmulator, _client
from test_flow_engine_native_dispatch import (
    _dispatcher,
    _NativeAuthority,
    _NativeChatwoot,
    _step,
    _templates,
)

from bridge.approved_templates import ApprovedTemplateError, is_static_url, parse_approved_template
from bridge.flow_engine import native_not_supported, native_variables_fit
from test_durable_dispatcher_approved_template import NOW

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURED = json.loads(
    (FIXTURES / "chatwoot_message_templates_inbox_11_bienvenida_20261026.json").read_text(encoding="utf-8"))
REAL = {template["name"]: template for template in CAPTURED["message_templates"]}
WELCOME = REAL["att1_bienvenida_01"]
WELCOME_BODY = next(c["text"] for c in WELCOME["components"] if c["type"] == "BODY")
PRODUCT = WELCOME["components"][1]["example"]["body_text"][0][0]
INBOX = {"message_templates": list(CAPTURED["message_templates"])}
COUNTS = {"contract": "flow-engine-v2", "since": "2026-10-19T12:52:00Z", "until": "2026-10-26T12:52:00Z",
          "events": 34, "people": 34}
OK = {"quien": "dan", "medio": "slack", "fecha": "2026-10-26"}


def test_the_fixture_is_the_captured_welcome() -> None:
    assert WELCOME["status"] == "APPROVED" and WELCOME["category"] == "UTILITY" and WELCOME["language"] == "es_MX"
    assert WELCOME_BODY.count("{{") == 1 and "{{1}}" in WELCOME_BODY
    [buttons] = [c["buttons"] for c in WELCOME["components"] if c["type"] == "BUTTONS"]
    assert [(b["type"], is_static_url(b["url"])) for b in buttons] == [("URL", True)]
    assert {"type": "HEADER", "format": "TEXT", "text": "Notificación"} in WELCOME["components"]


# -- El envio: parse_approved_template ----------------------------------------


def _parse(name: str, count: int, **kwargs: object):
    # La categoria de cada una, tal cual (el carrito es MARKETING): aca se mira el boton.
    return parse_approved_template(INBOX, template_name=name, expected_language="es_MX",
                                   expected_category=REAL[name]["category"], parameter_count=count, **kwargs)


def test_a_fixed_url_button_goes_without_button_parameters() -> None:
    approved = _parse("att1_bienvenida_01", 1, parameterless_buttons_only=True)
    assert approved.has_checkout_button is False and approved.button_url is None
    assert approved.processed_params({"1": PRODUCT}) == {"body": {"1": PRODUCT}}
    assert approved.render({"1": PRODUCT}) == WELCOME_BODY.replace("{{1}}", PRODUCT)
    # Y no acepta un sufijo de boton que no tiene donde ir.
    with pytest.raises(ApprovedTemplateError) as error:
        approved.processed_params({"1": PRODUCT}, button_suffix="x")
    assert error.value.detail == "unexpected_button_suffix"


def test_without_the_flag_the_fixed_url_button_is_refused_as_before() -> None:
    # Lo que factura (el legado) no cambia: sin el flag, un boton URL sigue siendo unsupported_button.
    with pytest.raises(ApprovedTemplateError) as error:
        _parse("att1_bienvenida_01", 1)
    assert (error.value.reason, error.value.detail) == ("approved_template_mismatch", "unsupported_button")


@pytest.mark.parametrize(("name", "count", "detail"), [
    ("att1_ficha_efectivo_01", 4, "url_button_with_variable"),
], ids=["url con variable"])
def test_other_buttons_stay_refused_in_a_native_send(name: str, count: int, detail: str) -> None:
    body = next(c["text"] for c in REAL[name]["components"] if c["type"] == "BODY")
    assert body.count("{{") == count
    with pytest.raises(ApprovedTemplateError) as error:
        _parse(name, count, parameterless_buttons_only=True)
    assert (error.value.reason, error.value.detail) == ("approved_template_mismatch", detail)


def test_quick_replies_go_without_button_parameters_in_a_native_send() -> None:
    # 2.6.0 (E-158): las tres respuestas rapidas del carrito pasan, sin parametros de boton.
    approved = _parse("att1_carrito_abandonado_01", 2, parameterless_buttons_only=True)
    assert approved.processed_params({"1": "María", "2": PRODUCT}) == {"body": {"1": "María", "2": PRODUCT}}


def test_the_flag_excludes_a_declared_url_button() -> None:
    with pytest.raises(ValueError, match="approved_template_parameterless_buttons_conflict"):
        _parse("att1_bienvenida_01", 1, parameterless_buttons_only=True, checkout_button=True)


@pytest.mark.parametrize(("url", "fixed"), [
    ("https://forms.gle/mzopWTpQae8TDBph9", True),
    ("https://pay.hotmart.com/{{1}}", False),
    ("http://forms.gle/x", False),
    ("https://forms.gle/a b", False),
    (None, False),
])
def test_what_a_fixed_url_is(url: object, fixed: bool) -> None:
    assert is_static_url(url) is fixed


# -- El envio del paso nativo: el dispatcher -----------------------------------


class _RealWelcomeChatwoot(_NativeChatwoot):
    """El inbox 11 con la bienvenida real en lugar de la derivada."""

    def __init__(self) -> None:
        super().__init__()
        self.catalog[:] = [t for t in self.catalog if t.get("name") != "att1_bienvenida_01"]
        self.catalog.extend(copy.deepcopy(list(REAL.values())))


def test_the_native_step_sends_the_real_welcome_with_one_body_parameter(tmp_path: Path) -> None:
    authority = _NativeAuthority(step=_step(template_variables=("producto",)))
    chatwoot = _RealWelcomeChatwoot()

    asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    [message] = _templates(chatwoot)
    params = message["template_params"]
    assert (params["name"], params["category"], params["language"]) == ("att1_bienvenida_01", "UTILITY", "es_MX")
    # Lo que Meta espera para esta plantilla: un parametro de cuerpo, cero de boton.
    assert params["processed_params"] == {"body": {"1": PRODUCT}}
    assert message["content"] == WELCOME_BODY.replace("{{1}}", PRODUCT)
    assert authority.acceptances


@pytest.mark.parametrize(("name", "variables"), [
    ("att1_ficha_efectivo_01", ("nombre", "producto")),
    ("att1_carrito_abandonado_01", ("nombre", "producto")),
], ids=["url con variable", "marketing con permiso compra"])
def test_a_native_step_with_another_button_never_goes_out(tmp_path: Path, name: str,
                                                          variables: tuple[str, ...]) -> None:
    authority = _NativeAuthority(step=_step(template_name=name, template_variables=variables))
    chatwoot = _RealWelcomeChatwoot()

    asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    assert _templates(chatwoot) == []
    assert authority.finalizations
    assert authority.finalizations[0]["reason_code"] == "approved_template_mismatch"


# -- La regla 14 nativa: las variables del paso ---------------------------------


@pytest.mark.parametrize(("variables", "fits"), [
    (["producto"], True), (["nombre"], True), (["nombre", "producto"], True), (["producto", "nombre"], True),
    ([], False), (["nombre", "nombre"], False), (["precio"], False), (["producto", "precio"], False),
    (None, False), ("producto", False), ([1], False),
])
def test_the_variables_a_native_step_may_send(variables: object, fits: bool) -> None:
    # El mismo criterio que el compilador de la base (validate_flow_engine_native_template_shape.mjs).
    assert native_variables_fit(variables) is fits


def _definition(variables: list[str]) -> dict:
    return {"dispara": {"evento": "compra"}, "entra": {"permiso": "compra"},
            "topes": {"repeticion": {"una_por": "persona", "otra_despues_de": "nunca"}},
            "pasos": [{"plantilla": "att1_bienvenida_01", "idioma": "es_MX", "variables": variables},
                      {"etiquetar": "cliente"}]}


def test_native_not_supported_reads_the_variables_of_the_step() -> None:
    assert native_not_supported(_definition(["producto"])) == ()
    assert native_not_supported(_definition(["nombre", "producto"])) == ()
    assert "engine_not_supported:variables_differ" in native_not_supported(_definition(["producto", "producto"]))


# -- La prueba de aceptacion: la prueba en seco de E-113 -------------------------


@pytest.fixture
def real_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    # La version y el sha de la v1.19.0 de ATT1 (E-113): sin ellos el doble contesta 503.
    monkeypatch.setenv("SETTER_VERSION", "1.19.0")
    monkeypatch.setenv("GIT_SHA", "e56871777d9b7d462948f638e7815a5c3d5879d1")
    templates = [t for t in app_tests.CATALOG["message_templates"] if t.get("name") not in REAL]
    monkeypatch.setitem(app_tests.CATALOG, "message_templates", [*templates, *copy.deepcopy(list(REAL.values()))])


def _item(pasos: list) -> dict:
    return {"producto": "att1", "flujo": "bienvenida", "tipo": "nuevo", "prender": "cohorte", "ok_del_cliente": OK,
            "definicion": {"nombre": "Bienvenida despues de la compra", "dispara": {"evento": "compra"},
                           "entra": {"permiso": "compra"}, "pasos": pasos, "horario": "siempre"}}


def _propose_and_validate(pasos: list) -> tuple[dict, dict]:
    emulator = PostgrestEmulator()
    emulator.new_flow_counts = dict(COUNTS)
    client, _ = _client(emulator)
    saved = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json={
        "items": [_item(pasos)], "base": {}, "purpose": "la bienvenida despues de la compra, con la etiqueta",
        "requested_by": SLACK})
    assert saved.status_code == 201, saved.json()
    data = saved.json()["data"]
    definition = emulator.proposals[data["proposal_id"]]["items"][0]["definicion"]
    validated = client.post(f"{PREFIX}/flow-changes/{data['proposal_id']}/validate", headers=AUTH,
                            json={"revision": data["revision"], "requested_by": SLACK})
    assert validated.status_code == 200, validated.json()
    return definition, validated.json()["data"]


def _blocks(result: dict) -> list[tuple[str, str]]:
    return [(block["code"], block["path"]) for block in result["blocks"]]


def test_acceptance_the_welcome_with_its_one_variable_is_validated(real_catalog: None) -> None:
    definition, result = _propose_and_validate(
        [{"plantilla": "att1_bienvenida_01", "variables": ["producto"]}, {"etiquetar": "cliente"}])

    assert definition["pasos"][0] == {"plantilla": "att1_bienvenida_01", "idioma": "es_MX", "variables": ["producto"]}
    assert result["blocks"] == []
    assert result["status"] == "validada"


def test_the_welcome_with_the_default_variables_says_how_many_markers_it_has(real_catalog: None) -> None:
    # Lo que mandaria el AE con el SOUL de la 1.19.0 (caso A de E-113): el esquema completa
    # [nombre, producto] y la plantilla tiene un solo marcador. Ya no hay bloqueo de boton.
    _, result = _propose_and_validate([{"plantilla": "att1_bienvenida_01"}, {"etiquetar": "cliente"}])

    assert result["status"] == "borrador"
    [block] = [b for b in result["blocks"] if b["code"] == "template_variable_count_mismatch"]
    assert block["path"].endswith("/pasos/0/variables")
    assert "un marcador" in block["reason"] and "nombre y producto" in block["reason"]
    assert "template_button_mismatch" not in {code for code, _ in _blocks(result)}


def test_a_native_flow_with_a_variable_url_button_is_blocked_with_a_clear_reason(real_catalog: None) -> None:
    _, result = _propose_and_validate(
        [{"plantilla": "att1_ficha_efectivo_01", "variables": ["nombre", "producto"]}, {"etiquetar": "cliente"}])

    assert result["status"] == "borrador"
    [block] = [b for b in result["blocks"] if b["code"] == "template_button_mismatch"]
    assert "URL con una variable" in block["reason"]


def test_a_native_flow_with_quick_replies_is_blocked_only_by_the_category(real_catalog: None) -> None:
    # 2.6.0 (E-158): las respuestas rapidas del carrito ya no frenan; con permiso compra, la
    # plantilla MARKETING la frena la 17.
    _, result = _propose_and_validate(
        [{"plantilla": "att1_carrito_abandonado_01", "variables": ["nombre", "producto"]}, {"etiquetar": "cliente"}])

    assert result["status"] == "borrador"
    codes = {code for code, _ in _blocks(result)}
    assert "template_button_mismatch" not in codes
    assert "purchase_permission_requires_single_utility_send" in codes


# -- Lo que sabe el Automation Expert --------------------------------------------

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "automation-expert"


def test_the_soul_says_the_step_writes_its_variables() -> None:
    soul = (PROFILE / "SOUL.md").read_text(encoding="utf-8")
    section = soul.split("**Creás un flujo nuevo cuando te lo piden**", 1)[1].split("- **No aprobás", 1)[0]
    # Lo que decia mal hasta la 1.19.0 (E-113): las variables no salen de la plantilla.
    assert "el idioma y las variables de la plantilla" not in section
    for phrase in ("`variables`", "una por cada marcador", "`[\"producto\"]`", "template_variable_count_mismatch",
                   "link fijo", "template_button_mismatch", "pedir otra plantilla a Meta"):
        assert phrase in section, phrase


def test_the_library_says_how_a_purchase_template_is_named_and_what_it_carries() -> None:
    page = (PROFILE / "plugins" / "automation-expert-read" / "biblioteca" / "chatwoot-whatsapp.md").read_text(
        encoding="utf-8")
    for phrase in ("variables: [\"producto\"]", "una por marcador", "los da quien la pidió", "link fijo",
                   "respuesta rápida", "template_button_mismatch", "solo la nota"):
        assert phrase in page, phrase
