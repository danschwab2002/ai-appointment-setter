"""El encabezado de imagen o de video de una plantilla aprobada (E-160).

El catalogo es la captura del inbox 11 de ATT1 del 01/10
(``chatwoot_inbox_11_message_templates_20261001.json``, leida por psql de
``channel_whatsapp.message_templates``). Ningun catalogo capturado tiene una
plantilla con encabezado de medio: el componente se agrega a una copia con la
forma que documenta Meta para el catalogo («Template components»: ``type``
``HEADER``, ``format`` ``IMAGE`` o ``VIDEO`` y ``example.header_handle``), que
es lo unico que el parser lee de el.

Lo que sale hacia Chatwoot es lo que lee Chatwoot 4.13 en
``Whatsapp::TemplateProcessorService#build_header_params`` (tag v4.13.0):
``processed_params.header = {media_url, media_type}``, que
``PopulateTemplateParametersService#build_media_parameter`` convierte en
``{type: "image", image: {link: url}}``. Antes corta cada parametro en 1000
caracteres y le saca ``<``, ``>``, ``"`` y ``'``: por eso esos son los limites
de la URL.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from bridge.approved_templates import (
    ApprovedTemplateError,
    TemplateHeaderMedia,
    parse_approved_template,
)
from bridge.messaging import WhatsAppTemplateConfig

FIXTURES = Path(__file__).parent / "fixtures"
CATALOG: list[dict[str, Any]] = json.loads(
    (FIXTURES / "chatwoot_inbox_11_message_templates_20261001.json").read_text(
        encoding="utf-8"
    )
)["templates"]
CART = "att1_carrito_abandonado_01"
FAILURE = "att1_compra_fallida_01"
URL = "https://bridge.example.test/medios/bono.png"


def _inbox(media_format: str | None, name: str = CART) -> dict[str, Any]:
    catalog = copy.deepcopy(CATALOG)
    if media_format is not None:
        for template in catalog:
            if template["name"] == name:
                template["components"].insert(0, {
                    "type": "HEADER",
                    "format": media_format,
                    "example": {"header_handle": ["https://scontent.whatsapp.net/example"]},
                })
    return {"message_templates": catalog}


def _parse(inbox: dict[str, Any], media: TemplateHeaderMedia | None, name: str = CART):
    return parse_approved_template(
        inbox,
        template_name=name,
        expected_language="es_MX",
        expected_category="MARKETING",
        parameter_count=2,
        header_media=media,
    )


VALUES = {"1": "Edith", "2": "Alimenta tu Tiroides"}


# ------------------------------------------------------------------ the parser


@pytest.mark.parametrize(("media_type", "media_format"), [("image", "IMAGE"), ("video", "VIDEO")])
def test_a_media_header_with_its_media_carries_the_header(
    media_type: str, media_format: str
) -> None:
    media = TemplateHeaderMedia(media_type, URL)
    approved = _parse(_inbox(media_format), media)
    plain = _parse(_inbox(None), None)

    assert approved.header_media == media
    # El cuerpo es el mismo: el encabezado no cambia el texto que se hashea.
    assert approved.render(VALUES) == plain.render(VALUES)
    assert approved.processed_params(VALUES) == {
        "body": VALUES,
        "header": {"media_url": URL, "media_type": media_type},
    }


def test_without_a_declared_media_nothing_changes() -> None:
    approved = _parse(_inbox(None), None)

    assert approved.header_media is None
    assert approved.processed_params(VALUES) == {"body": VALUES}


@pytest.mark.parametrize(
    ("media_format", "media", "detail"),
    [
        pytest.param("IMAGE", None, "component_requires_parameters", id="image header, no media"),
        pytest.param("VIDEO", None, "component_requires_parameters", id="video header, no media"),
        pytest.param(
            "DOCUMENT", TemplateHeaderMedia("image", URL), "component_requires_parameters",
            id="document header",
        ),
        pytest.param(
            "IMAGE", TemplateHeaderMedia("video", URL), "header_media_mismatch",
            id="video for an image header",
        ),
        pytest.param(
            "VIDEO", TemplateHeaderMedia("image", URL), "header_media_mismatch",
            id="image for a video header",
        ),
        pytest.param(
            None, TemplateHeaderMedia("image", URL), "header_media_missing",
            id="media for a template without media header",
        ),
    ],
)
def test_a_media_that_does_not_close_with_the_header_fails_closed(
    media_format: str | None, media: TemplateHeaderMedia | None, detail: str
) -> None:
    with pytest.raises(ApprovedTemplateError) as raised:
        _parse(_inbox(media_format), media)

    assert raised.value.reason == "approved_template_mismatch"
    assert raised.value.detail == detail


def test_two_media_headers_fail_closed() -> None:
    inbox = _inbox("IMAGE")
    for template in inbox["message_templates"]:
        if template["name"] == CART:
            template["components"].insert(0, {"type": "HEADER", "format": "IMAGE"})

    with pytest.raises(ApprovedTemplateError) as raised:
        _parse(inbox, TemplateHeaderMedia("image", URL))

    assert raised.value.detail == "header_media_mismatch"


def test_the_parser_refuses_a_media_that_is_not_a_header_media() -> None:
    with pytest.raises(ValueError, match="approved_template_header_media_invalid"):
        _parse(_inbox("IMAGE"), {"media_url": URL, "media_type": "image"})  # type: ignore[arg-type]


# ------------------------------------------------------------- the declaration


@pytest.mark.parametrize(
    ("media_type", "url", "error"),
    [
        pytest.param("document", URL, "template_header_media_type_invalid", id="document"),
        pytest.param("IMAGE", URL, "template_header_media_type_invalid", id="upper case"),
        pytest.param("image", "http://bridge.example.test/a.png", "template_header_media_url_invalid", id="http"),
        pytest.param("image", "https://bridge.example.test/a b.png", "template_header_media_url_invalid", id="space"),
        pytest.param("image", "https://bridge.example.test/a\".png", "template_header_media_url_invalid", id="quote"),
        pytest.param("image", "https://bridge.example.test/a'.png", "template_header_media_url_invalid", id="apostrophe"),
        pytest.param("image", "https://bridge.example.test/{{1}}.png", "template_header_media_url_invalid", id="variable"),
        pytest.param("image", "https://bridge.example.test/año.png", "template_header_media_url_invalid", id="not ascii"),
        pytest.param("image", "https://bridge.example.test/a|b.png", "template_header_media_url_invalid", id="pipe"),
        pytest.param("image", "https://bridge.example.test/a\\b.png", "template_header_media_url_invalid", id="backslash"),
        pytest.param("image", "https://bridge.example.test/a[1].png", "template_header_media_url_invalid", id="brackets"),
        pytest.param("image", "https://bridge.example.test/a^b`c.png", "template_header_media_url_invalid", id="caret and backtick"),
        # E-196: lo que Addressable#normalize (2.8.7, el de Chatwoot 4.13) reescribe.
        pytest.param("image", "https://Bridge.example.test/a.png", "template_header_media_url_invalid", id="host upper case"),
        pytest.param("image", "https://bridge.example.test:443/a.png", "template_header_media_url_invalid", id="default port"),
        pytest.param("image", "https://bridge.example.test", "template_header_media_url_invalid", id="empty path"),
        pytest.param("image", "https://bridge.example.test/a/../b.png", "template_header_media_url_invalid", id="dot segment"),
        pytest.param("image", "https://bridge.example.test/./b.png", "template_header_media_url_invalid", id="single dot segment"),
        pytest.param("image", "https://bridge.example.test/a%7e.png", "template_header_media_url_invalid", id="encoded tilde"),
        # E-196: fuera de la forma estrecha aunque Addressable no lo cambie.
        pytest.param("image", "https://bridge.example.test/a%2F.png", "template_header_media_url_invalid", id="any percent"),
        pytest.param("image", "https://user@bridge.example.test/a.png", "template_header_media_url_invalid", id="user"),
        pytest.param("image", "https://bridge.example.test:8443/a.png", "template_header_media_url_invalid", id="port"),
        pytest.param("image", "https://bridge.example.test/a.png#x", "template_header_media_url_invalid", id="fragment"),
        pytest.param("image", "https://bridge.example.test/a/", "template_header_media_url_invalid", id="trailing slash"),
        pytest.param("image", "https://bridge.example.test/a.png?v", "template_header_media_url_invalid", id="query without value"),
        pytest.param("image", "https://bridge.example.test/a.png?v=1;x=2", "template_header_media_url_invalid", id="query semicolon"),
        pytest.param(
            "image", "https://bridge.example.test/" + "a" * 973, "template_header_media_url_invalid",
            id="1001 characters",
        ),
    ],
)
def test_a_media_chatwoot_would_change_is_refused(media_type: str, url: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        TemplateHeaderMedia(media_type, url)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://bridge-maribel.example.test/medios/hotmart-bono.png", id="media library"),
        pytest.param("https://bridge.example.test/medios/a_b~c.v2.png?v=1&sha=ab12", id="query"),
        pytest.param("https://bridge.example.test/..x/.well-known/a.png", id="dots inside a segment"),
    ],
)
def test_a_url_chatwoot_leaves_as_it_is_is_accepted(url: str) -> None:
    # E-196: cada una sale igual de Addressable::URI.parse(url).normalize (2.8.7).
    assert TemplateHeaderMedia("image", url).url == url


def test_a_url_of_1000_characters_is_accepted() -> None:
    url = "https://bridge.example.test/" + "a" * 972
    assert len(url) == 1000
    assert TemplateHeaderMedia("video", url).processed_header() == {
        "media_url": url,
        "media_type": "video",
    }


# ------------------------------------------------------------------ the config


def _config(**overrides: Any) -> WhatsAppTemplateConfig:
    values: dict[str, Any] = {
        "first_touch_name": CART,
        "payment_failure_name": FAILURE,
        "followup_name": None,
        "language": "es_MX",
        "category": "MARKETING",
        "first_touch_parameter": "buyer_name_and_product",
        "first_touch_body_parameters": ("nombre", "producto"),
        "payment_failure_body_parameters": ("nombre", "producto"),
    }
    values.update(overrides)
    return WhatsAppTemplateConfig(**values)


def test_the_config_puts_the_media_only_in_its_template() -> None:
    media = TemplateHeaderMedia("image", URL)
    config = _config(header_media=((CART, media),))

    cart = config.params(
        content="x", followup=False, buyer_name="Edith", product_name="Tiroides",
        trigger_kind="cart_abandonment",
    )
    failure = config.params(
        content="x", followup=False, buyer_name="Edith", product_name="Tiroides",
        trigger_kind="payment_failure",
    )

    assert config.header_media_for(CART) == media
    assert config.header_media_for(FAILURE) is None
    assert config.header_media_for(None) is None
    assert cart["processed_params"] == {
        "body": {"1": "Edith", "2": "Tiroides"},
        "header": {"media_url": URL, "media_type": "image"},
    }
    assert failure["processed_params"] == {"body": {"1": "Edith", "2": "Tiroides"}}


def test_without_media_the_config_sends_what_it_sent_before() -> None:
    config = _config()

    assert config.header_media == ()
    assert config.params(
        content="x", followup=False, buyer_name="Edith", product_name="Tiroides",
        trigger_kind="cart_abandonment",
    ) == {
        "name": CART,
        "category": "MARKETING",
        "language": "es_MX",
        "processed_params": {"body": {"1": "Edith", "2": "Tiroides"}},
    }


@pytest.mark.parametrize(
    ("header_media", "error"),
    [
        pytest.param(
            ((CART, TemplateHeaderMedia("image", URL)), (CART, TemplateHeaderMedia("video", URL))),
            "duplicate_template_header_media",
            id="same template twice",
        ),
        pytest.param(((" " + CART, TemplateHeaderMedia("image", URL)),), "invalid_template_header_media", id="name with spaces"),
        pytest.param((("", TemplateHeaderMedia("image", URL)),), "invalid_template_header_media", id="empty name"),
        pytest.param(((CART, {"media_url": URL}),), "invalid_template_header_media", id="not a media"),
        pytest.param([(CART, TemplateHeaderMedia("image", URL))], "invalid_template_header_media", id="a list"),
    ],
)
def test_the_config_refuses_a_bad_declaration(header_media: Any, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        _config(header_media=header_media)
