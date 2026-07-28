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
