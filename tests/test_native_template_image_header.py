"""La imagen fija del encabezado de una plantilla, en el paso de un flujo nativo (E-370).

Mapa: ``documentacion/2026-11-09-imagenes-en-plantillas-mapa.md``, parte 1. Sigue a E-160
(``test_template_header_media.py``), que dejo el parser y ``processed_params.header``.

Datos:

* El catalogo son capturas reales: la bienvenida de ATT1 derivada en
  ``test_flow_engine_native_dispatch.py``, la plantilla de utilidad del 2026-10-21 de
  ``test_flow_validator.py`` y las ramas de Maribel de
  ``test_flow_engine_native_payment_failure_branches.py``.
* **Ninguna captura tiene un encabezado de imagen: hay deuda.** El componente se agrega a una
  copia con la forma documentada por Meta («Template components», ``type`` ``HEADER``,
  ``format`` ``IMAGE``, ``example.header_handle``), como hizo E-160. El primer envio real lo
  confirma o lo corrige.
* Lo que se le manda a Chatwoot lo fija el codigo del tag ``v4.13.0``, releido en GitHub el
  2026-11-09: ``Whatsapp::TemplateProcessorService#build_header_params`` toma ``media_url`` y
  ``media_type`` de ``processed_params['header']``.
* El envio sin imagen es el que armaba ``origin/main`` en ``d2ba90b``: el cuerpo entero del POST a
  Chatwoot, capturado corriendo estos dobles contra ese codigo el 2026-11-09 y pegado tal cual en
  ``SIN_IMAGEN_ATT1`` y ``SIN_IMAGEN_MARIBEL``.
"""

from __future__ import annotations

import copy
import io
import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import test_flow_change_api_app as app_tests
import test_flow_engine_native_dispatch as dispatch
import test_flow_engine_native_payment_failure_branches as branches
from test_durable_dispatcher_approved_template import TEMPLATE
from test_flow_change_api_app import AUTH, PREFIX, SLACK, PostgrestEmulator
from test_flow_engine_native_dispatch import (
    WELCOME,
    _dispatcher,
    _NativeAuthority,
    _NativeChatwoot,
    _run,
    _step,
    _templates,
)
from test_flow_validator import (
    CONFIRMED,
    CONFIRMED_STEP,
    INSTANCE,
    _follow_up,
    catalog_with,
    flow_def,
    run_new,
    version_item,
    check,
    welcome,
)
from test_flow_change_operator_script import script as operator_script
from test_flow_approval_page import PROPOSAL
from test_flow_approval_writes import _page, _post, _publish_fields, _signed
from test_flow_engine_native_template_shape import COUNTS, REAL, _blocks, _item
from test_instance_manifest import _load, _payload
from test_instance_wiring import _WABA_OUTBOUND, _att1_manifest, _settings

from bridge import flow_approval
from bridge.app import _waba_template_config
from bridge.approved_templates import TemplateHeaderMedia
from bridge.flow_change_api import _Context, _instance_view
from bridge.flow_change_operator import OperatorInputError, instance_from_input
from bridge.flow_definition import InstanceView
from bridge.flow_engine import legacy_motivo
from bridge.instance_manifest import ManifestError

