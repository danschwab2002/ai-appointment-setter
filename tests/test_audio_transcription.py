"""Transcripcion de audios entrantes: descarga de Chatwoot y OpenRouter con respaldo.

Los dos bordes externos vienen de datos capturados:

- ``chatwoot_message_created_audio_inbox_9_conv_200_20260928.json``: el webhook
  real del audio que se quedo sin respuesta en la conversacion 200.
- ``openrouter_audio_transcription_response_20260929.json``: la respuesta real
  del endpoint ``/api/v1/audio/transcriptions`` de OpenRouter, pedida desde el
  contenedor del bridge con un audio sintetico ogg/opus.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from pathlib import Path

import httpx
import pytest

from bridge.audio_transcription import (
    AUDIO_NOT_TRANSCRIBED_TEXT,
    AUDIO_TRANSCRIPT_PREFIX,
    DEFAULT_TRANSCRIPTION_MODELS,
    OPENROUTER_TRANSCRIPTIONS_URL,
    AudioTranscriber,
    AudioTranscriptionError,
    audio_attachments,
    needs_audio_transcription,
)


FIXTURES = Path(__file__).parent / "fixtures"
MEDIA_HOST = "chatwoot.example.test"
# Los primeros bytes de una nota de voz de WhatsApp: contenedor Ogg con Opus.
OGG_OPUS = b"OggS\x00\x02" + b"\x00" * 22 + b"OpusHead" + b"\x01" * 64


def _webhook_200() -> dict[str, object]:
    fixture = json.loads(
        (
            FIXTURES / "chatwoot_message_created_audio_inbox_9_conv_200_20260928.json"
        ).read_text(encoding="utf-8")
    )
    return fixture["webhook"]


def _openrouter() -> dict[str, object]:
    return json.loads(
        (FIXTURES / "openrouter_audio_transcription_response_20260929.json").read_text(
            encoding="utf-8"
        )
    )


def _respuesta_openrouter() -> dict[str, object]:
    return _openrouter()["response"]


def _rechazo_zdr() -> httpx.Response:
    # Lo que devolvio mistralai/voxtral-mini-transcribe con la cuenta real: la
    # politica de retencion cero lo excluye. Es la falla que motivo el respaldo.
    rechazo = _openrouter()["zdr_rejection"]
    return httpx.Response(rechazo["status"], json=rechazo["body"])


class _Red:
    """Chatwoot (302 a disk y el audio) y OpenRouter, con fallas por modelo."""

    def __init__(
        self,
        *,
        fallas: dict[str, httpx.Response] | None = None,
        redirect_host: str = MEDIA_HOST,
    ) -> None:
        self.fallas = fallas or {}
        self.redirect_host = redirect_host
        self.requests: list[httpx.Request] = []
        self.modelos: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.startswith("/rails/active_storage/blobs/redirect/"):
            return httpx.Response(
                302,
                headers={
                    "location": f"https://{self.redirect_host}"
                    "/rails/active_storage/disk/SANEADO/audio.ogg"
                },
            )
        if request.url.path.startswith("/rails/active_storage/disk/"):
            return httpx.Response(
                200, content=OGG_OPUS, headers={"content-type": "audio/opus"}
            )
        if str(request.url) == OPENROUTER_TRANSCRIPTIONS_URL:
            body = json.loads(request.content)
            self.modelos.append(body["model"])
            falla = self.fallas.get(body["model"])
            if falla is not None:
                return falla
            if body["model"] == DEFAULT_TRANSCRIPTION_MODELS[1]:
                return httpx.Response(200, json=_openrouter()["fallback_response"])
            return httpx.Response(200, json=_respuesta_openrouter())
        return httpx.Response(404)

    @property
    def descargas(self) -> int:
        return sum(
            r.url.path.startswith("/rails/active_storage/disk/") for r in self.requests
        )


def _transcriber(tmp_path: Path, red: _Red, **kwargs: object) -> AudioTranscriber:
    return AudioTranscriber(
        api_key="or-test-key",
        models=DEFAULT_TRANSCRIPTION_MODELS,
        media_host=MEDIA_HOST,
        cache_dir=tmp_path / "cache",
        transport=httpx.MockTransport(red),
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_captured_audio_webhook_needs_transcription() -> None:
    webhook = _webhook_200()
    assert webhook["content"] is None
    assert needs_audio_transcription(webhook) is True
    (attachment,) = audio_attachments(webhook)
    assert attachment.attachment_id == 260
    assert attachment.audio_format == "ogg"  # audio/opus viaja en Ogg
    assert attachment.file_size == 79794


def test_text_messages_and_images_do_not_need_transcription() -> None:
    webhook = _webhook_200()
    assert needs_audio_transcription({**webhook, "content": "hola"}) is False
    imagen = copy.deepcopy(webhook)
    imagen["attachments"][0]["file_type"] = "image"
    assert needs_audio_transcription(imagen) is False


def test_transcribes_with_the_primary_model_and_caches_it(tmp_path: Path) -> None:
    red = _Red()
    transcriber = _transcriber(tmp_path, red)
    expected = " ".join(str(_respuesta_openrouter()["text"]).split())

    text = asyncio.run(transcriber.transcribe_message(_webhook_200()))

    assert text == AUDIO_TRANSCRIPT_PREFIX + expected
    assert red.modelos == ["openai/whisper-large-v3-turbo"]
    pedido = next(
        r for r in red.requests if str(r.url) == OPENROUTER_TRANSCRIPTIONS_URL
    )
    assert pedido.headers["authorization"] == "Bearer or-test-key"
    body = json.loads(pedido.content)
    assert body["input_audio"]["format"] == "ogg"
    assert base64.b64decode(body["input_audio"]["data"]) == OGG_OPUS
    assert body["language"] == "es"

    # El historial se rearma en cada turno: el mismo audio no se paga dos veces.
    again = asyncio.run(transcriber.transcribe_message(_webhook_200()))
    assert again == text
    assert red.modelos == ["openai/whisper-large-v3-turbo"]
    assert red.descargas == 1


@pytest.mark.parametrize(
    "falla",
    [
        httpx.Response(503, json={"error": {"message": "provider down"}}),
        httpx.Response(200, json={"text": "   "}),
        httpx.Response(200, content=b"not json"),
        _rechazo_zdr(),
    ],
    ids=["http_503", "texto_vacio", "json_invalido", "rechazo_zdr_real"],
)
def test_falls_back_to_the_second_model(tmp_path: Path, falla: httpx.Response) -> None:
    red = _Red(fallas={"openai/whisper-large-v3-turbo": falla})
    text = asyncio.run(_transcriber(tmp_path, red).transcribe_message(_webhook_200()))
    assert text == AUDIO_TRANSCRIPT_PREFIX + str(
        _openrouter()["fallback_response"]["text"]
    )
    assert red.modelos == [
        "openai/whisper-large-v3-turbo",
        "microsoft/mai-transcribe-2",
    ]
    # El audio se baja una sola vez aunque se prueben dos modelos.
    assert red.descargas == 1


def test_fails_when_every_model_fails(tmp_path: Path) -> None:
    caida = httpx.Response(502, json={"error": {"message": "bad gateway"}})
    red = _Red(fallas={model: caida for model in DEFAULT_TRANSCRIPTION_MODELS})
    with pytest.raises(AudioTranscriptionError) as exc:
        asyncio.run(_transcriber(tmp_path, red).transcribe_message(_webhook_200()))
    assert exc.value.reason == "all_models_failed:provider_http_502"
    assert not (tmp_path / "cache" / "260.json").exists()


def test_does_not_download_from_another_host(tmp_path: Path) -> None:
    red = _Red()
    webhook = copy.deepcopy(_webhook_200())
    webhook["attachments"][0]["data_url"] = (
        "https://attacker.example.test/rails/active_storage/blobs/redirect/x/a.ogg"
    )
    with pytest.raises(AudioTranscriptionError) as exc:
        asyncio.run(_transcriber(tmp_path, red).transcribe_message(webhook))
    assert exc.value.reason == "audio_url_host_not_allowed"
    assert red.requests == []


def test_does_not_follow_a_redirect_out_of_the_chatwoot_host(tmp_path: Path) -> None:
    red = _Red(redirect_host="attacker.example.test")
    with pytest.raises(AudioTranscriptionError) as exc:
        asyncio.run(_transcriber(tmp_path, red).transcribe_message(_webhook_200()))
    assert exc.value.reason == "audio_redirect_host_not_allowed"
    assert red.modelos == []


def test_rejects_an_audio_larger_than_the_limit_before_downloading(
    tmp_path: Path,
) -> None:
    red = _Red()
    transcriber = _transcriber(tmp_path, red, max_audio_bytes=1000)
    with pytest.raises(AudioTranscriptionError) as exc:
        asyncio.run(transcriber.transcribe_message(_webhook_200()))
    assert exc.value.reason == "audio_too_large"
    assert red.requests == []


def test_history_gets_the_transcript_or_says_the_audio_was_not_understood(
    tmp_path: Path,
) -> None:
    # El mensaje tal como lo trae la API de mensajes: message_type 0.
    audio = copy.deepcopy(_webhook_200()["conversation"]["messages"][0])
    texto = {"id": 2471, "message_type": 1, "content": "Hola", "attachments": []}
    history = [texto, audio]

    enriched = asyncio.run(_transcriber(tmp_path, _Red()).enrich_history(history))
    assert enriched[0] is texto
    assert str(enriched[1]["content"]).startswith(AUDIO_TRANSCRIPT_PREFIX)
    assert history[1]["content"] is None  # la lista de Chatwoot no se toca

    caida = httpx.Response(500)
    red = _Red(fallas={model: caida for model in DEFAULT_TRANSCRIPTION_MODELS})
    enriched = asyncio.run(
        _transcriber(tmp_path / "otro", red).enrich_history(history)
    )
    assert enriched[1]["content"] == AUDIO_NOT_TRANSCRIBED_TEXT
