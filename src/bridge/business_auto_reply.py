"""La respuesta automatica de un negocio a nuestra plantilla (M8.m1, E-184).

Lo que paso (Maribel, conversacion 4, 27/10 22:15Z): el primer contacto salio a
las 22:15:44Z y a las 22:15:51Z llego «Gracias por comunicarte con …», la
respuesta automatica del WhatsApp Business de la persona, sin ``in_reply_to``
(no es un boton). Nada en el bridge la reconocia: el agente la tomo como una
persona y contesto con tres globos. En ATT1, en 30 dias, 12 entrantes llegaron
a menos de 20 s de una plantilla nuestra y 8 tenian esa forma.

Un entrante se trata como respuesta automatica solo si se cumplen las tres:

- no es una respuesta a un boton (``content_attributes.in_reply_to``);
- llego a ``BUSINESS_AUTO_REPLY_WINDOW_SECONDS`` o menos de nuestra ultima
  plantilla de la conversacion;
- el texto tiene la forma de una respuesta automatica (``_SHAPES``).

Una baja no se cruza con esto: ``detect_explicit_opt_out`` solo toma la baja
sola o con cortesias alrededor, y ninguna forma de ``_SHAPES`` es una cortesia
(lo fija un test).

Las funciones son puras y leen el historial tal como lo devuelve la API de
mensajes de Chatwoot (``created_at`` en segundos epoch y ``content_attributes``
como objeto; la API no trae ``additional_attributes``, por eso la plantilla se
reconoce por la marca que le pone el bridge al mandarla).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

# Del diseño (E-178): el entrante de la conversacion 4 llego a los 7 s; un
# minuto deja margen para un WhatsApp Business lento sin alcanzar a quien
# lee la plantilla y contesta.
BUSINESS_AUTO_REPLY_WINDOW_SECONDS = 60

# Las marcas que el bridge le pone a cada plantilla que manda, en
# ``content_attributes``: el primer toque y el seguimiento
# (``ChatwootClient.send_first_message`` / ``send_followup_message``), la
# reactivacion (``send_reactivation_template``) y el seguimiento con cupon
# (``send_followup_template``). Una respuesta del agente lleva
# ``appointment_setter_reply_hash`` y no es una plantilla.
TEMPLATE_MARKERS = frozenset({
    "recovery_first_touch_hash",
    "recovery_followup_hash",
    "reactivation_command_key",
    "conversation_followup_command_key",
})

# Una lista corta a proposito: cada patron es una frase que una persona casi
# nunca le escribe a un negocio en el primer minuto despues de una plantilla.
# Se comparan sobre el texto en minusculas y sin tildes. Medido el 29/10 sobre
# los 18 entrantes de ATT1 y Maribel que en 30 dias llegaron a 60 s o menos de
# una plantilla sin ser un boton: la lista toma 11, todos a 14 s o menos y de
# 61 a 511 caracteres; los 7 que deja son de 4 a 57 caracteres y, salvo uno,
# llegaron despues de los 35 s.
_SHAPES = tuple(
    re.compile(pattern)
    for pattern in (
        # «gracias por comunicarte», «gracias por enviarnos tu mensaje»
        r"\bgracias por (?:\w+ ){0,3}(?:mensaje|comunicarte|comunicarse"
        r"|contactarnos|contactarte|contactarse|escribirnos)\b",
        r"\bbienvenid[oa]s? a\b",
        r"\b(?:te|le|les) (?:responderemos|contestaremos|atenderemos)\b",
        r"\ben breve\b",
        r"\bhorario de atencion\b",
        r"\bfuera de(?:l)? horario\b",
        r"\b(?:mensaje|respuesta) automatic[oa]\b",
    )
)
_MAX_TEXT_CHARS = 2_000


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    )
    return " ".join(without_marks.split())


def has_business_auto_reply_shape(text: object) -> bool:
    """Si el texto tiene la forma de una respuesta automatica de un negocio."""
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT_CHARS:
        return False
    normalized = _normalize(text)
    return any(shape.search(normalized) is not None for shape in _SHAPES)


def is_button_reply(message: Mapping[str, object]) -> bool:
    """Si el mensaje es la respuesta rapida a un boton de una plantilla."""
    attributes = message.get("content_attributes")
    return isinstance(attributes, Mapping) and attributes.get("in_reply_to") is not None


def _epoch(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _is_our_template(message: Mapping[str, object]) -> bool:
    attributes = message.get("content_attributes")
    return (
        message.get("message_type") == 1
        and message.get("private") is False
        and isinstance(attributes, Mapping)
        and any(marker in attributes for marker in TEMPLATE_MARKERS)
    )


def last_template_at(
    history: Iterable[Mapping[str, object]], *, until: float
) -> float | None:
    """El ``created_at`` de nuestra ultima plantilla hasta ``until`` incluido."""
    latest: float | None = None
    for message in history:
        created_at = _epoch(message.get("created_at"))
        if created_at is None or created_at > until or not _is_our_template(message):
            continue
        if latest is None or created_at > latest:
            latest = created_at
    return latest


def looks_like_business_auto_reply(
    message: Mapping[str, object], last_template_at: float | None
) -> bool:
    """Si un entrante es la respuesta automatica de un negocio a la plantilla.

    ``message`` es un mensaje del historial de Chatwoot; ``last_template_at``,
    el momento de nuestra ultima plantilla de la conversacion (``None`` si no
    hay ninguna).
    """
    if last_template_at is None or is_button_reply(message):
        return False
    created_at = _epoch(message.get("created_at"))
    if created_at is None:
        return False
    elapsed = created_at - last_template_at
    if not 0 <= elapsed <= BUSINESS_AUTO_REPLY_WINDOW_SECONDS:
        return False
    return has_business_auto_reply_shape(message.get("content"))


def batch_is_business_auto_reply(
    history: Sequence[Mapping[str, object]], batch_message_ids: Iterable[int]
) -> bool:
    """Si todos los mensajes del lote son la respuesta automatica de un negocio.

    Basta un mensaje del lote sin esa forma (o que no aparezca en el historial)
    para que el lote se atienda como siempre: es lo que hace el agente hoy.
    """
    wanted = set(batch_message_ids)
    if not wanted:
        return False
    by_id = {
        message.get("id"): message
        for message in history
        if message.get("id") in wanted
    }
    if set(by_id) != wanted:
        return False
    for message in by_id.values():
        sender = message.get("sender")
        if (
            message.get("message_type") != 0
            or message.get("private") is not False
            or not isinstance(sender, Mapping)
            or sender.get("type") != "contact"
        ):
            return False
        created_at = _epoch(message.get("created_at"))
        if created_at is None:
            return False
        if not looks_like_business_auto_reply(
            message, last_template_at(history, until=created_at)
        ):
            return False
    return True
