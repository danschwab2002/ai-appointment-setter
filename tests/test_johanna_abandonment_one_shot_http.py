"""Controlled one-shot WABA template endpoint for Johanna abandonment."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from bridge.app import Settings, create_app
from bridge.messaging import FirstTouchResult

TOKEN = "isolated-johanna-one-shot-token-v1"
INTENT_ID = "1f581f3a-c469-45da-8208-9483d1b26f0b"
COMMAND_ID = "bfc778e7-5c9f-45e6-a910-651f92312157"
ALLOWED_PHONE = "".join(("120", "2555", "0123"))


class _FakeSupabase:
    def __init__(self, *, outcome: str = "started", status: str = "request_started") -> None:
        self.outcome = outcome
        self.status = status
        self.begin_calls: list[dict[str, object]] = []
        self.finish_calls: list[dict[str, object]] = []

    async def begin_johanna_abandonment_one_shot(self, **kwargs: object) -> object:
        self.begin_calls.append(kwargs)
        return SimpleNamespace(
            outcome=self.outcome,
            command_id=COMMAND_ID,
            command_status=self.status,
            target_phone=ALLOWED_PHONE,
            buyer_name="Lead de Prueba",
            buyer_email="lead@example.com",
            product_name="Liberate De La Ansiedad",
            template_name="johanna_carrito_abandonado_01",
            template_language="es_EC",
            template_category="MARKETING",
            copy_version="johanna-abandonment-one-shot-v1",
        )

    async def finish_johanna_abandonment_one_shot(self, **kwargs: object) -> object:
        self.finish_calls.append(kwargs)
        return SimpleNamespace(
            command_id=COMMAND_ID,
            command_status=str(kwargs["outcome"]),
        )


class _FakeSender:
    def __init__(self, result: FirstTouchResult | None = None) -> None:
        self.result = result or FirstTouchResult("sent", 321, 654)
        self.calls: list[dict[str, object]] = []

    async def send_first_touch(self, **kwargs: object) -> FirstTouchResult:
        self.calls.append(kwargs)
        return self.result


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": "unused",
        "allowed_jid": f"{ALLOWED_PHONE}@s.whatsapp.net",
        "capture_dir": "/tmp/unused-captures",
        "max_age_seconds": 300,
        "lead_precheckout_enabled": True,
        "lead_precheckout_secret": "lead-secret",
        "johanna_abandonment_one_shot_enabled": True,
        "johanna_abandonment_one_shot_token": TOKEN,
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 9,
        "pilot_scope_key": "johanna-abandonment-template-e2e",
        "pilot_scope_version": 1,
        "pilot_tenant_key": "psicologajohanna",
        "pilot_channel_provider": "waba",
        "pilot_channel_account_ref": "chatwoot-inbox:9",
        "waba_first_touch_template_name": "johanna_carrito_abandonado_01",
        "waba_followup_template_name": None,
        "waba_template_language": "es_EC",
        "waba_template_category": "MARKETING",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _post(app: object, *, token: str = TOKEN) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/internal/johanna/abandonment-one-shot",
                content=json.dumps(
                    {
                        "command_key": "johanna-abandonment-real-e2e-001",
                        "purchase_intent_id": INTENT_ID,
                    }
                ).encode(),
                headers={"X-JOHANNA-ONE-SHOT-TOKEN": token},
            )

    return asyncio.run(send())


def test_johanna_one_shot_is_disabled_by_default() -> None:
    supabase = _FakeSupabase()
    sender = _FakeSender()
    app = create_app(
        _settings(johanna_abandonment_one_shot_enabled=False),
        supabase_client=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
    )

    response = _post(app)

    assert response.status_code == 503
    assert response.json()["detail"] == "johanna_abandonment_one_shot_not_enabled"
    assert supabase.begin_calls == []
    assert sender.calls == []


def test_johanna_one_shot_rejects_weak_operator_token_at_startup() -> None:
    with pytest.raises(
        ValueError,
        match="JOHANNA_ABANDONMENT_ONE_SHOT_TOKEN must contain at least 32 characters",
    ):
        create_app(
            _settings(johanna_abandonment_one_shot_token="weak"),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
            message_sender=_FakeSender(),  # type: ignore[arg-type]
        )


def test_johanna_one_shot_provisions_and_sends_only_approved_template() -> None:
    supabase = _FakeSupabase()
    sender = _FakeSender()
    app = create_app(
        _settings(),
        supabase_client=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
    )

    response = _post(app)

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
            "command_key": "johanna-abandonment-real-e2e-001",
            "purchase_intent_id": INTENT_ID,
            "allowed_external_user_id": ALLOWED_PHONE,
            "chatwoot_account_id": 1,
            "chatwoot_inbox_id": 9,
            "scope_key": "johanna-abandonment-template-e2e",
            "scope_version": 1,
            "expected_generation": 0,
        }
    ]
    assert sender.calls == [
        {
            "phone": ALLOWED_PHONE,
            "buyer_name": "Lead de Prueba",
            "buyer_email": "lead@example.com",
            "product_name": "Liberate De La Ansiedad",
            "content": "Recuperación supervisada de carrito de Libre de Ansiedad.",
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


def test_johanna_one_shot_accepted_replay_never_sends_again() -> None:
    supabase = _FakeSupabase(outcome="replay", status="accepted_by_chatwoot")
    sender = _FakeSender()
    app = create_app(
        _settings(),
        supabase_client=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
    )

    response = _post(app)

    assert response.status_code == 200
    assert response.json()["message_count"] == 1
    assert sender.calls == []
    assert supabase.finish_calls == []


def test_johanna_one_shot_ambiguous_sender_failure_is_not_retried() -> None:
    supabase = _FakeSupabase()
    sender = _FakeSender(FirstTouchResult("failed", 321, None, "chatwoot_http_error"))
    app = create_app(
        _settings(),
        supabase_client=supabase,  # type: ignore[arg-type]
        message_sender=sender,  # type: ignore[arg-type]
    )

    response = _post(app)

    assert response.status_code == 502
    assert response.json()["detail"] == "johanna_abandonment_one_shot_failed"
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


@pytest.mark.parametrize(
    "overrides",
    [
        {"lead_precheckout_enabled": False},
        {"pilot_channel_provider": "evolution"},
        {"pilot_scope_key": "wrong-scope"},
        {"waba_first_touch_template_name": "wrong_template"},
        {"waba_followup_template_name": "johanna_compra_fallida_01"},
        {"waba_template_language": "es_AR"},
    ],
)
def test_johanna_one_shot_factory_fails_closed_on_scope_or_template_drift(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="Johanna abandonment one-shot requires"):
        create_app(
            _settings(**overrides),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
            message_sender=_FakeSender(),  # type: ignore[arg-type]
        )
