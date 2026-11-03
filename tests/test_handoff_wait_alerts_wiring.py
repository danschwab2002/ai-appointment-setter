"""El cableado del aviso de derivaciones sin atender en el bridge (E-309).

Las variables nuevas, apagadas por defecto, sus guardas de arranque, el
barredor que arma ``create_app`` (con el manifiesto de ATT1 de los fixtures y
el conector de Slack) y lo que publica ``/ready``. El criterio y el barredor se
prueban en ``tests/test_handoff_wait_alerts.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge import app as app_module
from bridge.app import Settings, create_app
from bridge.handoff_wait_alerts import HandoffWaitAlertSweeper
from bridge.slack_runtime import SlackBridgeRuntime

VARIABLES = (
    "HANDOFF_WAIT_ALERTS_ENABLED",
    "HANDOFF_WAIT_ALERT_AFTER_HOURS",
    "HANDOFF_WAIT_ALERT_MAX_AGE_HOURS",
    "HANDOFF_WAIT_ALERT_INTERVAL_SECONDS",
)


class _Producer:
    async def admit(self, command: Any) -> Any:  # pragma: no cover - el barrido no corre
        raise AssertionError("el barrido no corre en este test")

    async def aclose(self) -> None:
        return None


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


def _att1_app(tmp_path: Path, *, slack: bool = True, **overrides: Any) -> Any:
    from test_cart_discount_first_touch import _Chatwoot
    from test_flow_engine_bridge_settings import engine_settings
    from test_flow_engine_ready import _ReadyAuthority

    return create_app(
        engine_settings(tmp_path, flow_engine_enabled=False, **overrides),
        supabase_client=_ReadyAuthority(),  # type: ignore[arg-type]
        chatwoot_client=_Chatwoot().client(),
        slack_runtime=SlackBridgeRuntime(producer=_Producer()) if slack else None,
    )


def test_everything_is_off_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)
    settings = _settings_from_env(monkeypatch)
    assert settings.handoff_wait_alerts_enabled is False
    assert settings.handoff_wait_alert_after_hours == 2
    assert settings.handoff_wait_alert_max_age_hours == 168
    assert settings.handoff_wait_alert_interval_seconds == 600.0
    app = _att1_app(tmp_path)
    assert app.state.handoff_wait_alert_sweeper is None
    assert not any(key.startswith("handoff_wait") for key in _get_ready(app))


def test_the_variables_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HANDOFF_WAIT_ALERTS_ENABLED", "true")
    monkeypatch.setenv("HANDOFF_WAIT_ALERT_AFTER_HOURS", "3")
    monkeypatch.setenv("HANDOFF_WAIT_ALERT_MAX_AGE_HOURS", "72")
    monkeypatch.setenv("HANDOFF_WAIT_ALERT_INTERVAL_SECONDS", "900")
    # Prendido sin manifiesto no arranca; los valores se leen igual.
    with pytest.raises(ValueError, match="requires an instance manifest"):
        app_module._validate_handoff_wait_alerts(_settings_from_env(monkeypatch))
    monkeypatch.setenv("HANDOFF_WAIT_ALERTS_ENABLED", "false")
    settings = _settings_from_env(monkeypatch)
    assert settings.handoff_wait_alert_after_hours == 3
    assert settings.handoff_wait_alert_max_age_hours == 72
    assert settings.handoff_wait_alert_interval_seconds == 900.0


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("HANDOFF_WAIT_ALERTS_ENABLED", "yes", "^HANDOFF_WAIT_ALERTS_ENABLED must be true or false$"),
        ("HANDOFF_WAIT_ALERT_AFTER_HOURS", "dos", "must be a positive integer"),
        ("HANDOFF_WAIT_ALERT_AFTER_HOURS", "0", "must be a positive integer"),
    ],
)
def test_a_bad_value_does_not_start(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, message: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        _settings_from_env(monkeypatch)


def test_the_ranges_are_checked_even_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _settings_from_env(monkeypatch)
    app_module._validate_handoff_wait_alerts(base)
    for field, value, message in (
        ("handoff_wait_alert_after_hours", 73, "1-72"),
        ("handoff_wait_alert_max_age_hours", 12, "24-720"),
        ("handoff_wait_alert_interval_seconds", 10.0, "60-3600"),
    ):
        with pytest.raises(ValueError, match=message):
            app_module._validate_handoff_wait_alerts(replace(base, **{field: value}))


def test_on_with_the_att1_manifest_it_builds_the_sweeper_and_publishes_it(
    tmp_path: Path,
) -> None:
    app = _att1_app(tmp_path, handoff_wait_alerts_enabled=True)
    sweeper = app.state.handoff_wait_alert_sweeper
    assert isinstance(sweeper, HandoffWaitAlertSweeper)
    assert sweeper.after_hours == 2
    body = _get_ready(app)
    assert body["handoff_wait_alerts"] == "never"
    assert body["handoff_wait_alerts_last_scan"] == "never"
    assert body["handoff_wait_alerts_after"] == "2h"
    assert "handoff_wait_alerts_calendar" not in body


def test_on_without_the_slack_connector_it_does_not_start(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SLACK_CONNECTOR_PROJECTION_ENABLED"):
        _att1_app(tmp_path, slack=False, handoff_wait_alerts_enabled=True)


def test_the_flag_is_a_portable_capability() -> None:
    assert "handoff_wait_alerts_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


def test_the_business_hour_variables_of_e309_are_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    # E-309b (Dan, 03/11): sin corte por horario. Los nombres de la primera
    # version no se prendieron en ninguna aliada y ya no se leen.
    monkeypatch.setenv("HANDOFF_WAIT_ALERT_AFTER_BUSINESS_HOURS", "9")
    monkeypatch.setenv("HANDOFF_WAIT_ALERT_BUSINESS_HOURS", "08-20")
    monkeypatch.setenv("HANDOFF_WAIT_ALERT_BUSINESS_DAYS", "1-6")
    settings = _settings_from_env(monkeypatch)
    assert settings.handoff_wait_alert_after_hours == 2
    for name in ("handoff_wait_alert_after_business_hours", "handoff_wait_alert_business_hours",
                 "handoff_wait_alert_business_days"):
        assert not hasattr(settings, name)
