"""El seguimiento del pago rechazado armado por ``create_app``, y el registro de B1.

Contrato: ``docs/contracts/portable-payment-failure-followup-v1.md``.

El arranque reusa los ``Settings`` de B1 (``test_payment_failure_variant``,
sobre el manifiesto capturado de ATT1): el seguimiento va encima de B1 en
variantes. El registro de la variante reusa el despachador de B1
(``test_payment_failure_b1_dispatch``, con el catalogo capturado de las cuatro
B1).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_payment_failure_b1_dispatch import (
    BARE,
    NAME,
    PRODUCT,
    WHEN,
    _RefusalAuthority,
    _run,
    _sent,
)
from test_payment_failure_variant import _settings, instance  # noqa: F401
from test_instance_wiring import _environment, _first_contact_app
from test_payment_slip_wiring import _SlipAuthority

from bridge import app as app_module
from bridge.app import Settings
from bridge.payment_failure_followups import PaymentFailureFollowupConfig
from bridge.payment_failure_variant import VARIANT_DECLINED
from bridge.supabase import SupabaseError, SupabasePermanentError

B2 = "att1_pago_rechazado_descuento_01"
Z = "att1_ultima_renovacion_01"


# ------------------------------------------------------------------ arranque


def _with_followup(instance: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "payment_failure_followup_enabled": True,
        "waba_payment_failure_template_second_touch": B2,
        "waba_payment_failure_template_last_touch": Z,
    }
    values.update(overrides)
    return _settings(instance, **values)


def test_create_app_gives_the_dispatcher_b2_and_z(instance: Path) -> None:  # noqa: F811
    app = _first_contact_app(_with_followup(instance), _SlipAuthority())

    dispatcher = app.state.durable_dispatcher  # type: ignore[attr-defined]
    assert dispatcher._payment_failure_followup == PaymentFailureFollowupConfig(
        language=dispatcher._waba_template.language,
        second_touch_template=B2,
        last_touch_template=Z,
    )
    assert "payment_failure_followup_enabled" in (
        app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    )


def test_the_flag_is_off_by_default_and_nothing_is_wired(instance: Path) -> None:  # noqa: F811
    # Con las plantillas cargadas y el flag apagado no se lee nada: el paso
    # cierra como cualquier paso siguiente del modo directo.
    app = _first_contact_app(
        _with_followup(instance, payment_failure_followup_enabled=False), _SlipAuthority()
    )

    assert app.state.durable_dispatcher._payment_failure_followup is None  # type: ignore[attr-defined]
    assert Settings.__dataclass_fields__["payment_failure_followup_enabled"].default is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"payment_failure_variants_enabled": False},
         "PAYMENT_FAILURE_FOLLOWUP_ENABLED requires PAYMENT_FAILURE_VARIANTS_ENABLED"),
        ({"recovery_coupon_worker_enabled": False}, "RECOVERY_COUPON_WORKER_ENABLED"),
        ({"waba_payment_failure_template_second_touch": None},
         "WABA_PAYMENT_FAILURE_TEMPLATE_SECOND_TOUCH"),
        ({"waba_payment_failure_template_last_touch": None},
         "WABA_PAYMENT_FAILURE_TEMPLATE_LAST_TOUCH"),
        ({"waba_payment_failure_template_last_touch": B2}, "must differ"),
    ],
)
def test_an_incomplete_wiring_does_not_start(
    instance: Path, overrides: dict[str, object], message: str  # noqa: F811
) -> None:
    with pytest.raises(ValueError, match=message):
        _first_contact_app(_with_followup(instance, **overrides), _SlipAuthority())


def test_from_env_reads_the_followup_variables(
    monkeypatch: pytest.MonkeyPatch, instance: Path  # noqa: F811
) -> None:
    _environment(monkeypatch, INSTANCE_MANIFEST_PATH=str(instance / "instancia.toml"))
    for name in (
        "PAYMENT_FAILURE_FOLLOWUP_ENABLED",
        "WABA_PAYMENT_FAILURE_TEMPLATE_SECOND_TOUCH",
        "WABA_PAYMENT_FAILURE_TEMPLATE_LAST_TOUCH",
    ):
        monkeypatch.delenv(name, raising=False)

    off = Settings.from_env()

    assert off.payment_failure_followup_enabled is False
    assert off.waba_payment_failure_template_second_touch is None
    assert off.waba_payment_failure_template_last_touch is None

    monkeypatch.setenv("PAYMENT_FAILURE_FOLLOWUP_ENABLED", "true")
    monkeypatch.setenv("WABA_PAYMENT_FAILURE_TEMPLATE_SECOND_TOUCH", f" {B2} ")
    monkeypatch.setenv("WABA_PAYMENT_FAILURE_TEMPLATE_LAST_TOUCH", Z)

    on = Settings.from_env()

    assert on.payment_failure_followup_enabled is True
    assert on.waba_payment_failure_template_second_touch == B2
    assert on.waba_payment_failure_template_last_touch == Z


def test_the_env_example_ships_it_off() -> None:
    env_example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )
    assert "\nPAYMENT_FAILURE_FOLLOWUP_ENABLED=false\n" in env_example
    assert "\nWABA_PAYMENT_FAILURE_TEMPLATE_SECOND_TOUCH=\n" in env_example
    assert "\nWABA_PAYMENT_FAILURE_TEMPLATE_LAST_TOUCH=\n" in env_example


# ------------------------------------------------------- el registro de B1


@pytest.mark.parametrize(
    "error",
    [
        SupabaseError("HTTP 404"),
        SupabasePermanentError(
            "rejected", reason="payment_failure_first_touch_record_conflict"
        ),
    ],
)
def test_a_record_that_fails_never_stops_b1(tmp_path: Path, error: SupabaseError) -> None:
    # Una base sin la migracion 20261019000700, o un registro que choca: B1
    # sale igual. Lo peor que pasa es que el seguimiento salga como B2.
    authority = _RefusalAuthority(refusal_kind="declined", record_error=error)

    chatwoot = _run(authority, tmp_path)

    _sent(chatwoot, VARIANT_DECLINED, [NAME, WHEN, PRODUCT], BARE)
    assert authority.records == [{"variant": VARIANT_DECLINED, "coupon_code": None}]
    assert authority.events.index("record") < authority.events.index("request_started")


def test_without_the_variants_nothing_is_recorded(tmp_path: Path) -> None:
    authority = _RefusalAuthority(refusal_kind="insufficient_funds")

    _run(authority, tmp_path, variants=None)

    assert authority.records == []
