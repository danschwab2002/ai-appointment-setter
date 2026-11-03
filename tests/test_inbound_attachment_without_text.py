"""E-315: el adjunto sin texto (imagen, sticker, video, documento).

Parte de dos capturas reales:

- el mensaje 4601 de la conversacion 6 de Maribel (inbox 13), tal como lo trae
  la API de mensajes (``chatwoot_conversation_messages_inbox_13_conv_6_20261029
  .json``): ``content: null`` y un adjunto ``image/webp`` de 512x512. Es uno de
  los dos casos de E-314 que se quedaron sin respuesta;
- el webhook ``message_created`` del audio de la conversacion 200 de Johanna
  (``chatwoot_message_created_audio_inbox_9_conv_200_20260928.json``), el unico
  webhook de un entrante sin texto capturado. Para la imagen se le cambia el
  adjunto de audio por el adjunto real del 4601: deuda, hasta capturar el
  webhook de una imagen (``captures/`` del volumen del bridge, con el OK de Dan).

Lo que se prueba, con ``INBOUND_ATTACHMENT_WITHOUT_TEXT_ENABLED``: el primer
adjunto le llega al agente como un texto de sistema y su respuesta sale; el
segundo seguido se deriva con ``attachment_without_text_repeated``; apagado,
todo sigue como antes; el audio no cambia; el monitor de trabadas y la guarda de
envio aceptan el adjunto solo con el flag.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path

import httpx
import pytest

import bridge.app as app_module
from bridge.app import Settings, create_app
from bridge.chatwoot import ChatwootClient
from bridge.inbound_attachments import (
    ATTACHMENT_WITHOUT_TEXT_REPEATED_REASON,
    attachment_without_text_kinds,
    attachment_without_text_note,
    describe_attachments_without_text,
    is_repeated_attachment_without_text,
)
from test_audio_transcription import _webhook_200
from test_webhook import (
    StubChatwootClient,
    StubInboundCommercialSupabase,
    StubShadowProcessor,
    _post,
    _signed_headers,
)


FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "webhook-secret"
JID_DEL_LEAD_200 = "5210000000200@s.whatsapp.net"
IMAGEN_6 = 4601


def _conversacion_6() -> list[dict[str, object]]:
    captura = json.loads(
        (
            FIXTURES / "chatwoot_conversation_messages_inbox_13_conv_6_20261029.json"
        ).read_text(encoding="utf-8")
    )
    return captura["payload"]


def _mensaje_6(message_id: int) -> dict[str, object]:
    return copy.deepcopy(next(m for m in _conversacion_6() if m["id"] == message_id))


def _adjunto_real() -> dict[str, object]:
    return copy.deepcopy(_mensaje_6(IMAGEN_6)["attachments"][0])


def _como_jpeg(adjunto: dict[str, object]) -> dict[str, object]:
    return {**adjunto, "extension": "jpeg", "content_type": "image/jpeg"}


# --- el modulo -----------------------------------------------------------------


def test_the_captured_webp_without_text_reads_as_a_sticker() -> None:
    imagen = _mensaje_6(IMAGEN_6)
    assert imagen["content"] is None

    assert attachment_without_text_kinds(imagen) == ("sticker",)
    assert attachment_without_text_note(imagen) == (
        "[La persona mandó un sticker sin texto. No lo podés ver: preguntale "
        "qué es o en qué la podés ayudar.]"
    )


@pytest.mark.parametrize(
    ("adjuntos", "esperado"),
    [
        ([{"file_type": "image", "extension": "jpeg"}], "una imagen"),
        ([{"file_type": "video", "extension": "mp4"}], "un video"),
        ([{"file_type": "file", "extension": "pdf"}], "un documento"),
        ([{"file_type": "location"}], "una ubicación"),
        ([{"file_type": "otro_tipo"}], "un archivo"),
        (
            [{"file_type": "image", "extension": "jpeg"}] * 2,
            "2 imágenes",
        ),
        (
            [
                {"file_type": "image", "extension": "jpeg"},
                {"file_type": "file", "extension": "pdf"},
            ],
            "2 archivos",
        ),
    ],
)
def test_the_system_text_names_the_kind(
    adjuntos: list[dict[str, object]], esperado: str
) -> None:
    mensaje = {**_mensaje_6(IMAGEN_6), "attachments": adjuntos}

    nota = attachment_without_text_note(mensaje)

    assert nota is not None
    assert nota.startswith(f"[La persona mandó {esperado} sin texto.")


def test_audio_and_text_are_not_attachments_without_text() -> None:
    audio = _webhook_200()
    assert attachment_without_text_kinds(audio) is None
    con_texto = {**_mensaje_6(IMAGEN_6), "content": "Mirá esto"}
    assert attachment_without_text_kinds(con_texto) is None
    sin_adjuntos = {**_mensaje_6(IMAGEN_6), "attachments": []}
    assert attachment_without_text_kinds(sin_adjuntos) is None
    # Un audio junto con una imagen sigue siendo del camino del audio.
    mezcla = {
        **_mensaje_6(IMAGEN_6),
        "attachments": [_adjunto_real(), {"file_type": "audio", "extension": "ogg"}],
    }
    assert attachment_without_text_kinds(mezcla) is None


def test_the_history_gets_the_system_text_only_on_the_persons_attachment() -> None:
    historial = _conversacion_6()
    original = copy.deepcopy(historial)

    descrito = describe_attachments_without_text(historial)

    assert historial == original  # devuelve copias
    por_id = {m["id"]: m for m in descrito}
    assert por_id[IMAGEN_6]["content"].startswith("[La persona mandó un sticker")
    for antes in original:
        if antes["id"] != IMAGEN_6:
            assert por_id[antes["id"]]["content"] == antes["content"]
    # Un adjunto sin texto del agente no se describe.
    del_bot = {**_mensaje_6(4600), "content": None, "attachments": [_adjunto_real()]}
    assert describe_attachments_without_text([del_bot])[0]["content"] is None


def _respuesta_del_bot(message_id: int) -> dict[str, object]:
    return {**_mensaje_6(4600), "id": message_id}


def _otra_imagen(message_id: int) -> dict[str, object]:
    return {**_mensaje_6(IMAGEN_6), "id": message_id}


def test_the_first_attachment_after_a_text_is_not_repeated() -> None:
    # En la 6, antes del 4601 la persona escribio texto (4599).
    assert not is_repeated_attachment_without_text(
        _conversacion_6(), batch_message_ids=(IMAGEN_6,)
    )


def test_a_second_attachment_in_a_row_is_repeated_even_after_the_agent_asked() -> None:
    historial = [*_conversacion_6(), _respuesta_del_bot(4602), _otra_imagen(4603)]

    assert is_repeated_attachment_without_text(historial, batch_message_ids=(4603,))


def test_a_text_or_an_audio_in_between_breaks_the_streak() -> None:
    texto = {**_mensaje_6(4599), "id": 4602}
    audio = {
        **_mensaje_6(IMAGEN_6),
        "id": 4602,
        "attachments": [{"file_type": "audio", "extension": "ogg"}],
    }
    for en_el_medio in (texto, audio):
        historial = [*_conversacion_6(), en_el_medio, _otra_imagen(4603)]
        assert not is_repeated_attachment_without_text(
            historial, batch_message_ids=(4603,)
        )


def test_two_attachments_sent_together_are_one_question() -> None:
    historial = [*_conversacion_6(), _otra_imagen(4602)]

    # El 4601 y el 4602 en el mismo lote: antes del lote, la persona escribio.
    assert not is_repeated_attachment_without_text(
        historial, batch_message_ids=(IMAGEN_6, 4602)
    )
    # Un texto dentro del lote tampoco es repetido.
    texto = {**_mensaje_6(4599), "id": 4604}
    historial = [*_conversacion_6(), _respuesta_del_bot(4602), _otra_imagen(4603), texto]
    assert not is_repeated_attachment_without_text(
        historial, batch_message_ids=(4603, 4604)
    )


# --- de punta a punta: webhook, worker, agente o derivacion ----------------------


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": SECRET,
        "allowed_jid": JID_DEL_LEAD_200,
        "capture_dir": tmp_path / "captures",
        "max_age_seconds": 300,
        "agent_bot_id": 1,
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 9,
        "chatwoot_cut_b_admission_enabled": True,
        "chatwoot_cut_b_scope_key": "libre-de-ansiedad-inbound",
        "chatwoot_cut_b_scope_version": 2,
        "chatwoot_cut_b_agent_enabled": True,
        "automated_replies_enabled": True,
        "human_handoff_admission_enabled": True,
        "human_handoff_projection_enabled": True,
        "handoff_projection_policy_key": "lancemos-inbound-handoff",
        "handoff_projection_policy_version": 1,
        "human_handoff_projection_worker_id": "handoff-projection-test",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _webhook_imagen() -> dict[str, object]:
    # El webhook real del 2472, con el adjunto real del 4601 (en jpeg: una foto).
    webhook = _webhook_200()
    adjunto = {**_como_jpeg(_adjunto_real()), "id": 260, "message_id": 2472}
    webhook["attachments"] = [adjunto]
    webhook["conversation"]["messages"][0]["attachments"] = [copy.deepcopy(adjunto)]
    return webhook


def _mensaje_2472(webhook: dict[str, object]) -> dict[str, object]:
    return copy.deepcopy(webhook["conversation"]["messages"][0])


def _procesar(
    tmp_path: Path,
    *,
    webhook: dict[str, object],
    historial: list[dict[str, object]],
    prendido: bool,
) -> tuple[StubShadowProcessor, StubChatwootClient, StubInboundCommercialSupabase]:
    shadow = StubShadowProcessor({"decision": "reply", "reply": "¿Qué me mandaste?"})
    chatwoot = StubChatwootClient(messages=historial)
    supabase = StubInboundCommercialSupabase()
    app = create_app(
        _settings(tmp_path, inbound_attachment_without_text_enabled=prendido),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    response = _post(
        app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="imagen-200")
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return shadow, chatwoot, supabase


def test_the_first_attachment_reaches_the_agent_and_its_reply_is_sent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    webhook = _webhook_imagen()

    with caplog.at_level(logging.INFO):
        shadow, chatwoot, supabase = _procesar(
            tmp_path,
            webhook=webhook,
            historial=[_mensaje_2472(webhook)],
            prendido=True,
        )

    assert len(shadow.calls) == 1
    _, context = shadow.calls[0]
    prospect = [m for m in context["messages"] if m["actor"] == "prospect"]
    assert prospect[-1]["text"].startswith("[La persona mandó una imagen sin texto.")
    assert len(chatwoot.reply_calls) == 1
    assert supabase.handoff_calls == []
    assert (
        "chatwoot_inbound_attachment_without_text message=2472 kinds=imagen "
        "repeated=false"
    ) in caplog.text


def test_the_second_attachment_in_a_row_goes_to_a_person(tmp_path: Path) -> None:
    webhook = _webhook_imagen()
    anterior = {**_mensaje_2472(webhook), "id": 2470}
    pregunta_del_bot = {
        **_mensaje_2472(webhook),
        "id": 2471,
        "content": "¿Qué me mandaste?",
        "message_type": 1,
        "attachments": [],
        "sender": {"type": "agent_bot", "id": 1},
        "sender_type": "AgentBot",
        "sender_id": 1,
    }

    shadow, chatwoot, supabase = _procesar(
        tmp_path,
        webhook=webhook,
        historial=[anterior, pregunta_del_bot, _mensaje_2472(webhook)],
        prendido=True,
    )

    assert shadow.calls == []  # el agente no contesta: lo toma una persona
    assert len(supabase.handoff_calls) == 1
    assert supabase.handoff_calls[0]["detail_reason_code"] == (
        ATTACHMENT_WITHOUT_TEXT_REPEATED_REASON
    )
    assert chatwoot.calls == [(200, "automation_paused")]


def test_with_the_flag_off_the_attachment_is_still_only_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    webhook = _webhook_imagen()

    with caplog.at_level(logging.WARNING):
        shadow, chatwoot, supabase = _procesar(
            tmp_path,
            webhook=webhook,
            historial=[_mensaje_2472(webhook)],
            prendido=False,
        )

    assert shadow.calls == []
    assert chatwoot.reply_calls == []
    assert supabase.handoff_calls == []
    assert "chatwoot_inbound_without_text_ignored message=2472" in caplog.text


def test_an_audio_with_transcription_off_is_untouched_by_the_flag(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    webhook = _webhook_200()

    with caplog.at_level(logging.WARNING):
        shadow, chatwoot, supabase = _procesar(
            tmp_path,
            webhook=webhook,
            historial=[_mensaje_2472(webhook)],
            prendido=True,
        )

    assert shadow.calls == []
    assert chatwoot.reply_calls == []
    assert supabase.handoff_calls == []
    assert "chatwoot_inbound_without_text_ignored message=2472" in caplog.text


def test_the_flag_is_portable_and_off_by_default() -> None:
    assert (
        "inbound_attachment_without_text_enabled"
        in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    )
    assert Settings.__dataclass_fields__[
        "inbound_attachment_without_text_enabled"
    ].default is False


# --- el cliente real: monitor de trabadas y guarda de envio --------------------


def _cliente(prendido: bool, transport: httpx.MockTransport | None = None) -> ChatwootClient:
    return ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=3,
        access_token="control-token",
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=2,
        transport=transport,
        accept_attachment_without_text=prendido,
    )


def _conversacion_trabada() -> dict[str, object]:
    imagen = _mensaje_6(IMAGEN_6)
    return {
        "id": 6,
        "inbox_id": 13,
        "status": "open",
        "can_reply": True,
        "labels": [],
        "meta": {"sender": copy.deepcopy(imagen["sender"]), "assignee": None},
        "contact_inbox": {},
        "messages": [_mensaje_6(4600), imagen],
    }


@pytest.mark.parametrize("prendido", [False, True])
def test_the_stalled_monitor_takes_an_attachment_only_with_the_flag(
    prendido: bool,
) -> None:
    imagen = _mensaje_6(IMAGEN_6)

    candidato = _cliente(prendido)._stalled_conversation_candidate(
        _conversacion_trabada(),
        expected_inbox_id=13,
        stale_after_seconds=120,
        max_age_seconds=86_400,
        allow_any_scoped_sender=True,
        now_epoch=imagen["created_at"] + 600,
    )

    if not prendido:
        assert candidato is None
        return
    assert candidato is not None
    assert candidato.delivery_id == f"stalled-chatwoot:6:{IMAGEN_6}"
    assert candidato.payload["content"] is None
    assert candidato.payload["attachments"] == imagen["attachments"]
    assert attachment_without_text_note(candidato.payload) is not None


def _enviar_respuesta(tmp_path: Path, prendido: bool) -> tuple[dict[str, object], list[dict[str, object]]]:
    ruta = "/api/v1/accounts/3/conversations/6"
    historial = _conversacion_6()
    conversacion = {
        **{k: v for k, v in _conversacion_trabada().items() if k != "messages"},
        "meta": {
            "sender": {
                **copy.deepcopy(_mensaje_6(IMAGEN_6)["sender"]),
                "phone_number": "+520000000767",
            },
            "assignee": None,
        },
    }
    enviados: list[dict[str, object]] = []

    def chatwoot(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == ruta:
            return httpx.Response(200, json=conversacion)
        if request.method == "GET" and request.url.path == f"{ruta}/labels":
            return httpx.Response(200, json={"payload": []})
        if request.method == "GET" and request.url.path == f"{ruta}/messages":
            return httpx.Response(200, json={"payload": historial})
        if request.method == "POST" and request.url.path == f"{ruta}/messages":
            cuerpo = json.loads(request.content)
            enviados.append(cuerpo)
            creado = {
                **_mensaje_6(4600),
                "id": IMAGEN_6 + 1,
                "content": cuerpo["content"],
                "content_attributes": cuerpo["content_attributes"],
                "sender": {"type": "agent_bot", "id": 2},
            }
            return httpx.Response(200, json=creado)
        raise AssertionError(f"pedido inesperado: {request.method} {request.url}")

    cliente = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=3,
        access_token="control-token",
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=2,
        reply_dir=tmp_path,
        transport=httpx.MockTransport(chatwoot),
        accept_attachment_without_text=prendido,
    )
    resultado = asyncio.run(
        cliente.send_agent_bot_reply(
            conversation_id=6,
            trigger_message_id=IMAGEN_6,
            delivery_id="imagen-4601",
            content="¿Qué me mandaste?",
            expected_inbox_id=13,
            expected_jid="520000000767@s.whatsapp.net",
        )
    )
    return resultado, enviados


def test_the_send_guard_accepts_the_attachment_only_with_the_flag(
    tmp_path: Path,
) -> None:
    resultado, enviados = _enviar_respuesta(tmp_path / "apagado", prendido=False)
    assert resultado == {"status": "blocked", "reason": "invalid_trigger_message"}
    assert enviados == []

    resultado, enviados = _enviar_respuesta(tmp_path / "prendido", prendido=True)
    assert resultado == {"status": "sent", "message_id": IMAGEN_6 + 1}
    assert [cuerpo["content"] for cuerpo in enviados] == ["¿Qué me mandaste?"]


def test_a_photo_and_a_text_batched_together_get_one_answer(tmp_path: Path) -> None:
    """Con el debounce, una foto y un texto juntos son un solo turno.

    Sin el flag, la foto no tiene texto, la normalizacion la saltea y el turno
    falla con ``batched_messages_not_in_canonical_history``
    (``CanonicalHistoryIncompleteError``, reintentable): el lote se queda sin
    respuesta (medido aca, 03/11). Con el flag, la foto entra con su texto de
    sistema y el agente ve los dos mensajes.
    """
    import time

    imagen = _webhook_imagen()
    imagen["id"] = 2471
    imagen["conversation"]["messages"][0]["id"] = 2471
    texto = _webhook_imagen()
    texto["content"] = "Esto es lo que me sale"
    texto["attachments"] = []
    texto["conversation"]["messages"][0]["attachments"] = []
    texto["conversation"]["messages"][0]["content"] = "Esto es lo que me sale"
    shadow = StubShadowProcessor({"decision": "reply", "reply": "Ya veo."})
    chatwoot = StubChatwootClient(messages=[_mensaje_2472(imagen), _mensaje_2472(texto)])
    app = create_app(
        _settings(
            tmp_path,
            inbound_attachment_without_text_enabled=True,
            chatwoot_inbound_debounce_seconds=5,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=StubInboundCommercialSupabase(),  # type: ignore[arg-type]
    )
    for webhook, delivery in ((imagen, "lote-a"), (texto, "lote-b")):
        raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
        response = _post(
            app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery=delivery)
        )
        assert response.status_code == 202
    app.state.chatwoot_worker._clock = lambda: time.time() + 60

    asyncio.run(app.state.chatwoot_worker.run_once())

    assert len(shadow.calls) == 1
    prospect = [m["text"] for m in shadow.calls[0][1]["messages"] if m["actor"] == "prospect"]
    assert prospect[0].startswith("[La persona mandó una imagen sin texto.")
    assert prospect[-1] == "Esto es lo que me sale"
    assert len(chatwoot.reply_calls) == 1
