"""C1 de ATT1: el primer contacto una hora despues de la ficha de pago.

Contrato: docs/contracts/portable-payment-slip-first-contact-v1.md. Decision
de Dan del 18/10 («la ficha de OXXO va con scope propio y código global»,
puntos 4 y 5): C1 sale con ``att1_ficha_efectivo_01`` (nombre, producto,
tienda y vencimiento, y la ficha en el boton URL), y una ficha ya vencida en la
zona de su pais al componer no se manda (``payment_slip_expired``).

Datos capturados, no inventados:

* ``hotmart_payment_slip_printed_att1_20261018.json``: los 13
  ``PURCHASE_BILLET_PRINTED`` de att1-db desde el 12/10, anonimizados (el hash
  de la ficha queda en 48 ``f``; de la referencia, los digitos 19 a 26). Los
  hechos de la ficha que lee el dispatcher son los que la admision v2 guarda de
  ese payload (``commercial_ally_payment_slip_details``).
* ``chatwoot_message_templates_inbox_11_ficha_20261018.json``: el catalogo del
  inbox 11 con C1 (UTILITY, cuatro variables, primero el URL de la ficha de
  OXXO y despues «No más mensajes»).

La base y Chatwoot son los de ``test_durable_dispatcher_approved_template``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_durable_dispatcher_approved_template import (
    ACCOUNT_ID,
    ALLY,
    BOUNDARY,
    CART_TEMPLATE,
    FIXTURES,
    INBOX_ID,
    _Authority,
    _Chatwoot,
)

from bridge.instance_manifest import InstanceManifest
from bridge.messaging import (
    PAYMENT_SLIP_TRIGGER,
    ChatwootMessageSender,
    FinalMetaEffectGate,
    WhatsAppTemplateConfig,
)
from bridge.payment_slip import (
    PAYMENT_SLIP_TEMPLATE_PARAMETERS,
    parse_payment_slip_methods,
)
from bridge.payment_slip_first_touch import (
    PAYMENT_SLIP_CONTEXT_UNAVAILABLE,
    PAYMENT_SLIP_EXPIRED,
    PaymentSlipFirstTouchConfig,
    PaymentSlipPlanningConfig,
)
from bridge.supabase import PaymentSlipSendContext, SupabaseError, SupabasePermanentError
from bridge.worker import DurableDispatcher

SLIPS: list[dict[str, Any]] = json.loads(
    (FIXTURES / "hotmart_payment_slip_printed_att1_20261018.json").read_text(encoding="utf-8")
)["eventos"]
SLIP_CATALOG: list[dict[str, Any]] = json.loads(
    (FIXTURES / "chatwoot_message_templates_inbox_11_ficha_20261018.json").read_text(
        encoding="utf-8"
    )
)["message_templates"]
C1 = "att1_ficha_efectivo_01"
OXXO_ROW: dict[str, object] = {
    "medio": "CASHPAYMENT",
    "pais": "MX",
    "tienda": "OXXO",
    "zona_horaria": "America/Mexico_City",
    "referencia_largo": 34,
    "vencimiento_digitos": [19, 26],
    "vencimiento_dias_si_falta": 3,
    "boton_prefijo": "https://customer-local-latam.ebanx.com/print/oxxo?hash=",
    "habilitada": True,
}
METHODS = parse_payment_slip_methods([OXXO_ROW])
SLIP_CONFIG = PaymentSlipFirstTouchConfig(methods=METHODS)
TEMPLATE = WhatsAppTemplateConfig(
    first_touch_name=CART_TEMPLATE,
    followup_name=None,
    language="es_MX",
    category="MARKETING",
    first_touch_parameter="buyer_name_and_product",
    first_touch_body_parameters=("nombre", "producto"),
    payment_slip_name=C1,
    payment_slip_category="UTILITY",
    payment_slip_body_parameters=PAYMENT_SLIP_TEMPLATE_PARAMETERS,
)
# La ficha 3 del fixture: CASHPAYMENT de Mexico generada el 12/10 a las 21:56
# de CDMX (03:56Z del 13/10), vencimiento 20261015 en la referencia.
MX_SLIP = SLIPS[2]["payload"]
# La ficha 11: CASHPAYMENT de Chile, con otra base y sin referencia.
CL_SLIP = SLIPS[10]["payload"]
# Una hora despues de generada, como la politica de ATT1.
SEND_AT = "2026-10-13T04:56:30+00:00"


def _context(payload: dict[str, Any], **changes: object) -> PaymentSlipSendContext:
    """Lo que guarda la admision v2 de ese payload (20261019000100)."""
    purchase = payload["data"]["purchase"]
    payment = purchase["payment"]
    context = PaymentSlipSendContext(
        webhook_event_id="6f1d2c3b-0000-4000-8000-000000000103",
        payment_type=payment.get("type"),
        checkout_country_iso=purchase["checkout_country"]["iso"].upper(),
        billet_url=payment.get("billet_url"),
        billet_barcode=payment.get("billet_barcode"),
        generated_at=datetime.fromtimestamp(purchase["order_date"] / 1000, tz=UTC),
    )
    return replace(context, **changes)  # type: ignore[arg-type]


class _SlipAuthority(_Authority):
    def __init__(
        self,
        *,
        context: PaymentSlipSendContext | Exception | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("offer_code", "2uafw5bg")
        kwargs.setdefault("product_name", "Alimenta tu Tiroides")
        kwargs.setdefault("anchor_type", PAYMENT_SLIP_TRIGGER)
        super().__init__(**kwargs)
        self.action = replace(
            self.action,
            step_key="payment_slip_first_contact",
            due_at="2026-10-13T04:56:18+00:00",
            anchor_observed_at="2026-10-13T03:56:18+00:00",
        )
        self.context = replace(self.context, step_key="payment_slip_first_contact")
        self.slip_context = _context(MX_SLIP) if context is None else context
        self.slip_context_calls: list[dict[str, object]] = []
        self.started: list[dict[str, object]] = []

    async def get_portable_payment_slip_send_context(
        self, **kwargs: object
    ) -> PaymentSlipSendContext:
        self.events.append("slip_context")
        self.slip_context_calls.append(kwargs)
        if isinstance(self.slip_context, Exception):
            raise self.slip_context
        return self.slip_context

    async def mark_followup_request_started(self, **kwargs: object) -> object:
        self.started.append(kwargs)
        return await super().mark_followup_request_started(**kwargs)


def _dispatcher(
    authority: _SlipAuthority,
    chatwoot: _Chatwoot,
    tmp_path: Path,
    *,
    clock: str = SEND_AT,
    payment_slip: PaymentSlipFirstTouchConfig | None = SLIP_CONFIG,
    template: WhatsAppTemplateConfig = TEMPLATE,
    gate_open: bool = True,
) -> DurableDispatcher:
    client = chatwoot.client()
    return DurableDispatcher(
        supabase=authority,  # type: ignore[arg-type]
        worker_id="att1-dispatcher",
        chatwoot=client,
        chatwoot_account_id=ACCOUNT_ID,
        chatwoot_inbox_id=INBOX_ID,
        sender=ChatwootMessageSender(
            chatwoot=client,
            inbox_id=INBOX_ID,
            allowed_jid=None,
            dynamic_recipient_enabled=True,
            template=template,
        ),
        allowed_jid=None,
        commercial_ally_config=ALLY,
        portable_recipient_enabled=True,
        pilot_boundary=BOUNDARY,
        clock=lambda: clock,
        final_meta_effect_gate=FinalMetaEffectGate(
            enabled=gate_open, evidence_dir=tmp_path / "meta-effects"
        ),
        waba_template=template,
        approved_template_direct=True,
        payment_slip=payment_slip,
    )


def _run(
    authority: _SlipAuthority,
    tmp_path: Path,
    *,
    catalog: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> _Chatwoot:
    chatwoot = _Chatwoot(SLIP_CATALOG if catalog is None else catalog)
    clock = kwargs.get("clock", SEND_AT)
    asyncio.run(_dispatcher(authority, chatwoot, tmp_path, **kwargs).dispatch_due(now=clock))
    return chatwoot


def _c1_body(values: list[str]) -> str:
    [template] = [t for t in SLIP_CATALOG if t["name"] == C1]
    [body] = [c["text"] for c in template["components"] if c["type"] == "BODY"]
    for position, value in enumerate(values, start=1):
        body = body.replace("{{%d}}" % position, value)
    return body


def _nothing_went_out(chatwoot: _Chatwoot, authority: _SlipAuthority) -> dict[str, object]:
    assert chatwoot.posts("/conversations/200/messages") == []
    assert chatwoot.posts("/contacts") == []
    assert authority.started == []
    [closed] = authority.finalizations
    assert closed["outcome"] == "failed_before_request"
    return closed


# ------------------------------------------------------------------ el envio


def test_c1_goes_out_with_the_store_the_due_date_and_the_slip_in_the_button(
    tmp_path: Path,
) -> None:
    authority = _SlipAuthority()

    chatwoot = _run(authority, tmp_path)

    values = ["Edith García Pérez", "Alimenta tu Tiroides", "OXXO", "jueves 15 de octubre"]
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["content"] == _c1_body(values)
    assert message["template_params"] == {
        "name": C1,
        "category": "UTILITY",
        "language": "es_MX",
        "processed_params": {
            "body": {str(i): v for i, v in enumerate(values, start=1)},
            # El hash de la ficha (48 hex): lo que sigue a la base aprobada.
            "buttons": [{"type": "url", "parameter": "f" * 48}],
        },
    }
    assert authority.events == [
        "reevaluate", "reserve", "slip_context", "reevaluate", "request_started", "accepted",
    ]
    # Bajo el lease del dispatcher, y el arranque por el ancla de la ficha.
    assert authority.slip_context_calls == [{
        "action_id": "action-att1",
        "worker_id": "att1-dispatcher",
        "lease_generation": 1,
        "now": SEND_AT,
    }]
    [started] = authority.started
    assert started["anchor_type"] == "payment_slip"
    assert started["pilot_boundary"] == BOUNDARY


def test_c1_goes_out_on_the_last_day_and_not_the_day_after(tmp_path: Path) -> None:
    # 23:59 del jueves 15 en CDMX (UTC-6) es 05:59Z del 16: todavia se paga.
    last_minute = "2026-10-16T05:59:00+00:00"
    authority = _SlipAuthority()
    assert len(_run(authority, tmp_path, clock=last_minute).posts(
        "/conversations/200/messages")) == 1

    # 00:00 del viernes 16 en CDMX: la ficha ya vencio y no se manda.
    expired = _SlipAuthority()
    chatwoot = _run(expired, tmp_path / "expired", clock="2026-10-16T06:00:00+00:00")

    closed = _nothing_went_out(chatwoot, expired)
    assert closed["reason_code"] == PAYMENT_SLIP_EXPIRED
    assert closed["next_attempt_at"] is None


def test_without_a_usable_reference_the_due_date_is_the_day_plus_three(tmp_path: Path) -> None:
    # DERIVADO: la ficha 3 sin referencia. 12/10 en CDMX mas 3 = 15/10.
    authority = _SlipAuthority(context=_context(MX_SLIP, billet_barcode=None))

    chatwoot = _run(authority, tmp_path)

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["processed_params"]["body"]["4"] == "jueves 15 de octubre"


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        # La ficha de Chile: el medio y el pais no tienen fila habilitada.
        (_context(CL_SLIP), "payment_slip_method_not_enabled"),
        # DERIVADOS de la ficha 3, cambiando un campo.
        (_context(MX_SLIP, payment_type="MERCADO_PAGO"), "payment_slip_method_not_enabled"),
        (
            _context(MX_SLIP, billet_url="https://api.ebanx.com/ws/redirect?hash=" + "f" * 48),
            "payment_slip_url_prefix_mismatch",
        ),
        (
            _context(
                MX_SLIP,
                billet_url="https://customer-local-latam.ebanx.com/print/oxxo?hash=ff/ff&x=1",
            ),
            "payment_slip_button_suffix_invalid",
        ),
        (
            _context(MX_SLIP, billet_barcode=None, generated_at=None),
            "payment_slip_due_date_unknown",
        ),
    ],
)
def test_a_slip_that_does_not_resolve_closes_without_a_message(
    tmp_path: Path, context: PaymentSlipSendContext, reason: str
) -> None:
    authority = _SlipAuthority(context=context)

    closed = _nothing_went_out(_run(authority, tmp_path), authority)

    assert closed["reason_code"] == reason
    assert closed["next_attempt_at"] is None


def test_an_unreadable_slip_context_retries_and_a_missing_one_does_not(
    tmp_path: Path,
) -> None:
    down = _SlipAuthority(context=SupabaseError("down"))
    closed = _nothing_went_out(_run(down, tmp_path), down)
    assert closed["reason_code"] == PAYMENT_SLIP_CONTEXT_UNAVAILABLE
    assert closed["next_attempt_at"] is not None

    missing = _SlipAuthority(
        context=SupabasePermanentError("rejected", reason="payment_slip_details_missing")
    )
    closed = _nothing_went_out(_run(missing, tmp_path / "missing"), missing)
    assert closed["reason_code"] == "payment_slip_details_missing"
    assert closed["next_attempt_at"] is None


def test_a_catalog_whose_button_is_another_base_does_not_send(tmp_path: Path) -> None:
    # DERIVADO: C1 aprobada con otra base. La fila de Mexico declara la de OXXO:
    # el sufijo no se pega a una URL que no es la de la ficha.
    catalog = json.loads(json.dumps(SLIP_CATALOG))
    for template in catalog:
        if template["name"] == C1:
            for component in template["components"]:
                if component["type"] == "BUTTONS":
                    component["buttons"][0]["url"] = "https://api.ebanx.com/ws/redirect?hash={{1}}"
    authority = _SlipAuthority()

    closed = _nothing_went_out(_run(authority, tmp_path, catalog=catalog), authority)

    assert closed["reason_code"] == "approved_template_mismatch"
    # El catalogo lo puede corregir Meta o Chatwoot: se reintenta.
    assert closed["next_attempt_at"] is not None


def test_without_the_slip_flow_the_action_closes_and_never_takes_the_cart_template(
    tmp_path: Path,
) -> None:
    authority = _SlipAuthority()

    chatwoot = _run(authority, tmp_path, payment_slip=None)

    closed = _nothing_went_out(chatwoot, authority)
    assert closed["reason_code"] == "first_touch_template_not_configured"
    assert closed["next_attempt_at"] is None
    assert "slip_context" not in authority.events
    assert chatwoot.inbox_reads() == 0


def test_the_closed_gate_records_c1_and_sends_nothing(tmp_path: Path) -> None:
    authority = _SlipAuthority()

    chatwoot = _run(authority, tmp_path, gate_open=False)

    assert chatwoot.posts("/conversations/200/messages") == []
    [evidence] = list((tmp_path / "meta-effects").iterdir())
    assert json.loads(evidence.read_text(encoding="utf-8"))["template_name"] == C1


def test_an_unsafe_name_closes_c1_like_any_first_contact(tmp_path: Path) -> None:
    authority = _SlipAuthority(buyer_name="https://example.test/promo")

    closed = _nothing_went_out(_run(authority, tmp_path), authority)

    assert closed["reason_code"] == "template_parameters_missing"
    assert "slip_context" not in authority.events


def test_the_cart_keeps_its_own_template_with_the_slip_on(tmp_path: Path) -> None:
    from test_durable_dispatcher_approved_template import ATT1_CATALOG

    authority = _SlipAuthority(anchor_type="cart_abandonment")
    authority.action = replace(authority.action, step_key="first_contact")
    authority.context = replace(authority.context, step_key="first_contact")
    template = replace(TEMPLATE, first_touch_name="att1_carrito_abandonado_01")

    chatwoot = _run(authority, tmp_path, catalog=[*ATT1_CATALOG, *SLIP_CATALOG], template=template)

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == "att1_carrito_abandonado_01"
    assert "slip_context" not in authority.events


@pytest.mark.parametrize(
    "changes",
    [
        {"approved_template_direct": False},
        {"waba_template": replace(TEMPLATE, payment_slip_name=None, payment_slip_category=None,
                                  payment_slip_body_parameters=None)},
    ],
)
def test_the_slip_needs_the_direct_mode_and_its_template(changes: dict[str, Any]) -> None:
    client = _Chatwoot().client()
    arguments: dict[str, Any] = {
        "supabase": _SlipAuthority(),
        "worker_id": "att1-dispatcher",
        "chatwoot": client,
        "chatwoot_account_id": ACCOUNT_ID,
        "chatwoot_inbox_id": INBOX_ID,
        "sender": None,
        "allowed_jid": None,
        "waba_template": TEMPLATE,
        "approved_template_direct": True,
        "payment_slip": SLIP_CONFIG,
    }
    arguments.update(changes)
    with pytest.raises(ValueError, match="payment slip requires"):
        DurableDispatcher(**arguments)


# ------------------------------------------------------- la configuracion


def test_the_config_needs_an_enabled_row() -> None:
    with pytest.raises(ValueError, match="payment_slip_methods_none_enabled"):
        PaymentSlipFirstTouchConfig(methods=())
    with pytest.raises(ValueError, match="payment_slip_methods_none_enabled"):
        PaymentSlipFirstTouchConfig(
            methods=parse_payment_slip_methods([{**OXXO_ROW, "habilitada": False}])
        )
    with pytest.raises(ValueError, match="payment_slip_policy_invalid"):
        PaymentSlipPlanningConfig(
            methods=METHODS, policy_key="att1-ficha-un-toque", policy_version=0,
            pilot_boundary=BOUNDARY,
        )


# ------------------------------------------------- la plantilla en messaging


def test_the_slip_trigger_has_its_own_template_and_never_borrows_the_cart_one() -> None:
    assert TEMPLATE.first_touch_name_for(trigger_kind="payment_slip") == C1
    assert TEMPLATE.category_for(trigger_kind="payment_slip") == "UTILITY"
    assert TEMPLATE.declared_body_parameters(trigger_kind="payment_slip") == (
        "nombre", "producto", "tienda", "vencimiento",
    )
    without = replace(TEMPLATE, payment_slip_name=None, payment_slip_category=None,
                      payment_slip_body_parameters=None)
    # Riesgo D del diseno: sin esto, un disparador desconocido sale con la del carrito.
    assert without.first_touch_name_for(trigger_kind="payment_slip") is None
    assert without.first_touch_name_for(trigger_kind="otro") == CART_TEMPLATE
    # Las demas no cambian.
    assert TEMPLATE.first_touch_name_for(trigger_kind="cart_abandonment") == CART_TEMPLATE
    assert TEMPLATE.category_for(trigger_kind="cart_abandonment") == "MARKETING"


def test_the_config_does_not_build_the_slip_parameters_itself() -> None:
    assert TEMPLATE.body_values(
        trigger_kind="payment_slip", buyer_name="María", product_name="Alimenta tu Tiroides"
    ) is None
    with pytest.raises(ValueError, match="payment_slip_template_params_required"):
        TEMPLATE.params(content="x", followup=False, buyer_name="María",
                        product_name="Alimenta tu Tiroides", trigger_kind="payment_slip")
    # Solo nombre y producto son del caso: la tienda y el vencimiento no se
    # piden aca.
    assert TEMPLATE.body_parameters_missing(
        trigger_kind="payment_slip", buyer_name="María", product_name="Alimenta tu Tiroides"
    ) is False
    assert TEMPLATE.body_parameters_missing(
        trigger_kind="payment_slip", buyer_name=" ", product_name="Alimenta tu Tiroides"
    ) is True


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"payment_slip_name": " "}, "invalid_payment_slip_template_name"),
        ({"payment_slip_name": None, "payment_slip_category": None},
         "payment_slip_body_parameters_without_template"),
        ({"payment_slip_body_parameters": ("nombre", "precio")}, "invalid_template_body_parameters"),
        ({"payment_slip_category": "AUTH"}, "invalid_payment_slip_category"),
        ({"payment_slip_name": None, "payment_slip_body_parameters": None},
         "payment_slip_category_without_template"),
    ],
)
def test_the_slip_template_config_fails_closed(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        replace(TEMPLATE, **changes)


def test_the_sender_blocks_a_slip_without_its_parameters_before_chatwoot() -> None:
    chatwoot = _Chatwoot(SLIP_CATALOG)
    sender = ChatwootMessageSender(
        chatwoot=chatwoot.client(),
        inbox_id=INBOX_ID,
        allowed_jid=None,
        dynamic_recipient_enabled=True,
        template=TEMPLATE,
    )

    result = asyncio.run(sender.send_first_touch(
        phone="12025550124", buyer_name="María", buyer_email=None,
        product_name="Alimenta tu Tiroides", content="x", delivery_id="d-1",
        trigger_kind="payment_slip",
    ))

    assert (result.status, result.reason) == ("blocked", "template_parameters_missing")
    assert chatwoot.requests == []


def test_the_manifest_rows_and_template_are_what_the_dispatcher_receives() -> None:
    import tomllib

    payload = tomllib.loads(
        (FIXTURES / "instances" / "att1" / "instancia.toml").read_text(encoding="utf-8")
    )
    payload["eventos"] = [*payload["eventos"], "ficha_impresa"]
    payload["ficha"] = {"medios": [dict(OXXO_ROW)]}
    payload["plantillas"]["ficha"] = {"nombre": C1, "idioma": "es_MX"}
    payload["flujos"]["ficha"] = True
    manifest = InstanceManifest.from_mapping(payload)

    assert PaymentSlipFirstTouchConfig(methods=manifest.payment_slip_methods) == SLIP_CONFIG
    assert manifest.templates["ficha"].parameters == TEMPLATE.payment_slip_body_parameters
