"""El parser de la ficha generada (PURCHASE_BILLET_PRINTED), con las 13 fichas reales.

Fixture capturado: ``tests/fixtures/hotmart_payment_slip_printed_att1_20261018.json``
(att1-db, 2026-10-18 ~19:40Z, anonimizado). Llegan con el producto 5071808 y
las ofertas ``2uafw5bg`` y ``bmaztyhg``, que en ATT1 son ofertas adicionales del
binding (como en ``tests/test_commercial_ally_multi_offer.py``).

El binding de prueba usa el nombre del producto tal como llega en estas fichas,
``Alimenta tu Tiroides``. Las variantes (otra oferta, otro evento, sin contacto)
son derivaciones marcadas en cada test.
"""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from bridge.commercial_ally import CommercialAllyConfig
from bridge.hotmart import (
    EVENT_PURCHASE_BILLET_PRINTED,
    HotmartBuyerData,
    HotmartPaymentSlipData,
    parse_hotmart_payment_failure_payload,
    parse_hotmart_payment_slip_buyer_payload,
    parse_hotmart_payment_slip_payload,
)

SLIPS = json.loads(
    (
        Path(__file__).parent / "fixtures" / "hotmart_payment_slip_printed_att1_20261018.json"
    ).read_text(encoding="utf-8")
)["eventos"]
PAYLOADS = [event["payload"] for event in SLIPS]


def _config(**overrides: object) -> CommercialAllyConfig:
    values = dict(
        tenant_ref="lancemos",
        funnel_ref="att1",
        binding_version=1,
        ally_ref="att1",
        lead_ally_name="Dra. Nina Garza",
        lead_site="metodoraizana",
        lead_landing_id="ads-a",
        lead_page_host="www.metodoraizana.com",
        lead_page_path="/att1/evg/vsl/ads-a",
        product_hotlink="D98014973Y",
        product_name="Alimenta tu Tiroides",
        product_price=Decimal("47"),
        currency="USD",
        offer_code="gopi6lh7",
        consent_copy_version="att1-whatsapp-contact-v1",
        hotmart_product_id=5071808,
        chatwoot_account_id=2,
        chatwoot_inbox_id=11,
        inbound_scope_key="att1-inbound",
        inbound_scope_version=1,
        additional_offer_codes=("bmaztyhg", "2uafw5bg"),
    )
    values.update(overrides)
    return CommercialAllyConfig(**values)  # type: ignore[arg-type]


def _id(payload: dict[str, object]) -> str:
    purchase = payload["data"]["purchase"]  # type: ignore[index]
    return f"{payload['id'][-3:]}-{purchase['payment']['type']}-{purchase['checkout_country']['iso']}"  # type: ignore[index]


@pytest.mark.parametrize("payload", PAYLOADS, ids=[_id(p) for p in PAYLOADS])
def test_every_captured_slip_parses_against_the_att1_binding(
    payload: dict[str, object],
) -> None:
    parsed = parse_hotmart_payment_slip_payload(payload, config=_config())

    data = payload["data"]  # type: ignore[index]
    purchase = data["purchase"]
    assert isinstance(parsed, HotmartPaymentSlipData)
    assert parsed.event_id == payload["id"]
    assert parsed.creation_date_ms == payload["creation_date"]
    assert parsed.transaction == purchase["transaction"]
    assert parsed.status == "BILLET_PRINTED"
    assert parsed.payment_type == purchase["payment"]["type"]
    assert parsed.buyer_email == data["buyer"]["email"]
    assert parsed.buyer_phone == data["buyer"]["checkout_phone"]
    assert parsed.product_id == 5071808
    assert parsed.product_ucode == data["product"]["ucode"]
    assert parsed.offer_code == purchase["offer"]["code"]


def test_the_capture_covers_both_additional_offers() -> None:
    parsed = [parse_hotmart_payment_slip_payload(p, config=_config()) for p in PAYLOADS]
    assert {p.offer_code for p in parsed if p is not None} == {"2uafw5bg", "bmaztyhg"}


