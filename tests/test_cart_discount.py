"""Las variables de A1 del carrito con cupon personal (ATT1).

Los textos esperados salen de los ejemplos que Meta aprobo con la plantilla
``att1_carrito_descuento_01`` (fixture ``chatwoot_message_templates_inbox_11_
carrito_20261018.json``): «hoy a las 3:10 p. m.», «USD 47», «mañana a las
3:10 p. m.». La zona es la del pais de la persona (decision de Dan, 18/10).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bridge.approved_templates import parse_approved_template
from bridge.cart_discount import (
    cart_discount_values,
    coupon_expiry,
    format_moment,
    format_price,
    timezone_for_country,
)

CATALOG = (
    Path(__file__).parent / "fixtures" / "chatwoot_message_templates_inbox_11_carrito_20261018.json"
)
MX = ZoneInfo("America/Mexico_City")


def test_the_examples_meta_approved_are_what_the_formatter_writes() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    [a1] = [t for t in catalog["message_templates"] if t["name"] == "att1_carrito_descuento_01"]
    [body] = [c for c in a1["components"] if c["type"] == "BODY"]
    _, _, cuando, precio, vence = body["example"]["body_text"][0]

    now = datetime(2026, 10, 18, 21, 10, tzinfo=UTC)  # 15:10 en CDMX
    assert format_moment(now, now=now, zone=MX) == cuando
    assert format_price("47", "USD") == precio
    assert format_moment(now + timedelta(days=1), now=now, zone=MX) == vence


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2026, 10, 18, 5, 59, tzinfo=UTC), "ayer a las 11:59 p. m."),  # CDMX es UTC-6
        (datetime(2026, 10, 18, 6, 5, tzinfo=UTC), "hoy a las 12:05 a. m."),
        (datetime(2026, 10, 18, 18, 0, tzinfo=UTC), "hoy a las 12:00 p. m."),
        (datetime(2026, 10, 19, 15, 9, tzinfo=UTC), "mañana a las 9:09 a. m."),
        (datetime(2026, 10, 21, 15, 9, tzinfo=UTC), "el 21/10 a las 9:09 a. m."),
    ],
)
def test_the_day_and_the_hour_are_the_ones_of_mexico_city(at: datetime, expected: str) -> None:
    now = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
    assert format_moment(at, now=now, zone=MX) == expected


def test_the_same_moment_reads_in_the_buyer_s_country() -> None:
    # 22:30 en CDMX es la 01:30 del dia siguiente en Buenos Aires.
    now = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
    at = datetime(2026, 10, 19, 4, 30, tzinfo=UTC)
    assert format_moment(at, now=now, zone=timezone_for_country("MX")) == "hoy a las 10:30 p. m."
    assert format_moment(at, now=now, zone=timezone_for_country("ar")) == "mañana a las 1:30 a. m."


@pytest.mark.parametrize("country", [None, "", "  ", "XX", "MEX", 52])
def test_without_a_known_country_the_zone_is_mexico_city(country: object) -> None:
    assert timezone_for_country(country) == MX  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [("47", "USD", "USD 47"), (47, "USD", "USD 47"), ("47.5", "USD", "USD 47.50"),
     ("83467", "ARS", "ARS 83467")],
)
def test_the_price(amount: object, currency: str, expected: str) -> None:
    assert format_price(amount, currency) == expected


@pytest.mark.parametrize(("amount", "currency"), [("0", "USD"), ("-1", "USD"), ("x", "USD"),
                                                  ("nan", "USD"), ("47", "usd"), ("47", "")])
def test_a_price_that_cannot_be_said_is_an_error(amount: str, currency: str) -> None:
    with pytest.raises(ValueError):
        format_price(amount, currency)


def test_the_a1_values_fill_the_approved_template() -> None:
    now = datetime(2026, 10, 18, 21, 10, tzinfo=UTC)
    due = now
    values = cart_discount_values(
        buyer_name="  María\nJosé ",
        product_name="Alimenta tu Tiroides",
        abandoned_at=now - timedelta(hours=1),
        coupon_expires_at=coupon_expiry(due),
        price="47",
        currency="USD",
        country="MX",
        now=now,
    )
    assert values == {
        "nombre": "María José",
        "producto": "Alimenta tu Tiroides",
        "cuando": "hoy a las 2:10 p. m.",
        "precio": "USD 47",
        "vence": "mañana a las 3:10 p. m.",
    }
    template = parse_approved_template(
        json.loads(CATALOG.read_text(encoding="utf-8")),
        template_name="att1_carrito_descuento_01",
        expected_language="es_MX",
        expected_category="MARKETING",
        parameter_count=5,
        checkout_button=True,
    )
    ordered = {str(i): values[k] for i, k in enumerate(
        ("nombre", "producto", "cuando", "precio", "vence"), start=1)}
    assert "vale hasta mañana a las 3:10 p. m.;" in template.render(ordered)


def test_a_coupon_that_already_expired_is_not_promised() -> None:
    now = datetime(2026, 10, 18, 21, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="already_expired"):
        cart_discount_values(
            buyer_name="María", product_name="Alimenta tu Tiroides",
            abandoned_at=now - timedelta(hours=1), coupon_expires_at=now,
            price="47", currency="USD", country="MX", now=now,
        )


def test_the_coupon_lasts_24_hours_from_a1() -> None:
    due = datetime(2026, 10, 18, 21, 10, tzinfo=UTC)
    assert coupon_expiry(due) == datetime(2026, 10, 19, 21, 10, tzinfo=UTC)
    with pytest.raises(ValueError):
        coupon_expiry(datetime(2026, 10, 18, 21, 10))
