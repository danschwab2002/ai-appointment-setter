"""HTTP tests for the provisional pre-checkout receiver."""

import asyncio
import copy
import json
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Iterator

import httpx
import pytest
import uvicorn

from bridge.app import Settings, create_app

FORM_TOKEN = "isolated-precheckout-test-token"
EMULATED_PAYLOAD: dict[str, object] = {
    "id": "form-submit-0001",
    "event": "PRECHECKOUT_FORM_SUBMITTED",
    "version": "1.0.0-emulated",
    "created_at": "replaced-by-test",
    "lead": {
        "full_name": "Lead de Prueba",
        "phone_e164": "+1" + "202" + "555" + "0123",
    },
}


class _FakeSupabase:
    def __init__(self, outcome: str = "inserted") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def admit_precheckout_form_submission(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            outcome=self.outcome,
            submission_id="bfc778e7-5c9f-45e6-a910-651f92312157",
            purchase_intent_id="1f581f3a-c469-45da-8208-9483d1b26f0b",
        )


class _TimerFakeSupabase(_FakeSupabase):
    def __init__(self) -> None:
        super().__init__()
        self.timer_polled = threading.Event()
        self.timer_calls: list[dict[str, object]] = []

    async def list_due_hotmart_abandonment_reevaluations(
        self, **kwargs: object
    ) -> list[str]:
        self.timer_calls.append(kwargs)
        self.timer_polled.set()
        return []


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "webhook_secret": "unused",
        "allowed_jid": "12025550123@s.whatsapp.net",
        "capture_dir": "/tmp/unused-captures",
        "max_age_seconds": 300,
        "precheckout_form_enabled": True,
        "precheckout_form_token": FORM_TOKEN,
        "precheckout_max_age_seconds": 300,
        "precheckout_test_mode_enabled": True,
        "precheckout_test_phone_e164": "+12025550123",
        "precheckout_offer_ref": "bxjge6zq",
        "precheckout_consent_copy_version": "form-screenshot-2026-08-14",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _payload() -> dict[str, object]:
    payload = copy.deepcopy(EMULATED_PAYLOAD)
    payload["created_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return payload


def _post(app: object, payload: object, token: str = FORM_TOKEN) -> httpx.Response:
    return _post_raw(app, json.dumps(payload).encode(), token=token)


def _post_raw(app: object, body: bytes, token: str = FORM_TOKEN) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/webhooks/precheckout",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-PRECHECKOUT-TOKEN": token,
                },
            )

    return asyncio.run(send())


@contextmanager
def _real_http_server(app: object) -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = int(sock.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on")  # type: ignore[arg-type]
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def test_valid_emulated_submission_is_durably_admitted_without_effect() -> None:
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, _payload())

    assert response.status_code == 202
    assert response.json() == {
        "status": "received",
        "submission_id": "form-submit-0001",
        "purchase_intent_id": "1f581f3a-c469-45da-8208-9483d1b26f0b",
        "activation_authorized": False,
        "test_only": True,
        "generalizable": False,
    }
    assert len(supabase.calls) == 1


def test_minimal_submission_crosses_real_tcp_and_asgi_lifespan() -> None:
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    with _real_http_server(app) as base_url:
        response = httpx.post(
            f"{base_url}/webhooks/precheckout",
            json=_payload(),
            headers={"X-PRECHECKOUT-TOKEN": FORM_TOKEN},
            timeout=3,
        )

    assert response.status_code == 202
    assert response.json()["status"] == "received"
    assert response.json()["activation_authorized"] is False
    assert len(supabase.calls) == 1


