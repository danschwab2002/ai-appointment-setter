from pathlib import Path

import pytest

from bridge.app import Settings, create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_hotmart_abandonment_timer_worker_is_declared_default_off(
    tmp_path: Path,
) -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    assert "HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED=false" in env_example
    assert "HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED:" in compose
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
    )
    assert settings.hotmart_abandonment_timer_worker_enabled is False
    assert settings.hotmart_abandonment_timer_poll_interval_seconds == 5.0
    assert settings.hotmart_abandonment_timer_batch_size == 10


def test_hotmart_abandonment_timer_worker_requires_only_supabase(
    tmp_path: Path,
) -> None:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
        hotmart_abandonment_timer_worker_enabled=True,
    )

    with pytest.raises(
        ValueError,
        match="Supabase is required when HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED=true",
    ):
        create_app(settings)


def test_hotmart_abandonment_timer_worker_builds_without_outbound_dependencies(
    tmp_path: Path,
) -> None:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
        supabase_base_url="https://supabase.example.test",
        supabase_service_role_key="test-service-role",
        hotmart_abandonment_timer_worker_enabled=True,
    )

    app = create_app(settings)

    assert app.state.hotmart_abandonment_timer_worker is not None
    assert app.state.resolution_worker is None
    assert app.state.durable_dispatcher is None


def test_purchase_worker_flag_is_declared_and_disabled_by_default(
    tmp_path: Path,
) -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    assert "HOTMART_PURCHASE_WORKER_ENABLED=false" in env_example
    assert "HOTMART_PURCHASE_WORKER_ENABLED:" in compose
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
    )
    assert settings.hotmart_purchase_worker_enabled is False


def test_purchase_worker_requires_resolution_worker(tmp_path: Path) -> None:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
        hotmart_purchase_worker_enabled=True,
        worker_enabled=False,
    )

    with pytest.raises(
        ValueError,
        match="HOTMART_PURCHASE_WORKER_ENABLED requires RESOLUTION_WORKER_ENABLED",
    ):
        create_app(settings)


def test_deployment_declares_required_chatwoot_control_variables() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()
    required = {
        "CHATWOOT_BASE_URL",
        "CHATWOOT_ACCOUNT_ID",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN",
        "CHATWOOT_AGENT_BOT_ID",
        "CHATWOOT_PAUSE_MACRO_ID",
        "CHATWOOT_HUMAN_PAUSE_ENABLED",
    }

    for variable in required:
        assert f"{variable}=" in env_example
        assert f"{variable}:" in compose

    assert "CHATWOOT_HUMAN_PAUSE_ENABLED=false" in env_example
    assert "${CHATWOOT_HUMAN_PAUSE_ENABLED:-false}" in compose


def test_deployment_declares_waba_pilot_contract_default_off() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()
    required = {
        "HOTMART_HOTTOK",
        "CHATWOOT_INBOX_ID",
        "LANCEMOS_PILOT_BOUNDARY_ENABLED",
        "LANCEMOS_PILOT_SCOPE_KEY",
        "LANCEMOS_PILOT_SCOPE_VERSION",
        "LANCEMOS_PILOT_TENANT_KEY",
        "LANCEMOS_PILOT_CHANNEL_PROVIDER",
        "LANCEMOS_PILOT_CHANNEL_ACCOUNT_REF",
        "WABA_FIRST_TOUCH_TEMPLATE_NAME",
        "WABA_FOLLOWUP_TEMPLATE_NAME",
        "WABA_TEMPLATE_LANGUAGE",
        "WABA_TEMPLATE_CATEGORY",
        "DURABLE_DISPATCHER_ENABLED",
        "DURABLE_OUTBOUND_ENABLED",
    }

    for variable in required:
        assert f"{variable}=" in env_example
        assert f"{variable}:" in compose

    assert "LANCEMOS_PILOT_BOUNDARY_ENABLED=false" in env_example
    assert "DURABLE_DISPATCHER_ENABLED=false" in env_example
    assert "DURABLE_OUTBOUND_ENABLED=false" in env_example
    assert "${LANCEMOS_PILOT_BOUNDARY_ENABLED:-false}" in compose
    assert "${DURABLE_DISPATCHER_ENABLED:-false}" in compose
    assert "${DURABLE_OUTBOUND_ENABLED:-false}" in compose