URL = "https://recursos.ejemplo.com/imagenes/bono.png"
MEDIA = TemplateHeaderMedia("image", URL)
NOT_DECLARED = "approved_template_header_media_not_declared"
CONFIRMED_NAME = CONFIRMED["name"]
_HASH = "0e097b04e5a97dfdb15def7c24488af65a45d845401b8c4e7d01a164af694389"
# Lo que salia y tiene que seguir saliendo: el cuerpo entero del POST a Chatwoot, sin imagen.
SIN_IMAGEN_ATT1 = (
    '{"content": "¡Hola Edith García Pérez! Gracias por tu compra de Alimenta tu Tiroides. Ya tienes tu acceso.", '
    '"content_attributes": {"recovery_first_touch_hash": "' + _HASH + '"}, "content_type": "text", '
    '"message_type": "outgoing", "private": false, "template_params": {"category": "UTILITY", "language": "es_MX", '
    '"name": "att1_bienvenida_01", "processed_params": {"body": {"1": "Edith García Pérez", '
    '"2": "Alimenta tu Tiroides"}}}}'
)
SIN_IMAGEN_MARIBEL = (
    '{"content": "Hola, Edith García Pérez. El pago que intentaste por *Alimenta tu Tiroides* no se aprobó.\\n\\n'
    'Te escribo del equipo de El Protocolo. Si fue con tarjeta, pasa seguido con las compras en línea o '
    'internacionales, y muchas veces se resuelve en un minuto desde la app de tu banco. Si prefieres otra tarjeta '
    'u otro medio, el pago está preparado en el botón con tu *10 % de descuento*.\\n\\n¿De qué banco es tu '
    'tarjeta? Te digo qué activar.", "content_attributes": {"recovery_first_touch_hash": "' + _HASH + '"}, '
    '"content_type": "text", "message_type": "outgoing", "private": false, "template_params": {"category": '
    '"MARKETING", "language": "es_MX", "name": "maribel_pago_rechazado_banco_01", "processed_params": {"body": '
    '{"1": "Edith García Pérez", "2": "Alimenta tu Tiroides"}, "buttons": [{"parameter": "D98014973Y?off=u8ikyx78&'
    'checkoutMode=10&src=hermes&sck=hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ2&offDiscount=PROTOCOLO10", "type": "url"}]}}}'
)


def _body(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, sort_keys=True)


def _image_header() -> dict[str, Any]:
    return {"type": "HEADER", "format": "IMAGE",
            "example": {"header_handle": ["https://scontent.whatsapp.net/example"]}}


def _with_header(template: dict[str, Any], header: dict[str, Any] | None) -> None:
    template["components"] = [c for c in template["components"] if c.get("type") != "HEADER"]
    if header is not None:
        template["components"].insert(0, header)


def _chatwoot(header: dict[str, Any] | None) -> _NativeChatwoot:
    chatwoot = _NativeChatwoot()
    if header is not None:
        for template in chatwoot.catalog:
            if template["name"] == WELCOME:
                _with_header(template, header)
    return chatwoot


@pytest.fixture
def declared(monkeypatch: pytest.MonkeyPatch) -> None:
    """La instancia declara la imagen de la bienvenida (lo que llena ``_waba_template_config``)."""
    monkeypatch.setattr(dispatch, "TEMPLATE", replace(TEMPLATE, header_media=((WELCOME, MEDIA),)))


# -- El envio ---------------------------------------------------------------------------------


@pytest.mark.usefixtures("declared")
def test_a_native_step_sends_the_declared_image_in_the_header(tmp_path: Path) -> None:
    authority = _NativeAuthority()
    chatwoot = _chatwoot(_image_header())

    _run(_dispatcher(authority, chatwoot, tmp_path))

    [message] = _templates(chatwoot)
    assert message["template_params"] == {
        "name": WELCOME, "category": "UTILITY", "language": "es_MX",
        "processed_params": {
            "body": {"1": "Edith García Pérez", "2": "Alimenta tu Tiroides"},
            "header": {"media_url": URL, "media_type": "image"},
        },
    }
    assert authority.acceptances


def test_a_native_step_with_the_checkout_button_also_sends_the_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = "maribel_pago_rechazado_banco_01"
    monkeypatch.setattr(dispatch, "TEMPLATE", replace(TEMPLATE, header_media=((name, MEDIA),)))
    step = replace(branches._parsed_step(), template_branches=None)
    authority = branches._BranchAuthority(step=step)
    chatwoot = branches._chatwoot()
    for template in chatwoot.catalog:
        if template["name"] == name:
            _with_header(template, _image_header())

    _run(_dispatcher(authority, chatwoot, tmp_path))

    [message] = _templates(chatwoot)
    processed = message["template_params"]["processed_params"]
    assert processed["header"] == {"media_url": URL, "media_type": "image"}
    assert processed["buttons"] == [{"type": "url", "parameter": f"{branches.BARE}&offDiscount=PROTOCOLO10"}]


