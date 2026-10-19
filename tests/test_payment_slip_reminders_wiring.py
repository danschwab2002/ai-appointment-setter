"""C2 y C3 de ATT1: el manifiesto y el arranque de los recordatorios de la ficha.

Contrato: docs/contracts/portable-payment-slip-reminders-v1.md. Las plantillas
son las aprobadas en Meta para ATT1 (catalogo capturado el 2026-10-19,
``chatwoot_message_templates_inbox_11_ficha_recordatorios_20261019.json``):
``att1_ficha_vence_manana_01`` (MARKETING) y ``att1_ficha_vence_hoy_01``
(UTILITY).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_instance_manifest import _load, _payload, _with_slip
from test_payment_slip_wiring import (  # noqa: F401  (instance es un fixture)
    _slip_manifest,
    _slip_settings,
    _SlipAuthority,
    instance,
)
from test_instance_wiring import _first_contact_app

from bridge.instance_manifest import ManifestError, Template
from bridge.messaging import PaymentSlipStepTemplate

C2 = "att1_ficha_vence_manana_01"
C3 = "att1_ficha_vence_hoy_01"


# ------------------------------------------------------------ el manifiesto


def _with_reminders(payload: dict) -> dict:
    payload["plantillas"]["ficha_vence_manana"] = {"nombre": C2, "idioma": "es_MX"}
    payload["plantillas"]["ficha_vence_hoy"] = {"nombre": C3, "idioma": "es_MX"}
    return payload


def test_the_reminders_load_with_the_order_of_their_approved_templates() -> None:
    manifest = _load(_with_reminders(_with_slip(_payload("att1"))))

    assert manifest.templates["ficha_vence_manana"] == Template(
        name=C2, language="es_MX",
        parameters=("nombre", "tienda", "producto", "vencimiento"),
    )
    assert manifest.templates["ficha_vence_hoy"] == Template(
        name=C3, language="es_MX", parameters=("nombre", "tienda", "producto"),
    )


def test_a_manifest_without_the_reminders_loads_as_before() -> None:
    manifest = _load(_with_slip(_payload("att1")))

    assert "ficha_vence_manana" not in manifest.templates
    assert "ficha_vence_hoy" not in manifest.templates
    assert manifest.flow_blockers("ficha") == ()


def test_a_reminder_takes_the_vocabulary_of_the_slip() -> None:
    payload = _with_reminders(_with_slip(_payload("att1")))
    payload["plantillas"]["ficha_vence_hoy"]["parametros"] = ["nombre", "vencimiento"]
    assert _load(payload).templates["ficha_vence_hoy"].parameters == ("nombre", "vencimiento")

    payload["plantillas"]["ficha_vence_hoy"]["parametros"] = ["nombre", "cupon"]
    with pytest.raises(ManifestError, match="variables no soportadas cupon"):
        _load(payload)


def test_a_reminder_without_the_slip_template_does_not_load() -> None:
    payload = _with_reminders(_with_slip(_payload("att1"), template=False))

    with pytest.raises(ManifestError, match="ficha_vence_manana es un recordatorio de la ficha"):
        _load(payload)


# ------------------------------------------------------------ el arranque


def _reminder_manifest() -> object:
    manifest = _slip_manifest()
    return replace(manifest, templates={
        **manifest.templates,
        "ficha_vence_manana": Template(
            name=C2, language="es_MX",
            parameters=("nombre", "tienda", "producto", "vencimiento"),
        ),
        "ficha_vence_hoy": Template(
            name=C3, language="es_MX", parameters=("nombre", "tienda", "producto"),
        ),
    })


REMINDERS = {
    "waba_payment_slip_due_eve_template_name": C2,
    "waba_payment_slip_due_eve_template_category": "MARKETING",
    "waba_payment_slip_due_day_template_name": C3,
    "waba_payment_slip_due_day_template_category": "UTILITY",
}


def test_the_bridge_hands_each_reminder_template_to_the_dispatcher(instance: Path) -> None:  # noqa: F811
    app = _first_contact_app(
        _slip_settings(instance, instance_manifest=_reminder_manifest(), **REMINDERS),
        _SlipAuthority(),
    )

    template = app.state.durable_dispatcher._waba_template  # type: ignore[attr-defined]
    assert template.payment_slip_steps == (
        PaymentSlipStepTemplate(
            step_key="payment_slip_due_eve", name=C2, category="MARKETING",
            body_parameters=("nombre", "tienda", "producto", "vencimiento"),
        ),
        PaymentSlipStepTemplate(
            step_key="payment_slip_due_day", name=C3, category="UTILITY",
            body_parameters=("nombre", "tienda", "producto"),
        ),
    )


def test_without_the_reminders_the_slip_stays_in_c1(instance: Path) -> None:  # noqa: F811
    app = _first_contact_app(_slip_settings(instance), _SlipAuthority())

    template = app.state.durable_dispatcher._waba_template  # type: ignore[attr-defined]
    assert template.payment_slip_steps == ()
    assert template.payment_slip_name == "att1_ficha_efectivo_01"


def test_with_the_slip_flag_off_no_reminder_is_wired(instance: Path) -> None:  # noqa: F811
    app = _first_contact_app(
        _slip_settings(
            instance, instance_manifest=_reminder_manifest(),
            portable_hotmart_payment_slip_enabled=False, **REMINDERS,
        ),
        _SlipAuthority(),
    )

    template = app.state.durable_dispatcher._waba_template  # type: ignore[attr-defined]
    assert template.payment_slip_steps == ()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        # Declarado en el manifiesto sin su variable.
        ({"waba_payment_slip_due_eve_template_name": None},
         "WABA_PAYMENT_SLIP_DUE_EVE_TEMPLATE_NAME must match plantillas.ficha_vence_manana"),
        ({"waba_payment_slip_due_day_template_name": "att1_otra_01"},
         "WABA_PAYMENT_SLIP_DUE_DAY_TEMPLATE_NAME must match plantillas.ficha_vence_hoy"),
        ({"waba_payment_slip_due_day_template_category": None},
         "WABA_PAYMENT_SLIP_DUE_DAY_TEMPLATE_CATEGORY must be MARKETING or UTILITY"),
    ],
)
def test_an_incomplete_reminder_does_not_start(
    instance: Path, overrides: dict[str, object], message: str  # noqa: F811
) -> None:
    settings = _slip_settings(
        instance, instance_manifest=_reminder_manifest(), **{**REMINDERS, **overrides}
    )
    with pytest.raises(ValueError, match=message):
        _first_contact_app(settings, _SlipAuthority())


def test_a_reminder_variable_without_its_manifest_slot_does_not_start(
    instance: Path,  # noqa: F811
) -> None:
    with pytest.raises(ValueError, match="plantillas.ficha_vence_manana.nombre"):
        _first_contact_app(_slip_settings(instance, **REMINDERS), _SlipAuthority())
