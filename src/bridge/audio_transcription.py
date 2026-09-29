"""La transcripcion de los audios que un lead manda por WhatsApp.

Medido el 2026-09-28 sobre los webhooks que el bridge capturo en produccion: los
audios de las conversaciones 124, 138, 174, 177 y 200 del inbox 9 llegaron con
``content: null`` y un adjunto ``file_type: "audio"``, ``extension: "ogg"``,
``content_type: "audio/opus"`` y ``transcribed_text`` vacio (Chatwoot v4.13.0 no
transcribe). Sin texto, ``_shadow_context`` devolvia None y el worker terminaba
el trabajo sin responder, sin derivar y sin dejar una linea en el log: ninguno
de los cinco tuvo respuesta. El webhook esta en
``tests/fixtures/chatwoot_message_created_audio_inbox_9_conv_200_20260928.json``.

El audio se baja de ``data_url``: una URL firmada de Active Storage en el mismo
host que ``CHATWOOT_BASE_URL``, que redirige (302) a ``/rails/active_storage/
disk/...`` en ese mismo host y no pide autenticacion (medido desde el contenedor
del bridge el 2026-09-29: 200, ``audio/opus``, contenedor ``OggS`` con
``OpusHead``). La descarga solo sigue redirecciones dentro de ese host.

La transcripcion va al endpoint de OpenRouter ``/api/v1/audio/transcriptions``,
probando los modelos en orden: si el primero falla, devuelve un error o un texto
vacio, se prueba el siguiente. El primero y el de respaldo son de proveedores
distintos a proposito: una caida de uno no se lleva al otro.

Cada transcripcion se guarda en disco por id de adjunto. El bridge rearma el
historial desde Chatwoot en cada turno, y sin el cache volveria a pagar por el
mismo audio cada vez que el lead escribe.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx


logger = logging.getLogger(__name__)

OPENROUTER_TRANSCRIPTIONS_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
DEFAULT_TRANSCRIPTION_MODELS = (
    "openai/whisper-large-v3-turbo",
    "microsoft/mai-transcribe-2",
)
DEFAULT_TRANSCRIPTION_LANGUAGE = "es"
# OpenRouter corta las subidas en 25 MB y el base64 agrega un tercio: 16 MB de
# audio son unos 21 MB de JSON. Una nota de voz de WhatsApp de un minuto pesa
# unos 100 KB (la de la conversacion 200, de 36 s, pesa 79.794 bytes).
DEFAULT_MAX_AUDIO_BYTES = 16 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 4000
AUDIO_TRANSCRIPT_PREFIX = "[Audio transcrito] "
AUDIO_NOT_TRANSCRIBED_TEXT = "[Audio que no se pudo transcribir]"

# Lo que acepta OpenRouter segun su documentacion; opus viaja en un contenedor
# Ogg, asi que se manda como "ogg".
_FORMAT_BY_EXTENSION = {
    "ogg": "ogg",
    "oga": "ogg",
    "opus": "ogg",
    "mp3": "mp3",
    "wav": "wav",
    "m4a": "m4a",
    "aac": "aac",
    "flac": "flac",
    "webm": "webm",
}
_DOWNLOAD_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
# El endpoint de OpenRouter corta a los 60 s por pedido.
_TRANSCRIPTION_TIMEOUT = httpx.Timeout(connect=5.0, read=65.0, write=30.0, pool=5.0)


class AudioTranscriptionError(RuntimeError):
    """El audio no se pudo transcribir. ``reason`` dice en que paso."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AudioAttachment:
    attachment_id: int
    data_url: str
    audio_format: str
    file_size: int | None


def audio_attachments(message: object) -> tuple[AudioAttachment, ...]:
    """Los adjuntos de audio de un mensaje, en el orden en que llegaron."""
    if not isinstance(message, dict):
        return ()
    attachments = message.get("attachments")
    if not isinstance(attachments, list):
        return ()
    found: list[AudioAttachment] = []
    for attachment in attachments:
        if not isinstance(attachment, dict) or attachment.get("file_type") != "audio":
            continue
        attachment_id = attachment.get("id")
        data_url = attachment.get("data_url")
        extension = attachment.get("extension")
        file_size = attachment.get("file_size")
        if (
            not isinstance(attachment_id, int)
            or isinstance(attachment_id, bool)
            or attachment_id <= 0
            or not isinstance(data_url, str)
            or not data_url
        ):
            continue
        audio_format = _FORMAT_BY_EXTENSION.get(
            extension.lower() if isinstance(extension, str) else ""
        )
        if audio_format is None:
            continue
        found.append(
            AudioAttachment(
                attachment_id=attachment_id,
                data_url=data_url,
                audio_format=audio_format,
                file_size=(
                    file_size
                    if isinstance(file_size, int) and not isinstance(file_size, bool)
                    else None
                ),
            )
        )
    return tuple(found)


