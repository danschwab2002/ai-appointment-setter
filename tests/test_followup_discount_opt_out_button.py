"""E-272: el seguimiento con cupon con la respuesta rapida «No más mensajes».

Decision de Dan del 2026-11-02: toda plantilla de marketing del recuperador lleva
la baja. El seguimiento con cupon mandaba solo plantillas de un boton (el
checkout), asi que las ``_02`` llevan el checkout primero y «No más mensajes»
despues.

No hay todavia una captura de una ``_seguimiento_descuento_02`` (Meta no la
aprobo). La forma sale de dos capturas del catalogo de la bandeja 11 de ATT1 por
la API de Chatwoot, sin inventar nada:

* ``chatwoot_inbox_11_message_templates_20261010.json``: la
  ``att1_seguimiento_descuento_01`` aprobada (cuerpo con ``{{1}}`` y ``{{2}}``,
  un solo boton URL al checkout);
* ``chatwoot_message_templates_inbox_11_carrito_20261018.json``: la
  ``att1_carrito_descuento_01`` y la ``att1_ultima_renovacion_01`` aprobadas
  (MARKETING), con el mismo boton URL al checkout primero y la respuesta rapida
  «No más mensajes» despues. Es el precedente del orden y de la forma del
  boton de respuesta rapida que devuelve Chatwoot (``{"text", "type"}``).

La ``_02`` de la prueba es la ``_01`` capturada con ese segundo boton copiado tal
cual de la captura del A1. La baja de punta a punta usa los webhooks reales de la
conversacion 536 de ATT1 (``chatwoot_message_created_buttons_inbox_11_conv_536_20261028.json``,
una respuesta rapida tocada en produccion), con el texto del boton leido de la
plantilla.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest
from test_cart_renewal_batch_opt_out import FORM_ID, OPT_OUT_ID, _batch
from test_cart_followups_wiring import _Renewal
from test_followup_discount import DOCUMENTED, _FakeChatwoot, _FakeSupabase, _sweeper

from bridge import flow_validator, template_request
from bridge.approved_templates import parse_approved_template
from bridge.chatwoot import ChatwootProtocolError
from bridge.followup_discount import (
    FOLLOWUP_OPT_OUT_BUTTON,
    followup_button_suffix,
    parse_followup_template,
)
from bridge.opt_out import detect_explicit_opt_out

FIXTURES = Path(__file__).parent / "fixtures"
ATT1_CATALOG = json.loads(
    (FIXTURES / "chatwoot_inbox_11_message_templates_20261010.json").read_text(encoding="utf-8")
)
CART_CATALOG = json.loads(
    (FIXTURES / "chatwoot_message_templates_inbox_11_carrito_20261018.json").read_text(encoding="utf-8")
)
OLD = "att1_seguimiento_descuento_01"
NEW = "att1_seguimiento_descuento_02"
LINK = (
    "https://pay.hotmart.com/D98014973Y?off=2uafw5bg&checkoutMode=10&src=hermes"
    "&sck=hermes~v1~01K5ABCDEFX2VYB4M6X9CDPTF1"
)


def _template(catalog: dict[str, Any], name: str) -> dict[str, Any]:
    [template] = [t for t in catalog["message_templates"] if t["name"] == name]
    return template


def _buttons(template: dict[str, Any]) -> list[dict[str, Any]]:
    return next(c for c in template["components"] if c["type"] == "BUTTONS")["buttons"]


def _captured_opt_out_button() -> dict[str, Any]:
    # El segundo boton del A1 aprobado, tal como lo devuelve Chatwoot.
    first, second = _buttons(_template(CART_CATALOG, "att1_carrito_descuento_01"))
    assert first["type"] == "URL" and first["url"] == "https://pay.hotmart.com/{{1}}"
    assert second == {"text": "No más mensajes", "type": "QUICK_REPLY"}
    return copy.deepcopy(second)


def _catalog_with_02(*, buttons: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """El catalogo de ATT1 con la _02: la _01 capturada mas la baja del A1."""
    payload = copy.deepcopy(ATT1_CATALOG)
    new = copy.deepcopy(_template(payload, OLD))
    new["name"] = NEW
    group = next(c for c in new["components"] if c["type"] == "BUTTONS")
    group["buttons"] = (
        buttons if buttons is not None else [*group["buttons"], _captured_opt_out_button()]
    )
    payload["message_templates"].append(new)
    return payload


def _placeholders(template: dict[str, Any]) -> int:
    body = next(c for c in template["components"] if c["type"] == "BODY")["text"]
    return len(set(re.findall(r"\{\{(\d+)\}\}", body)))


def _checkout() -> dict[str, Any]:
    return copy.deepcopy(_buttons(_template(ATT1_CATALOG, OLD))[0])


# --- el parser ------------------------------------------------------------------


def test_the_02_with_checkout_then_opt_out_parses_and_sends_the_url_at_index_0() -> None:
    template = parse_followup_template(_catalog_with_02(), template_name=NEW, expected_language="es_MX")
    assert template.has_opt_out_button is True
    assert (template.category, template.carries_coupon) == ("MARKETING", False)

    suffix = followup_button_suffix(LINK, "TIROIDES10")
    params = template.params(
        first_name="María", product_name="Alimenta tu Tiroides", coupon_code="TIROIDES10", button_suffix=suffix
    )
    # La respuesta rapida no lleva parametro: la lista tiene solo el boton de URL, en el indice 0.
    assert params["processed_params"]["buttons"] == [{"type": "url", "parameter": suffix}]
    assert params["processed_params"]["body"] == {"1": "María", "2": "Alimenta tu Tiroides"}


def test_the_02_sends_the_buttons_exactly_like_the_cart_a1_with_the_same_two_buttons() -> None:
    # El precedente: el A1 del carrito (checkout y despues «No más mensajes») sale por
    # ApprovedTemplate.processed_params con el boton de URL en el indice 0 y nada para la respuesta rapida.
    suffix = followup_button_suffix(LINK, "TIROIDES10")
    a1 = parse_approved_template(
        CART_CATALOG,
        template_name="att1_carrito_descuento_01",
        expected_language="es_MX",
        expected_category="MARKETING",
        parameter_count=_placeholders(_template(CART_CATALOG, "att1_carrito_descuento_01")),
        checkout_button=True,
    )
    a1_buttons = a1.processed_params(
        {str(n): "x" for n in range(1, a1.parameter_count + 1)}, button_suffix=suffix
    )["buttons"]
    followup = parse_followup_template(_catalog_with_02(), template_name=NEW, expected_language="es_MX")
    followup_buttons = followup.params(
        first_name="María", product_name="Alimenta tu Tiroides", coupon_code="TIROIDES10", button_suffix=suffix
    )["processed_params"]["buttons"]
    assert followup_buttons == a1_buttons == [{"type": "url", "parameter": suffix}]


def test_the_01_with_only_the_checkout_still_parses_without_the_opt_out() -> None:
    template = parse_followup_template(ATT1_CATALOG, template_name=OLD, expected_language="es_MX")
    assert template.has_opt_out_button is False


@pytest.mark.parametrize(
    ("buttons", "error"),
    [
        # La baja primero: el boton de URL quedaria en el indice 1 y el bridge lo manda en el 0.
        (lambda: [_captured_opt_out_button(), _checkout()], "followup_template_checkout_button_not_first"),
        # Otra respuesta rapida en lugar de la baja.
        (lambda: [_checkout(), {"text": "Necesito ayuda", "type": "QUICK_REPLY"}],
         "followup_template_unexpected_button"),
        # La baja sin el acento: el texto es exacto (decision de Dan del 2026-11-02).
        (lambda: [_checkout(), {"text": "No mas mensajes", "type": "QUICK_REPLY"}],
         "followup_template_unexpected_button"),
        # Un tercer boton.
        (lambda: [_checkout(), _captured_opt_out_button(), {"text": "Necesito ayuda", "type": "QUICK_REPLY"}],
         "followup_template_unexpected_button"),
        # Dos checkouts.
        (lambda: [_checkout(), _checkout()], "followup_template_unexpected_button"),
        # Solo la baja, sin checkout.
        (lambda: [_captured_opt_out_button()], "followup_template_button_not_dynamic_checkout"),
        (lambda: [], "followup_template_button_missing"),
    ],
    ids=["opt_out_first", "other_reply", "no_accent", "three", "two_checkouts", "only_opt_out", "none"],
)
def test_any_other_pair_of_buttons_fails_closed(buttons, error: str) -> None:
    with pytest.raises(ChatwootProtocolError, match=error):
        parse_followup_template(_catalog_with_02(buttons=buttons()), template_name=NEW, expected_language="es_MX")


def test_the_three_copies_of_the_opt_out_text_are_the_same() -> None:
    assert FOLLOWUP_OPT_OUT_BUTTON == template_request.OPT_OUT_BUTTON == flow_validator.FOLLOWUP_OPT_OUT_BUTTON
    assert FOLLOWUP_OPT_OUT_BUTTON == _captured_opt_out_button()["text"]


# --- el barrido -------------------------------------------------------------------


def _with_opt_out(payload: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(payload)
    for template in payload["message_templates"]:
        _buttons(template).append(_captured_opt_out_button())
    return payload


def test_the_sweeper_sends_the_02_with_the_same_params_and_no_warning(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="bridge.followup_discount")
    chatwoot = _FakeChatwoot(["194"], inbox_payload=_with_opt_out(DOCUMENTED))
    supabase = _FakeSupabase()
    assert asyncio.run(_sweeper(chatwoot, supabase, hours_after=25).run_once()) == 1
    [sent] = chatwoot.sent
    assert sent["template_params"]["processed_params"]["buttons"] == [
        {"type": "url", "parameter": followup_button_suffix(
            "https://pay.hotmart.com/F106691755G?off=mgbgpp19&checkoutMode=10&src=hermes"
            "&sck=hermes%7Cv1%7C01K5ABCDEFX2VYB4M6X9CDPTF1", "JOHANNA10")}
    ]
    assert "template_without_opt_out_button" not in caplog.text


def test_the_sweeper_still_sends_the_01_and_warns_once_per_process(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="bridge.followup_discount")
    chatwoot = _FakeChatwoot(["194"])
    supabase = _FakeSupabase()
    sweeper = _sweeper(chatwoot, supabase, hours_after=25)
    assert asyncio.run(sweeper.run_once()) == 1
    asyncio.run(sweeper.run_once())
    warnings = [r.getMessage() for r in caplog.records if "template_without_opt_out_button" in r.getMessage()]
    assert warnings == ["conversation_followup_template_without_opt_out_button template=johanna_seguimiento_descuento_01"]


# --- la baja de punta a punta -----------------------------------------------------


def test_pressing_the_opt_out_of_the_02_records_the_opt_out(tmp_path: Path) -> None:
    # El texto del boton sale de la plantilla que el barrido acepto: es lo que WhatsApp manda como
    # mensaje del lead al tocarla (content de la respuesta rapida en la captura de la 536).
    template = _template(_catalog_with_02(), NEW)
    parse_followup_template(_catalog_with_02(), template_name=NEW, expected_language="es_MX")
    pressed = _buttons(template)[1]["text"]
    assert detect_explicit_opt_out([pressed]).rule_key == "stop_receiving_messages"

    supabase = _Renewal(identities=(FORM_ID,))
    chatwoot, shadow, _ = _batch(tmp_path, supabase, {OPT_OUT_ID: pressed})

    [opt_out] = supabase.opt_outs
    assert opt_out["external_user_id"] == FORM_ID
    assert opt_out["chatwoot_message_id"] == OPT_OUT_ID
    assert opt_out["rule_key"] == "stop_receiving_messages"
    # Nadie le contesta despues de la baja.
    assert shadow.calls == [] and chatwoot.reply_calls == []


# --- el validador de F-146 ---------------------------------------------------------


def _followup_request(*buttons: dict[str, str]) -> dict[str, Any]:
    return {
        "nombre": "att1_seguimiento_descuento_02",
        "idioma": "es_MX",
        "categoria": "MARKETING",
        "cuerpo": "Hola, {{nombre}}. Te escribo del equipo. Sobre {{producto}}, te reservamos un 10 % de descuento.",
        "botones": list(buttons),
    }


def _att1_context() -> template_request.TemplateContext:
    from bridge.instance_manifest import InstanceManifest

    manifest = InstanceManifest.from_toml_file(FIXTURES / "instances" / "att1" / "instancia.toml")
    return template_request.context_from_manifest(manifest)


def test_the_followup_request_with_checkout_and_opt_out_passes_and_without_it_is_blocked() -> None:
    context = _att1_context()
    checkout = {"tipo": "checkout", "texto": "Usar mi descuento"}
    opt_out = {"tipo": "respuesta", "texto": "No más mensajes"}

    ok = template_request.check(template_request.parse_template(_followup_request(checkout, opt_out)), context)
    assert ok.blocks == [], [f.to_json() for f in ok.blocks]
    request = template_request.meta_request(
        template_request.parse_template(_followup_request(checkout, opt_out)), context
    )
    [group] = [c for c in request["components"] if c["type"] == "BUTTONS"]
    assert [b["type"] for b in group["buttons"]] == ["URL", "QUICK_REPLY"]
    assert group["buttons"][0]["url"] == "https://pay.hotmart.com/{{1}}"
    assert group["buttons"][1]["text"] == FOLLOWUP_OPT_OUT_BUTTON

    without = template_request.check(template_request.parse_template(_followup_request(checkout)), context)
    assert "template_marketing_without_opt_out" in {f.code for f in without.blocks}

    # El paso del seguimiento nativo la espera con el checkout primero.
    result = template_request.TemplateResult()
    template_request.check_for_step(
        template_request.parse_template(_followup_request(checkout, opt_out)),
        {"entra": {"permiso": "conversacion", "contesto_a": "cualquier_plantilla"}},
        {"plantilla": {"a_crear": "att1_seguimiento_descuento_02"}, "idioma": "es_MX",
         "variables": ["nombre", "producto"], "boton": {"link": "checkout", "cupon": "TIROIDES10"}},
        result,
    )
    assert result.blocks == []