@pytest.mark.parametrize("other_declared", [False, True], ids=["nada declarado", "otra plantilla declarada"])
def test_a_template_without_an_image_sends_exactly_what_it_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other_declared: bool
) -> None:
    if other_declared:
        monkeypatch.setattr(dispatch, "TEMPLATE", replace(TEMPLATE, header_media=(("otra_plantilla_01", MEDIA),)))
    authority = _NativeAuthority()
    chatwoot = _chatwoot(None)

    _run(_dispatcher(authority, chatwoot, tmp_path))

    [message] = _templates(chatwoot)
    assert _body(message) == SIN_IMAGEN_ATT1


def test_the_checkout_button_without_an_image_sends_exactly_what_it_sent(tmp_path: Path) -> None:
    step = replace(branches._parsed_step(), template_branches=None)
    authority = branches._BranchAuthority(step=step)
    chatwoot = branches._chatwoot()

    _run(_dispatcher(authority, chatwoot, tmp_path))

    [message] = _templates(chatwoot)
    assert _body(message) == SIN_IMAGEN_MARIBEL


def test_an_image_template_without_a_declared_image_never_goes_out_and_says_why(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    authority = _NativeAuthority()
    chatwoot = _chatwoot(_image_header())

    with caplog.at_level(logging.WARNING, logger="bridge.worker"):
        _run(_dispatcher(authority, chatwoot, tmp_path))

    assert _templates(chatwoot) == []
    assert not authority.acceptances
    closed = authority.finalizations[-1]
    assert closed["outcome"] == "failed_before_request"
    assert closed["reason_code"] == NOT_DECLARED
    # Falta una declaracion: reintentar no la trae.
    assert closed["next_attempt_at"] is None
    assert f"reason={NOT_DECLARED}" in caplog.text
    # Sin arranque de la solicitud: no consume la bolsa del flujo.
    assert "request_started" not in authority.engine_calls


@pytest.mark.parametrize(("header", "detail"), [
    (None, "header_media_missing"),
    ({"type": "HEADER", "format": "VIDEO", "example": {"header_handle": ["https://x.test/v"]}},
     "header_media_mismatch"),
], ids=["el catalogo no tiene encabezado", "el catalogo pide un video"])
@pytest.mark.usefixtures("declared")
def test_a_declared_image_that_does_not_match_the_catalog_never_goes_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, header: dict[str, Any] | None, detail: str
) -> None:
    authority = _NativeAuthority()
    chatwoot = _chatwoot(header)

    with caplog.at_level(logging.WARNING, logger="bridge.worker"):
        _run(_dispatcher(authority, chatwoot, tmp_path))

    assert _templates(chatwoot) == []
    closed = authority.finalizations[-1]
    assert closed["reason_code"] == "approved_template_mismatch"
    # El catalogo puede cambiar: se reintenta.
    assert closed["next_attempt_at"] is not None
    assert f"detail={detail}" in caplog.text


# -- La declaracion -----------------------------------------------------------------------------


def test_the_manifest_declares_the_image_by_template_name() -> None:
    payload = _payload("att1")
    payload["plantillas"]["imagenes"] = {"att1_bono_01": URL}

    manifest = _load(payload)

    assert manifest.template_images == (("att1_bono_01", MEDIA),)
    assert manifest.template_image_names == frozenset({"att1_bono_01"})
    # No es un slot: no se confunde con una plantilla.
    assert "imagenes" not in manifest.templates


