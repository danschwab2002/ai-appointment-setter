"""E-483: el cableado de create_app de los flags del motor nativo (correccion de la revision, 2026-11-13).

Cada flag, prendido y apagado, llega a lo que lo usa: ``FLOW_ENGINE_NATIVE_MULTITOUCH_ENABLED`` al despachador
(los toques siguientes), al runtime del motor (``le_gana_a`` y la audiencia) y al ``NativeRuntime`` del validador;
``FLOW_ENGINE_NATIVE_PERSONAL_COUPON_ENABLED`` al despachador y al ``NativeRuntime``;
``FLOW_ENGINE_NATIVE_PERSON_CAP_ENABLED`` al despachador. Y cada requisito de arranque del cupon propio corta el
arranque con su ``ValueError``.

El arranque reusa el carrito de ATT1 con sus tres toques de ``test_flow_engine_bridge_settings``.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_cart_discount_first_touch import _app
from test_flow_engine_bridge_settings import engine_settings

from bridge import app as app_module
from bridge.flow_validator import NativeRuntime
from bridge.native_personal_coupon import NativePersonalCouponConfig


def _wired(tmp_path: Path, **overrides: Any) -> tuple[Any, Any, Any]:
    settings = engine_settings(tmp_path, **overrides)
    app = _app(settings)
    runtime = app_module.instance_runtime_from(settings, native_silence_sweeper=False)
    return app.state.durable_dispatcher, app.state.resolution_worker._flow_engine, runtime


def test_with_every_e483_flag_off_nothing_is_wired(tmp_path: Path) -> None:
    dispatcher, engine, runtime = _wired(tmp_path)
    assert dispatcher._native_multitouch_enabled is False
    assert dispatcher._native_person_cap is None
    assert dispatcher._native_personal_coupon is None
    assert engine.native_priority_wins is False and engine.native_audience is False
    assert isinstance(runtime, NativeRuntime)
    assert runtime.native_multitouch is False and runtime.native_personal_coupon is False


def test_the_multitouch_flag_reaches_the_dispatcher_the_engine_and_the_validator(tmp_path: Path) -> None:
    dispatcher, engine, runtime = _wired(tmp_path, flow_engine_native_multitouch_enabled=True)
    assert dispatcher._native_multitouch_enabled is True
    # C7 y la audiencia van con el mismo flag.
    assert engine.native_priority_wins is True and engine.native_audience is True
    assert runtime.native_multitouch is True and runtime.native_personal_coupon is False


def test_the_person_cap_flag_reaches_the_dispatcher_with_its_values(tmp_path: Path) -> None:
    dispatcher, _, _ = _wired(tmp_path, flow_engine_native_person_cap_enabled=True,
                              flow_engine_native_person_cap_max_sends=3, flow_engine_native_person_cap_window_hours=48)
    assert dispatcher._native_person_cap == app_module.NativePersonCap(max_sends=3, window_hours=48)


def test_the_personal_coupon_flag_reaches_the_dispatcher_and_the_validator(tmp_path: Path) -> None:
    dispatcher, _, runtime = _wired(tmp_path, flow_engine_native_personal_coupon_enabled=True,
                                    recovery_coupon_worker_enabled=True)
    settings = engine_settings(tmp_path, flow_engine_native_personal_coupon_enabled=True,
                               recovery_coupon_worker_enabled=True)
    assert dispatcher._native_personal_coupon == NativePersonalCouponConfig(
        price=settings.recovery_discount_price, currency=settings.recovery_discount_currency)
    assert runtime.native_personal_coupon is True and runtime.native_multitouch is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"flow_engine_enabled": False, "worker_enabled": False},
         "FLOW_ENGINE_NATIVE_PERSONAL_COUPON_ENABLED requires FLOW_ENGINE_ENABLED"),
        ({"recovery_coupon_worker_enabled": False},
         "FLOW_ENGINE_NATIVE_PERSONAL_COUPON_ENABLED requires RECOVERY_COUPON_WORKER_ENABLED"),
        ({"dispatcher_approved_template_direct_enabled": False},
         "FLOW_ENGINE_NATIVE_PERSONAL_COUPON_ENABLED requires DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED"),
    ],
)
def test_each_startup_requirement_of_the_personal_coupon(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    fields: dict[str, Any] = {"flow_engine_native_personal_coupon_enabled": True,
                              "recovery_coupon_worker_enabled": True, **overrides}
    settings = engine_settings(tmp_path, **fields)
    with pytest.raises(ValueError, match=f"^{message}$"):
        app_module._validate_flow_engine_native_personal_coupon(settings)


def test_a_price_that_cannot_be_said_stops_the_personal_coupon(tmp_path: Path) -> None:
    settings = engine_settings(tmp_path, flow_engine_native_personal_coupon_enabled=True,
                               recovery_coupon_worker_enabled=True, recovery_discount_price=Decimal("-1"))
    with pytest.raises(ValueError):
        app_module._validate_flow_engine_native_personal_coupon(settings)
    # Apagado no se lee nada, tampoco el precio.
    app_module._validate_flow_engine_native_personal_coupon(
        engine_settings(tmp_path, recovery_discount_price=Decimal("-1")))


def test_create_app_runs_the_personal_coupon_gate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="RECOVERY_COUPON_WORKER_ENABLED"):
        _app(engine_settings(tmp_path, flow_engine_native_personal_coupon_enabled=True,
                             recovery_coupon_worker_enabled=False))
