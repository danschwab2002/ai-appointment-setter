"""The variables of the cart first touch with a personal coupon (A1 of ATT1).

A1 (``att1_carrito_descuento_01``) says when the cart was left, over what
price the 10 % is and until when it holds:

    ... tu inscripción de {{cuando}} quedó en el último paso. El 10 % es sobre
    los {{precio}} y vale hasta {{vence}}; ...

Decided by Dan on 2026-10-18 (decision «A1 espera al cupón, que dura 24 h, en
la hora del país»): both moments are shown in the time zone of the buyer's
country when Hotmart sends it, and in Mexico City's otherwise; the price is the
manifest's (``USD 47``) even if the checkout shows local currency.

The text follows the examples Meta approved with the template: «hoy a las
3:10 p. m.», «mañana a las 3:10 p. m.». Pure functions: no clock, no I/O.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "America/Mexico_City"

# ISO 3166-1 alpha-2 of the buyer's country (``checkout_country`` of the
# Hotmart payload) to the zone its clocks show. One zone per country: for the
# countries with several, the one where most of the population lives.
COUNTRY_TIMEZONES = {
    "MX": "America/Mexico_City",
    "AR": "America/Argentina/Buenos_Aires",
    "BO": "America/La_Paz",
    "BR": "America/Sao_Paulo",
    "CL": "America/Santiago",
    "CO": "America/Bogota",
    "CR": "America/Costa_Rica",
    "DO": "America/Santo_Domingo",
    "EC": "America/Guayaquil",
    "ES": "Europe/Madrid",
    "GT": "America/Guatemala",
    "HN": "America/Tegucigalpa",
    "NI": "America/Managua",
    "PA": "America/Panama",
    "PE": "America/Lima",
    "PR": "America/Puerto_Rico",
    "PY": "America/Asuncion",
    "SV": "America/El_Salvador",
    "US": "America/New_York",
    "UY": "America/Montevideo",
    "VE": "America/Caracas",
}

_CURRENCY_RE = re.compile(r"[A-Z]{3}")


def timezone_for_country(country: str | None) -> ZoneInfo:
    """The zone of ``country``, or Mexico City's when it is missing or unknown."""
    code = country.strip().upper() if isinstance(country, str) else ""
    return ZoneInfo(COUNTRY_TIMEZONES.get(code, DEFAULT_TIMEZONE))


def format_moment(at: datetime, *, now: datetime, zone: ZoneInfo) -> str:
    """«hoy a las 3:10 p. m.», «mañana a las 9:05 a. m.», «ayer a las ...».

    Further than a day away it names the date: «el 20/10 a las 3:10 p. m.».
    Both datetimes must be aware; the day is the one of ``zone``.
    """
    if at.tzinfo is None or now.tzinfo is None:
        raise ValueError("cart_discount_naive_datetime")
    local = at.astimezone(zone)
    today = now.astimezone(zone).date()
    offset = (local.date() - today).days
    day = {0: "hoy", 1: "mañana", -1: "ayer"}.get(
        offset, f"el {local.day:02d}/{local.month:02d}"
    )
    hour = local.hour % 12 or 12
    meridiem = "a. m." if local.hour < 12 else "p. m."
    return f"{day} a las {hour}:{local.minute:02d} {meridiem}"


def format_price(amount: object, currency: str) -> str:
    """«USD 47», or «USD 47.50» when it has cents."""
    if not isinstance(currency, str) or _CURRENCY_RE.fullmatch(currency) is None:
        raise ValueError("cart_discount_invalid_currency")
    try:
        value = Decimal(str(amount))
    except InvalidOperation as exc:
        raise ValueError("cart_discount_invalid_price") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("cart_discount_invalid_price")
    text = f"{value:.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    return f"{currency} {text}"


def cart_discount_values(
    *,
    buyer_name: str,
    product_name: str,
    abandoned_at: datetime,
    coupon_expires_at: datetime,
    price: object,
    currency: str,
    country: str | None,
    now: datetime,
) -> dict[str, str]:
    """The A1 variables by manifest name: nombre, producto, cuando, precio, vence.

    Whitespace is collapsed like ``WhatsAppTemplateConfig.body_values``: Meta
    refuses a parameter with a new line, a tab or five spaces in a row.
    """
    if coupon_expires_at <= now:
        raise ValueError("cart_discount_coupon_already_expired")
    zone = timezone_for_country(country)
    values = {
        "nombre": buyer_name or "",
        "producto": product_name or "",
        "cuando": format_moment(abandoned_at, now=now, zone=zone),
        "precio": format_price(price, currency),
        "vence": format_moment(coupon_expires_at, now=now, zone=zone),
    }
    return {key: " ".join(value.split()) for key, value in values.items()}


def coupon_expiry(a1_due_at: datetime, *, validity: timedelta = timedelta(hours=24)) -> datetime:
    """When the personal coupon really expires: 24 h after A1 is due (Dan, 18/10)."""
    if a1_due_at.tzinfo is None:
        raise ValueError("cart_discount_naive_datetime")
    return a1_due_at + validity