def test_a_manifest_without_images_loads_as_before() -> None:
    manifest = _load(_payload("att1"))
    assert manifest.template_images == ()
    assert manifest.template_image_names == frozenset()


@pytest.mark.parametrize("url", [
    "http://recursos.ejemplo.com/a.png",
    "https://Recursos.ejemplo.com/a.png",
    "https://recursos.ejemplo.com:443/a.png",
    "https://recursos.ejemplo.com/a%20b.png",
    "https://recursos.ejemplo.com/a b.png",
    "https://recursos.ejemplo.com/{{1}}.png",
    "https://recursos.ejemplo.com",
    "<URL_DE_LA_IMAGEN>",
    "https://recursos.ejemplo.com/" + "a" * 1000,
    123,
    "https://usuario:clave@recursos.ejemplo.com/a.png",
    "https://192.168.0.10/a.png",
    "https://8.8.8.8/a.png",
], ids=["http", "host mayuscula", "puerto", "porcentaje", "espacio", "variable", "sin ruta", "marcador", "larga",
        "numero", "usuario y clave", "ip privada", "ip publica"])
def test_an_invalid_image_url_refuses_to_load(url: object) -> None:
    payload = _payload("att1")
    payload["plantillas"]["imagenes"] = {"att1_bono_01": url}
    with pytest.raises(ManifestError, match=r"plantillas\.imagenes\.att1_bono_01"):
        _load(payload)


def test_a_host_with_digits_is_still_a_name() -> None:
    url = "https://cdn1.ejemplo2.com/a.png"
    assert TemplateHeaderMedia("image", url).url == url


@pytest.mark.parametrize(("images", "message"), [
    ("https://recursos.ejemplo.com/a.png", "debe ser una tabla"),
    ({"Att1 Bono": URL}, "nombre de Meta"),
], ids=["no es una tabla", "nombre invalido"])
def test_a_badly_formed_images_table_refuses_to_load(images: object, message: str) -> None:
    payload = _payload("att1")
    payload["plantillas"]["imagenes"] = images
    with pytest.raises(ManifestError, match=message):
        _load(payload)


def test_the_instance_config_carries_what_the_manifest_declares() -> None:
    manifest = replace(_att1_manifest(carrito=True, pago_fallido=True), template_images=(("att1_bono_01", MEDIA),))
    template = _waba_template_config(_settings(manifest, **_WABA_OUTBOUND))
    assert template is not None
    assert template.header_media_for("att1_bono_01") == MEDIA


def test_an_instance_without_images_keeps_its_config() -> None:
    template = _waba_template_config(_settings(_att1_manifest(carrito=True, pago_fallido=True), **_WABA_OUTBOUND))
    assert template is not None
    assert template.header_media == ()


# -- La regla 13 --------------------------------------------------------------------------------


def _image_instance() -> InstanceView:
    return InstanceView("lancemos", "att1", frozenset({"att1"}), "America/Mexico_City",
                        template_images=frozenset({CONFIRMED_NAME}))


def _catalog(header: dict[str, Any] | None):
    def change(payload: dict[str, Any]) -> None:
        template = copy.deepcopy(CONFIRMED)
        _with_header(template, header)
        payload["message_templates"].append(template)
    return catalog_with(change)


def _header_findings(findings: list[Any]) -> list[Any]:
    return [finding for finding in findings if finding.code == "template_variable_count_mismatch"]


def test_a_native_step_with_a_declared_image_passes_rule_13() -> None:
    assert _header_findings(run_new(welcome(), catalog=_catalog(_image_header()), instance=_image_instance())) == []


def test_a_native_step_with_an_undeclared_image_is_blocked_with_its_own_reason() -> None:
    [finding] = _header_findings(run_new(welcome(), catalog=_catalog(_image_header()), instance=INSTANCE))
    assert "imagen" in finding.reason and "declarada" in finding.reason
    assert "piden parámetros" not in finding.reason
    assert "equipo técnico" in finding.alternative


