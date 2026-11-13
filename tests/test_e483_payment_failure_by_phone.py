"""E-483 (pieza PF, 2026-11-12): el pago rechazado de una oferta de campana sin formulario.

Medido en ATT1 el 12/11: la oferta Black ``wmkq4ba9`` tiene una campana del AE con ``codigo_oferta``, y sus
pagos rechazados de quien no lleno el formulario quedaban sin caso ni mensaje
(``payment_failure_correlation_unresolved``). La base (20261112210400) abre el recorrido nativo del pago
rechazado con el telefono del evento de Hotmart, solo para las ofertas que el bridge le manda en
``p_instance.native_payment_failure_by_phone_offers`` (lo mide ``validate_e483_payment_failure_by_phone.mjs``
en PGlite: caso, recorrido sin intencion, B1 de su rama, lo que no entra y los rojos).

Aca: la forma de la migracion, el bridge (la lista va solo con
``FLOW_ENGINE_NATIVE_PAYMENT_FAILURE_BY_PHONE_ENABLED``, solo con el pago rechazado, y sale de las campanas
vigentes del plan de etiquetas de campaign-entry-v1 §4b; apagado, el cuerpo es el de antes), y que el plan de
§4b etiqueta el recorrido que abre la base (el origen sin landing, por la oferta).

Datos: el rechazo capturado de ATT1 (``hotmart_purchase_canceled_att1_20261019.json``, el primero: tarjeta,
«Transaction refused») con la oferta cambiada a la de Black (``wmkq4ba9``, la del checkout capturado
``hotmart_checkout_offer_black_att1_20261110.json``). El origen del recorrido es el que contesta
``flow_engine_native_campaign_origin_v1`` para un recorrido sin intencion (``landing`` nula, lo afirma la
prueba de la base).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from test_campaign_entry import (
    BLACK,
    BLACK_OFFER,
    OFFER,
    ROWS,
    _ae_segment,
    _campaign_dispatcher,
    _CampaignAuthority,
    _entry,
    _LiveEntries,
    _origin,
)
from test_flow_engine_native_dispatch import _run
from test_segment_labels import END, INSIDE, LABEL, START, _SegmentChatwoot

from bridge.campaign_entry import CampaignEntryLabels, agent_campaign_entries
from bridge.supabase import FlowEngineNativeAdmissionRow, SupabaseClient

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20261112210400_e483_payment_failure_by_phone_v1.sql"
FIXTURES = ROOT / "tests" / "fixtures"
CAPTURE = json.loads((FIXTURES / "hotmart_purchase_canceled_att1_20261019.json").read_text(encoding="utf-8"))
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1"}
EVENT = {"fuente": "hotmart", "tipo": "pago_fallido", "evento_id": "00000000-0000-4000-8000-000000000001"}
ROW = {"flow_id": None, "outcome": "sin_flujo", "reason": None, "recovery_case_id": None,
       "scheduled_action_id": None, "created": None}
CONTACT = "00000000-0000-4000-8000-000000000002"


def _refusal_of_black() -> dict[str, Any]:
    payload = copy.deepcopy(CAPTURE["eventos"][0]["payload"])
    assert payload["event"] == "PURCHASE_CANCELED"
    assert payload["data"]["purchase"]["payment"]["refusal_reason"] == "Transaction refused"
    payload["data"]["purchase"]["offer"]["code"] = BLACK
    return payload


# -- La migracion ----------------------------------------------------------------


def test_the_migration_is_one_transaction_with_its_guard() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("\nbegin;\n") == 1 and sql.rstrip().endswith("commit;")
    assert "set local lock_timeout = '5s';" in sql
    assert "message = 'e483_payment_failure_by_phone_already_applied'" in sql
    # Solo con la lista del bridge, solo con el pago rechazado, y la variable se borra.
    assert "p_instance -> ''native_payment_failure_by_phone_offers''" in sql
    assert "case when v_type = ''pago_fallido''" in sql
    assert "e483_payment_failure_by_phone_offers_reset" in sql
    # Solo sin ninguna intencion, y la baja en la puerta.
    assert "v_details.correlation_outcome is distinct from 'unmatched'" in sql
    assert "e483_payment_failure_by_phone_recheck: begin" in sql
    # La funcion nueva no es de nadie de la API.
    assert "revoke all on function public._flow_engine_native_payment_failure_phone_audience" in sql


def test_the_new_test_runs_with_npm_test() -> None:
    package = json.loads((ROOT / "tests/sql/followup_engine/package.json").read_text(encoding="utf-8"))
    # Despues va el de C2 (20261112210500).
    assert package["scripts"]["test"].endswith(
        "node validate_e483_native_condition_after_send.mjs --check && node validate_e483_payment_failure_by_phone.mjs"
        " && node validate_e483_native_personal_coupon.mjs --check")


# -- El cliente de la base -------------------------------------------------------


def _client(seen: list[dict[str, Any]]) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=[ROW])

    return SupabaseClient(base_url="https://supabase.example", service_role_key="service",
                          transport=httpx.MockTransport(handler))


def test_without_the_list_the_admission_body_is_the_one_of_before() -> None:
    seen: list[dict[str, Any]] = []
    asyncio.run(_client(seen).admit_flow_engine_event(INSTANCE, EVENT))
    assert seen == [{"p_instance": INSTANCE, "p_flow_event": EVENT}]


def test_with_the_list_the_admission_carries_the_campaign_offers() -> None:
    seen: list[dict[str, Any]] = []
    asyncio.run(_client(seen).admit_flow_engine_event(INSTANCE, EVENT, payment_failure_by_phone_offers=[BLACK]))
    assert seen == [{"p_instance": {**INSTANCE, "native_payment_failure_by_phone_offers": [BLACK]},
                     "p_flow_event": EVENT}]
    assert "native_payment_failure_by_phone_offers" not in INSTANCE


# -- La admision del bridge ------------------------------------------------------


class _Admission:
    """La admision nativa, que anota con que la llamo el bridge (sin red)."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def admit_flow_engine_event(self, instance: Any, flow_event: dict[str, Any], **kwargs: Any) -> tuple[Any, ...]:
        self.calls.append((dict(flow_event), kwargs))
        return (FlowEngineNativeAdmissionRow("flow-1", "motor_arranco", None, "case-1", "action-1", True),)


