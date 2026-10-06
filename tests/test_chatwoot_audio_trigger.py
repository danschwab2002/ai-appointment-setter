"""La respuesta a una nota de voz pasa el control previo al envio.

Parte de la conversacion 1 de ATT1 tal como la devolvio la API de Chatwoot,
capturada y anonimizada el 2026-10-06 desde el bridge de ATT1. El lead mando un
audio (mensaje 2682: ``content: null`` y un adjunto ``audio/ogg``); el bridge lo
transcribio, el agente propuso una respuesta y el divisor la termino, pero
``_current_authorization_result`` exigia texto en el disparador y la descarto con
``invalid_trigger_message``, sin una linea en el log.

``test_app_audio_transcription.py`` no lo vio porque usa un cliente de Chatwoot
falso, que da la respuesta por enviada sin pasar por este control. Aca corre el
cliente real contra la conversacion capturada.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from bridge.chatwoot import ChatwootClient


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "chatwoot_audio_trigger_inbox_11_conv_1_20261006.json"
)
CUENTA = 2
INBOX = 11
CONVERSACION = 1
AUDIO = 2682
RESPUESTA_DEL_BOT = 2668
AGENT_BOT_ID = 2
JID_DEL_LEAD = "5210000000001@s.whatsapp.net"
RUTA = f"/api/v1/accounts/{CUENTA}/conversations/{CONVERSACION}"
TEXTO = "Claro, te cuento como es el programa."


def _captura() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _mensaje(captura: dict[str, object], message_id: int) -> dict[str, object]:
    return next(
        m for m in captura["messages"]["payload"] if m["id"] == message_id
    )


def _enviar(
    tmp_path: Path, captura: dict[str, object]
) -> tuple[dict[str, object], list[dict[str, object]]]:
    enviados: list[dict[str, object]] = []

    def chatwoot(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == RUTA:
            return httpx.Response(200, json=captura["conversation"])
        if request.method == "GET" and request.url.path == f"{RUTA}/labels":
            return httpx.Response(200, json=captura["labels"])
        if request.method == "GET" and request.url.path == f"{RUTA}/messages":
            return httpx.Response(200, json=captura["messages"])
        if request.method == "POST" and request.url.path == f"{RUTA}/messages":
            cuerpo = json.loads(request.content)
            enviados.append(cuerpo)
            # Chatwoot devuelve el mensaje creado con la misma forma con la que
            # lo lista: el molde es la respuesta real del agent bot (2668).
            creado = copy.deepcopy(_mensaje(captura, RESPUESTA_DEL_BOT))
            creado.update(
                id=AUDIO + 1,
                content=cuerpo["content"],
                content_attributes=cuerpo["content_attributes"],
            )
            return httpx.Response(200, json=creado)
        raise AssertionError(f"pedido inesperado: {request.method} {request.url}")

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=CUENTA,
        access_token="control-token",
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=AGENT_BOT_ID,
        reply_dir=tmp_path,
        transport=httpx.MockTransport(chatwoot),
    )
    # Como lo llama el bridge con los remitentes acotados del entrante.
    resultado = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=CONVERSACION,
            trigger_message_id=AUDIO,
            delivery_id="audio-2682",
            content=TEXTO,
            expected_inbox_id=INBOX,
            expected_jid=JID_DEL_LEAD,
        )
    )
    return resultado, enviados


def test_the_reply_to_a_lead_voice_note_is_sent(tmp_path: Path) -> None:
    captura = _captura()
    audio = _mensaje(captura, AUDIO)
    assert audio["content"] is None
    assert [a["file_type"] for a in audio["attachments"]] == ["audio"]

    resultado, enviados = _enviar(tmp_path, captura)

    assert resultado == {"status": "sent", "message_id": AUDIO + 1}
    assert [cuerpo["content"] for cuerpo in enviados] == [TEXTO]


def _sin_adjuntos(mensaje: dict[str, object]) -> None:
    del mensaje["attachments"]


def _con_una_imagen(mensaje: dict[str, object]) -> None:
    mensaje["attachments"][0].update(file_type="image", extension="jpeg")


@pytest.mark.parametrize("sacar_el_audio", [_sin_adjuntos, _con_una_imagen])
def test_a_trigger_without_text_or_audio_is_still_blocked(
    tmp_path: Path, sacar_el_audio: Callable[[dict[str, object]], None]
) -> None:
    captura = _captura()
    sacar_el_audio(_mensaje(captura, AUDIO))

    resultado, enviados = _enviar(tmp_path, captura)

    assert resultado == {"status": "blocked", "reason": "invalid_trigger_message"}
    assert enviados == []