def test_deployment_declares_chatwoot_cut_b_admission_default_off() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    for variable in (
        "CHATWOOT_CUT_B_ADMISSION_ENABLED",
        "CHATWOOT_CUT_B_SCOPE_KEY",
        "CHATWOOT_CUT_B_SCOPE_VERSION",
    ):
        assert f"{variable}=" in env_example
        assert f"{variable}:" in compose

    assert "CHATWOOT_CUT_B_ADMISSION_ENABLED=false" in env_example
    assert "${CHATWOOT_CUT_B_ADMISSION_ENABLED:-false}" in compose


def test_deployment_declares_chatwoot_cut_b_agent_default_off() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    assert "CHATWOOT_CUT_B_AGENT_ENABLED=false" in env_example
    assert "CHATWOOT_CUT_B_AGENT_ENABLED:" in compose
    assert "${CHATWOOT_CUT_B_AGENT_ENABLED:-false}" in compose


def test_deployment_declares_johanna_full_mvp_flags_default_off(
    tmp_path: Path,
) -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    for variable in (
        "CHATWOOT_SCOPED_INBOUND_SENDERS_ENABLED",
        "JOHANNA_PAYMENT_FAILURE_HOTMART_ENABLED",
        "JOHANNA_PAYMENT_FAILURE_OUTBOUND_ENABLED",
    ):
        assert f"{variable}=false" in env_example
        assert f"{variable}:" in compose
        assert f"${{{variable}:-false}}" in compose

    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
    )
    assert settings.chatwoot_scoped_inbound_senders_enabled is False
    assert settings.johanna_payment_failure_hotmart_enabled is False
    assert settings.johanna_payment_failure_outbound_enabled is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chatwoot_account_id", 2),
        ("chatwoot_inbox_id", 8),
        ("chatwoot_cut_b_scope_key", "other-scope"),
        ("chatwoot_cut_b_scope_version", 3),
    ],
)
def test_scoped_inbound_requires_exact_johanna_scope(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "webhook_secret": "test-secret",
        "allowed_jid": "12025550123@s.whatsapp.net",
        "capture_dir": tmp_path,
        "max_age_seconds": 300,
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 9,
        "chatwoot_cut_b_scope_key": "libre-de-ansiedad-inbound",
        "chatwoot_cut_b_scope_version": 2,
        "chatwoot_cut_b_admission_enabled": True,
        "chatwoot_cut_b_agent_enabled": True,
        "chatwoot_scoped_inbound_senders_enabled": True,
        "automated_replies_enabled": True,
        "chatwoot_durable_opt_out_enabled": True,
        "chatwoot_human_pause_enabled": True,
        "human_handoff_admission_enabled": True,
        "human_handoff_projection_enabled": True,
    }
    values[field] = value

    with pytest.raises(ValueError, match="exact Johanna inbound scope"):
        create_app(Settings(**values))  # type: ignore[arg-type]


def test_scoped_inbound_requires_all_stop_and_handoff_gates(
    tmp_path: Path,
) -> None:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        chatwoot_cut_b_scope_key="libre-de-ansiedad-inbound",
        chatwoot_cut_b_scope_version=2,
        chatwoot_cut_b_admission_enabled=True,
        chatwoot_cut_b_agent_enabled=True,
        chatwoot_scoped_inbound_senders_enabled=True,
        automated_replies_enabled=True,
        chatwoot_durable_opt_out_enabled=False,
        chatwoot_human_pause_enabled=True,
        human_handoff_admission_enabled=True,
        human_handoff_projection_enabled=True,
    )

    with pytest.raises(ValueError, match="stop and handoff gates"):
        create_app(settings)


def test_deployment_declares_precheckout_test_receiver_default_off() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    required = {
        "PRECHECKOUT_FORM_ENABLED",
        "PRECHECKOUT_FORM_TOKEN",
        "PRECHECKOUT_TEST_MODE_ENABLED",
        "PRECHECKOUT_TEST_PHONE_E164",
        "PRECHECKOUT_MAX_AGE_SECONDS",
        "PRECHECKOUT_FIRST_TOUCH_ENABLED",
        "PRECHECKOUT_FIRST_TOUCH_TOKEN",
    }
    for variable in required:
        assert f"{variable}=" in env_example
        assert f"{variable}:" in compose

    assert "PRECHECKOUT_FORM_ENABLED=false" in env_example
    assert "PRECHECKOUT_TEST_MODE_ENABLED=false" in env_example
    assert "PRECHECKOUT_FIRST_TOUCH_ENABLED=false" in env_example
    assert "${PRECHECKOUT_FORM_ENABLED:-false}" in compose
    assert "${PRECHECKOUT_TEST_MODE_ENABLED:-false}" in compose
    assert "${PRECHECKOUT_FIRST_TOUCH_ENABLED:-false}" in compose


