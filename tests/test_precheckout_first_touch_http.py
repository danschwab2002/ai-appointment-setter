"""One-shot HTTP tests for the allowlisted pre-checkout first touch."""

import asyncio
import json
from types import SimpleNamespace

import httpx

from bridge.app import Settings, create_app
from bridge.messaging import FirstTouchResult

TOKEN = "isolated-first-touch-token"
INTENT_ID = "1f581f3a-c469-45da-8208-9483d1b26f0b"
COMMAND_ID = "bfc778e7-5c9f-45e6-a910-651f92312157"
ALLOWED_PHONE = "".join(("120", "2555", "0123"))


class _FakeSupabase:
    def __init__(self, *, outcome: str = "started", status: str = "request_started") -> None:
        self.outcome = outcome
        self.status = status
        self.begin_calls: list[dict[str, object]] = []
        self.finish_calls: list[dict[str, object]] = []

    async def begin_precheckout_test_first_touch(self, **kwargs: object) -> object:
        self.begin_calls.append(kwargs)
        return SimpleNamespace(
            outcome=self.outcome,
            command_id=COMMAND_ID,
            command_status=self.status,
            target_phone=ALLOWED_PHONE,
            buyer_name="Lead de Prueba",
            chatwoot_conversation_id=321,
            template_name="libre_ansiedad_test_first_touch_v1",
            template_language="es_AR",
            template_category="MARKETING",
            copy_version="libre-ansiedad-precheckout-first-touch-v1",
        )

    async def finish_precheckout_test_first_touch(self, **kwargs: object) -> object:
        self.finish_calls.append(kwargs)
        return SimpleNamespace(
            command_id=COMMAND_ID,
            command_status=str(kwargs["outcome"]),
        )


class _FakeSender:
    def __init__(self, result: FirstTouchResult | None = None) -> None:
        self.result = result or FirstTouchResult(
            status="sent",
            conversation_id=321,
            message_id=654,
        )
        self.calls: list[dict[str, object]] = []

    async def send_first_touch_to_conversation(self, **kwargs: object) -> FirstTouchResult:
        self.calls.append(kwargs)
        return self.result


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": "unused",
        "allowed_jid": f"{ALLOWED_PHONE}@s.whatsapp.net",
        "capture_dir": "/tmp/unused-captures",
        "max_age_seconds": 300,
        "precheckout_first_touch_enabled": True,
        "precheckout_first_touch_token": TOKEN,
        "precheckout_form_enabled": True,
        "precheckout_form_token": "form-token",
        "precheckout_test_mode_enabled": True,
        "precheckout_test_phone_e164": f"+{ALLOWED_PHONE}",
        "chatwoot_inbox_id": 1,
        "chatwoot_account_id": 1,
        "pilot_channel_provider": "waba",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _post(app: object, body: bytes, *, token: str = TOKEN) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/internal/precheckout/test-first-touch",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-PRECHECKOUT-FIRST-TOUCH-TOKEN": token,
                },
            )

    return asyncio.run(send())


def _body() -> bytes:
    return json.dumps(
        {"command_key": "controlled-first-touch-001", "purchase_intent_id": INTENT_ID}
    ).encode()


def test_first_touch_is_disabled_by_default() -> None:
    supabase = _FakeSupabase()
    sender = _FakeSender()
    app = create_app(
        _settings(precheckout_first_touch_enabled=False),
        supabase_client=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
    )

    response = _post(app, _body())

    assert response.status_code == 503
    assert response.json()["detail"] == "precheckout_first_touch_not_enabled"
    assert supabase.begin_calls == []
    assert sender.calls == []


def test_first_touch_rejects_wrong_token_before_parsing_body() -> None:
    supabase = _FakeSupabase()
    sender = _FakeSender()
    app = create_app(
        _settings(),
        supabase_client=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
    )

    response = _post(app, b"not-json", token="wrong-token")

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_token"
    assert supabase.begin_calls == []
    assert sender.calls == []


