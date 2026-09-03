import pickle
import json
from pathlib import Path

import pytest

from bridge.app import Settings, create_app
from bridge.messaging import ChatwootMessageSender

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_commercial_ally_manifest_is_declared_and_loaded_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = {
        "tenant_ref": "att1",
        "funnel_ref": "att1-main",
        "binding_version": 1,
        "ally_ref": "ally-one",
        "lead_ally_name": "Ally One",
        "lead_site": "ally-one-site",
        "lead_landing_id": "main",
        "lead_page_host": "ally-one.example",
        "lead_page_path": "/offer/main",
        "product_hotlink": "ATT1HOTLINK",
        "product_name": "ATT1 Offer",
        "product_price": "49",
        "currency": "USD",
        "offer_code": "att1offer",
        "consent_copy_version": "att1-whatsapp-v1",
        "hotmart_product_id": 123456,
        "chatwoot_account_id": 42,
        "chatwoot_inbox_id": 24,
        "inbound_scope_key": "att1-inbound",
        "inbound_scope_version": 1,
    }
    path = tmp_path / "commercial-ally.json"
    path.write_text(json.dumps(manifest))
    required_environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "42",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "24",
        "COMMERCIAL_ALLY_CONFIG_PATH": str(path),
    }
    for name, value in required_environment.items():
        monkeypatch.setenv(name, value)

    settings = Settings.from_env()

    assert settings.commercial_ally_config.tenant_ref == "att1"
    assert settings.lead_precheckout_site == "ally-one-site"
    assert settings.lead_precheckout_landing_id == "main"
    assert settings.lead_precheckout_offer_code == "att1offer"
    assert "COMMERCIAL_ALLY_CONFIG_PATH=" in (PROJECT_ROOT / ".env.example").read_text()
    assert "COMMERCIAL_ALLY_CONFIG_PATH:" in (PROJECT_ROOT / "compose.yaml").read_text()


def test_nonlegacy_chatwoot_scope_requires_a_commercial_ally_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "42",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "24",
    }
    for name, value in required_environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(
        ValueError,
        match="COMMERCIAL_ALLY_CONFIG_PATH is required for non-legacy Chatwoot scope",
    ):
        Settings.from_env()


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


def test_precheckout_delayed_first_touch_is_declared_default_off(
    tmp_path: Path,
) -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    assert "PRECHECKOUT_DELAYED_FIRST_TOUCH_ENABLED=false" in env_example
    assert "PRECHECKOUT_DELAYED_FIRST_TOUCH_ENABLED:" in compose
    assert "${PRECHECKOUT_DELAYED_FIRST_TOUCH_ENABLED:-false}" in compose
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
    )
    assert settings.precheckout_delayed_first_touch_enabled is False


def test_precheckout_delayed_first_touch_flag_is_parsed_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "PRECHECKOUT_DELAYED_FIRST_TOUCH_ENABLED": "true",
    }
    for name, value in required_environment.items():
        monkeypatch.setenv(name, value)

    assert Settings.from_env().precheckout_delayed_first_touch_enabled is True


def test_precheckout_delayed_first_touch_requires_timer_worker(
    tmp_path: Path,
) -> None:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
        precheckout_delayed_first_touch_enabled=True,
    )

    with pytest.raises(
        ValueError,
        match="PRECHECKOUT_DELAYED_FIRST_TOUCH_ENABLED requires "
        "HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED",
    ):
        create_app(settings)


def test_precheckout_delayed_first_touch_builds_dynamic_fenced_waba_sender(
    tmp_path: Path,
) -> None:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
        agent_bot_id=7,
        chatwoot_base_url="https://chatwoot.example.test",
        chatwoot_account_id=1,
        chatwoot_control_api_access_token="control-token",
        chatwoot_agent_bot_access_token="agent-bot-token",
        chatwoot_pause_macro_id=11,
        chatwoot_inbox_id=9,
        supabase_base_url="https://supabase.example.test",
        supabase_service_role_key="test-service-role",
        pilot_channel_provider="waba",
        hotmart_abandonment_timer_worker_enabled=True,
        precheckout_delayed_first_touch_enabled=True,
    )

    app = create_app(settings)
    worker = app.state.hotmart_abandonment_timer_worker

    assert worker is not None
    assert worker._precheckout_first_touch_enabled is True
    assert worker._precheckout_outbound_enabled is False
    assert worker._isolate_precheckout_sender_process is True
    assert worker._message_sender is None
    assert worker._precheckout_sender_factory is not None
    sender = worker._precheckout_sender_factory("12025550999")
    assert isinstance(sender, ChatwootMessageSender)
    round_tripped = pickle.loads(pickle.dumps(sender))
    assert round_tripped._inbox_id == 9
    assert round_tripped._allowed_jid == "12025550999@s.whatsapp.net"
    assert sender._template is not None
    assert sender._template.first_touch_name == (
        "johanna_interes_precheckout_01"
    )
    assert sender._template.language == "es_EC"
    assert sender._template.category == "MARKETING"
    assert sender._template.first_touch_parameter == "buyer_name_and_product"
    assert sender._template.params(
        content="unused",
        followup=False,
        buyer_name="María",
        product_name="Libre de Ansiedad",
    )["processed_params"] == {
        "body": {"1": "María", "2": "Libre de Ansiedad"}
    }


