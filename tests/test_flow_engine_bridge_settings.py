"""FLOW_ENGINE_ENABLED: el bridge del motor de flujos, apagado por defecto.

Contrato: ``docs/contracts/flow-engine-v1.md``, seccion 8. Plan de la T2.4,
commit 2 y decision D4: prendido, el arranque exige el manifiesto, la frontera
del piloto, el worker de resolucion y el despachador en plantilla directa.
Apagado no exige ni lee nada. Desde E-165, A1 con cupon y A2/Z no frenan el
arranque: los pide una ruta del carrito en ``motor`` (sin ellos, ese carrito va
forzado al legado; ``test_flow_engine_resolution``, los ``test_f_``).

El arranque reusa los ``Settings`` de A2 y Z (``test_cart_followups_wiring``,
sobre A1 de ``test_cart_discount_first_touch``), que ya son los del carrito de
ATT1 con sus tres toques.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_cart_discount_first_touch import _app
from test_cart_followups_wiring import _with_followups

from bridge import app as app_module
from bridge.app import Settings


def engine_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """El carrito de ATT1 con sus tres toques, el worker y el flag prendido."""
    fields: dict[str, Any] = {
        "worker_enabled": True,
        "followup_policy_key": "att1-carrito-tres-toques",
        "followup_policy_version": 1,
        "flow_engine_enabled": True,
    }
    fields.update(overrides)
    return _with_followups(tmp_path, **fields)


def test_the_flag_is_off_by_default() -> None:
    assert Settings.__dataclass_fields__["flow_engine_enabled"].default is False
    assert "flow_engine_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES


def test_off_it_asks_for_nothing(tmp_path: Path) -> None:
    # Sin el worker ni nada del motor, con el flag apagado arranca como hoy.
    app = _app(engine_settings(tmp_path, flow_engine_enabled=False, worker_enabled=False))
    assert app is not None


def test_on_with_the_whole_cart_it_starts(tmp_path: Path) -> None:
    app = _app(engine_settings(tmp_path))
    assert app is not None


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"instance_manifest": None}, "FLOW_ENGINE_ENABLED requires an instance manifest"),
        ({"pilot_boundary_enabled": False}, "FLOW_ENGINE_ENABLED requires LANCEMOS_PILOT_BOUNDARY_ENABLED"),
        ({"worker_enabled": False}, "FLOW_ENGINE_ENABLED requires RESOLUTION_WORKER_ENABLED"),
        ({"dispatcher_enabled": False}, "FLOW_ENGINE_ENABLED requires DURABLE_DISPATCHER_ENABLED"),
        ({"dispatcher_approved_template_direct_enabled": False},
         "FLOW_ENGINE_ENABLED requires DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED"),
    ],
)
def test_an_incomplete_wiring_does_not_start(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    settings = engine_settings(tmp_path, **overrides)
    with pytest.raises(ValueError, match=f"^{message}$"):
        app_module._validate_flow_engine(settings)


def test_the_required_flags_are_the_four_of_d4_without_the_cart() -> None:
    assert [env for _, env in app_module.FLOW_ENGINE_REQUIRED_FLAGS] == [
        "LANCEMOS_PILOT_BOUNDARY_ENABLED",
        "RESOLUTION_WORKER_ENABLED",
        "DURABLE_DISPATCHER_ENABLED",
        "DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED",
    ]
    assert [env for _, env in app_module.FLOW_ENGINE_CART_ROUTE_FLAGS] == [
        "RECOVERY_DISCOUNT_FIRST_TOUCH_ENABLED",
        "RECOVERY_CART_FOLLOWUPS_ENABLED",
    ]
    for field_name, _ in (
        *app_module.FLOW_ENGINE_REQUIRED_FLAGS,
        *app_module.FLOW_ENGINE_CART_ROUTE_FLAGS,
    ):
        assert field_name in Settings.__dataclass_fields__


@pytest.mark.parametrize(
    "overrides",
    [
        {"recovery_cart_followups_enabled": False},
        {"recovery_cart_followups_enabled": False, "recovery_discount_first_touch_enabled": False},
    ],
)
def test_without_the_cart_touches_the_engine_starts(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    # E-165: el carrito de un toque (Maribel) prende el motor por sus flujos nativos.
    settings = engine_settings(tmp_path, **overrides)
    app_module._validate_flow_engine(settings)
    assert app_module.flow_engine_cart_touches_enabled(settings) is False
    app = _app(settings)
    runtime = app.state.resolution_worker._flow_engine
    assert runtime.cart_touches_enabled is False
    assert runtime.readiness()["flow_engine_cart_touches"] == "disabled"


def test_with_the_whole_cart_the_touches_are_enabled(tmp_path: Path) -> None:
    # ATT1: sus tres toques, el carrito de una ruta en motor lo arranca el motor como hoy.
    settings = engine_settings(tmp_path)
    assert app_module.flow_engine_cart_touches_enabled(settings) is True
    runtime = _app(settings).state.resolution_worker._flow_engine
    assert runtime.cart_touches_enabled is True
    assert runtime.readiness()["flow_engine_cart_touches"] == "enabled"


@pytest.mark.parametrize(
    "field_name",
    [field for field, _ in app_module.FLOW_ENGINE_REQUIRED_FLAGS],
)
def test_every_required_flag_is_checked(tmp_path: Path, field_name: str) -> None:
    settings = engine_settings(tmp_path)
    app_module._validate_flow_engine(settings)
    object.__setattr__(settings, field_name, False)
    with pytest.raises(ValueError, match="^FLOW_ENGINE_ENABLED requires "):
        app_module._validate_flow_engine(settings)


def test_the_startup_runs_the_gate(tmp_path: Path) -> None:
    # Prendido sin el worker de resolucion, create_app no arranca.
    with pytest.raises(ValueError, match="FLOW_ENGINE_ENABLED requires RESOLUTION_WORKER_ENABLED"):
        _app(engine_settings(tmp_path, worker_enabled=False))


def test_from_env_reads_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # El minimo para leer el entorno, el mismo de test_cart_followups_wiring.
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("FLOW_ENGINE_ENABLED", raising=False)
    assert Settings.from_env().flow_engine_enabled is False
    monkeypatch.setenv("FLOW_ENGINE_ENABLED", "true")
    assert Settings.from_env().flow_engine_enabled is True
