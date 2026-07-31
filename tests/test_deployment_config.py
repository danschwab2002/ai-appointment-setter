from pathlib import Path

import pytest

from bridge.app import Settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_deployment_declares_required_chatwoot_control_variables() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()
    required = {
        "CHATWOOT_BASE_URL",
        "CHATWOOT_ACCOUNT_ID",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN",
        "CHATWOOT_AGENT_BOT_ID",
        "CHATWOOT_PAUSE_MACRO_ID",
    }

    for variable in required:
        assert f"{variable}=" in env_example
        assert f"{variable}:" in compose


def test_deployment_declares_optional_hermes_shadow_variables() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()
    required = {
        "HERMES_SHADOW_ENABLED",
        "HERMES_API_BASE_URL",
        "HERMES_API_KEY",
        "HERMES_MODEL_NAME",
        "SHADOW_DIR",
    }

    for variable in required:
        assert f"{variable}=" in env_example
        assert f"{variable}:" in compose

    assert "HERMES_SHADOW_ENABLED=false" in env_example
    assert "HERMES_API_KEY=replace-me" in env_example


def test_deployment_declares_automated_reply_variables_disabled_by_default() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()
    required = {
        "CHATWOOT_AUTOMATED_REPLIES_ENABLED",
        "CHATWOOT_AGENT_BOT_ACCESS_TOKEN",
        "REPLY_DIR",
    }

    for variable in required:
        assert f"{variable}=" in env_example
        assert f"{variable}:" in compose

    assert "CHATWOOT_AUTOMATED_REPLIES_ENABLED=false" in env_example


def test_enabling_replies_requires_shadow_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHATWOOT_AUTOMATED_REPLIES_ENABLED", "true")
    monkeypatch.setenv("CHATWOOT_AGENT_BOT_ACCESS_TOKEN", "test-agent-bot-token")
    monkeypatch.setenv("HERMES_SHADOW_ENABLED", "false")

    with pytest.raises(ValueError, match="requires HERMES_SHADOW_ENABLED"):
        Settings.from_env()


def test_enabling_replies_requires_the_agent_bot_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHATWOOT_AUTOMATED_REPLIES_ENABLED", "true")
    monkeypatch.setenv("HERMES_SHADOW_ENABLED", "true")
    monkeypatch.delenv("CHATWOOT_AGENT_BOT_ACCESS_TOKEN", raising=False)

    with pytest.raises(ValueError, match="CHATWOOT_AGENT_BOT_ACCESS_TOKEN"):
        Settings.from_env()


def test_production_config_requires_the_agent_bot_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
    }
    for name, value in required_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CHATWOOT_AGENT_BOT_ID", raising=False)

    with pytest.raises(KeyError, match="CHATWOOT_AGENT_BOT_ID"):
        Settings.from_env()


def test_enabling_shadow_mode_requires_hermes_connection_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "HERMES_SHADOW_ENABLED": "true",
    }
    for name, value in required_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("HERMES_API_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)

    with pytest.raises(KeyError, match="HERMES_API_BASE_URL"):
        Settings.from_env()


def test_enabling_shadow_mode_rejects_blank_hermes_connection_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "HERMES_SHADOW_ENABLED": "true",
        "HERMES_API_BASE_URL": "   ",
        "HERMES_API_KEY": "",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="HERMES_API_BASE_URL"):
        Settings.from_env()


def test_enabling_shadow_mode_rejects_a_blank_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "HERMES_SHADOW_ENABLED": "true",
        "HERMES_API_BASE_URL": "https://hermes.example.test/v1",
        "HERMES_API_KEY": "test-hermes-key",
        "HERMES_MODEL_NAME": "   ",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="HERMES_MODEL_NAME"):
        Settings.from_env()


def test_enabling_shadow_mode_rejects_untrusted_plain_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "HERMES_SHADOW_ENABLED": "true",
        "HERMES_API_BASE_URL": "http://external.example.test:8643/v1",
        "HERMES_API_KEY": "test-hermes-key",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="HTTPS or trusted internal HTTP"):
        Settings.from_env()


def test_enabling_shadow_mode_accepts_the_internal_hermes_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "HERMES_SHADOW_ENABLED": "true",
        "HERMES_API_BASE_URL": "http://hermes:8643/v1",
        "HERMES_API_KEY": "test-hermes-key",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    settings = Settings.from_env()

    assert settings.hermes_api_base_url == "http://hermes:8643/v1"


def test_enabling_shadow_mode_rejects_https_without_a_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "HERMES_SHADOW_ENABLED": "true",
        "HERMES_API_BASE_URL": "https://",
        "HERMES_API_KEY": "test-hermes-key",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="valid hostname"):
        Settings.from_env()