def test_precheckout_delayed_outbound_is_declared_default_off() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()

    assert "PRECHECKOUT_DELAYED_OUTBOUND_ENABLED=false" in env_example
    assert "PRECHECKOUT_DELAYED_OUTBOUND_ENABLED:" in compose
    assert "${PRECHECKOUT_DELAYED_OUTBOUND_ENABLED:-false}" in compose


def test_precheckout_delayed_outbound_rejects_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required_environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "HOTMART_ABANDONMENT_TIMER_WORKER_ENABLED": "false",
        "PRECHECKOUT_DELAYED_OUTBOUND_ENABLED": "invalid",
    }
    for name, value in required_environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(
        ValueError,
        match="PRECHECKOUT_DELAYED_OUTBOUND_ENABLED must be true or false",
    ):
        Settings.from_env()


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
        "WABA_PAYMENT_FAILURE_TEMPLATE_NAME",
        "WABA_FOLLOWUP_TEMPLATE_NAME",
        "WABA_TEMPLATE_LANGUAGE",
        "WABA_TEMPLATE_CATEGORY",
        "DURABLE_DISPATCHER_ENABLED",
        "DURABLE_OUTBOUND_ENABLED",
        "PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED",
    }

    for variable in required:
        assert f"{variable}=" in env_example
        assert f"{variable}:" in compose

    assert "LANCEMOS_PILOT_BOUNDARY_ENABLED=false" in env_example
    assert "DURABLE_DISPATCHER_ENABLED=false" in env_example
    assert "DURABLE_OUTBOUND_ENABLED=false" in env_example
    assert "PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED=false" in env_example
    assert "${LANCEMOS_PILOT_BOUNDARY_ENABLED:-false}" in compose
    assert "${DURABLE_DISPATCHER_ENABLED:-false}" in compose
    assert "${DURABLE_OUTBOUND_ENABLED:-false}" in compose
    assert "${PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED:-false}" in compose


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
def test_scoped_inbound_requires_exact_commercial_ally_scope(
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

    with pytest.raises(ValueError, match="match commercial ally inbound scope"):
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


def test_deployment_does_not_require_fixed_allowed_jid_for_dynamic_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()
    assert "ALLOWED_WHATSAPP_JID=\n" in env_example
    assert "ALLOWED_WHATSAPP_JID: ${ALLOWED_WHATSAPP_JID:-}" in compose

    required_environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
    }
    for name, value in required_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ALLOWED_WHATSAPP_JID", raising=False)

    settings = Settings.from_env()

    assert settings.allowed_jid is None


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


def test_deployment_declares_durable_opt_out_variables_disabled_by_default() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text()
    compose = (PROJECT_ROOT / "compose.yaml").read_text()
    expected_compose = {
        "CHATWOOT_DURABLE_OPT_OUT_ENABLED": (
            "${CHATWOOT_DURABLE_OPT_OUT_ENABLED:-false}"
        ),
        "CHATWOOT_OPT_OUT_MACRO_ID": "${CHATWOOT_OPT_OUT_MACRO_ID:-}",
        "CHATWOOT_OPT_OUT_PROJECTION_WORKER_ID": (
            "${CHATWOOT_OPT_OUT_PROJECTION_WORKER_ID:-}"
        ),
    }

    for variable, mapping in expected_compose.items():
        assert f"{variable}=" in env_example
        assert f"{variable}: {mapping}" in compose

    assert "CHATWOOT_DURABLE_OPT_OUT_ENABLED=false" in env_example
    assert "CHATWOOT_OPT_OUT_MACRO_ID=\n" in env_example
    assert "CHATWOOT_OPT_OUT_PROJECTION_WORKER_ID=\n" in env_example


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