def _admit(trigger: str, offers: Any, payload: dict[str, Any] | None = None) -> _Admission:
    from bridge.flow_engine_runtime import FlowEngineRuntime
    from bridge.hotmart import normalize_phone
    from bridge.native_flows import admit_native_cart

    payload = payload or _refusal_of_black()
    admission = _Admission()
    runtime = FlowEngineRuntime(supabase=None, instance=INSTANCE,  # type: ignore[arg-type]
                                payment_failure_by_phone_offers=offers)
    destination = normalize_phone(payload["data"]["buyer"]["checkout_phone"])
    assert destination is not None
    result = asyncio.run(admit_native_cart(
        admission,  # type: ignore[arg-type]
        runtime=runtime, webhook_event_id=EVENT["evento_id"], product_ref="att1", contact_id=CONTACT,
        offer_code=payload["data"]["purchase"]["offer"]["code"], moment="2026-11-12T12:00:00+00:00",
        chatwoot_account_id=1, chatwoot_inbox_id=11, destination=destination, trigger=trigger))
    assert result.recovery_case_id == "case-1"
    return admission


def test_the_captured_refusal_of_the_campaign_offer_carries_the_list() -> None:
    from bridge.native_flows import NATIVE_PAYMENT_FAILURE_TRIGGER

    payload = _refusal_of_black()
    admission = _admit(NATIVE_PAYMENT_FAILURE_TRIGGER, lambda: frozenset({BLACK, OFFER}), payload)
    [(flow_event, kwargs)] = admission.calls
    # La lista va ordenada; el evento es el de siempre: la oferta y el telefono del rechazo de Hotmart.
    assert kwargs == {"priority_wins": False, "payment_failure_by_phone_offers": sorted({BLACK, OFFER})}
    assert flow_event["tipo"] == "pago_fallido" and flow_event["oferta"] == BLACK
    assert flow_event["destino"] == payload["data"]["buyer"]["checkout_phone"]


@pytest.mark.parametrize(
    ("trigger", "offers"),
    [("pago_fallido", None), ("carrito", lambda: frozenset({BLACK}))],
    ids=["flag-apagado", "carrito"],
)
def test_without_the_flag_or_with_the_cart_the_call_is_the_one_of_before(trigger: str, offers: Any) -> None:
    [(_, kwargs)] = _admit(trigger, offers).calls
    assert kwargs == {"priority_wins": False}


