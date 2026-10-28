"""El pago rechazado con su scope y su politica propios, sin las variantes (E-157).

``PAYMENT_FAILURE_OWN_POLICY_ENABLED`` deja planificar el pago rechazado con
``LANCEMOS_PILOT_PAYMENT_FAILURE_SCOPE_*`` y ``PAYMENT_FAILURE_FOLLOWUP_POLICY_*``
(la gracia de su politica, los topes de su scope) y mandar la plantilla de hoy
(``WABA_PAYMENT_FAILURE_TEMPLATE_NAME``), sin B1: sin las cuatro plantillas, sin
el worker del cupon y sin el modo directo. El carrito no cambia.

Datos capturados, no inventados: el rechazo es la persona 12 de los 17
``PURCHASE_CANCELED`` de att1-db (``hotmart_purchase_canceled_att1_20261019.json``,
capturado el 2026-10-19 y anonimizado adentro de la base). El carrito del ultimo
test es DERIVADO de ese mismo aviso (solo cambia el tipo de evento y la oferta
de lugar), como el de ``test_payment_slip_wiring``. El planificador lo
autoriza la base contra el scope del caso
(``mark_portable_payment_failure_request_started``, 20261019000700), que no
mira si salio una variante: sin variante no se registra nada.
"""

from __future__ import annotations

import asyncio
import copy
import shutil
from pathlib import Path

import pytest
from test_instance_wiring import ATT1 as _ATT1_INSTANCE
from test_instance_wiring import _first_contact_app
from test_payment_failure_variant import (
    CAPTURE,
    NAMES,
    PLANNING,
    REFUSAL_SCOPE,
    _manifest,
    _RefusalBase,
    _RefusalScopeAuthority,
)
from test_payment_failure_variant import _settings as _b1_settings
from test_payment_slip_wiring import _get_ready, _SlipAuthority

from bridge import app as app_module
from bridge.app import Settings
from bridge.worker import ResolutionWorker

# Lo de B1 que este flag no pide: sin esto tiene que arrancar igual.
_WITHOUT_B1 = {
    "payment_failure_variants_enabled": False,
    "recovery_coupon_worker_enabled": False,
    "waba_payment_failure_template_coupon": None,
    "waba_payment_failure_template_two_refusals": None,
    "waba_payment_failure_template_insufficient_funds": None,
    "waba_payment_failure_template_declined": None,
}


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    target = tmp_path / "instancia"
    shutil.copytree(_ATT1_INSTANCE, target)
    return target


def _settings(instance: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        **_WITHOUT_B1,
        "payment_failure_own_policy_enabled": True,
    }
    values.update(overrides)
    return _b1_settings(instance, **values)


# ---------------------------------------------------------- el arranque


def test_the_flag_hands_its_own_policy_to_the_worker_without_the_variants(
    instance: Path,
) -> None:
    app = _first_contact_app(_settings(instance), _SlipAuthority())

    worker = app.state.resolution_worker  # type: ignore[attr-defined]
    dispatcher = app.state.durable_dispatcher  # type: ignore[attr-defined]
    # El pago rechazado: su scope y su politica.
    assert worker._payment_failure_planning == PLANNING
    # Sin B1: la plantilla de hoy, sin variantes.
    assert dispatcher._payment_failure_variants is None
    assert dispatcher._waba_template.payment_failure_name == "att1_compra_fallida_01"
    # El carrito, igual: su scope y su politica.
    assert (worker._policy_key, worker._policy_version) == ("att1-recuperacion-un-toque", 1)
    assert worker._pilot_boundary.scope_key == "att1-recuperacion"
    assert "payment_failure_own_policy_enabled" in (
        app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    )


def test_it_does_not_ask_for_the_direct_mode_or_the_coupon_worker(instance: Path) -> None:
    # B1 exige el modo directo y el worker del cupon; la plantilla de hoy no.
    # (El arranque entero de este set de prueba igual necesita el modo directo:
    # no tiene Hermes. Por eso se mira la validacion sola.)
    settings = _settings(instance, dispatcher_approved_template_direct_enabled=False)

    app_module._validate_payment_failure_own_scope(settings)
    with pytest.raises(ValueError, match="DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED"):
        app_module._validate_payment_failure_own_scope(
            _b1_settings(instance, dispatcher_approved_template_direct_enabled=False)
        )


def test_with_both_flags_off_nothing_is_wired_even_with_its_constants(
    instance: Path,
) -> None:
    # generar.py escribe las constantes siempre: sin los flags se ignoran y el
    # pago rechazado sigue con el scope y la politica del carrito.
    app = _first_contact_app(
        _settings(instance, payment_failure_own_policy_enabled=False), _SlipAuthority()
    )

    assert app.state.resolution_worker._payment_failure_planning is None  # type: ignore[attr-defined]
    assert app.state.durable_dispatcher._payment_failure_variants is None  # type: ignore[attr-defined]
    assert Settings.__dataclass_fields__["payment_failure_own_policy_enabled"].default is False


