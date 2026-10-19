"""B1 del pago rechazado: la eleccion de la variante, su configuracion y su cableado.

Datos capturados, no inventados: los 17 ``PURCHASE_CANCELED`` de att1-db desde
el 11/10, anonimizados adentro de la base
(``hotmart_purchase_canceled_att1_20261019.json``), contra el manifiesto de ATT1
de ``tests/fixtures/instances/att1/instancia.toml``. El tipo de rechazo de cada
uno lo da la base (``payment_failure_refusal_kind``) y lo fija
``tests/sql/followup_engine/validate_portable_payment_failure_variants.mjs``;
aca se usa ese resultado.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_instance_wiring import (
    ATT1 as _ATT1_INSTANCE,
)
from test_instance_wiring import (
    _att1_manifest,
    _first_contact_app,
    _first_contact_settings,
)
from test_payment_slip_wiring import _Base, _get_ready, _SlipAuthority

from bridge import app as app_module
from bridge.app import Settings
from bridge.payment_failure_variant import (
    VARIANT_COUPON,
    VARIANT_DECLINED,
    VARIANT_INSUFFICIENT_FUNDS,
    VARIANT_TWO_REFUSALS,
    PaymentFailurePlanningConfig,
    PaymentFailureVariantsConfig,
    choose_variant,
    payment_failure_values,
    recovery_button_suffix,
)
from bridge.supabase import PilotBoundaryConfig
from bridge.worker import ResolutionWorker

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURE: list[dict[str, Any]] = json.loads(
    (FIXTURES / "hotmart_purchase_canceled_att1_20261019.json").read_text(encoding="utf-8")
)["eventos"]
# Lo que da payment_failure_refusal_kind sobre los 17, en orden (lo fija el
# validador de la migracion).
KINDS = [
    "declined", "declined", "declined", "insufficient_funds", "declined", "not_a_refusal",
    "declined", "not_a_refusal", "declined", "not_a_refusal", "not_a_refusal", "declined",
    "declined", "declined", "insufficient_funds", "insufficient_funds", "not_a_refusal",
]
NAMES = {
    VARIANT_COUPON: "att1_pago_rechazado_cupon_01",
    VARIANT_TWO_REFUSALS: "att1_pago_rechazado_dos_veces_01",
    VARIANT_INSUFFICIENT_FUNDS: "att1_pago_rechazado_saldo_01",
    VARIANT_DECLINED: "att1_pago_rechazado_banco_01",
}
LINK = (
    "https://pay.hotmart.com/D98014973Y?off=2uafw5bg&checkoutMode=10"
    "&src=hermes&sck=hermes~v1~01JABCDEFGHJKMNPQRSTVWXYZ1"
)


# ------------------------------------------------------------- la variante


def _people() -> dict[int, list[tuple[dict[str, Any], str]]]:
    people: dict[int, list[tuple[dict[str, Any], str]]] = {}
    for event, kind in zip(CAPTURE, KINDS, strict=True):
        people.setdefault(event["persona"], []).append((event["payload"], kind))
    return people


def test_the_captured_refusals_pick_the_variant_of_the_flow() -> None:
    # Una sola plantilla por persona, la del ultimo evento, con los rechazos
    # que lleva hasta ahi. Los cancelados sin motivo no se planifican.
    picked: dict[int, str] = {}
    for persona, events in _people().items():
        refusals = [kind for _, kind in events if kind != "not_a_refusal"]
        if not refusals:
            continue
        picked[persona] = choose_variant(
            refusal_kind=refusals[-1],
            payment_failure_count=len(refusals),
            other_coupon_code=None,
        )

    assert picked == {
        1: VARIANT_DECLINED, 2: VARIANT_TWO_REFUSALS, 3: VARIANT_INSUFFICIENT_FUNDS,
        4: VARIANT_DECLINED, 5: VARIANT_DECLINED, 7: VARIANT_DECLINED,
        9: VARIANT_TWO_REFUSALS, 10: VARIANT_DECLINED, 11: VARIANT_INSUFFICIENT_FUNDS,
        12: VARIANT_INSUFFICIENT_FUNDS,
    }


@pytest.mark.parametrize(
    ("kind", "count", "coupon", "expected"),
    [
        ("declined", 1, None, VARIANT_DECLINED),
        ("insufficient_funds", 1, None, VARIANT_INSUFFICIENT_FUNDS),
        ("insufficient_funds", 2, None, VARIANT_TWO_REFUSALS),
        ("declined", 3, None, VARIANT_TWO_REFUSALS),
        ("declined", 2, "ATT1CARRITO00001", VARIANT_COUPON),
        ("insufficient_funds", 1, "ATT1CARRITO00001", VARIANT_COUPON),
    ],
)
def test_the_first_variant_that_applies_wins(
    kind: str, count: int, coupon: str | None, expected: str
) -> None:
    assert choose_variant(
        refusal_kind=kind, payment_failure_count=count, other_coupon_code=coupon
    ) == expected


@pytest.mark.parametrize(("kind", "count"), [("not_a_refusal", 1), ("otra", 1), ("declined", 0)])
def test_a_context_out_of_contract_does_not_pick_a_text(kind: str, count: int) -> None:
    with pytest.raises(ValueError):
        choose_variant(refusal_kind=kind, payment_failure_count=count, other_coupon_code=None)


# ------------------------------------------------- las variables y el boton


def test_the_values_go_in_the_hour_of_the_country_and_only_the_variant_ones() -> None:
    now = datetime(2026, 10, 19, 16, 0, tzinfo=UTC)
    failed = now - timedelta(minutes=15)

    declined = payment_failure_values(
        variant=VARIANT_DECLINED, buyer_name="Ana  María", product_name="Alimenta tu Tiroides",
        failed_at=failed, coupon_expires_at=None, country="MX", now=now,
    )
    funds = payment_failure_values(
        variant=VARIANT_INSUFFICIENT_FUNDS, buyer_name="Ana", product_name="Alimenta tu Tiroides",
        failed_at=failed, coupon_expires_at=now + timedelta(hours=24), country="MX", now=now,
    )

    # 15:45Z son las 9:45 en Ciudad de Mexico.
    assert declined == {
        "nombre": "Ana María", "cuando": "hoy a las 9:45 a. m.",
        "producto": "Alimenta tu Tiroides",
    }
    assert list(funds) == ["nombre", "cuando", "producto", "vence"]
    assert funds["vence"] == "mañana a las 10:00 a. m."


def test_a_coupon_variant_without_a_live_coupon_is_refused() -> None:
    now = datetime(2026, 10, 19, 16, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="payment_failure_coupon_already_expired"):
        payment_failure_values(
            variant=VARIANT_COUPON, buyer_name="Ana", product_name="P",
            failed_at=now, coupon_expires_at=now, country="MX", now=now,
        )


def test_the_button_carries_our_link_and_the_coupon_only_when_there_is_one() -> None:
    bare = LINK.removeprefix("https://pay.hotmart.com/")
    assert recovery_button_suffix(LINK, None) == bare
    assert recovery_button_suffix(LINK, "ATT1SALDO0000001") == f"{bare}&offDiscount=ATT1SALDO0000001"
    for wrong in ("https://otro.example/D98014973Y?off=x", "https://pay.hotmart.com/"):
        with pytest.raises(ValueError):
            recovery_button_suffix(wrong, None)


# --------------------------------------------------------- la configuracion


def test_the_config_needs_the_four_distinct_templates() -> None:
    PaymentFailureVariantsConfig(templates=NAMES, language="es_MX")
    with pytest.raises(ValueError, match="payment_failure_templates_invalid"):
        PaymentFailureVariantsConfig(
            templates={k: v for k, v in NAMES.items() if k != VARIANT_COUPON}, language="es_MX"
        )
    with pytest.raises(ValueError, match="payment_failure_templates_not_distinct"):
        PaymentFailureVariantsConfig(
            templates={**NAMES, VARIANT_COUPON: NAMES[VARIANT_DECLINED]}, language="es_MX"
        )
    with pytest.raises(ValueError, match="payment_failure_categories_invalid"):
        PaymentFailureVariantsConfig(
            templates=NAMES, language="es_MX",
            categories={**dict.fromkeys(NAMES, "UTILITY"), VARIANT_COUPON: "AUTHENTICATION"},
        )


# ------------------------------------------------- el worker de resolucion

REFUSAL_SCOPE = PilotBoundaryConfig(
    scope_key="att1-pago-rechazado",
    scope_version=1,
    tenant_key="lancemos",
    channel_provider="waba",
    channel_account_ref="chatwoot-inbox:11",
)
PLANNING = PaymentFailurePlanningConfig(
    policy_key="att1-pago-rechazado-un-toque", policy_version=1, pilot_boundary=REFUSAL_SCOPE,
)


class _RefusalBase(_Base):
    def __init__(self) -> None:
        super().__init__()
        self.failure_plans: list[dict[str, object]] = []

    async def plan_payment_failure_recovery(self, **kwargs: object) -> object:
        self.failure_plans.append(kwargs)
        return type("Plan", (), {"recovery_case_id": "00000000-0000-4000-8000-000000000011"})()


def _process(planning: PaymentFailurePlanningConfig | None) -> _RefusalBase:
    from test_payment_slip_wiring import ALLY, RECOVERY

    base = _RefusalBase()
    worker = ResolutionWorker(
        supabase=base,  # type: ignore[arg-type]
        policy_key="att1-recuperacion-un-toque",
        policy_version=5,
        allowed_jid=None,
        chatwoot_account_id=ALLY.chatwoot_account_id,
        chatwoot_inbox_id=ALLY.chatwoot_inbox_id,
        pilot_boundary=RECOVERY,
        commercial_ally_config=ALLY,
        payment_failure_enabled=True,
        payment_failure_planning=planning,
    )
    # La persona 12: «Saldo insuficiente.», de Mexico.
    [payload] = [event["payload"] for event in CAPTURE if event["persona"] == 12]
    asyncio.run(worker._process_one({
        "id": "6f1d2c3b-0000-4000-8000-000000000112",
        "event_type": payload["event"],
        "payload": payload,
    }))
    return base


def test_a_refusal_is_planned_under_its_own_scope_and_policy() -> None:
    [plan] = _process(PLANNING).failure_plans

    assert plan["policy_key"] == "att1-pago-rechazado-un-toque"
    assert plan["policy_version"] == 1
    assert plan["pilot_boundary"] == REFUSAL_SCOPE


def test_without_its_own_scope_a_refusal_is_planned_as_today() -> None:
    from test_payment_slip_wiring import RECOVERY

    [plan] = _process(None).failure_plans

    assert plan["policy_key"] == "att1-recuperacion-un-toque"
    assert plan["pilot_boundary"] == RECOVERY


# ---------------------------------------------------------- el arranque

@pytest.fixture
def instance(tmp_path: Path) -> Path:
    target = tmp_path / "instancia"
    shutil.copytree(_ATT1_INSTANCE, target)
    return target


def _manifest() -> object:
    # El del primer contacto del formulario, con el flujo del pago fallido.
    manifest = _att1_manifest(precheckout=True, inbound=True, pago_fallido=True)
    return replace(manifest, events=frozenset(manifest.events | {"intencion"}))


def _settings(instance: Path, **overrides: object) -> Settings:
    credentials = instance.parent / "hotmart-api-credentials"
    # Valores inventados con la forma de la credencial (test_recovery_coupon).
    credentials.write_text(
        "HOTMART_CLIENT_ID=00000000-0000-4000-8000-000000000001\n"
        "HOTMART_CLIENT_SECRET=00000000-0000-4000-8000-000000000002\n"
        "HOTMART_BASIC=MDAwMDAwMDAtMDAwMDo=\n"
    )
    values: dict[str, object] = {
        "instance_manifest": _manifest(),
        "hotmart_api_credentials_file": credentials,
        "worker_enabled": True,
        "followup_policy_key": "att1-recuperacion-un-toque",
        "followup_policy_version": 1,
        "portable_hotmart_payment_failure_enabled": True,
        "pilot_payment_failure_scope_key": "att1-pago-rechazado",
        "pilot_payment_failure_scope_version": 1,
        "payment_failure_followup_policy_key": "att1-pago-rechazado-un-toque",
        "payment_failure_followup_policy_version": 1,
        "payment_failure_variants_enabled": True,
        "recovery_coupon_worker_enabled": True,
        "waba_payment_failure_template_coupon": NAMES[VARIANT_COUPON],
        "waba_payment_failure_template_two_refusals": NAMES[VARIANT_TWO_REFUSALS],
        "waba_payment_failure_template_insufficient_funds": NAMES[VARIANT_INSUFFICIENT_FUNDS],
        "waba_payment_failure_template_declined": NAMES[VARIANT_DECLINED],
    }
    values.update(overrides)
    return _first_contact_settings(instance, **values)


def test_the_bridge_hands_b1_to_the_worker_and_the_dispatcher(instance: Path) -> None:
    app = _first_contact_app(_settings(instance), _SlipAuthority())

    worker = app.state.resolution_worker  # type: ignore[attr-defined]
    dispatcher = app.state.durable_dispatcher  # type: ignore[attr-defined]
    assert worker._payment_failure_planning == PLANNING
    config = dispatcher._payment_failure_variants
    assert dict(config.templates) == NAMES
    assert config.language == dispatcher._waba_template.language
    assert "payment_failure_variants_enabled" in (
        app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    )


def test_with_the_flag_off_nothing_of_b1_is_wired_even_with_its_constants(
    instance: Path,
) -> None:
    # Como la ficha: generar.py escribe las constantes siempre, y sin el flag
    # el bridge arranca igual con ellas y no las lee.
    app = _first_contact_app(
        _settings(instance, payment_failure_variants_enabled=False), _SlipAuthority()
    )

    assert app.state.resolution_worker._payment_failure_planning is None  # type: ignore[attr-defined]
    assert app.state.durable_dispatcher._payment_failure_variants is None  # type: ignore[attr-defined]
    assert Settings.__dataclass_fields__["payment_failure_variants_enabled"].default is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"pilot_payment_failure_scope_version": None}, "go together"),
        ({"payment_failure_followup_policy_key": None}, "go together"),
        ({"portable_hotmart_payment_failure_enabled": False},
         "PORTABLE_HOTMART_PAYMENT_FAILURE_ENABLED"),
        ({"pilot_payment_failure_scope_version": 0}, "must be positive"),
        ({"pilot_payment_failure_scope_key": "att1-recuperacion"}, "must differ"),
        ({"payment_failure_followup_policy_key": "att1-recuperacion-un-toque"}, "must differ"),
        ({"recovery_coupon_worker_enabled": False}, "RECOVERY_COUPON_WORKER_ENABLED"),
        ({"waba_payment_failure_template_declined": None}, "WABA_PAYMENT_FAILURE_TEMPLATE"),
        ({"dispatcher_approved_template_direct_enabled": False},
         "DURABLE_APPROVED_TEMPLATE_DIRECT_ENABLED"),
        ({"pilot_payment_failure_scope_key": None,
          "pilot_payment_failure_scope_version": None,
          "payment_failure_followup_policy_key": None,
          "payment_failure_followup_policy_version": None},
         "requires LANCEMOS_PILOT_PAYMENT_FAILURE_SCOPE"),
    ],
)
def test_an_incomplete_b1_wiring_does_not_start(
    instance: Path, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _first_contact_app(_settings(instance, **overrides), _SlipAuthority())


def test_from_env_reads_the_b1_variables(
    monkeypatch: pytest.MonkeyPatch, instance: Path
) -> None:
    from test_instance_wiring import _environment

    _environment(monkeypatch, INSTANCE_MANIFEST_PATH=str(instance / "instancia.toml"))
    for name, value in {
        "LANCEMOS_PILOT_PAYMENT_FAILURE_SCOPE_KEY": "att1-pago-rechazado",
        "LANCEMOS_PILOT_PAYMENT_FAILURE_SCOPE_VERSION": "1",
        "PAYMENT_FAILURE_FOLLOWUP_POLICY_KEY": "att1-pago-rechazado-un-toque",
        "PAYMENT_FAILURE_FOLLOWUP_POLICY_VERSION": "2",
        "PAYMENT_FAILURE_VARIANTS_ENABLED": "true",
        "WABA_PAYMENT_FAILURE_TEMPLATE_COUPON": NAMES[VARIANT_COUPON],
        "WABA_PAYMENT_FAILURE_TEMPLATE_TWO_REFUSALS": NAMES[VARIANT_TWO_REFUSALS],
        "WABA_PAYMENT_FAILURE_TEMPLATE_INSUFFICIENT_FUNDS": NAMES[VARIANT_INSUFFICIENT_FUNDS],
        "WABA_PAYMENT_FAILURE_TEMPLATE_DECLINED": NAMES[VARIANT_DECLINED],
    }.items():
        monkeypatch.setenv(name, value)

    settings = Settings.from_env()

    assert settings.pilot_payment_failure_scope_key == "att1-pago-rechazado"
    assert settings.pilot_payment_failure_scope_version == 1
    assert settings.payment_failure_followup_policy_key == "att1-pago-rechazado-un-toque"
    assert settings.payment_failure_followup_policy_version == 2
    assert settings.payment_failure_variants_enabled is True
    assert settings.waba_payment_failure_template_declined == NAMES[VARIANT_DECLINED]


def test_replace_keeps_the_planning_config_comparable() -> None:
    assert replace(PLANNING, policy_version=1) == PLANNING


class _RefusalScopeAuthority(_SlipAuthority):
    def __init__(self, **refusal_status: object) -> None:
        super().__init__()
        self.refusal_status = {
            "configured": True, "runtime_state": "armed", "runtime_generation": 1,
            "reason_code": "pilot_runtime_armed", **refusal_status,
        }
        self.refusal_boundaries: list[object] = []

    async def get_portable_payment_failure_pilot_runtime_status(
        self, *, pilot_boundary: object
    ) -> object:
        from types import SimpleNamespace

        self.refusal_boundaries.append(pilot_boundary)
        return SimpleNamespace(**self.refusal_status)


def test_readiness_reports_the_refusal_scope_only_with_the_flag(instance: Path) -> None:
    authority = _RefusalScopeAuthority()
    off_authority = _RefusalScopeAuthority()

    ready_on = _get_ready(_first_contact_app(_settings(instance), authority))
    ready_off = _get_ready(_first_contact_app(
        _settings(instance, payment_failure_variants_enabled=False), off_authority,
    ))

    assert ready_on.status_code == 200 and ready_off.status_code == 200
    assert ready_on.json()["portable_payment_failure_own_scope"] == "armed"
    assert "portable_payment_failure_own_scope" not in ready_off.json()
    assert off_authority.refusal_boundaries == []
    [boundary] = authority.refusal_boundaries
    assert boundary == REFUSAL_SCOPE


def test_readiness_fails_when_the_refusal_scope_is_not_published(instance: Path) -> None:
    authority = _RefusalScopeAuthority(
        configured=False, runtime_state=None, reason_code="pilot_scope_config_mismatch"
    )

    ready = _get_ready(_first_contact_app(_settings(instance), authority))

    assert ready.status_code == 503
    assert ready.json()["detail"] == "portable_payment_failure_pilot_scope_config_mismatch"
