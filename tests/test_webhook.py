import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx
import pytest

from bridge.app import Settings, build_app, create_app


class StubChatwootClient:
    def __init__(
        self,
        *,
        changed: bool = True,
        fail: bool = False,
        messages: list[dict[str, object]] | None = None,
    ) -> None:
        self.changed = changed
        self.fail = fail
        self.calls: list[tuple[int, str]] = []
        self.messages = messages or []
        self.history_calls: list[tuple[int, int]] = []
        self.reply_calls: list[dict[str, object]] = []

    async def get_conversation_messages(
        self, *, conversation_id: int, limit: int = 20
    ) -> list[dict[str, object]]:
        self.history_calls.append((conversation_id, limit))
        if self.fail:
            request = httpx.Request("GET", "https://chatwoot.example.test")
            raise httpx.ConnectError("unavailable", request=request)
        return self.messages[-limit:]

    async def ensure_conversation_label(
        self, *, conversation_id: int, label: str
    ) -> bool:
        self.calls.append((conversation_id, label))
        if self.fail:
            request = httpx.Request("GET", "https://chatwoot.example.test")
            raise httpx.ConnectError("unavailable", request=request)
        return self.changed

    async def send_agent_bot_reply(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        delivery_id: str,
        content: str,
    ) -> dict[str, object]:
        self.reply_calls.append(
            {
                "conversation_id": conversation_id,
                "trigger_message_id": trigger_message_id,
                "delivery_id": delivery_id,
                "content": content,
            }
        )
        return {"status": "sent", "message_id": 900}


class StubShadowProcessor:
    def __init__(self, proposal: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failures: list[tuple[str, str]] = []
        self.completed_delivery_ids: set[str] = set()
        self.proposal = proposal

    async def run(
        self, *, delivery_id: str, context: dict[str, object]
    ) -> None:
        self.calls.append((delivery_id, context))
        self.completed_delivery_ids.add(delivery_id)

    def record_failure(self, *, delivery_id: str, reason: str) -> None:
        self.failures.append((delivery_id, reason))
        self.completed_delivery_ids.add(delivery_id)

    def has_result(self, *, delivery_id: str) -> bool:
        return delivery_id in self.completed_delivery_ids

    def get_completed_proposal(
        self, *, delivery_id: str
    ) -> dict[str, object] | None:
        if delivery_id not in self.completed_delivery_ids:
            return None
        return self.proposal


def _signed_headers(
    raw_body: bytes,
    *,
    secret: str,
    delivery: str,
    timestamp: int | None = None,
) -> dict[str, str]:
    timestamp_text = str(timestamp if timestamp is not None else int(time.time()))
    signed = timestamp_text.encode("ascii") + b"." + raw_body
    signature = "sha256=" + hmac.new(
        secret.encode("utf-8"), signed, hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Chatwoot-Signature": signature,
        "X-Chatwoot-Timestamp": timestamp_text,
        "X-Chatwoot-Delivery": delivery,
    }


def _post(app: object, raw_body: bytes, headers: dict[str, str]) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/webhooks/chatwoot", content=raw_body, headers=headers
            )

    return asyncio.run(send())


def test_captures_a_signed_allowed_incoming_message(tmp_path: Path) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 789,
        "content": "Hola",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
        )
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="delivery-1"),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "captured",
        "delivery_id": "delivery-1",
    }
    captures = list(tmp_path.glob("*.json"))
    assert len(captures) == 1
    assert json.loads(captures[0].read_text()) == payload


def test_processes_a_normalized_shadow_evaluation_for_an_allowed_message(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 789,
        "content": "Hola",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    shadow = StubShadowProcessor()
    chatwoot = StubChatwootClient(
        messages=[
            {
                "id": 789,
                "message_type": 0,
                "private": False,
                "content": "Hola",
                "sender": {"type": "contact", "id": 20},
            }
        ]
    )
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="shadow-delivery"),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "shadow_processed",
        "delivery_id": "shadow-delivery",
    }
    assert shadow.calls == [
        (
            "shadow-delivery",
            {
                "conversation_ref": "123",
                "human_handoff_confirmed": False,
                "known_fields": {
                    "person_name": None,
                    "location": None,
                    "role": None,
                    "company_name": None,
                    "company_size": None,
                    "business_model": None,
                    "company_operational": None,
                    "can_invest_in_education": None,
                },
                "messages": [
                    {
                        "actor": "prospect",
                        "text": "Hola",
                    }
                ],
            },
        )
    ]
    assert chatwoot.reply_calls == []


