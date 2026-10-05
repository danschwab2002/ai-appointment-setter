"""El link de pago en el boton de una plantilla de WhatsApp.

Pedido de Dan del 2026-10-01. El link que manda el agente cuando el lead lo pide
mide entre 318 y 352 caracteres si el lead vino de un anuncio (solo el
``fbclid`` ocupa entre 161 y 195), y el equipo marco que es largo y molesto
para la persona. WhatsApp tiene un boton de URL fuera de plantilla
(``cta_url``), pero Chatwoot 4.13 no lo manda, y tampoco la 4.18.0: sus
interactivos son botones de respuesta y listas. Un boton de plantilla si sale
por Chatwoot, como el del seguimiento con cupon, y lleva el link emitido entero
como sufijo, asi que la atribucion no cambia.

Dos piezas:

* ``parse_payment_link_template`` valida la plantilla del catalogo que publica
  Chatwoot: aprobada, ningun marcador en el texto y exactamente un boton de URL
  dinamica sobre ``https://pay.hotmart.com/``.
* ``payment_link_button_suffix`` saca la parte variable del boton del link que
  emitio la base, sin tocarlo.

La entrega en dos partes (el texto del agente y despues la plantilla) vive en
``bridge.checkout_delivery``. Ver ``docs/contracts/johanna-payment-link-v2.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from bridge.chatwoot import ChatwootProtocolError
from bridge.followup_discount import (
    _BUTTON_SUFFIX_RE,
    HOTMART_CHECKOUT_PREFIX,
    MAX_BUTTON_SUFFIX_CHARS,
)


PAYMENT_LINK_BUTTON_URL = HOTMART_CHECKOUT_PREFIX + "{{1}}"
_TEXT_COMPONENT_TYPES = frozenset({"HEADER", "BODY", "FOOTER"})


@dataclass(frozen=True)
class PaymentLinkTemplate:
    """La plantilla aprobada: texto fijo y un boton de URL al checkout."""

    name: str
    language: str
    category: str
    body: str

    def content(self, *, final_url: str) -> str:
        """Lo que queda escrito en Chatwoot para el equipo.

        Chatwoot manda la plantilla con su boton y usa ``content`` solo para
        mostrar el mensaje. Lleva el link entero a proposito: el equipo lo ve y
        lo puede copiar, el seguimiento con cupon reconoce que el link salio
        (busca ``pay.hotmart.com`` en los mensajes del agente) y los lectores
        del OS siguen contando los links del agente.
        """
        if not isinstance(final_url, str) or not final_url.startswith(
            HOTMART_CHECKOUT_PREFIX
        ):
            raise ValueError("payment_link_checkout_url_invalid")
        return f"{self.body}\n{final_url}"

    def params(self, *, button_suffix: str) -> dict[str, object]:
        """``template_params`` tal como los espera la API de Chatwoot.

        Sin ``body``: la plantilla no tiene marcadores, y Chatwoot 4.13 omite el
        componente cuando falta (``Whatsapp::TemplateProcessorService``,
        ``process_body_components``). El boton va en el indice 0, el unico.
        """
        if not isinstance(button_suffix, str) or not button_suffix:
            raise ValueError("payment_link_button_suffix_empty")
        return {
            "name": self.name,
            "category": self.category,
            "language": self.language,
            "processed_params": {
                "buttons": [{"type": "url", "parameter": button_suffix}],
            },
        }


def parse_payment_link_template(
    inbox_payload: object,
    *,
    template_name: str,
    expected_language: str | None = None,
) -> PaymentLinkTemplate:
    """Extraer la plantilla del link del catalogo que publica Chatwoot.

    Falla cerrado ante cualquier duda. Se lee del inbox en cada entrega: si Meta
    la da de baja o la pausa, el catalogo lo refleja y el link sale como texto,
    como antes. Chatwoot crea el mensaje al instante y lo manda a WhatsApp
    despues: con una plantilla que no esta aprobada, el mensaje queda fallido
    sin que el bridge se entere.
    """
    if not isinstance(template_name, str) or not template_name.strip():
        raise ValueError("payment_link_template_name_missing")
    if not isinstance(inbox_payload, dict):
        raise ChatwootProtocolError("invalid_inbox_payload")
    templates = inbox_payload.get("message_templates")
    if not isinstance(templates, list):
        raise ChatwootProtocolError("invalid_inbox_payload")

    wanted = template_name.strip()
    matches = [
        template
        for template in templates
        if isinstance(template, dict) and template.get("name") == wanted
    ]
    if len(matches) != 1:
        raise ChatwootProtocolError("payment_link_template_not_found")
    template = matches[0]
    if template.get("status") != "APPROVED":
        raise ChatwootProtocolError("payment_link_template_not_approved")

    language = template.get("language")
    category = template.get("category")
    if (
        not isinstance(language, str)
        or not language.strip()
        or not isinstance(category, str)
        or not category.strip()
    ):
        raise ChatwootProtocolError("payment_link_template_invalid_metadata")
    if (
        expected_language is not None
        and language.strip() != expected_language.strip()
    ):
        raise ChatwootProtocolError("payment_link_template_language_mismatch")

    components = template.get("components")
    if not isinstance(components, list) or not all(
        isinstance(component, dict) for component in components
    ):
        raise ChatwootProtocolError("payment_link_template_invalid_body")
    for component in components:
        component_type = component.get("type")
        if component_type == "BUTTONS":
            continue
        # Un encabezado de imagen, video o documento pide su propio parametro,
        # y el bridge solo manda el del boton.
        if component_type not in _TEXT_COMPONENT_TYPES or (
            component_type == "HEADER" and component.get("format") != "TEXT"
        ):
            raise ChatwootProtocolError("payment_link_template_unexpected_component")
        text = component.get("text")
        if not isinstance(text, str):
            raise ChatwootProtocolError("payment_link_template_invalid_body")
        # Un marcador sin su valor hace que Meta rechace el envio, y el lead se
        # queda sin link.
        if "{{" in text:
            raise ChatwootProtocolError(
                "payment_link_template_unexpected_placeholders"
            )
    bodies = [
        component for component in components if component.get("type") == "BODY"
    ]
    if len(bodies) != 1:
        raise ChatwootProtocolError("payment_link_template_invalid_body")
    body = bodies[0].get("text")
    if not isinstance(body, str) or not body.strip():
        raise ChatwootProtocolError("payment_link_template_invalid_body")

    # Exactamente un boton, de URL dinamica sobre el checkout de Hotmart: es lo
    # que reemplaza al link escrito.
    button_groups = [
        component for component in components if component.get("type") == "BUTTONS"
    ]
    if len(button_groups) != 1:
        raise ChatwootProtocolError("payment_link_template_button_missing")
    buttons = button_groups[0].get("buttons")
    if not isinstance(buttons, list) or len(buttons) != 1:
        raise ChatwootProtocolError("payment_link_template_button_missing")
    button = buttons[0]
    if (
        not isinstance(button, dict)
        or button.get("type") != "URL"
        or button.get("url") != PAYMENT_LINK_BUTTON_URL
    ):
        raise ChatwootProtocolError(
            "payment_link_template_button_not_dynamic_checkout"
        )

    return PaymentLinkTemplate(
        name=wanted,
        language=language.strip(),
        category=category.strip(),
        body=body.strip(),
    )


def payment_link_button_suffix(checkout_url_final: str) -> str:
    """La parte variable del boton: el link emitido sin ``https://pay.hotmart.com/``.

    El link no se toca. Ya trae la oferta del lead, ``src=hermes``, el ``sck``
    con la marca del recuperador (``~``, literal; la ``|`` de los links
    anteriores y del sck del anuncio viene codificada como ``%7C``) y,
    si el lead vino de un anuncio, el ``fbclid``.
    """
    if (
        not isinstance(checkout_url_final, str)
        or not checkout_url_final.startswith(HOTMART_CHECKOUT_PREFIX)
    ):
        raise ValueError("payment_link_checkout_url_invalid")
    suffix = checkout_url_final[len(HOTMART_CHECKOUT_PREFIX):]
    if (
        len(suffix) > MAX_BUTTON_SUFFIX_CHARS
        or not _BUTTON_SUFFIX_RE.fullmatch(suffix)
    ):
        raise ValueError("payment_link_checkout_url_invalid")
    return suffix