def needs_audio_transcription(message: object) -> bool:
    """Un mensaje sin texto que trae al menos un audio."""
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return False
    return bool(audio_attachments(message))


def _is_incoming(message: dict[str, object]) -> bool:
    # El webhook trae "incoming"; la API de mensajes trae 0.
    message_type = message.get("message_type")
    return message_type == "incoming" or (
        message_type == 0 and not isinstance(message_type, bool)
    )


class AudioTranscriber:
    """Baja el audio de Chatwoot y lo transcribe por OpenRouter, con respaldo."""

    def __init__(
        self,
        *,
        api_key: str,
        models: tuple[str, ...],
        media_host: str,
        cache_dir: Path,
        language: str = DEFAULT_TRANSCRIPTION_LANGUAGE,
        max_audio_bytes: int = DEFAULT_MAX_AUDIO_BYTES,
        transcription_url: str = OPENROUTER_TRANSCRIPTIONS_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("audio_transcription_api_key_required")
        if not models or any(
            not isinstance(model, str) or not model or len(model) > 200
            for model in models
        ):
            raise ValueError("invalid_audio_transcription_models")
        if not isinstance(media_host, str) or not media_host:
            raise ValueError("audio_transcription_media_host_required")
        if max_audio_bytes <= 0:
            raise ValueError("invalid_audio_transcription_max_bytes")
        self._api_key = api_key
        self._models = tuple(models)
        self._media_host = media_host.lower()
        self._cache_dir = cache_dir
        self._language = language
        self._max_audio_bytes = max_audio_bytes
        self._transcription_url = transcription_url
        self._transport = transport

    @property
    def models(self) -> tuple[str, ...]:
        return self._models

    async def transcribe_message(self, message: dict[str, object]) -> str:
        """El texto con el que el agente ve el mensaje. Falla si algun audio falla."""
        parts: list[str] = []
        for attachment in audio_attachments(message):
            parts.append(await self.transcribe_attachment(attachment))
        if not parts:
            raise AudioTranscriptionError("no_audio_attachment")
        return AUDIO_TRANSCRIPT_PREFIX + "\n".join(parts)

    async def enrich_history(
        self,
        messages: list[dict[str, object]],
        *,
        max_new_transcriptions: int = 5,
    ) -> list[dict[str, object]]:
        """Le pone texto a los audios entrantes del historial.

        Devuelve copias: la lista de Chatwoot no se toca. Un audio que no se
        pudo transcribir entra con un texto que lo dice, en vez de desaparecer:
        si desapareciera, el historial canonico perderia el mensaje que el
        worker esta procesando y el trabajo se reintentaria sin fin.
        """
        enriched: list[dict[str, object]] = []
        new_budget = max_new_transcriptions
        for message in messages:
            if not _is_incoming(message) or not needs_audio_transcription(message):
                enriched.append(message)
                continue
            attachments = audio_attachments(message)
            all_cached = all(
                self._read_cache(attachment.attachment_id) is not None
                for attachment in attachments
            )
            if not all_cached and new_budget <= 0:
                logger.warning(
                    "audio_transcription_history_budget_exhausted message=%s",
                    message.get("id"),
                )
                enriched.append({**message, "content": AUDIO_NOT_TRANSCRIBED_TEXT})
                continue
            if not all_cached:
                new_budget -= 1
            try:
                text = await self.transcribe_message(message)
            except AudioTranscriptionError as exc:
                logger.warning(
                    "audio_transcription_history_failed message=%s reason=%s",
                    message.get("id"),
                    exc.reason,
                )
                text = AUDIO_NOT_TRANSCRIBED_TEXT
            enriched.append({**message, "content": text})
        return enriched

    async def transcribe_attachment(self, attachment: AudioAttachment) -> str:
        cached = self._read_cache(attachment.attachment_id)
        if cached is not None:
            return cached
        if (
            attachment.file_size is not None
            and attachment.file_size > self._max_audio_bytes
        ):
            raise AudioTranscriptionError("audio_too_large")
        audio = await self._download(attachment.data_url)
        encoded = base64.b64encode(audio).decode("ascii")
        last_reason = "no_model_tried"
        for model in self._models:
            try:
                text, usage = await self._transcribe_with(
                    model=model, encoded=encoded, audio_format=attachment.audio_format
                )
            except AudioTranscriptionError as exc:
                last_reason = exc.reason
                logger.warning(
                    "audio_transcription_model_failed attachment=%s model=%s reason=%s",
                    attachment.attachment_id,
                    model,
                    exc.reason,
                )
                continue
            logger.info(
                "audio_transcription_ok attachment=%s model=%s bytes=%s "
                "seconds=%s cost=%s",
                attachment.attachment_id,
                model,
                len(audio),
                usage.get("seconds"),
                usage.get("cost"),
            )
            self._write_cache(attachment.attachment_id, model=model, text=text)
            return text
        raise AudioTranscriptionError(f"all_models_failed:{last_reason}")

    async def _download(self, data_url: str) -> bytes:
        parsed = urlsplit(data_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != self._media_host:
            raise AudioTranscriptionError("audio_url_host_not_allowed")

        async def only_media_host(request: httpx.Request) -> None:
            # Las redirecciones de Active Storage se quedan en el mismo host; una
            # que saliera de ahi no se sigue.
            if (
                request.url.scheme != "https"
                or (request.url.host or "").lower() != self._media_host
            ):
                raise AudioTranscriptionError("audio_redirect_host_not_allowed")

        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=_DOWNLOAD_TIMEOUT,
                follow_redirects=True,
                max_redirects=3,
                event_hooks={"request": [only_media_host]},
            ) as client:
                async with client.stream("GET", data_url) as response:
                    if response.status_code != 200:
                        raise AudioTranscriptionError(
                            f"audio_download_http_{response.status_code}"
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self._max_audio_bytes:
                            raise AudioTranscriptionError("audio_too_large")
        except AudioTranscriptionError:
            raise
        except httpx.HTTPError as exc:
            raise AudioTranscriptionError("audio_download_transport_error") from exc
        if not body:
            raise AudioTranscriptionError("audio_download_empty")
        return bytes(body)

    async def _transcribe_with(
        self, *, model: str, encoded: str, audio_format: str
    ) -> tuple[str, dict[str, Any]]:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=_TRANSCRIPTION_TIMEOUT,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self._transcription_url,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": model,
                        "input_audio": {"data": encoded, "format": audio_format},
                        "language": self._language,
                    },
                )
                if response.status_code != 200:
                    raise AudioTranscriptionError(
                        f"provider_http_{response.status_code}"
                    )
                body: Any = response.json()
        except AudioTranscriptionError:
            raise
        except httpx.HTTPError as exc:
            raise AudioTranscriptionError("provider_transport_error") from exc
        except ValueError as exc:
            raise AudioTranscriptionError("provider_invalid_json") from exc
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise AudioTranscriptionError("provider_empty_text")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return " ".join(text.split())[:MAX_TRANSCRIPT_CHARS], usage

    def _cache_path(self, attachment_id: int) -> Path:
        return self._cache_dir / f"{attachment_id}.json"

    def _read_cache(self, attachment_id: int) -> str | None:
        try:
            data = json.loads(self._cache_path(attachment_id).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        text = data.get("text") if isinstance(data, dict) else None
        return text if isinstance(text, str) and text.strip() else None

    def _write_cache(self, attachment_id: int, *, model: str, text: str) -> None:
        # Falla blanda: sin cache, el proximo turno vuelve a transcribir y paga
        # de nuevo, pero el lead recibe su respuesta.
        try:
            self._cache_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                dir=self._cache_dir, prefix=f".{attachment_id}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "attachment_id": attachment_id,
                            "model": model,
                            "text": text,
                            "transcribed_at": datetime.now(UTC).isoformat(),
                        },
                        handle,
                        ensure_ascii=False,
                    )
                os.replace(temporary, self._cache_path(attachment_id))
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError:
            logger.warning(
                "audio_transcription_cache_write_failed attachment=%s", attachment_id
            )
