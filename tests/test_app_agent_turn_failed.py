"""Un turno del agente que falla deriva a una persona (E-175, G1 de Maribel).

El 28/10/2026 los dos turnos de la conversacion 9 de Maribel terminaron en
``invalid_agent_output``: el modelo contesto en prosa (con la propuesta al
final en dos de las cuatro respuestas) y ``process_chatwoot_work`` salio por
``completed_proposal is None`` sin contestar, sin derivar y sin log. La persona
quedo horas sin respuesta. Las respuestas del agente son los fixtures
``hermes_glm_prose_*`` (la forma medida, con texto sintetico). La conversacion
y el mensaje de la persona son de prueba: no hay texto ni datos de nadie.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest

from bridge.app import Settings, create_app
from bridge.hermes import HermesShadowProcessor
from test_webhook import (
    StubChatwootClient,
    StubInboundCommercialSupabase,
    StubShadowProcessor,
    _post,
    _signed_headers,
)


SECRET = "webhook-secret"
JID = "12025550123@s.whatsapp.net"
CONVERSATION_ID = 909
MESSAGE_ID = 9901
# Mensaje de prueba, no de una persona.
CONTENT = "Hola, quiero pagar el programa"
FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": SECRET,
        "allowed_jid": JID,
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


def _webhook() -> dict[str, object]:
    return {
        "event": "message_created",
        "id": MESSAGE_ID,
        "content": CONTENT,
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 9},
        "conversation": {
            "id": CONVERSATION_ID,
            "inbox_id": 9,
            "contact_inbox": {"source_id": JID},
        },
    }


def _history() -> list[dict[str, object]]:
    return [
        {
            "id": MESSAGE_ID,
            "created_at": 1793158783,
            "message_type": 0,
            "private": False,
            "content": CONTENT,
            "sender": {"type": "contact", "id": 20},
        }
    ]


class _Hermes:
    """El api_server de Hermes: contesta cada intento con el texto que toca."""

    def __init__(self, answers: list[str] | None = None, status: int = 200) -> None:
        self.answers = list(answers or [])
        self.status = status
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "down"})
        return httpx.Response(
            200,
            json={
                "model": "z-ai/glm-5.3-flash",
                "choices": [{"message": {"content": self.answers.pop(0)}}],
            },
        )


def _processor(tmp_path: Path, hermes: _Hermes) -> HermesShadowProcessor:
    return HermesShadowProcessor(
        base_url="https://hermes.example.test/v1",
        api_key="test-hermes-key",
        model_name="agente-comercial",
        shadow_dir=tmp_path / "shadow",
        transport=httpx.MockTransport(hermes),
    )


def _process(
    tmp_path: Path,
    shadow: object,
    **overrides: object,
) -> tuple[StubChatwootClient, StubInboundCommercialSupabase]:
    chatwoot = StubChatwootClient(messages=_history())
    supabase = StubInboundCommercialSupabase()
    app = create_app(
        _settings(tmp_path, **overrides),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,  # type: ignore[arg-type]
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    raw_body = json.dumps(_webhook(), separators=(",", ":")).encode("utf-8")
    response = _post(
        app, raw_body, _signed_headers(raw_body, secret=SECRET, delivery="turno-9")
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    return chatwoot, supabase


def _prose_only() -> str:
    content = _fixture("hermes_glm_prose_only_maribel_20261028.json")["content"]
    assert isinstance(content, str)
    return content


def _without_reply() -> str:
    content = _fixture(
        "hermes_glm_prose_then_json_without_reply_maribel_20261028.json"
    )["content"]
    assert isinstance(content, str)
    return content


def test_a_failed_turn_goes_to_a_person_instead_of_silence(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """La forma de la conversacion 9: prosa pura y despues un objeto sin reply."""
    hermes = _Hermes([_prose_only(), _without_reply()])

    with caplog.at_level(logging.WARNING):
        chatwoot, supabase = _process(tmp_path, _processor(tmp_path, hermes))

    assert hermes.requests == 2  # los dos intentos de siempre (D3)
    assert chatwoot.reply_calls == []  # la prosa nunca se manda
    assert len(supabase.handoff_calls) == 1
    assert supabase.handoff_calls[0]["detail_reason_code"] == "agent_turn_failed"
    assert chatwoot.calls == [(CONVERSATION_ID, "automation_paused")]
    assert (
        "chatwoot_inbound_agent_turn_failed reason=invalid_agent_output "
        f"conversation={CONVERSATION_ID}"
    ) in caplog.text


def test_hermes_down_also_goes_to_a_person(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    hermes = _Hermes(status=503)

    with caplog.at_level(logging.WARNING):
        chatwoot, supabase = _process(tmp_path, _processor(tmp_path, hermes))

    assert hermes.requests == 1
    assert chatwoot.reply_calls == []
    assert [call["detail_reason_code"] for call in supabase.handoff_calls] == [
        "agent_turn_failed"
    ]
    assert "reason=hermes_unavailable" in caplog.text


def test_a_prose_then_json_turn_is_answered_and_not_handed_off(
    tmp_path: Path,
) -> None:
    """La forma de la respuesta 476 de ATT1: el rescate salva el turno."""
    responses = _fixture("hermes_glm_prose_then_json_att1_20261029.json")[
        "responses"
    ]
    assert isinstance(responses, list)
    content = responses[0]["content"]
    hermes = _Hermes([content])

    chatwoot, supabase = _process(tmp_path, _processor(tmp_path, hermes))

    assert hermes.requests == 1
    assert supabase.handoff_calls == []
    # Lo que sale es el reply del objeto, nunca el texto entero del modelo.
    expected_reply = json.loads(content[content.index("{") :], strict=False)["reply"]
    assert [call["content"] for call in chatwoot.reply_calls] == [expected_reply]
    assert chatwoot.reply_calls[0]["conversation_id"] == CONVERSATION_ID


class _StoredFailure(StubShadowProcessor):
    """Un resultado `failed` ya guardado, como el de la reentrada del monitor.

    El monitor de trabadas reentra con el mismo delivery_id; el `failed` es
    terminal, asi que el agente no vuelve a correr. Antes de E-175 esa
    reentrada tampoco hacia nada: la persona seguia esperando.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(None)
        self.reason = reason

    def has_result(self, *, delivery_id: str) -> bool:
        return True

    def get_failure_reason(self, *, delivery_id: str) -> str | None:
        return self.reason