def test_real_tcp_lifespan_polls_precheckout_timer_default_effect_free() -> None:
    supabase = _TimerFakeSupabase()
    app = create_app(
        _settings(
            precheckout_form_enabled=False,
            hotmart_abandonment_timer_worker_enabled=True,
            hotmart_abandonment_timer_poll_interval_seconds=0.01,
            precheckout_delayed_first_touch_enabled=True,
            chatwoot_base_url="https://chatwoot.example.invalid",
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            chatwoot_control_api_access_token="test-control-token",
            chatwoot_pause_macro_id=11,
            agent_bot_id=7,
            chatwoot_agent_bot_access_token="test-agent-bot-token",
            pilot_channel_provider="waba",
        ),
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    with _real_http_server(app) as base_url:
        response = httpx.get(f"{base_url}/health", timeout=3)
        assert supabase.timer_polled.wait(timeout=3)

    assert response.status_code == 200
    assert supabase.timer_calls
    assert supabase.timer_calls[0]["include_precheckout"] is True
    assert supabase.calls == []


def test_receiver_is_disabled_by_default() -> None:
    supabase = _FakeSupabase()
    app = create_app(
        _settings(precheckout_form_enabled=False),
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(app, _payload())

    assert response.status_code == 503
    assert response.json()["detail"] == "precheckout_not_enabled"
    assert supabase.calls == []


def test_provisional_receiver_cannot_be_enabled_from_deployment_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "PRECHECKOUT_FORM_ENABLED": "true",
        "PRECHECKOUT_FORM_TOKEN": FORM_TOKEN,
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="provisional contract cannot be enabled"):
        Settings.from_env()


def test_provisional_receiver_can_be_enabled_only_for_exact_test_jid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "PRECHECKOUT_FORM_ENABLED": "true",
        "PRECHECKOUT_FORM_TOKEN": FORM_TOKEN,
        "PRECHECKOUT_TEST_MODE_ENABLED": "true",
        "PRECHECKOUT_TEST_PHONE_E164": "+12025550123",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    settings = Settings.from_env()

    assert settings.precheckout_form_enabled is True
    assert settings.precheckout_test_mode_enabled is True
    assert settings.precheckout_test_phone_e164 == "+12025550123"


def test_receiver_rejects_phone_outside_test_allowlist_before_persistence() -> None:
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]
    payload = _payload()
    assert isinstance(payload["lead"], dict)
    payload["lead"]["phone_e164"] = "+12025550124"

    response = _post(app, payload)

    assert response.status_code == 403
    assert response.json()["detail"] == "precheckout_test_phone_not_allowed"
    assert supabase.calls == []


def test_receiver_rejects_injected_settings_that_disagree_with_allowed_jid() -> None:
    supabase = _FakeSupabase()
    app = create_app(
        _settings(precheckout_test_phone_e164="+12025550124"),
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    payload = _payload()
    assert isinstance(payload["lead"], dict)
    payload["lead"]["phone_e164"] = "+12025550124"

    response = _post(app, payload)

    assert response.status_code == 403
    assert response.json()["detail"] == "precheckout_test_phone_not_allowed"
    assert supabase.calls == []


def test_receiver_rejects_wrong_token_before_persistence() -> None:
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, _payload(), token="wrong-token")

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_token"
    assert supabase.calls == []


def test_receiver_returns_400_for_malformed_utf8() -> None:
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post_raw(app, b"\xff")

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_json"
    assert supabase.calls == []


def test_receiver_rejects_caller_supplied_fields_outside_minimal_contract() -> None:
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]
    payload = _payload()
    payload["commerce"] = {"offer_ref": "another-offer"}

    response = _post(app, payload)

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_precheckout_payload"
    assert supabase.calls == []


def test_receiver_rejects_payload_above_64_kib_before_persistence() -> None:
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, {"padding": "x" * (64 * 1024)})

    assert response.status_code == 413
    assert response.json()["detail"] == "precheckout_webhook_body_too_large"
    assert supabase.calls == []


def test_semantic_conflict_is_not_reported_as_duplicate() -> None:
    supabase = _FakeSupabase(outcome="semantic_conflict")
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, _payload())

    assert response.status_code == 200
    assert response.json()["status"] == "conflict"
    assert response.json()["activation_authorized"] is False