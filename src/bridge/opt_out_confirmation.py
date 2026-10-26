"""E-107: la baja se confirma con una sola linea, sin vender.

Hasta aca una baja nueva se registraba y no se contestaba (regla del
25/10, ``docs/contracts/commercial-ally-runtime-v1.md``). Dan, 26/10: una
baja se confirma con una sola linea, sin vender (por ejemplo «Listo, no te
escribimos mas»). La persona acaba de escribir, asi que el mensaje cae
dentro de la ventana de 24 h y es gratis, y una confirmacion baja la chance
de que bloquee o denuncie el numero. En GHL, Maribel ya contestaba «Hemos
recibido tu solicitud…».

El texto lo manda el bridge, no el modelo: no depende de que el agente se
acuerde de una regla y no gasta caracteres del SOUL comun. Cada instancia
puede cambiarlo (``OPT_OUT_CONFIRMATION_TEXT``); el texto se valida al
arrancar para que no lleve link, cupon, precio ni pregunta.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

OPT_OUT_CONFIRMATION_DEFAULT_TEXT = "Listo, no te escribimos más."

# La marca del mensaje en Chatwoot (``appointment_setter_decision``): la misma
# que lee la revision diaria para saber por que salio cada mensaje. Ningun
# otro mensaje del agente sale con esta decision.
OPT_OUT_CONFIRMATION_DECISION = "opt_out_confirmation"

# Los resultados de apply_chatwoot_inbound_opt_out que confirman: la baja
# quedo guardada (con contacto, sin contacto o con mas de uno). Un
# ``already_applied`` es el mismo mensaje que vuelve (un reintento del
# webhook): se confirma igual, porque la marca de la conversacion y la
# idempotencia del envio impiden que salga dos veces. Un
# ``evidence_conflict`` no confirma: la base no reconocio la baja como esa.
OPT_OUT_CONFIRMATION_OUTCOMES = frozenset({
    "applied",
    "already_applied",
    "recorded_unmatched",
    "recorded_ambiguous",
})

OPT_OUT_CONFIRMATION_MAX_CHARS = 160

_URL_RE = re.compile(
    r"https?://|www\.|://|\b[\w-]+\.(?:com|net|org|io|app|link|ly|me|mx|ar|co|es|cl|pe)\b",
    re.IGNORECASE,
)
# Un cupon tiene la forma de un codigo: letras y numeros pegados, como
# PROTOCOLO10+ o JOHANNA15.
_COUPON_RE = re.compile(r"\b(?=[A-Za-z0-9+]*[A-Za-z])(?=[A-Za-z0-9+]*\d)[A-Za-z0-9+]{4,}")


def validate_opt_out_confirmation_text(text: str) -> str:
    """El texto, o un ``ValueError`` si no es una sola linea sin vender."""
    candidate = text.strip()
    if not candidate:
        raise ValueError("OPT_OUT_CONFIRMATION_TEXT must not be empty")
    if "\n" in candidate or "\r" in candidate:
        raise ValueError("OPT_OUT_CONFIRMATION_TEXT must be a single line")
    if len(candidate) > OPT_OUT_CONFIRMATION_MAX_CHARS:
        raise ValueError(
            "OPT_OUT_CONFIRMATION_TEXT must be at most "
            f"{OPT_OUT_CONFIRMATION_MAX_CHARS} characters"
        )
    if _URL_RE.search(candidate):
        raise ValueError("OPT_OUT_CONFIRMATION_TEXT must not carry a link")
    if _COUPON_RE.search(candidate):
        raise ValueError("OPT_OUT_CONFIRMATION_TEXT must not carry a coupon")
    if any(mark in candidate for mark in ("?", "¿", "%", "$")):
        raise ValueError(
            "OPT_OUT_CONFIRMATION_TEXT must not ask, discount or price"
        )
    return candidate


def opt_out_confirmation_already_sent(messages: Iterable[object]) -> bool:
    """Si la conversacion ya tiene una confirmacion de baja del agente.

    La confirmacion sale una sola vez por conversacion: si la persona vuelve
    a pedir la baja en la misma conversacion no se le repite. Se reconoce por
    la marca que el bridge le pone al mandarla.
    """
    for message in messages:
        if not isinstance(message, dict):
            continue
        sender = message.get("sender")
        attributes = message.get("content_attributes")
        if (
            message.get("private") is False
            and message.get("message_type") == 1
            and isinstance(sender, dict)
            and sender.get("type") == "agent_bot"
            and isinstance(attributes, dict)
            and attributes.get("appointment_setter_decision")
            == OPT_OUT_CONFIRMATION_DECISION
        ):
            return True
    return False