def test_injected_settings_cannot_bypass_test_receiver_gates() -> None:
    supabase = _FakeSupabase()
    sender = _FakeSender()

    for overrides in (
        {"precheckout_form_enabled": False},
        {"precheckout_test_mode_enabled": False},
        {"precheckout_test_phone_e164": "+12025550124"},
    ):
        try:
            create_app(
                _settings(**overrides),
                supabase_client=supabase,  # type: ignore[arg-type]
                message_sender=sender,  # type: ignore[arg-type]
            )
        except ValueError as exc:
            assert "precheckout first touch requires" in str(exc)
        else:
            raise AssertionError("injected settings bypassed first-touch gates")


def test_first_touch_starts_once_sends_template_and_finalizes() -> None:
    supabase = _FakeSupabase()
    sender = _FakeSender()
    app = create_app(
        _settings(),
        supabase_client=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
    )

    response = _post(app, _body())

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted_by_chatwoot",
        "command_id": COMMAND_ID,
        "message_count": 1,
        "followups_allowed": 0,
        "test_only": True,
        "generalizable": False,
    }
    assert supabase.begin_calls == [
        {
            "command_key": "controlled-first-touch-001",
            "purchase_intent_id": INTENT_ID,
            "allowed_external_user_id": ALLOWED_PHONE,
            "chatwoot_account_id": 1,
            "chatwoot_inbox_id": 1,
        }
    ]
    assert sender.calls == [
        {
            "conversation_id": 321,
            "phone": ALLOWED_PHONE,
            "buyer_name": "Lead de Prueba",
            "content": "¡Hola, Lead de Prueba! Te habla el equipo de Johanna. Vimos que completaste el formulario de Libre de Ansiedad. ¿Te parece si avanzamos por acá?",
            "delivery_id": COMMAND_ID,
        }
    ]
    assert supabase.finish_calls == [
        {
            "command_id": COMMAND_ID,
            "outcome": "accepted_by_chatwoot",
            "chatwoot_conversation_id": 321,
            "chatwoot_message_id": 654,
            "failure_code": None,
        }
    ]


def test_first_touch_replay_never_sends_again() -> None:
    supabase = _FakeSupabase(outcome="replay", status="request_started")
    sender = _FakeSender()
    app = create_app(
        _settings(),
        supabase_client=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
    )

    response = _post(app, _body())

    assert response.status_code == 409
    assert response.json()["detail"] == "precheckout_first_touch_reconciliation_required"
    assert sender.calls == []
    assert supabase.finish_calls == []


def test_first_touch_failure_is_terminal_and_not_retried() -> None:
    supabase = _FakeSupabase()
    sender = _FakeSender(
        FirstTouchResult(
            status="failed",
            conversation_id=321,
            message_id=None,
            reason="chatwoot_http_error",
        )
    )
    app = create_app(
        _settings(),
        supabase_client=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
    )

    response = _post(app, _body())

    assert response.status_code == 502
    assert response.json()["detail"] == "precheckout_first_touch_failed"
    assert len(sender.calls) == 1
    assert supabase.finish_calls == [
        {
            "command_id": COMMAND_ID,
            "outcome": "delivery_unknown",
            "chatwoot_conversation_id": None,
            "chatwoot_message_id": None,
            "failure_code": "chatwoot_http_error",
        }
    ]


def test_first_touch_pre_request_block_is_definite_failure() -> None:
    supabase = _FakeSupabase()
    sender = _FakeSender(
        FirstTouchResult(
            status="blocked",
            conversation_id=None,
            message_id=None,
            reason="target_not_allowed",
        )
    )
    app = create_app(
        _settings(),
        supabase_client=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
    )

    response = _post(app, _body())

    assert response.status_code == 502
    assert len(sender.calls) == 1
    assert supabase.finish_calls == [
        {
            "command_id": COMMAND_ID,
            "outcome": "failed",
            "chatwoot_conversation_id": None,
            "chatwoot_message_id": None,
            "failure_code": "target_not_allowed",
        }
    ]