def test_a_list_that_cannot_be_read_falls_back_to_the_form(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> frozenset[str]:
        raise RuntimeError("catalogo")

    caplog.set_level(logging.WARNING, logger="bridge")
    [(_, kwargs)] = _admit("pago_fallido", broken).calls
    assert kwargs == {"priority_wins": False}
    assert "flow_engine_native_payment_failure_by_phone_offers_unavailable" in caplog.text


# -- Las ofertas de las campanas vigentes y su etiqueta (§4b) ----------------------


def test_the_offers_are_those_of_the_campaigns_that_run() -> None:
    running = agent_campaign_entries([(_ae_segment(), BLACK)], [BLACK_OFFER])
    later = agent_campaign_entries(
        [(_ae_segment(key="otra", label="otra", valid_from=END, valid_until=END + timedelta(days=1)), OFFER)],
        [SimpleNamespace(code=OFFER, landing_id="x")])
    labels = CampaignEntryLabels(source=_LiveEntries((*running, *later)), clock=lambda: INSIDE)
    assert labels.campaign_offers() == frozenset({BLACK})
    # Vencida: ninguna.
    assert CampaignEntryLabels(source=_LiveEntries(running), clock=lambda: END).campaign_offers() == frozenset()
    assert CampaignEntryLabels(source=_LiveEntries(running), clock=lambda: START).campaign_offers() == frozenset({BLACK})
    # Una de landing sola (sin oferta propia) no suma ofertas.
    assert CampaignEntryLabels(campaigns=[_entry()], labelers={LABEL: _LiveEntries().labeler_of[LABEL]},
                               clock=lambda: INSIDE).campaign_offers() == frozenset()


def test_the_run_opened_by_phone_gets_the_label_of_its_campaign(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bridge")
    source = _LiveEntries(agent_campaign_entries([(_ae_segment(), BLACK)], [BLACK_OFFER]))
    labels = CampaignEntryLabels(source=source, clock=lambda: INSIDE)
    # El origen de un recorrido del pago rechazado sin intencion: sin landing, la oferta del evento.
    origin = _origin(ROWS["cart"], disparador="pago_fallido", oferta=BLACK, landing=None)
    plan = asyncio.run(labels.plan(_CampaignAuthority(origin=origin), action_id="a"))
    assert plan is not None and plan.segment.label == LABEL
    assert "by=oferta" in caplog.text and "trigger=pago_fallido" in caplog.text


def test_the_label_goes_before_the_first_template_of_the_run(tmp_path: Path) -> None:
    authority = _CampaignAuthority(origin=_origin(ROWS["cart"], disparador="pago_fallido", landing=None))
    chatwoot = _SegmentChatwoot()
    campaign = _entry(landings=frozenset(), offers=frozenset({OFFER}))

    _run(_campaign_dispatcher(authority, chatwoot, tmp_path, campaign=campaign))

    assert chatwoot.order() == ["segmento", "plantilla"]
    assert chatwoot.labels == [LABEL]


# -- El cableado -----------------------------------------------------------------


def test_the_capability_and_the_flag_are_off_by_default() -> None:
    from bridge import app as app_module
    from bridge.flow_engine_runtime import FlowEngineRuntime

    assert "flow_engine_native_payment_failure_by_phone_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    field = app_module.Settings.__dataclass_fields__["flow_engine_native_payment_failure_by_phone_enabled"]
    assert field.default is False
    assert FlowEngineRuntime(supabase=None, instance=INSTANCE).payment_failure_by_phone_offers is None  # type: ignore[arg-type]


def test_the_flag_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from bridge.app import Settings

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
    flag = "FLOW_ENGINE_NATIVE_PAYMENT_FAILURE_BY_PHONE_ENABLED"
    monkeypatch.delenv(flag, raising=False)
    assert Settings.from_env().flow_engine_native_payment_failure_by_phone_enabled is False
    monkeypatch.setenv(flag, "yes")
    assert Settings.from_env().flow_engine_native_payment_failure_by_phone_enabled is False
    monkeypatch.setenv(flag, " TRUE ")
    assert Settings.from_env().flow_engine_native_payment_failure_by_phone_enabled is True


def test_create_app_does_not_start_with_the_flag_and_without_the_campaign_entry(tmp_path: Path) -> None:
    from test_customer_status import _settings as _manifest_app_settings
    from test_whatsapp_inbound_equivalence import StubChatwootClient

    from bridge.app import create_app

    settings = _manifest_app_settings(tmp_path / "m")
    assert settings.flow_engine_campaign_entry_enabled is False
    with pytest.raises(ValueError, match="FLOW_ENGINE_NATIVE_PAYMENT_FAILURE_BY_PHONE_ENABLED requires"):
        create_app(replace(settings, flow_engine_native_payment_failure_by_phone_enabled=True),
                   chatwoot_client=StubChatwootClient(messages=[]))


def test_the_runtime_gets_the_offers_of_the_campaign_entry_plan() -> None:
    from bridge import app

    source = Path(app.__file__).read_text(encoding="utf-8")
    assert "payment_failure_by_phone_offers = native_segment_labels.campaign_offers" in source
    assert "payment_failure_by_phone_offers=payment_failure_by_phone_offers," in source
