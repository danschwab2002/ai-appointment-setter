"""El cableado de los rechazos de Meta en el bridge (E-284).

Las tres variables nuevas, apagadas por defecto, sus guardas de arranque, el
barredor que arma ``create_app`` y lo que publica ``/ready``. El criterio y el
barredor se prueban en ``tests/test_meta_delivery.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.chatwoot import ChatwootClient
from bridge.instance_manifest import InstanceManifest
from bridge.meta_delivery import MetaDeliveryRejectionSweeper

ATT1 = Path(__file__).parent / "fixtures" / "instances" / "att1"
FLAGS = (
    "META_DELIVERY_REJECTIONS_ENABLED",
    "META_FIRST_TOUCH_RETRY_ENABLED",
    "META_UNREACHABLE_CLOSE_ENABLED",
    "META_FIRST_TOUCH_RETRY_TEMPLATES",
    "META_FIRST_TOUCH_RETRY_SEND_HOURS",
    "META_DELIVERY_SCAN_INTERVAL_SECONDS",
    "META_ACCOUNT_HOLD_RETRY_ENABLED",
    "META_ACCOUNT_HOLD_CODES",
    "META_ACCOUNT_HOLD_MAX_AGE_SECONDS",
)


class _FakeSupabase:
    async def admit_johanna_funnel_event(self, **_: object) -> object:
        return SimpleNamespace(outcome="inserted", event_id="x")


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": "unused",
        "allowed_jid": "12025550123@s.whatsapp.net",
        "capture_dir": Path("/tmp/meta-delivery-wiring-tests"),
        "max_age_seconds": 300,
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 9,
        "chatwoot_agent_bot_access_token": "agent-bot-token",
        "agent_bot_id": 1,
        "supabase_base_url": "https://supabase.example.test",
        "supabase_service_role_key": "service-role",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _chatwoot_client() -> ChatwootClient:
    return ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="test-control-token",
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(404)),
    )


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


def _get_ready(app: object) -> dict:
    async def get() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/ready")

    return asyncio.run(get()).json()


def test_everything_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in FLAGS:
        monkeypatch.delenv(name, raising=False)
    settings = _settings_from_env(monkeypatch)
    assert settings.meta_delivery_rejections_enabled is False
    assert settings.meta_first_touch_retry_enabled is False
    assert settings.meta_unreachable_close_enabled is False
    assert settings.meta_first_touch_retry_templates == frozenset()
    assert settings.meta_first_touch_retry_send_hours is None
    assert settings.meta_delivery_scan_interval_seconds == 600.0
    # E-348: el rechazo de la cuenta, apagado, con sus defaults (D1 y D2).
    assert settings.meta_account_hold_retry_enabled is False
    assert settings.meta_account_hold_codes == frozenset({"131042"})
    assert settings.meta_account_hold_max_age_seconds == 259_200
    app = create_app(_settings(), chatwoot_client=_chatwoot_client(),
                     supabase_client=_FakeSupabase())  # type: ignore[arg-type]
    assert app.state.meta_delivery_sweeper is None
    assert not any(key.startswith("meta_") for key in _get_ready(app))


def test_the_variables_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_DELIVERY_REJECTIONS_ENABLED", "true")
    monkeypatch.setenv("META_FIRST_TOUCH_RETRY_ENABLED", "true")
    monkeypatch.setenv("META_UNREACHABLE_CLOSE_ENABLED", "true")
    monkeypatch.setenv("META_FIRST_TOUCH_RETRY_TEMPLATES", "att1_carrito_abandonado_01,att1_interes_01")
    monkeypatch.setenv("META_FIRST_TOUCH_RETRY_SEND_HOURS", "09-21")
    monkeypatch.setenv("META_DELIVERY_SCAN_INTERVAL_SECONDS", "900")
    settings = _settings_from_env(monkeypatch)
    assert settings.meta_delivery_rejections_enabled is True
    assert settings.meta_first_touch_retry_enabled is True
    assert settings.meta_unreachable_close_enabled is True
    assert settings.meta_first_touch_retry_templates == frozenset(
        {"att1_carrito_abandonado_01", "att1_interes_01"})
    assert settings.meta_first_touch_retry_send_hours == (9, 21)
    assert settings.meta_delivery_scan_interval_seconds == 900.0


@pytest.mark.parametrize("value", ["yes", "1", "TRUEISH"])
def test_a_flag_that_is_not_true_or_false_does_not_start(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("META_FIRST_TOUCH_RETRY_ENABLED", value)
    with pytest.raises(ValueError, match="^META_FIRST_TOUCH_RETRY_ENABLED must be true or false$"):
        _settings_from_env(monkeypatch)


def test_registering_builds_the_sweeper_without_retry_and_publishes_it() -> None:
    app = create_app(_settings(meta_delivery_rejections_enabled=True), chatwoot_client=_chatwoot_client(),
                     supabase_client=_FakeSupabase())  # type: ignore[arg-type]
    sweeper = app.state.meta_delivery_sweeper
    assert isinstance(sweeper, MetaDeliveryRejectionSweeper)
    assert sweeper.retry_enabled is False and sweeper.close_unreachable is False
    body = _get_ready(app)
    assert body["meta_delivery_rejections"] == "never"
    assert body["meta_first_touch_retry"] == "disabled"
    assert body["meta_unreachable_close"] == "disabled"
    assert body["meta_account_hold_retry"] == "disabled"
    assert sweeper.account_hold_enabled is False


@pytest.mark.parametrize("flag", ["meta_first_touch_retry_enabled", "meta_unreachable_close_enabled"])
def test_retry_and_close_need_the_register(flag: str) -> None:
    with pytest.raises(ValueError, match="require META_DELIVERY_REJECTIONS_ENABLED"):
        create_app(_settings(**{flag: True}), chatwoot_client=_chatwoot_client(),
                   supabase_client=_FakeSupabase())  # type: ignore[arg-type]


def test_the_retry_without_a_manifest_does_not_start() -> None:
    settings = _settings(meta_delivery_rejections_enabled=True, meta_first_touch_retry_enabled=True,
                         meta_first_touch_retry_templates=frozenset({"x_01"}),
                         meta_first_touch_retry_send_hours=(9, 21))
    with pytest.raises(ValueError, match="requires an instance manifest"):
        create_app(settings, chatwoot_client=_chatwoot_client(),
                   supabase_client=_FakeSupabase())  # type: ignore[arg-type]


def test_the_retry_guards_with_the_att1_manifest() -> None:
    manifest = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")
    base = _settings(meta_delivery_rejections_enabled=True, meta_first_touch_retry_enabled=True,
                     instance_manifest=manifest, meta_final_effect_enabled=True,
                     meta_first_touch_retry_templates=frozenset({"att1_carrito_abandonado_01"}),
                     meta_first_touch_retry_send_hours=(9, 21))
    app_module._validate_meta_delivery(base)
    with pytest.raises(ValueError, match="requires META_FINAL_EFFECT_ENABLED"):
        app_module._validate_meta_delivery(replace(base, meta_final_effect_enabled=False))
    with pytest.raises(ValueError, match="requires META_FIRST_TOUCH_RETRY_TEMPLATES"):
        app_module._validate_meta_delivery(replace(base, meta_first_touch_retry_templates=frozenset()))
    with pytest.raises(ValueError, match="META_FIRST_TOUCH_RETRY_SEND_HOURS or CONVERSATION_FOLLOWUP_SEND_HOURS"):
        app_module._validate_meta_delivery(replace(base, meta_first_touch_retry_send_hours=None))
    # Sin horario propio, el del seguimiento con cupon.
    app_module._validate_meta_delivery(replace(base, meta_first_touch_retry_send_hours=None,
                                               conversation_followup_send_hours=(10, 20)))
    assert app_module._meta_first_touch_retry_send_hours(
        replace(base, meta_first_touch_retry_send_hours=None, conversation_followup_send_hours=(10, 20))
    ) == (10, 20)
    with pytest.raises(ValueError, match="META_DELIVERY_SCAN_INTERVAL_SECONDS"):
        app_module._validate_meta_delivery(replace(base, meta_delivery_scan_interval_seconds=5.0))


def test_the_flags_are_portable_capabilities() -> None:
    for flag in ("meta_delivery_rejections_enabled", "meta_first_touch_retry_enabled",
                 "meta_unreachable_close_enabled", "meta_account_hold_retry_enabled"):
        assert flag in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


# ------------------------------------------------------------- E-348 (P-33)


def test_the_account_hold_variables_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_ACCOUNT_HOLD_RETRY_ENABLED", "true")
    monkeypatch.setenv("META_ACCOUNT_HOLD_CODES", "131042,131031")
    monkeypatch.setenv("META_ACCOUNT_HOLD_MAX_AGE_SECONDS", "172800")
    settings = _settings_from_env(monkeypatch)
    assert settings.meta_account_hold_retry_enabled is True
    assert settings.meta_account_hold_codes == frozenset({"131042", "131031"})
    assert settings.meta_account_hold_max_age_seconds == 172_800
    monkeypatch.setenv("META_ACCOUNT_HOLD_CODES", "131049")
    with pytest.raises(ValueError, match="META_ACCOUNT_HOLD_CODES"):
        _settings_from_env(monkeypatch)


def test_the_account_hold_needs_the_register() -> None:
    with pytest.raises(ValueError, match="META_ACCOUNT_HOLD_RETRY_ENABLED requires META_DELIVERY_REJECTIONS_ENABLED"):
        create_app(_settings(meta_account_hold_retry_enabled=True), chatwoot_client=_chatwoot_client(),
                   supabase_client=_FakeSupabase())  # type: ignore[arg-type]


def test_the_account_hold_guards_with_the_att1_manifest() -> None:
    manifest = InstanceManifest.from_toml_file(ATT1 / "instancia.toml")
    base = _settings(meta_delivery_rejections_enabled=True, meta_account_hold_retry_enabled=True,
                     instance_manifest=manifest, meta_final_effect_enabled=True,
                     meta_first_touch_retry_templates=frozenset({"att1_carrito_abandonado_01"}),
                     meta_first_touch_retry_send_hours=(9, 21))
    # No exige el reintento del 131049 prendido: solo su lista y su horario (D3).
    assert base.meta_first_touch_retry_enabled is False
    app_module._validate_meta_delivery(base)
    with pytest.raises(ValueError, match="requires an instance manifest"):
        app_module._validate_meta_delivery(replace(base, instance_manifest=None))
    with pytest.raises(ValueError, match="META_ACCOUNT_HOLD_RETRY_ENABLED requires META_FINAL_EFFECT_ENABLED"):
        app_module._validate_meta_delivery(replace(base, meta_final_effect_enabled=False))
    with pytest.raises(ValueError, match="META_ACCOUNT_HOLD_RETRY_ENABLED requires META_FIRST_TOUCH_RETRY_TEMPLATES"):
        app_module._validate_meta_delivery(replace(base, meta_first_touch_retry_templates=frozenset()))
    with pytest.raises(ValueError, match="META_ACCOUNT_HOLD_RETRY_ENABLED requires META_FIRST_TOUCH_RETRY_SEND_HOURS"):
        app_module._validate_meta_delivery(replace(base, meta_first_touch_retry_send_hours=None))
    # Los rangos se miran aunque este apagado.
    with pytest.raises(ValueError, match="META_ACCOUNT_HOLD_MAX_AGE_SECONDS must be 3600-604800"):
        app_module._validate_meta_delivery(replace(base, meta_account_hold_retry_enabled=False,
                                                   meta_account_hold_max_age_seconds=60))
    with pytest.raises(ValueError, match="META_ACCOUNT_HOLD_CODES"):
        app_module._validate_meta_delivery(replace(base, meta_account_hold_codes=frozenset({"131026"})))


def test_the_account_hold_builds_into_the_sweeper_and_publishes_it(tmp_path: Path) -> None:
    from test_cart_discount_first_touch import _Chatwoot
    from test_flow_engine_bridge_settings import engine_settings
    from test_flow_engine_ready import _ReadyAuthority

    app = create_app(
        engine_settings(tmp_path, flow_engine_enabled=False, meta_delivery_rejections_enabled=True,
                        meta_account_hold_retry_enabled=True, meta_final_effect_enabled=True,
                        meta_first_touch_retry_templates=frozenset({"att1_carrito_abandonado_01"}),
                        meta_first_touch_retry_send_hours=(9, 21)),
        supabase_client=_ReadyAuthority(),  # type: ignore[arg-type]
        chatwoot_client=_Chatwoot().client(),
    )
    sweeper = app.state.meta_delivery_sweeper
    assert isinstance(sweeper, MetaDeliveryRejectionSweeper)
    assert sweeper.account_hold_enabled is True and sweeper.retry_enabled is False
    body = _get_ready(app)
    assert body["meta_account_hold_retry"] == "enabled"
    assert body["meta_first_touch_retry"] == "disabled"