def test_sends_the_validated_agent_reply_for_an_allowed_message(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    proposal: dict[str, object] = {
        "decision": "ask_question",
        "qualification_status": "in_progress",
        "reason_code": "need_person_name",
        "reply": "¡Hola! Soy el asistente virtual de Dan. ¿Cómo te llamás?",
        "captured_fields": {
            "person_name": None,
            "location": None,
            "role": None,
            "company_name": None,
            "company_size": None,
            "business_model": None,
            "company_operational": None,
            "can_invest_in_education": None,
        },
        "missing_fields": ["person_name"],
    }
    payload = {
        "event": "message_created",
        "id": 789,
        "content": "Hola",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    shadow = StubShadowProcessor(proposal)
    chatwoot = StubChatwootClient(
        messages=[
            {
                "id": 789,
                "message_type": 0,
                "private": False,
                "content": "Hola",
                "sender": {"type": "contact", "id": 20},
            }
        ]
    )
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            automated_replies_enabled=True,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="reply-delivery"),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "reply_sent",
        "delivery_id": "reply-delivery",
        "message_id": 900,
    }
    assert chatwoot.reply_calls == [
        {
            "conversation_id": 123,
            "trigger_message_id": 789,
            "delivery_id": "reply-delivery",
            "content": "¡Hola! Soy el asistente virtual de Dan. ¿Cómo te llamás?",
        }
    ]


