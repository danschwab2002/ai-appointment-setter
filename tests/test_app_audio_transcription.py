"""Un audio entrante de punta a punta: webhook, worker, agente o derivacion.

Parte del webhook real de la conversacion 200 (28/09/2026), que llego con
``content: null`` y un adjunto de audio y se quedo sin respuesta: el worker
salia en ``_shadow_context`` sin responder, sin derivar y sin loguear.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path

import httpx
import pytest

from bridge.app import Settings, create_app
from bridge.audio_transcription import (
    AUDIO_TRANSCRIPT_PREFIX,
    DEFAULT_TRANSCRIPTION_MODELS,
    AudioTranscriber,
)
from test_audio_transcription import MEDIA_HOST, _Red, _webhook_200
from test_webhook import (
    StubChatwootClient,
    StubInboundCommercialSupabase,
    StubShadowProcessor,
    _post,
    _signed_headers,
)


SECRET = "webhook-secret"
JID_DEL_LEAD_200 = "5210000000200@s.whatsapp.net"


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


def _historial_200() -> list[dict[str, object]]:
    # El mismo mensaje 2472 tal como lo trae la API de mensajes.
    return [copy.deepcopy(_webhook_200()["conversation"]["messages"][0])]


def _procesar(
    tmp_path: Path,
    *,
    red: _Red | None,
    proposal: dict[str, object] | None = None,
) -> tuple[StubShadowProcessor, StubChatwootClient, StubInboundCommercialSupabase]:
    shadow = StubShadowProcessor(
        proposal or {"decision": "reply", "reply": "Te leemos, Laura."}
    )
    chatwoot = StubChatwootClient(messages=_historial_200())
    supabase = StubInboundCommercialSupabase()
    transcriber = (
        AudioTranscriber(
            api_key="or-test-key",
            models=DEFAULT_TRANSCRIPTION_MODELS,
            media_host=MEDIA_HOST,
            cache_dir=tmp_path / "transcripciones",
            transport=httpx.MockTransport(red),
        )
        if red is not None
        else None
    )
    app = create_app(
        _settings(
            tmp_path,
            chatwoot_audio_transcription_enabled=red is not None,
            openrouter_api_key="or-test-key" if red is not None else None,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
        audio_transcriber=transcriber,
    )
    raw_body = json.dumps(_webhook_200(), separators=(",", ":")).encode("utf-8")
    response = _post(
        app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="audio-200")
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return shadow, chatwoot, supabase


def test_the_agent_reads_the_transcribed_audio_and_replies(tmp_path: Path) -> None:
    shadow, chatwoot, supabase = _procesar(tmp_path, red=_Red())

    assert len(shadow.calls) == 1
    _, context = shadow.calls[0]
    prospect = [m for m in context["messages"] if m["actor"] == "prospect"]
    assert prospect[-1]["text"].startswith(AUDIO_TRANSCRIPT_PREFIX)
    assert len(chatwoot.reply_calls) == 1
    assert supabase.handoff_calls == []


def test_when_every_model_fails_the_conversation_goes_to_a_person(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caida = httpx.Response(503, json={"error": {"message": "down"}})
    red = _Red(fallas={model: caida for model in DEFAULT_TRANSCRIPTION_MODELS})

    with caplog.at_level(logging.WARNING):
        shadow, chatwoot, supabase = _procesar(tmp_path, red=red)

    assert shadow.calls == []  # el agente no contesta lo que no leyo
    assert chatwoot.reply_calls == []
    assert len(supabase.handoff_calls) == 1
    assert supabase.handoff_calls[0]["detail_reason_code"] == (
        "audio_transcription_failed"
    )
    assert chatwoot.calls == [(200, "automation_paused")]
    assert "chatwoot_audio_transcription_failed message=2472" in caplog.text


def test_with_transcription_off_the_audio_is_logged_instead_of_vanishing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        shadow, chatwoot, supabase = _procesar(tmp_path, red=None)

    assert shadow.calls == []
    assert chatwoot.reply_calls == []
    assert supabase.handoff_calls == []
    assert "chatwoot_inbound_without_text_ignored message=2472" in caplog.text
