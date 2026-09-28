"""El cableado del seguimiento con cupon en el bridge."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bridge.app import Settings, create_app


class _FakeSupabase:
    async def admit_johanna_funnel_event(self, **_: object) -> object:
        return SimpleNamespace(outcome="inserted", event_id="x")


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": "unused",
        "allowed_jid": None,
        "capture_dir": Path("/tmp/followup-discount-wiring-tests"),
        "max_age_seconds": 300,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _followup_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "conversation_followup_enabled": True,
        "conversation_followup_template_name": "johanna_seguimiento_descuento_01",
        "conversation_followup_coupon_code": "JOHANNA10",
        "conversation_followup_product_name": "Libérate de la Ansiedad",
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 9,
        "chatwoot_agent_bot_access_token": "agent-bot-token",
        "agent_bot_id": 1,
        "chatwoot_cut_b_admission_enabled": True,
        "supabase_base_url": "https://supabase.example.test",
        "supabase_service_role_key": "service-role",
        "allowed_jid": "12025550123@s.whatsapp.net",
    }
    values.update(overrides)
    return _settings(**values)


def _settings_from_env(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "unused",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "unused",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    return Settings.from_env()


def test_followup_settings_default_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_from_env(monkeypatch)
    assert settings.conversation_followup_enabled is False
    assert settings.conversation_followup_coupon_code is None
    # 24 a 72 h desde el ultimo mensaje del lead (decision de Dan, 2026-09-28).
    assert settings.conversation_followup_min_age_seconds == 86_400
    assert settings.conversation_followup_max_age_seconds == 259_200
    assert settings.conversation_followup_interval_seconds == 300.0


def test_followup_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_ENABLED", "true")
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_TEMPLATE_NAME", "johanna_seguimiento_descuento_01")
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_TEMPLATE_LANGUAGE", "es_EC")
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_COUPON_CODE", "JOHANNA10")
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_PRODUCT_NAME", "Libérate de la Ansiedad")
    monkeypatch.setenv("CONVERSATION_FOLLOWUP_MAX_AGE_SECONDS", "172800")
    settings = _settings_from_env(monkeypatch)
    assert settings.conversation_followup_enabled is True
    assert settings.conversation_followup_template_name == "johanna_seguimiento_descuento_01"
    assert settings.conversation_followup_template_language == "es_EC"
    assert settings.conversation_followup_coupon_code == "JOHANNA10"
    assert settings.conversation_followup_product_name == "Libérate de la Ansiedad"
    assert settings.conversation_followup_max_age_seconds == 172_800


@pytest.mark.parametrize(
    "overrides",
    [
        {"conversation_followup_template_name": None},
        {"conversation_followup_coupon_code": None},
        {"conversation_followup_coupon_code": "JOHANNA 10"},
        {"conversation_followup_product_name": None},
        {"chatwoot_agent_bot_access_token": None},
        {"chatwoot_cut_b_admission_enabled": False},
        {"allowed_jid": None, "chatwoot_scoped_inbound_senders_enabled": False},
    ],
)
def test_enabling_the_followup_half_configured_refuses_to_start(
    overrides: dict[str, object],
) -> None:
    # Un cupon es un efecto externo irreversible: sin plantilla, sin cupon o sin
    # la admision de la que sale el link, el bridge no arranca a medias.
    with pytest.raises(ValueError) as error:
        create_app(
            _followup_settings(**overrides),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )
    assert "CONVERSATION_FOLLOWUP_" in str(error.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"conversation_followup_interval_seconds": 0.0},
        {"conversation_followup_max_sends_per_scan": 0},
        {"conversation_followup_max_pages": 21},
        {
            "conversation_followup_min_age_seconds": 100,
            "conversation_followup_max_age_seconds": 10,
        },
    ],
)
def test_invalid_followup_configuration_refuses_to_start_even_disabled(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValueError) as error:
        create_app(
            _settings(conversation_followup_enabled=False, **overrides),
            supabase_client=_FakeSupabase(),  # type: ignore[arg-type]
        )
    assert "conversation followup configuration" in str(error.value)


def test_the_followup_is_not_a_portable_capability() -> None:
    # Su reserva depende hoy del catalogo de ofertas de Johanna: prenderlo en un
    # runtime portable (ATT1) tiene que frenar el arranque, no mandar mal.
    from bridge import app as app_module

    assert "conversation_followup_enabled" not in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