def test_retries_shadow_processing_for_a_capture_without_a_terminal_result(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 300,
        "message_type": "incoming",
        "content": "Hola",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    headers = _signed_headers(raw_body, secret=secret, delivery="durable-shadow")
    settings = Settings(
        secret,
        "12025550123@s.whatsapp.net",
        tmp_path,
        300,
        agent_bot_id=1,
    )

    capture_response = _post(create_app(settings), raw_body, headers)

    shadow = StubShadowProcessor()
    chatwoot = StubChatwootClient(
        messages=[
            {
                "id": 300,
                "message_type": 0,
                "private": False,
                "content": "Hola",
                "sender": {"type": "contact", "id": 20},
            }
        ]
    )
    retry_response = _post(
        create_app(
            settings,
            chatwoot_client=chatwoot,
            shadow_processor=shadow,
        ),
        raw_body,
        headers,
    )

    assert capture_response.status_code == 202
    assert retry_response.status_code == 202
    assert retry_response.json() == {
        "status": "shadow_processed",
        "delivery_id": "durable-shadow",
    }
    assert shadow.calls
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_records_failure_without_canonical_chatwoot_context(tmp_path: Path) -> None:
    secret = "webhook-secret"
    shadow = StubShadowProcessor()
    payload = {
        "event": "message_created",
        "id": 301,
        "message_type": "incoming",
        "content": "Hola",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    app = create_app(
        Settings(secret, "12025550123@s.whatsapp.net", tmp_path, 300),
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="missing-canonical"),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "shadow_processed"
    assert shadow.calls == []
    assert shadow.failures == [
        ("missing-canonical", "chatwoot_history_not_configured")
    ]


def test_build_app_wires_the_shadow_processor_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "webhook-secret"
    settings = Settings(
        webhook_secret=secret,
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        agent_bot_id=1,
        chatwoot_base_url="https://chatwoot.example.test",
        chatwoot_account_id=1,
        chatwoot_control_api_access_token="test-control-token",
        chatwoot_pause_macro_id=1,
        hermes_shadow_enabled=True,
        hermes_api_base_url="https://hermes.example.test/v1",
        hermes_api_key="test-hermes-key",
        hermes_model_name="agente-comercial",
        shadow_dir=tmp_path / "shadow",
    )
    created_with: list[dict[str, object]] = []
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeHermesShadowProcessor:
        def __init__(self, **kwargs: object) -> None:
            created_with.append(kwargs)

        async def run(
            self, *, delivery_id: str, context: dict[str, object]
        ) -> None:
            calls.append((delivery_id, context))

        def has_result(self, *, delivery_id: str) -> bool:
            return False

        def get_completed_proposal(
            self, *, delivery_id: str
        ) -> dict[str, object] | None:
            return None

        def record_failure(self, *, delivery_id: str, reason: str) -> None:
            raise AssertionError(reason)

    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        "bridge.app.HermesShadowProcessor",
        FakeHermesShadowProcessor,
    )
    monkeypatch.setattr(
        "bridge.app.ChatwootClient",
        lambda **kwargs: StubChatwootClient(
            messages=[
                {
                    "id": 790,
                    "message_type": 0,
                    "private": False,
                    "content": "Hola",
                    "sender": {"type": "contact", "id": 20},
                }
            ]
        ),
    )
    app = build_app()
    payload = {
        "event": "message_created",
        "id": 790,
        "content": "Hola",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": 124,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="factory-shadow"),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "shadow_processed"
    assert created_with == [
        {
            "base_url": "https://hermes.example.test/v1",
            "api_key": "test-hermes-key",
            "model_name": "agente-comercial",
            "shadow_dir": tmp_path / "shadow",
        }
    ]
    assert calls[0][0] == "factory-shadow"


@pytest.mark.parametrize(
    ("content", "conversation_id"),
    [
        (None, 123),
        ("Hola", None),
    ],
)
def test_does_not_queue_shadow_without_normalized_business_context(
    tmp_path: Path,
    content: object,
    conversation_id: object,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 791,
        "content": content,
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": conversation_id,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    shadow = StubShadowProcessor()
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
        ),
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="invalid-context"),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "captured"
    assert shadow.calls == []


def test_uses_canonical_chatwoot_history_for_the_shadow_context(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    history = [
        {
            "id": 10,
            "message_type": 0,
            "private": False,
            "content": "Hola",
            "sender": {"type": "contact", "id": 20},
        },
        {
            "id": 11,
            "message_type": 1,
            "private": False,
            "content": "¿Cómo te llamás?",
            "sender": {"type": "agent_bot", "id": 1},
        },
        {
            "id": 12,
            "message_type": 1,
            "private": True,
            "content": "Nota privada",
            "sender": {"type": "user", "id": 2},
        },
        {
            "id": 13,
            "message_type": 1,
            "private": False,
            "content": "Respuesta de otro bot",
            "sender": {"type": "agent_bot", "id": 99},
        },
        {
            "id": 14,
            "message_type": 0,
            "private": False,
            "content": "Juan",
            "sender": {"type": "contact", "id": 20},
        },
        {
            "id": 15,
            "message_type": 0,
            "private": False,
            "content": "Mensaje posterior",
            "sender": {"type": "contact", "id": 20},
        },
    ]
    chatwoot = StubChatwootClient(messages=history)
    shadow = StubShadowProcessor()
    payload = {
        "event": "message_created",
        "id": 14,
        "content": "Juan",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="history-shadow"),
    )

    assert response.status_code == 202
    assert chatwoot.history_calls == [(123, 20)]
    assert shadow.calls[0][1]["messages"] == [
        {"actor": "prospect", "text": "Hola"},
        {"actor": "assistant", "text": "¿Cómo te llamás?"},
        {"actor": "prospect", "text": "Juan"},
    ]


def test_records_shadow_failure_when_chatwoot_history_is_unavailable(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    chatwoot = StubChatwootClient(fail=True)
    shadow = StubShadowProcessor()
    payload = {
        "event": "message_created",
        "id": 15,
        "content": "Juan",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="history-failed"),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "shadow_processed"
    assert shadow.calls == []
    assert shadow.failures == [
        ("history-failed", "chatwoot_history_unavailable")
    ]


def test_records_shadow_failure_when_current_message_is_missing_from_history(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    chatwoot = StubChatwootClient(
        messages=[
            {
                "id": 15,
                "message_type": 0,
                "private": False,
                "content": "Mensaje posterior",
                "sender": {"type": "contact", "id": 20},
            }
        ]
    )
    shadow = StubShadowProcessor()
    payload = {
        "event": "message_created",
        "id": 14,
        "content": "Juan",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(
            raw_body,
            secret=secret,
            delivery="current-message-missing",
        ),
    )

    assert response.status_code == 202
    assert shadow.calls == []
    assert shadow.failures == [
        (
            "current-message-missing",
            "current_message_not_in_canonical_history",
        )
    ]


def test_recognizes_configured_agent_bot_without_capturing_it(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "private": False,
        "sender": {
            "id": 1,
            "type": "agent_bot",
        },
        "conversation": {
            "meta": {
                "sender": {
                    "identifier": "12025550123@s.whatsapp.net",
                }
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        )
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="agentbot-delivery"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "automation_outgoing",
    }
    assert list(tmp_path.iterdir()) == []


def test_pauses_automation_when_a_human_sends_a_public_message(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "private": False,
        "sender": {
            "id": 1,
            "type": "user",
        },
        "conversation": {
            "id": 2,
            "meta": {
                "sender": {
                    "identifier": "12025550123@s.whatsapp.net",
                }
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    chatwoot = StubChatwootClient()
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="human-delivery"),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "automation_paused",
        "reason": "human_outgoing",
        "label_status": "added",
    }
    assert chatwoot.calls == [(2, "automation_paused")]
    assert list(tmp_path.iterdir()) == []


def test_fails_closed_when_chatwoot_cannot_apply_the_pause_label(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "private": False,
        "sender": {
            "id": 1,
            "type": "user",
        },
        "conversation": {
            "id": 2,
            "meta": {
                "sender": {
                    "identifier": "12025550123@s.whatsapp.net",
                }
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    chatwoot = StubChatwootClient(fail=True)
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="failed-human-delivery"),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "chatwoot_control_unavailable"}
    assert chatwoot.calls == [(2, "automation_paused")]
    assert list(tmp_path.iterdir()) == []


def test_rejects_a_stale_webhook(tmp_path: Path) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 789,
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            }
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
        )
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(
            raw_body,
            secret=secret,
            delivery="stale-delivery",
            timestamp=int(time.time()) - 301,
        ),
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "stale_webhook"}
    assert list(tmp_path.glob("*.json")) == []


def test_treats_a_repeated_delivery_as_an_idempotent_duplicate(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 789,
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            }
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = _signed_headers(
        raw_body,
        secret=secret,
        delivery="same-delivery",
    )
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
        )
    )

    first = _post(app, raw_body, headers)
    duplicate = _post(app, raw_body, headers)

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "status": "duplicate",
        "delivery_id": "same-delivery",
    }
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_ignores_a_message_from_any_other_whatsapp_jid(tmp_path: Path) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 790,
        "content": "Este mensaje no debe activar el flujo",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": {
                "source_id": "12025550124@s.whatsapp.net",
            }
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
        )
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="other-sender"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "sender_not_allowed",
    }
    assert list(tmp_path.glob("*.json")) == []