def test_deployment_declares_observed_lead_receiver_default_off() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    required = {
        "LEAD_PRECHECKOUT_ENABLED",
        "LEAD_PRECHECKOUT_SECRET",
        "LEAD_PRECHECKOUT_MAX_AGE_SECONDS",
        "LEAD_PRECHECKOUT_SITE",
        "LEAD_PRECHECKOUT_LANDING_ID",
        "LEAD_PRECHECKOUT_OFFER_CODE",
    }
    for variable in required:
        assert f"{variable}=" in env_example
        assert f"{variable}:" in compose

    assert "LEAD_PRECHECKOUT_ENABLED=false" in env_example
    assert "${LEAD_PRECHECKOUT_ENABLED:-false}" in compose


def test_deployment_declares_johanna_one_shot_default_off() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    for variable in (
        "JOHANNA_ABANDONMENT_ONE_SHOT_ENABLED",
        "JOHANNA_ABANDONMENT_ONE_SHOT_TOKEN",
        "JOHANNA_ABANDONMENT_HOTMART_AUTO_ENABLED",
    ):
        assert f"{variable}=" in env_example
        assert f"{variable}:" in compose

    assert "JOHANNA_ABANDONMENT_ONE_SHOT_ENABLED=false" in env_example
    assert "${JOHANNA_ABANDONMENT_ONE_SHOT_ENABLED:-false}" in compose
    assert "JOHANNA_ABANDONMENT_HOTMART_AUTO_ENABLED=false" in env_example
    assert "${JOHANNA_ABANDONMENT_HOTMART_AUTO_ENABLED:-false}" in compose


def test_deployment_uses_supabase_service_role_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    assert "SUPABASE_SERVICE_ROLE_KEY=" in env_example
    assert "SUPABASE_SERVICE_ROLE_KEY:" in compose
    assert "SUPABASE_ANON_KEY" not in env_example
    assert "SUPABASE_ANON_KEY" not in compose

    required_environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "SUPABASE_SERVICE_ROLE_KEY": "fake-service-role-key",
        "SUPABASE_ANON_KEY": "legacy-anon-key",
    }
    for name, value in required_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CHATWOOT_INBOUND_DEBOUNCE_SECONDS", raising=False)
    settings = Settings.from_env()

    assert settings.supabase_service_role_key == "fake-service-role-key"
    assert settings.chatwoot_inbound_debounce_seconds == 30
    assert settings.reply_splitter_enabled is False
    assert settings.reply_part_delay_seconds == 2
    assert settings.chatwoot_human_pause_enabled is False
    assert not hasattr(settings, "supabase_anon_key")


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
        "CHATWOOT_INBOUND_DEBOUNCE_SECONDS",
        "CHATWOOT_REPLY_SPLITTER_ENABLED",
        "CHATWOOT_REPLY_PART_DELAY_SECONDS",
        "HERMES_REPLY_SPLITTER_PROVIDER",
        "HERMES_REPLY_SPLITTER_MODEL_NAME",
        "REPLY_DIR",
    }

    for variable in required:
        assert f"{variable}=" in env_example
        assert f"{variable}:" in compose

    assert "CHATWOOT_AUTOMATED_REPLIES_ENABLED=false" in env_example
    assert "CHATWOOT_INBOUND_DEBOUNCE_SECONDS=30" in env_example
    assert "CHATWOOT_REPLY_SPLITTER_ENABLED=false" in env_example
    assert "CHATWOOT_REPLY_PART_DELAY_SECONDS=2" in env_example


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


def test_compose_passes_through_durable_handoff_configuration() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()
    required = {
        "HUMAN_HANDOFF_ADMISSION_ENABLED",
        "HUMAN_HANDOFF_PROJECTION_ENABLED",
        "HUMAN_HANDOFF_PROJECTION_WORKER_ID",
        "HANDOFF_PROJECTION_POLICY_KEY",
        "HANDOFF_PROJECTION_POLICY_VERSION",
    }

    for variable in required:
        assert f"{variable}=" in env_example
        assert f"{variable}:" in compose

    assert "HUMAN_HANDOFF_ADMISSION_ENABLED=false" in env_example
    assert "HUMAN_HANDOFF_PROJECTION_ENABLED=false" in env_example