def test_with_the_variants_too_it_is_b1_as_today(instance: Path) -> None:
    # ATT1 corre con las variantes: sumarle este flag no le cambia nada.
    on = _first_contact_app(
        _b1_settings(instance, payment_failure_own_policy_enabled=True), _SlipAuthority()
    )
    today = _first_contact_app(_b1_settings(instance), _SlipAuthority())

    for app in (on, today):
        assert app.state.resolution_worker._payment_failure_planning == PLANNING  # type: ignore[attr-defined]
        assert dict(app.state.durable_dispatcher._payment_failure_variants.templates) == NAMES  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"pilot_payment_failure_scope_version": None}, "go together"),
        ({"payment_failure_followup_policy_version": None}, "go together"),
        ({"pilot_payment_failure_scope_key": None,
          "pilot_payment_failure_scope_version": None,
          "payment_failure_followup_policy_key": None,
          "payment_failure_followup_policy_version": None},
         "PAYMENT_FAILURE_OWN_POLICY_ENABLED requires LANCEMOS_PILOT_PAYMENT_FAILURE_SCOPE"),
        ({"portable_hotmart_payment_failure_enabled": False},
         "PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED"),
        ({"payment_failure_followup_policy_version": 0}, "must be positive"),
        ({"pilot_payment_failure_scope_key": "att1-recuperacion"}, "must differ"),
        ({"payment_failure_followup_policy_key": "att1-recuperacion-un-toque"}, "must differ"),
        ({"waba_payment_failure_template_name": None, "dispatcher_outbound_enabled": False},
         "PAYMENT_FAILURE_OWN_POLICY_ENABLED requires WABA_PAYMENT_FAILURE_TEMPLATE_NAME"),
    ],
)
def test_an_incomplete_own_policy_does_not_start(
    instance: Path, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _first_contact_app(_settings(instance, **overrides), _SlipAuthority())


def test_from_env_reads_the_flag(monkeypatch: pytest.MonkeyPatch, instance: Path) -> None:
    from test_instance_wiring import _environment

    _environment(monkeypatch, INSTANCE_MANIFEST_PATH=str(instance / "instancia.toml"))
    assert Settings.from_env().payment_failure_own_policy_enabled is False
    monkeypatch.setenv("PAYMENT_FAILURE_OWN_POLICY_ENABLED", "true")

    settings = Settings.from_env()

    assert settings.payment_failure_own_policy_enabled is True
    assert settings.payment_failure_variants_enabled is False


def test_readiness_reports_the_refusal_scope_with_the_flag(instance: Path) -> None:
    authority = _RefusalScopeAuthority()

    ready = _get_ready(_first_contact_app(_settings(instance), authority))

    assert ready.status_code == 200
    assert ready.json()["portable_payment_failure_own_scope"] == "armed"
    assert authority.refusal_boundaries == [REFUSAL_SCOPE]


def test_readiness_fails_when_the_refusal_scope_is_not_armed_or_published(
    instance: Path,
) -> None:
    authority = _RefusalScopeAuthority(
        configured=False, runtime_state=None, reason_code="pilot_scope_config_mismatch"
    )

    ready = _get_ready(_first_contact_app(_settings(instance), authority))

    assert ready.status_code == 503
    assert ready.json()["detail"] == "portable_payment_failure_pilot_scope_config_mismatch"


# ------------------------------------------------- el worker de resolucion


def _worker(base: _RefusalBase) -> ResolutionWorker:
    from test_payment_slip_wiring import ALLY, RECOVERY

    return ResolutionWorker(
        supabase=base,  # type: ignore[arg-type]
        policy_key="att1-recuperacion-un-toque",
        policy_version=5,
        allowed_jid=None,
        chatwoot_account_id=ALLY.chatwoot_account_id,
        chatwoot_inbox_id=ALLY.chatwoot_inbox_id,
        pilot_boundary=RECOVERY,
        commercial_ally_config=ALLY,
        payment_failure_enabled=True,
        payment_failure_planning=PLANNING,
    )


def _refusal() -> dict[str, object]:
    # La persona 12: «Saldo insuficiente.», de Mexico.
    [payload] = [event["payload"] for event in CAPTURE if event["persona"] == 12]
    return copy.deepcopy(payload)


def test_a_refusal_is_planned_with_the_grace_of_its_own_policy() -> None:
    base = _RefusalBase()
    payload = _refusal()

    asyncio.run(_worker(base)._process_one({
        "id": "6f1d2c3b-0000-4000-8000-000000000212",
        "event_type": payload["event"],
        "payload": payload,
    }))

    [plan] = base.failure_plans
    assert (plan["policy_key"], plan["policy_version"]) == ("att1-pago-rechazado-un-toque", 1)
    assert plan["pilot_boundary"] == REFUSAL_SCOPE
    assert base.cart_plans == []


def test_a_cart_with_the_flag_is_planned_as_today() -> None:
    from test_payment_slip_wiring import RECOVERY

    base = _RefusalBase()
    cart = _refusal()
    cart["event"] = "PURCHASE_OUT_OF_SHOPPING_CART"
    cart["data"]["offer"] = cart["data"]["purchase"]["offer"]  # type: ignore[index]

    asyncio.run(_worker(base)._process_one({
        "id": "6f1d2c3b-0000-4000-8000-000000000213",
        "event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
        "payload": cart,
    }))

    [plan] = base.cart_plans
    assert (plan["policy_key"], plan["policy_version"]) == ("att1-recuperacion-un-toque", 5)
    assert plan["pilot_boundary"] is RECOVERY
    assert base.failure_plans == []


def test_the_manifest_of_the_test_declares_the_refusal_flow() -> None:
    # El flag cuelga de [flujos].pago_fallido (PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED).
    assert "pago_fallido" in _manifest().flows  # type: ignore[attr-defined]