@pytest.mark.parametrize("payload", PAYLOADS, ids=[_id(p) for p in PAYLOADS])
def test_the_buyer_projection_reads_the_country_where_it_arrives(
    payload: dict[str, object],
) -> None:
    buyer = parse_hotmart_payment_slip_buyer_payload(payload, config=_config())

    data = payload["data"]  # type: ignore[index]
    assert isinstance(buyer, HotmartBuyerData)
    assert buyer.event_type == EVENT_PURCHASE_BILLET_PRINTED
    assert buyer.buyer_name == data["buyer"]["name"]
    assert buyer.product_name == "Alimenta tu Tiroides"
    assert buyer.checkout_country_iso == data["purchase"]["checkout_country"]["iso"]
    assert buyer.checkout_country_name == data["purchase"]["checkout_country"]["name"]


def test_an_offer_outside_the_binding_is_not_parsed() -> None:
    # El binding sin las ofertas adicionales: ninguna de las 13 entra.
    config = _config(additional_offer_codes=())
    assert all(parse_hotmart_payment_slip_payload(p, config=config) is None for p in PAYLOADS)


def test_another_product_is_not_parsed() -> None:
    config = _config(hotmart_product_id=123456)
    assert all(parse_hotmart_payment_slip_payload(p, config=config) is None for p in PAYLOADS)


def test_another_product_name_drops_only_the_buyer_projection() -> None:
    config = _config(product_name="Alimenta Tu Tiroides")
    payload = PAYLOADS[0]
    assert parse_hotmart_payment_slip_payload(payload, config=config) is not None
    assert parse_hotmart_payment_slip_buyer_payload(payload, config=config) is None


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.__setitem__("event", "PURCHASE_EXPIRED"), id="expired-event"),
        pytest.param(lambda p: p.__setitem__("event", "PURCHASE_CANCELED"), id="canceled-event"),
        pytest.param(lambda p: p.__setitem__("version", "1.0.0"), id="other-version"),
        pytest.param(
            lambda p: p["data"]["purchase"].__setitem__("status", "EXPIRED"), id="other-status"
        ),
        pytest.param(
            lambda p: p["data"]["purchase"].__setitem__("transaction", "no-hp"), id="bad-transaction"
        ),
        pytest.param(lambda p: p["data"]["purchase"].pop("transaction"), id="no-transaction"),
        pytest.param(lambda p: p.pop("creation_date"), id="no-creation-date"),
        pytest.param(lambda p: p.__setitem__("creation_date", "1791863793103"), id="text-date"),
        pytest.param(lambda p: p.__setitem__("id", "  "), id="blank-id"),
        pytest.param(
            lambda p: p["data"].__setitem__("buyer", {"name": "Sin contacto"}), id="no-contact"
        ),
    ],
)
def test_a_derived_invalid_slip_is_not_parsed(mutate) -> None:  # type: ignore[no-untyped-def]
    # Derivado: la primera ficha de OXXO real con un cambio por caso.
    payload = deepcopy(PAYLOADS[2])
    mutate(payload)
    assert parse_hotmart_payment_slip_payload(payload, config=_config()) is None
    assert parse_hotmart_payment_slip_buyer_payload(payload, config=_config()) is None


def test_the_phone_alone_or_the_email_alone_is_enough() -> None:
    # Derivado: la ficha real sin email, y sin telefono.
    without_email = deepcopy(PAYLOADS[2])
    del without_email["data"]["buyer"]["email"]
    without_phone = deepcopy(PAYLOADS[2])
    del without_phone["data"]["buyer"]["checkout_phone"]

    slip = parse_hotmart_payment_slip_payload(without_email, config=_config())
    assert slip is not None and slip.buyer_email is None and slip.buyer_phone
    slip = parse_hotmart_payment_slip_payload(without_phone, config=_config())
    assert slip is not None and slip.buyer_phone is None and slip.buyer_email


def test_a_slip_is_not_a_payment_failure() -> None:
    assert all(
        parse_hotmart_payment_failure_payload(p, config=_config()) is None for p in PAYLOADS
    )