def test_a_declared_image_on_a_template_without_one_is_blocked() -> None:
    [finding] = _header_findings(run_new(welcome(), catalog=_catalog(None), instance=_image_instance()))
    assert "no tiene encabezado de imagen" in finding.reason


def test_a_template_without_image_and_without_declaration_is_untouched() -> None:
    assert _header_findings(run_new(welcome(), catalog=_catalog(None), instance=INSTANCE)) == []


def test_the_silence_sweep_does_not_send_an_image_so_it_stays_blocked() -> None:
    definition = _follow_up()
    assert definition["pasos"] == [CONFIRMED_STEP]
    [finding] = _header_findings(run_new(definition, catalog=_catalog(_image_header()), instance=_image_instance()))
    assert "piden parámetros" in finding.reason


def test_a_video_header_stays_blocked_even_with_an_image_declared() -> None:
    video = {"type": "HEADER", "format": "VIDEO"}
    findings = _header_findings(run_new(welcome(), catalog=_catalog(video), instance=_image_instance()))
    assert [f.reason for f in findings][0].startswith("El encabezado o el pie")


def test_a_legacy_flow_with_an_image_stays_blocked_as_before() -> None:
    with_header = catalog_with(lambda p: next(t for t in p["message_templates"]
                                              if t["name"] == "att1_carrito_abandonado_01")["components"].insert(
        0, _image_header()))
    instance = InstanceView("lancemos", "att1", frozenset({"att1"}), "America/Mexico_City",
                            template_images=frozenset({"att1_carrito_abandonado_01"}))
    [finding] = check([version_item(flow_def("carrito"))], catalog=with_header, instance=instance).blocks
    assert finding.code == "template_variable_count_mismatch"
    assert "piden parámetros" in finding.reason


# -- Los tres caminos que arman la vista de la instancia ------------------------------------------


def test_the_change_api_view_carries_the_declared_names() -> None:
    context = _Context(instance={"tenant_ref": "lancemos", "ally_ref": "att1"}, time_zone="America/Mexico_City",
                       inbox_id=11, git_sha="x", template_images=frozenset({CONFIRMED_NAME}))
    assert _instance_view(context, {"products": ["att1"]}).template_images == frozenset({CONFIRMED_NAME})


def test_the_operator_input_carries_them_and_they_are_optional() -> None:
    base = {"tenant_ref": "lancemos", "ally_ref": "att1", "products": ["att1"], "time_zone": "America/Mexico_City"}
    assert instance_from_input(base).template_images == frozenset()
    assert instance_from_input({**base, "template_images": [CONFIRMED_NAME]}).template_images == frozenset(
        {CONFIRMED_NAME})
    with pytest.raises(OperatorInputError):
        instance_from_input({**base, "template_images": "no-es-lista"})


def _operator_output(blocks: list[dict[str, Any]]) -> str:
    out = io.StringIO()
    operator_script.show_validation(operator_script.Ops(config=None, out=out), {  # type: ignore[arg-type]
        "result": {"blocks": blocks, "warnings": []}, "preview": {"huecos": []}})
    return out.getvalue()


def test_the_operator_command_says_it_does_not_know_the_declared_images() -> None:
    [finding] = _header_findings(run_new(welcome(), catalog=_catalog(_image_header()), instance=INSTANCE))
    assert operator_script.IMAGE_BLOCK_NOTE in _operator_output([finding.to_json()])


def test_the_operator_command_says_nothing_about_images_on_other_blocks() -> None:
    assert operator_script.IMAGE_BLOCK_NOTE not in _operator_output([
        {"code": "template_variable_count_mismatch", "path": "/0", "reason": "Otro motivo."}])


