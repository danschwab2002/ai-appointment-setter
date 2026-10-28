"""El motor nativo manda plantillas con respuestas rapidas, y la categoria sale del permiso (E-158).

flow-engine-v2 2.6.0. Datos: la plantilla de los flujos de Maribel,
``maribel_retomar_proceso_01`` (APPROVED, MARKETING, es_MX), en
``tests/fixtures/chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json``.
Es una RECONSTRUCCION, no una captura del catalogo de Chatwoot: el cuerpo y los
botones son el pedido exacto que ``meta_plantillas.py`` subio a Meta el
2026-10-26T23:24Z, y el id, el estado y la categoria salen de ``meta_plantillas.py
listar`` del 2026-10-27 ~00:35Z (el detalle esta en el ``_capture`` del fixture).
Cuando se capture el inbox 13, la captura reemplaza al fixture y estas pruebas
corren igual.

Lo que fijan:
- el validador deja pasar sus dos respuestas rapidas en un flujo nativo; con
  ``permiso: compra`` la frena la regla 17 por ser MARKETING, y con otro permiso
  no la frena ni el boton ni la categoria (el permiso lo frena todavia la 14
  nativa: la base solo admite la compra, y lo levanta el PR 2 del hito 4);
- el envio nativo la arma sin parametros de boton, con la categoria que acepta
  el permiso: ``UTILITY`` con ``compra``, ``MARKETING`` o ``UTILITY`` con otro;
- con la compra, que es el unico permiso nativo de hoy, el paso que la pide se
  cierra sin mandar nada; la misma plantilla en ``UTILITY`` (la categoria con la
  que se subio, 26/10) sale con sus dos respuestas rapidas.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
import test_flow_change_api_app as app_tests
import test_flow_engine_native_template_shape as shape
from test_flow_engine_native_dispatch import _dispatcher, _NativeAuthority, _NativeChatwoot, _step, _templates

from bridge.approved_templates import ApprovedTemplateError, parse_approved_template
from bridge.native_flows import NATIVE_PURCHASE_PERMISSION, native_template_categories
from test_durable_dispatcher_approved_template import NOW

FIXTURES = Path(__file__).parent / "fixtures"
RECONSTRUCTED = json.loads((FIXTURES / "chatwoot_message_templates_inbox_13_maribel_reconstruida_20261027.json")
                           .read_text(encoding="utf-8"))
[MARIBEL] = RECONSTRUCTED["message_templates"]
NAME = MARIBEL["name"]
BODY = next(c["text"] for c in MARIBEL["components"] if c["type"] == "BODY")
NOMBRE, PRODUCTO = MARIBEL["components"][0]["example"]["body_text"][0]
# La misma plantilla con la categoria con la que se subio a Meta (UTILITY, 26/10), antes de
# que Meta la pasara a MARKETING: la unica forma de ver salir sus botones con el permiso compra.
AS_SUBMITTED = dict(copy.deepcopy(MARIBEL), category="UTILITY")


def test_the_fixture_is_the_maribel_template_with_two_quick_replies() -> None:
    assert RECONSTRUCTED["_capture"]["captured_at"] is None  # reconstruida: falta la captura del inbox 13
    assert (NAME, MARIBEL["status"], MARIBEL["category"], MARIBEL["language"]) == (
        "maribel_retomar_proceso_01", "APPROVED", "MARKETING", "es_MX")
    assert [c["type"] for c in MARIBEL["components"]] == ["BODY", "BUTTONS"]
    assert BODY.count("{{") == 2 and "{{1}}" in BODY and "{{2}}" in BODY
    [buttons] = [c["buttons"] for c in MARIBEL["components"] if c["type"] == "BUTTONS"]
    assert buttons == [{"type": "QUICK_REPLY", "text": "Retomar proceso"},
                       {"type": "QUICK_REPLY", "text": "No mas mensajes"}]


# -- La categoria segun el permiso ---------------------------------------------


@pytest.mark.parametrize(("permission", "categories"), [
    ("compra", {"UTILITY"}),
    ("formulario", {"MARKETING", "UTILITY"}),
    ("carrito", {"MARKETING", "UTILITY"}),
    (None, {"MARKETING", "UTILITY"}),
])
def test_the_categories_a_native_send_accepts(permission: object, categories: set[str]) -> None:
    assert native_template_categories(permission) == categories
    assert NATIVE_PURCHASE_PERMISSION == "compra"


# -- El envio: parse_approved_template ----------------------------------------


def _parse(template: dict, permission: str):
    return parse_approved_template({"message_templates": [template]}, template_name=NAME, expected_language="es_MX",
                                   expected_category=native_template_categories(permission), parameter_count=2,
                                   parameterless_buttons_only=True)


def test_with_another_permission_it_goes_with_body_parameters_only() -> None:
    approved = _parse(MARIBEL, "formulario")
    assert approved.category == "MARKETING"
    assert approved.has_checkout_button is False and approved.button_url is None
    assert approved.processed_params({"1": NOMBRE, "2": PRODUCTO}) == {"body": {"1": NOMBRE, "2": PRODUCTO}}
    assert approved.render({"1": NOMBRE, "2": PRODUCTO}) == BODY.replace("{{1}}", NOMBRE).replace("{{2}}", PRODUCTO)


def test_with_the_purchase_permission_a_marketing_template_is_refused() -> None:
    with pytest.raises(ApprovedTemplateError) as error:
        _parse(MARIBEL, "compra")
    assert (error.value.reason, error.value.detail) == ("approved_template_mismatch", "category_mismatch")


def test_a_quick_reply_after_a_variable_url_is_still_refused() -> None:
    template = copy.deepcopy(MARIBEL)
    template["components"][1]["buttons"].insert(0, {"type": "URL", "text": "Pagar",
                                                    "url": "https://pay.hotmart.com/{{1}}"})
    with pytest.raises(ApprovedTemplateError) as error:
        _parse(template, "formulario")
    assert error.value.detail == "url_button_with_variable"


def test_without_the_flag_the_legacy_path_reads_it_as_before() -> None:
    # El camino del legado (la ranura del manifiesto, que hoy manda Maribel) no cambia.
    approved = parse_approved_template({"message_templates": [MARIBEL]}, template_name=NAME,
                                       expected_language="es_MX", expected_category="MARKETING", parameter_count=2)
    assert approved.category == "MARKETING"
    with pytest.raises(ValueError, match="approved_template_expected_category_invalid"):
        parse_approved_template({"message_templates": [MARIBEL]}, template_name=NAME, expected_language="es_MX",
                                expected_category=frozenset(), parameter_count=2)


# -- El envio del paso nativo: el dispatcher -----------------------------------


class _MaribelChatwoot(_NativeChatwoot):
    def __init__(self, template: dict) -> None:
        super().__init__()
        self.catalog.append(copy.deepcopy(template))


def test_a_native_purchase_step_never_sends_the_marketing_template(tmp_path: Path) -> None:
    authority = _NativeAuthority(step=_step(template_name=NAME, template_variables=("nombre", "producto")))
    chatwoot = _MaribelChatwoot(MARIBEL)

    asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    assert _templates(chatwoot) == []
    assert authority.finalizations[0]["reason_code"] == "approved_template_mismatch"


def test_a_native_step_sends_the_quick_replies_with_the_category_of_the_catalog(tmp_path: Path) -> None:
    authority = _NativeAuthority(step=_step(template_name=NAME, template_variables=("nombre", "producto")))
    chatwoot = _MaribelChatwoot(AS_SUBMITTED)

    asyncio.run(_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    [message] = _templates(chatwoot)
    params = message["template_params"]
    assert (params["name"], params["category"], params["language"]) == (NAME, "UTILITY", "es_MX")
    # Las respuestas rapidas no llevan parametro: solo los dos del cuerpo.
    assert set(params["processed_params"]) == {"body"}
    assert list(params["processed_params"]["body"]) == ["1", "2"]
    assert authority.acceptances


# -- El validador: la regla 13 y la 17 -------------------------------------------


@pytest.fixture
def maribel_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "1.19.0")
    monkeypatch.setenv("GIT_SHA", "e56871777d9b7d462948f638e7815a5c3d5879d1")
    templates = [t for t in app_tests.CATALOG["message_templates"] if t.get("name") != NAME]
    monkeypatch.setitem(app_tests.CATALOG, "message_templates", [*templates, copy.deepcopy(MARIBEL)])


STEPS = [{"plantilla": NAME, "variables": ["nombre", "producto"]}, {"etiquetar": "cliente"}]


def test_with_the_purchase_permission_the_validator_blocks_it_by_the_category(maribel_catalog: None) -> None:
    _, result = shape._propose_and_validate(STEPS)

    codes = {code for code, _ in shape._blocks(result)}
    assert "template_button_mismatch" not in codes
    assert ("purchase_permission_requires_single_utility_send", "/0/definicion/pasos/0/plantilla") in \
        shape._blocks(result)


def test_with_another_permission_neither_the_buttons_nor_the_category_block(
        maribel_catalog: None, monkeypatch: pytest.MonkeyPatch) -> None:
    original = shape._item

    def item(pasos: list) -> dict:
        built = original(pasos)
        built["definicion"]["entra"] = {"permiso": "formulario"}
        return built

    monkeypatch.setattr(shape, "_item", item)
    _, result = shape._propose_and_validate(STEPS)

    codes = {code for code, _ in shape._blocks(result)}
    assert "template_button_mismatch" not in codes
    assert "purchase_permission_requires_single_utility_send" not in codes
    # Lo que sigue frenando es el permiso del motor nativo, que solo sabe la compra (PR 2 del hito 4).
    assert codes == {"motor_nativo:permiso"}
