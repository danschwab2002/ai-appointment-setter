"""E-528 (C4, D-P25-6): la ficha del legado y la ficha nativa, separadas.

``PORTABLE_HOTMART_PAYMENT_SLIP_LEGACY_ENABLED`` (por defecto ``true``: ATT1 no cambia). En ``false``,
``PORTABLE_HOTMART_PAYMENT_SLIP_ENABLED`` prende solo la admision v2, el filtro de ``[[ficha.medios]]`` y la
ficha nativa (``FLOW_ENGINE_NATIVE_PAYMENT_SLIP_ENABLED``, obligatorio), sin el scope, la politica ni la
plantilla del legado: nunca mandan las dos. Es lo que necesita Maribel, cuya ficha del legado nunca mando
(``ficha = false`` en su manifiesto).

El molde es ``test_payment_slip_wiring.py`` (la instancia de ATT1 copiada, la ficha capturada de ATT1).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_payment_slip_wiring import (
    METHODS,
    EVENT_ID,
    _Base,
    _SlipAuthority,
    _process,
    _slip,
    _slip_manifest,
    _slip_settings,
    instance,  # noqa: F401 - el fixture
)
from test_instance_wiring import _first_contact_app

from bridge.app import Settings
from bridge.payment_slip_first_touch import PaymentSlipFirstTouchConfig
from bridge.worker import ResolutionWorker

NATIVE_ONLY = {
    "portable_hotmart_payment_slip_native_only": True,
    "flow_engine_native_payment_slip_enabled": True,
    "flow_engine_enabled": True,
}


def test_the_legacy_slip_is_on_by_default_and_only_false_turns_it_off(monkeypatch: pytest.MonkeyPatch,
                                                                      instance: Path) -> None:  # noqa: F811
    from test_instance_wiring import _environment

    assert Settings.__dataclass_fields__["portable_hotmart_payment_slip_native_only"].default is False
    _environment(monkeypatch, INSTANCE_MANIFEST_PATH=str(instance / "instancia.toml"))
    assert Settings.from_env().portable_hotmart_payment_slip_legacy_enabled is True
    monkeypatch.setenv("PORTABLE_HOTMART_PAYMENT_SLIP_LEGACY_ENABLED", "false")
    assert Settings.from_env().portable_hotmart_payment_slip_legacy_enabled is False


def test_without_the_legacy_only_the_native_slip_is_wired(instance: Path) -> None:  # noqa: F811
    # Sin el scope, la politica ni la plantilla del legado, y con el manifiesto sin flujos.ficha (Maribel).
    manifest = replace(_slip_manifest(), flows={**_slip_manifest().flows, "ficha": False})
    settings = _slip_settings(instance, instance_manifest=manifest, pilot_payment_slip_scope_key=None,
                              pilot_payment_slip_scope_version=None, payment_slip_followup_policy_key=None,
                              payment_slip_followup_policy_version=None, waba_payment_slip_template_name=None,
                              waba_payment_slip_template_category=None, **NATIVE_ONLY)
    app = _first_contact_app(settings, _SlipAuthority())

    worker = app.state.resolution_worker  # type: ignore[attr-defined]
    dispatcher = app.state.durable_dispatcher  # type: ignore[attr-defined]
    assert worker._payment_slip is None and worker._native_payment_slip_methods == METHODS
    assert dispatcher._payment_slip is None
    assert dispatcher._native_payment_slip == PaymentSlipFirstTouchConfig(methods=METHODS)
    assert dispatcher._waba_template.first_touch_name_for(trigger_kind="payment_slip") is None


def test_the_legacy_off_needs_the_native_slip(instance: Path) -> None:  # noqa: F811
    with pytest.raises(ValueError, match="requires FLOW_ENGINE_NATIVE_PAYMENT_SLIP_ENABLED"):
        _first_contact_app(_slip_settings(instance, portable_hotmart_payment_slip_native_only=True),
                           _SlipAuthority())


def test_the_native_slip_needs_the_slip_admission(instance: Path) -> None:  # noqa: F811
    with pytest.raises(ValueError, match="requires FLOW_ENGINE_ENABLED and PORTABLE_HOTMART_PAYMENT_SLIP_ENABLED"):
        _first_contact_app(_slip_settings(instance, portable_hotmart_payment_slip_enabled=False,
                                          flow_engine_native_payment_slip_enabled=True, flow_engine_enabled=True),
                           _SlipAuthority())


def test_with_both_on_att1_keeps_its_legacy_slip(instance: Path) -> None:  # noqa: F811
    app = _first_contact_app(_slip_settings(instance), _SlipAuthority())
    assert app.state.resolution_worker._payment_slip is not None  # type: ignore[attr-defined]
    assert app.state.resolution_worker._native_payment_slip_methods is None  # type: ignore[attr-defined]
    assert app.state.durable_dispatcher._native_payment_slip is None  # type: ignore[attr-defined]


def _native_only_worker(base: _Base) -> ResolutionWorker:
    from test_payment_slip_wiring import ALLY, RECOVERY

    return ResolutionWorker(
        supabase=base,  # type: ignore[arg-type]
        policy_key="maribel-recuperacion",
        policy_version=1,
        allowed_jid=None,
        chatwoot_account_id=ALLY.chatwoot_account_id,
        chatwoot_inbox_id=ALLY.chatwoot_inbox_id,
        pilot_boundary=RECOVERY,
        commercial_ally_config=ALLY,
        payment_failure_enabled=True,
        payment_slip=None,
        native_payment_slip_enabled=True,
        native_payment_slip_methods=METHODS,
    )


def test_without_the_legacy_a_slip_no_native_flow_takes_ends_ignored_without_a_plan() -> None:
    # Sin el motor (ningun flujo nativo de la ficha): ni el legado ni un caso.
    base = _Base()
    _process(_native_only_worker(base), _slip(2))
    assert base.slip_plans == [] and base.cart_plans == []
    [status] = base.statuses
    assert status["status"] == "ignored"
    assert status["event_id"] == EVENT_ID


def test_without_the_legacy_a_slip_of_a_method_not_enabled_is_ignored_with_its_reason() -> None:
    base = _Base()
    _process(_native_only_worker(base), _slip(0))  # YAPE de Peru: no hay fila habilitada
    assert base.slip_plans == []
    [status] = base.statuses
    assert status["status"] == "ignored" and status["error"].startswith("payment_slip_")


def test_without_the_native_flag_a_slip_without_the_legacy_is_unsupported_as_today() -> None:
    from test_payment_slip_wiring import _worker

    base = _Base()
    _process(_worker(base, payment_slip=None), _slip(2))
    assert base.statuses[-1]["error"] == "unsupported_persisted_event_type"