def test_proposing_with_the_operator_command_also_says_it(tmp_path: Path) -> None:
    [finding] = _header_findings(run_new(welcome(), catalog=_catalog(_image_header()), instance=INSTANCE))

    class _Ops(operator_script.Ops):
        def check_approver(self, who: str) -> None:
            pass

        def read_world(self) -> dict[str, Any]:
            return {}

        def read_catalog(self) -> dict[str, Any]:
            return {}

        def module(self, subcommand: str, payload: Any, *, name: str = "") -> tuple[int, dict[str, Any]]:
            return 3, {"result": {"blocks": [finding.to_json()]}}

    items = tmp_path / "items.json"
    items.write_text("[]", encoding="utf-8")
    out = io.StringIO()
    with pytest.raises(operator_script.Refused):
        operator_script.cmd_proponer(_Ops(config=None, out=out),  # type: ignore[arg-type]
                                     SimpleNamespace(quien="dan", archivo=str(items), motivo="x"))
    assert operator_script.IMAGE_BLOCK_NOTE in out.getvalue()


def test_the_closing_reason_has_its_motive_for_the_contract() -> None:
    assert legacy_motivo(NOT_DECLARED) == "meta:<regla>"


# -- De punta a punta por el bridge: lo que el Automation Expert valida y lo que Dan publica -------


def _image_manifest(name: str = WELCOME) -> Any:
    return replace(_att1_manifest(), template_images=((name, MEDIA),))


@pytest.fixture
def image_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """El catalogo capturado de la bienvenida real, con un encabezado de imagen en vez del de texto."""
    monkeypatch.setenv("SETTER_VERSION", "1.19.0")
    monkeypatch.setenv("GIT_SHA", "e56871777d9b7d462948f638e7815a5c3d5879d1")
    real = copy.deepcopy(list(REAL.values()))
    for template in real:
        if template["name"] == WELCOME:
            _with_header(template, _image_header())
    others = [t for t in app_tests.CATALOG["message_templates"] if t.get("name") not in REAL]
    monkeypatch.setitem(app_tests.CATALOG, "message_templates", [*others, *real])


def _validate_welcome(**overrides: object) -> dict[str, Any]:
    emulator = PostgrestEmulator()
    emulator.new_flow_counts = dict(COUNTS)
    client, _ = app_tests._client(emulator, **overrides)
    saved = client.post(f"{PREFIX}/flow-changes", headers=AUTH, json={
        "items": [_item([{"plantilla": WELCOME, "variables": ["producto"]}, {"etiquetar": "cliente"}])],
        "base": {}, "purpose": "la bienvenida con imagen", "requested_by": SLACK})
    assert saved.status_code == 201, saved.json()
    data = saved.json()["data"]
    validated = client.post(f"{PREFIX}/flow-changes/{data['proposal_id']}/validate", headers=AUTH,
                            json={"revision": data["revision"], "requested_by": SLACK})
    assert validated.status_code == 200, validated.json()
    return validated.json()["data"]


@pytest.mark.usefixtures("image_catalog")
def test_the_agent_validates_a_flow_with_a_declared_image() -> None:
    result = _validate_welcome(instance_manifest=_image_manifest())
    assert result["blocks"] == []
    assert result["status"] == "validada"


@pytest.mark.usefixtures("image_catalog")
def test_the_agent_is_told_the_image_is_not_declared() -> None:
    result = _validate_welcome()
    assert _blocks(result) == [("template_variable_count_mismatch", "/0/definicion/pasos/0/plantilla")]
    assert "declarada" in result["blocks"][0]["reason"]


def test_the_approval_page_revalidates_with_the_declared_names(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = flow_approval.validate_proposal

    def spy(payload: Any, *, environ: object) -> dict:
        seen.append(payload["instance"])
        return real(payload, environ=environ)  # type: ignore[arg-type]

    monkeypatch.setattr(flow_approval, "validate_proposal", spy)
    client, _ = _signed(instance_manifest=_image_manifest("att1_bono_01"))
    _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))

    assert seen and all(instance["template_images"] == ["att1_bono_01"] for instance in seen)
