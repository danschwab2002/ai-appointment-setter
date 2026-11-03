"""Los entrantes que traen un adjunto y ningun texto (imagen, sticker, video, documento).

Medido el 2026-11-03 (E-314): la conversacion 604 de ATT1 y la 6 de Maribel
terminaron en una imagen sin texto y nadie contesto. ``_shadow_context`` devolvia
None con ``content`` vacio y el worker salia con
``chatwoot_inbound_without_text_ignored``; el monitor de trabadas no tomaba la
conversacion (pide texto en el ultimo mensaje) y la guarda de envio habria
rechazado la respuesta (``invalid_trigger_message``). El mensaje 4601 de la 6,
tal como lo trae la API, esta en
``tests/fixtures/chatwoot_conversation_messages_inbox_13_conv_6_20261029.json``:
``content: null`` y un adjunto ``file_type: "image"``, ``extension: "webp"``,
512x512 (la forma de un sticker de WhatsApp).

Con ``INBOUND_ATTACHMENT_WITHOUT_TEXT_ENABLED`` (decision de Dan, 03/11: «que
primero pregunte que es y luego pase si llega una segunda»):

- el primer adjunto sin texto entra al agente como un texto de sistema por tipo
  (``[La persona mandó una imagen sin texto. No lo podés ver: ...]``), y el
  agente pregunta que es;
- si el anterior mensaje de la persona tambien fue un adjunto sin texto (sin un
  texto suyo en el medio), la conversacion se deriva a una persona con
  ``attachment_without_text_repeated``.

El audio no pasa por aca: lo resuelve ``bridge.audio_transcription``. Un mensaje
que trae un audio, aunque traiga otro adjunto, queda fuera de este modulo.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

ATTACHMENT_WITHOUT_TEXT_REPEATED_REASON = "attachment_without_text_repeated"

# Chatwoot (v4) llama file_type a lo que guarda de cada adjunto. WhatsApp manda
# los stickers como image/webp: se nombran aparte para que el agente no pregunte
# por «la imagen» cuando la persona mando un sticker.
_KIND_BY_FILE_TYPE = {
    "image": "imagen",
    "video": "video",
    "file": "documento",
    "location": "ubicacion",
    "contact": "contacto",
}
_SINGULAR = {
    "imagen": "una imagen",
    "sticker": "un sticker",
    "video": "un video",
    "documento": "un documento",
    "ubicacion": "una ubicación",
    "contacto": "un contacto",
    "archivo": "un archivo",
}
_PLURAL = {
    "imagen": "imágenes",
    "sticker": "stickers",
    "video": "videos",
    "documento": "documentos",
    "ubicacion": "ubicaciones",
    "contacto": "contactos",
    "archivo": "archivos",
}


def _has_text(message: Mapping[str, object]) -> bool:
    content = message.get("content")
    return isinstance(content, str) and bool(content.strip())


def _attachment_kind(attachment: Mapping[str, object]) -> str:
    file_type = attachment.get("file_type")
    if file_type == "image":
        extension = attachment.get("extension")
        content_type = attachment.get("content_type")
        if (isinstance(extension, str) and extension.lower() == "webp") or (
            isinstance(content_type, str) and content_type.lower() == "image/webp"
        ):
            return "sticker"
    if isinstance(file_type, str):
        return _KIND_BY_FILE_TYPE.get(file_type, "archivo")
    return "archivo"


def attachment_without_text_kinds(message: object) -> tuple[str, ...] | None:
    """Los tipos de adjunto de un mensaje sin texto, o None si no aplica.

    None si el mensaje trae texto, no trae adjuntos, o trae un audio (el audio
    tiene su propio camino).
    """
    if not isinstance(message, Mapping) or _has_text(message):
        return None
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        return None
    kinds: list[str] = []
    for attachment in attachments:
        if not isinstance(attachment, Mapping):
            continue
        if attachment.get("file_type") == "audio":
            return None
        kinds.append(_attachment_kind(attachment))
    return tuple(kinds) or None


def attachment_without_text_note(message: object) -> str | None:
    """El texto de sistema que ve el agente en lugar del adjunto sin texto."""
    kinds = attachment_without_text_kinds(message)
    if kinds is None:
        return None
    distinct = sorted(set(kinds))
    if len(kinds) == 1:
        what = _SINGULAR[kinds[0]]
    elif len(distinct) == 1:
        what = f"{len(kinds)} {_PLURAL[distinct[0]]}"
    else:
        what = f"{len(kinds)} archivos"
    return (
        f"[La persona mandó {what} sin texto. No lo podés ver: preguntale "
        "qué es o en qué la podés ayudar.]"
    )


def _is_prospect_message(message: Mapping[str, object]) -> bool:
    sender = message.get("sender")
    message_type = message.get("message_type")
    return (
        message.get("private") is False
        and (message_type == 0 or message_type == "incoming")
        and not isinstance(message_type, bool)
        and isinstance(sender, Mapping)
        and sender.get("type") == "contact"
    )


def describe_attachments_without_text(
    messages: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Le pone el texto de sistema a los adjuntos sin texto de la persona.

    Devuelve copias: la lista de Chatwoot no se toca. Sin esto, la
    normalizacion del historial saltea el mensaje sin texto y el turno no
    encuentra el mensaje que lo disparo (``current_message_not_in_canonical_
    history``).
    """
    described: list[dict[str, object]] = []
    for message in messages:
        note = (
            attachment_without_text_note(message)
            if _is_prospect_message(message)
            else None
        )
        described.append(
            {**message, "content": note} if note is not None else dict(message)
        )
    return described


def is_repeated_attachment_without_text(
    messages: Sequence[Mapping[str, object]],
    *,
    batch_message_ids: Iterable[int],
) -> bool:
    """Si este lote es el segundo adjunto sin texto seguido de la persona.

    El lote (lo que el debounce junto) cuenta como un solo envio: dos fotos
    mandadas juntas son una sola pregunta. Es repetido cuando ningun mensaje
    del lote trae texto y el ultimo mensaje de la persona anterior al lote
    tambien fue un adjunto sin texto. Lo que el agente o el equipo contesto en
    el medio no cuenta: lo que corta la racha es un texto (o un audio) de la
    persona.
    """
    batch = {
        message_id
        for message_id in batch_message_ids
        if isinstance(message_id, int) and not isinstance(message_id, bool)
    }
    if not batch:
        return False
    first_in_batch = min(batch)
    previous: Mapping[str, object] | None = None
    previous_id = -1
    for message in messages:
        message_id = message.get("id")
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or not _is_prospect_message(message)
        ):
            continue
        if message_id in batch:
            if attachment_without_text_kinds(message) is None:
                return False
            continue
        if previous_id < message_id < first_in_batch:
            previous, previous_id = message, message_id
    return previous is not None and (
        attachment_without_text_kinds(previous) is not None
    )