def test_a_stored_failed_result_hands_off_without_running_the_agent(
    tmp_path: Path,
) -> None:
    shadow = _StoredFailure("invalid_agent_output")

    chatwoot, supabase = _process(tmp_path, shadow)

    assert shadow.calls == []
    assert chatwoot.reply_calls == []
    assert [call["detail_reason_code"] for call in supabase.handoff_calls] == [
        "agent_turn_failed"
    ]


def test_a_failure_of_the_installation_does_not_hand_off(tmp_path: Path) -> None:
    """`chatwoot_history_not_configured` no es una falla del agente."""
    shadow = _StoredFailure("chatwoot_history_not_configured")

    chatwoot, supabase = _process(tmp_path, shadow)

    assert chatwoot.reply_calls == []
    assert supabase.handoff_calls == []


def test_without_handoff_admission_it_only_leaves_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """D1: cuelga de HUMAN_HANDOFF_ADMISSION_ENABLED, como el audio."""
    hermes = _Hermes([_prose_only(), _prose_only()])

    with caplog.at_level(logging.WARNING):
        chatwoot, supabase = _process(
            tmp_path,
            _processor(tmp_path, hermes),
            human_handoff_admission_enabled=False,
            human_handoff_projection_enabled=False,
            handoff_projection_policy_key=None,
            handoff_projection_policy_version=None,
            human_handoff_projection_worker_id=None,
        )

    assert chatwoot.reply_calls == []
    assert supabase.handoff_calls == []
    assert "chatwoot_inbound_agent_turn_failed reason=invalid_agent_output" in (
        caplog.text
    )
    assert f"chatwoot_agent_turn_failed_handoff_unavailable message={MESSAGE_ID}" in (
        caplog.text
    )


def test_a_processor_without_the_reader_behaves_as_before(tmp_path: Path) -> None:
    """Un doble sin get_failure_reason (y sin propuesta) sale como antes."""
    shadow = StubShadowProcessor(None)

    chatwoot, supabase = _process(tmp_path, shadow)

    assert len(shadow.calls) == 1
    assert chatwoot.reply_calls == []
    assert supabase.handoff_calls == []
