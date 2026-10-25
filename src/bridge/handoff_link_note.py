"""F-034, D8: al derivar, una nota privada con el link que recibio la persona.

Cuando el agente deriva, quien del equipo toma la conversacion no ve que link
de pago le mandamos a la persona: las plantillas del carrito (A1, el primer
mensaje del carrito, y sus seguimientos) dejan en Chatwoot solo el texto, sin el
link ni el cupon, y la API de mensajes no devuelve ``template_params``. Si la
persona vuelve a pedir el link, el equipo manda uno a mano (sin ``src`` ni
``sck``: una venta que no se puede contar) o un cupon distinto.

La nota sale de la base (``read_handoff_note_checkout_link_v1``), es privada
(nunca le llega a la persona), va una vez por derivacion (el marcador lleva el
``handoff_request_id``) y la escribe el bridge con un texto fijo. Si la persona
nunca recibio un link nuestro, no hay nota (decision D-091-1, con default,
para que la revise Dan): queda una linea en el log.

El aviso a la persona (``handoff_notice.py``, #327) no cambia.
"""

from __future__ import annotations

import re

from bridge.followup_discount import followup_button_suffix
from bridge.supabase import HandoffNoteCheckoutLink

HANDOFF_LINK_NOTE_MARKER_PREFIX = "[supportmagician-derivacion-link:"
HANDOFF_LINK_NOTE_MAX_CHARS = 1800
_HOTMART_CHECKOUT_PREFIX = "https://pay.hotmart.com/"
_REQUEST_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def handoff_link_note_marker(handoff_request_id: str) -> str:
    """El marcador de idempotencia: uno por derivacion."""
    if not isinstance(handoff_request_id, str) or not _REQUEST_ID_RE.fullmatch(
        handoff_request_id
    ):
        raise ValueError("handoff_request_id must be a lowercase uuid")
    return f"{HANDOFF_LINK_NOTE_MARKER_PREFIX}{handoff_request_id}]"


def handoff_link_note_url(link: HandoffNoteCheckoutLink) -> str:
    """El link tal como lo recibio la persona: con su cupon, si lo tiene."""
    if link.coupon_code is None:
        return link.checkout_url_final
    return _HOTMART_CHECKOUT_PREFIX + followup_button_suffix(
        link.checkout_url_final, link.coupon_code
    )


def handoff_link_note_body(link: HandoffNoteCheckoutLink) -> str:
    """El texto de la nota. Fijo, sin datos de la persona salvo su link."""
    accepted = link.accepted_at.strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"Último link de pago que le mandamos a esta persona ({accepted}):",
        handoff_link_note_url(link),
    ]
    if link.coupon_code is not None:
        lines.append(f"Ya trae su cupón personal {link.coupon_code}.")
    lines.append(
        "Si pide el link, envíale este mismo y no otro, ni otro cupón: "
        "así la venta queda registrada."
    )
    body = "\n".join(lines)
    if len(body) > HANDOFF_LINK_NOTE_MAX_CHARS - 80:
        raise ValueError("handoff_link_note_too_long")
    return body
