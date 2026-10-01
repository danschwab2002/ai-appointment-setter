"""El link de pago en el boton de una plantilla.

Los casos parten de dos capturas de produccion del 2026-10-01: el catalogo de
plantillas del inbox 9 tal como lo devuelve la API de Chatwoot
(``chatwoot_message_templates_inbox_9_20261001.json``) y el link que el agente
mando en la conversacion 201 (mensaje 2507 de
``chatwoot_followup_resolved_conversations_inbox_9_20261001.json``).

La plantilla del link, ``johanna_enlace_pago_01``, todavia no esta aprobada por
Meta. Hasta capturarla, su entrada se arma sobre la del seguimiento capturada:
se cambian el nombre, el idioma, la categoria, el texto y el boton, y se
conservan las demas claves tal como las devuelve Chatwoot. PENDIENTE: cambiarla
por la captura real cuando Meta la apruebe.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from bridge.chatwoot import ChatwootProtocolError
from bridge.payment_link_template import (
    PAYMENT_LINK_BUTTON_URL,
    parse_payment_link_template,
    payment_link_button_suffix,
)


FIXTURES = Path(__file__).parent / "fixtures"
CATALOG_FIXTURE = FIXTURES / "chatwoot_message_templates_inbox_9_20261001.json"
CONVERSATION_FIXTURE = (
    FIXTURES / "chatwoot_followup_resolved_conversations_inbox_9_20261001.json"
)
LINK_TEMPLATE = "johanna_enlace_pago_01"
LINK_BODY = (
    "Aquí tienes tu enlace de pago. Toca el botón de abajo para ir directo al "
    "pago seguro en Hotmart. Si tienes alguna duda antes de pagar, escríbeme "
    "por aquí."
)


def captured_catalog() -> dict[str, object]:
    return json.loads(CATALOG_FIXTURE.read_text(encoding="utf-8"))


def catalog_with_link_template(**changes: object) -> dict[str, object]:
    """El catalogo capturado mas la plantilla del link, armada sobre el seguimiento."""
    catalog = captured_catalog()
    templates = catalog["message_templates"]
    assert isinstance(templates, list)
    followup = next(
        template
        for template in templates
        if template["name"] == "johanna_seguimiento_descuento_01"
    )
    link = copy.deepcopy(followup)
    link.update(
        {
            "name": LINK_TEMPLATE,
            "language": "es_EC",
            "category": "UTILITY",
            "components": [
                {"type": "BODY", "text": LINK_BODY},
                {
                    "type": "BUTTONS",
                    "buttons": [
                        {
                            "type": "URL",
                            "text": "Ir al pago",
                            "url": PAYMENT_LINK_BUTTON_URL,
                            "example": [
                                "https://pay.hotmart.com/F106691755G"
                                "?off=mgbgpp19&checkoutMode=10"
                            ],
                        }
                    ],
                },
            ],
        }
    )
    link.update(changes)
    templates.append(link)
    return catalog


def captured_agent_link() -> str:
    """El link que mando el agente en la conversacion 201 (anonimizado)."""
    data = json.loads(CONVERSATION_FIXTURE.read_text(encoding="utf-8"))
    for message in data["conversation"]["messages"]:
        found = re.findall(r"https://pay\.hotmart\.com/\S+", message.get("content") or "")
        if found and message.get("message_type") == 1:
            return found[0]
    raise AssertionError("the captured conversation has no agent link")


def test_reads_the_link_template_from_the_captured_catalog() -> None:
    template = parse_payment_link_template(
        catalog_with_link_template(),
        template_name=LINK_TEMPLATE,
        expected_language="es_EC",
    )

    assert template.name == LINK_TEMPLATE
    assert template.language == "es_EC"
    assert template.category == "UTILITY"
    assert template.body == LINK_BODY


def test_the_button_carries_the_whole_emitted_link() -> None:
    link = captured_agent_link()
    template = parse_payment_link_template(
        catalog_with_link_template(), template_name=LINK_TEMPLATE
    )
    suffix = payment_link_button_suffix(link)

    # El link no se toca: oferta, src, el sck con la marca del recuperador y el
    # fbclid viajan enteros en el sufijo.
    assert "https://pay.hotmart.com/" + suffix == link
    for parameter in ("off=", "checkoutMode=", "src=hermes", "sck=", "fbclid="):
        assert parameter in suffix
    assert template.params(button_suffix=suffix) == {
        "name": LINK_TEMPLATE,
        "category": "UTILITY",
        "language": "es_EC",
        "processed_params": {"buttons": [{"type": "url", "parameter": suffix}]},
    }
    # Lo que ve el equipo en Chatwoot lleva el link entero.
    assert template.content(final_url=link) == f"{LINK_BODY}\n{link}"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"status": "PAUSED"}, "payment_link_template_not_approved"),
        ({"status": "REJECTED"}, "payment_link_template_not_approved"),
        ({"language": ""}, "payment_link_template_invalid_metadata"),
        (
            {"components": [{"type": "BODY", "text": LINK_BODY}]},
            "payment_link_template_button_missing",
        ),
        (
            {
                "components": [
                    {"type": "BODY", "text": "Hola, {{1}}. " + LINK_BODY},
                    {
                        "type": "BUTTONS",
                        "buttons": [
                            {
                                "type": "URL",
                                "text": "Ir al pago",
                                "url": PAYMENT_LINK_BUTTON_URL,
                            }
                        ],
                    },
                ]
            },
            "payment_link_template_unexpected_placeholders",
        ),
        (
            {
                "components": [
                    {"type": "BODY", "text": LINK_BODY},
                    {
                        "type": "BUTTONS",
                        "buttons": [
                            {
                                "type": "URL",
                                "text": "Ir al pago",
                                "url": "https://pay.hotmart.com/F106691755G",
                            }
                        ],
                    },
                ]
            },
            "payment_link_template_button_not_dynamic_checkout",
        ),
        (
            {
                "components": [
                    {"type": "HEADER", "format": "IMAGE"},
                    {"type": "BODY", "text": LINK_BODY},
                    {
                        "type": "BUTTONS",
                        "buttons": [
                            {
                                "type": "URL",
                                "text": "Ir al pago",
                                "url": PAYMENT_LINK_BUTTON_URL,
                            }
                        ],
                    },
                ]
            },
            "payment_link_template_unexpected_component",
        ),
    ],
)
def test_a_template_the_bridge_cannot_fill_is_refused(
    changes: dict[str, object], reason: str
) -> None:
    with pytest.raises(ChatwootProtocolError, match=reason):
        parse_payment_link_template(
            catalog_with_link_template(**changes), template_name=LINK_TEMPLATE
        )


@pytest.mark.parametrize(
    ("template_name", "reason"),
    [
        # La del seguimiento, real: tiene nombre, producto y cupon como marcadores.
        ("johanna_seguimiento_descuento_01", "payment_link_template_unexpected_placeholders"),
        # hello_world, real y de utilidad: no tiene boton.
        ("hello_world", "payment_link_template_button_missing"),
        # Hoy la del link no esta en el catalogo: Meta todavia no la aprobo.
        (LINK_TEMPLATE, "payment_link_template_not_found"),
    ],
)
def test_the_captured_catalog_has_no_usable_link_template_yet(
    template_name: str, reason: str
) -> None:
    with pytest.raises(ChatwootProtocolError, match=reason):
        parse_payment_link_template(captured_catalog(), template_name=template_name)


def test_a_language_other_than_the_expected_one_is_refused() -> None:
    with pytest.raises(
        ChatwootProtocolError, match="payment_link_template_language_mismatch"
    ):
        parse_payment_link_template(
            catalog_with_link_template(),
            template_name=LINK_TEMPLATE,
            expected_language="en",
        )


def test_two_templates_with_the_same_name_are_refused() -> None:
    catalog = catalog_with_link_template()
    templates = catalog["message_templates"]
    assert isinstance(templates, list)
    templates.append(copy.deepcopy(templates[-1]))

    with pytest.raises(ChatwootProtocolError, match="payment_link_template_not_found"):
        parse_payment_link_template(catalog, template_name=LINK_TEMPLATE)


@pytest.mark.parametrize(
    "url",
    [
        "https://pay.hotmart.com/",
        "https://example.com/F106691755G?off=mgbgpp19",
        "https://pay.hotmart.com/F106691755G?off=mgbgpp19 extra",
        "https://pay.hotmart.com/F106691755G?off=" + "a" * 1_800,
    ],
)
def test_a_link_that_does_not_fit_the_button_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="payment_link_checkout_url_invalid"):
        payment_link_button_suffix(url)
