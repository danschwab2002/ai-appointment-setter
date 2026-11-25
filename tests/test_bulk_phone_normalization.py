"""La normalizacion de telefonos de una lista de masivo (bridge.phones.normalize_bulk_phone).

Porta los casos de `normalizar_tel` y `variantes` del `--selftest` del kit
`masivo.py` (el masivo del Black de Maribel, 2026-11-22, que corrio en
produccion), con una diferencia de forma: el kit devolvia `+` y digitos, el
bridge devuelve solo digitos, como `whatsapp_delivery_phone`. Los numeros son
de rangos de prueba (digitos repetidos o ejemplos de libro), no de personas.

No hay borde externo: `phonenumbers` es una biblioteca local, y los casos de
"no es movil" se eligen por su tipo en la metadata instalada (9.0.x).
"""

from __future__ import annotations

import phonenumbers
import pytest

from bridge.phones import (
    BULK_PHONE_OK,
    BULK_PHONE_REASONS,
    BulkPhone,
    canonical_whatsapp_phone,
    equivalent_whatsapp_phones,
    normalize_bulk_phone,
    whatsapp_delivery_phone,
)

# masivo.py, selftest(): la instancia en Colombia (pais "57").
KIT_CASES = [
    ("+57 300 123 4567", "573001234567", "ok"),
    ("3001234567", "573001234567", "ok"),
    ("573001234567", "573001234567", "ok"),
    ("0057 3001234567", "573001234567", "ok"),
    ("6012345678", None, "sin_pais"),
    ("+57 601 2345678", None, "co_no_movil"),
    ("+52 55 1234 5678", "5215512345678", "ok"),
    ("+521 55 1234 5678", "5215512345678", "ok"),
    ("+54 9 11 2345 6789", "541123456789", "ok"),
    ("+1 305 555 0101", "13055550101", "ok"),
    ("", None, "vacio"),
    ("abc", None, "sin_digitos"),
]


@pytest.mark.parametrize(("raw", "phone", "reason"), KIT_CASES)
def test_kit_cases_for_a_colombian_instance(raw: str, phone: str | None, reason: str) -> None:
    assert normalize_bulk_phone(raw, "57") == BulkPhone(phone, reason)


@pytest.mark.parametrize(
    ("raw", "country", "phone", "reason"),
    [
        # Sin codigo de pais, el de la instancia: 10 digitos nacionales.
        ("55 1234 5678", "52", "5215512345678", "ok"),
        ("5512345678", "57", None, "sin_pais"),
        ("5512345678", None, None, "sin_pais"),
        ("3001234567", None, None, "sin_pais"),
        ("11 2345 6789", "54", "541123456789", "ok"),
        ("+52", "52", None, "mx_largo"),
        # El codigo de la instancia puede venir con "+".
        ("55 1234 5678", "+52", "5215512345678", "ok"),
        # Argentina con o sin el 9, Mexico con o sin el 1: la misma forma de envio.
        ("+54 11 2345 6789", "57", "541123456789", "ok"),
        ("5491123456789", "57", "541123456789", "ok"),
        ("525512345678", "57", "5215512345678", "ok"),
        ("00 52 1 55 1234 5678", "57", "5215512345678", "ok"),
        # Largos por pais.
        ("+52 1 55 1234 56789", "57", None, "mx_largo"),
        ("+52 55 1234 567", "57", None, "mx_largo"),
        ("+54 11 2345 67890", "57", None, "ar_largo"),
        ("+54 9 11 2345", "57", None, "ar_largo"),
        ("+57 300 123 45678", "57", None, "co_no_movil"),
        ("+999 1", "57", None, "largo"),
        ("+51 9123 4567 8901 2345", "57", None, "largo"),
        # Un numero que llega como numero (JSON, Excel), no como texto.
        (573001234567, "57", "573001234567", "ok"),
        (None, "57", None, "vacio"),
        ("   ", "57", None, "vacio"),
        ("+-() ", "57", None, "sin_digitos"),
    ],
)
def test_country_and_shape_rules(raw: object, country: str | None, phone: str | None, reason: str) -> None:
    assert normalize_bulk_phone(raw, country) == BulkPhone(phone, reason)


@pytest.mark.parametrize(
    ("raw", "phone", "reason"),
    [
        # phonenumbers como segunda opinion: valido y de un tipo que no es movil.
        ("+34 912 345 678", None, "no_movil"),  # Madrid, fijo
        ("+34 612 345 678", "34612345678", "ok"),  # Espana, movil
        ("+51 1 2345678", None, "no_movil"),  # Lima, fijo
        ("+51 912 345 678", "51912345678", "ok"),  # Peru, movil
        ("+1 800 555 0101", None, "no_movil"),  # EE. UU., gratuito
    ],
)
def test_phonenumbers_second_opinion(raw: str, phone: str | None, reason: str) -> None:
    assert normalize_bulk_phone(raw, "57") == BulkPhone(phone, reason)


def test_a_number_phonenumbers_does_not_know_is_not_left_out() -> None:
    # Su metadata puede atrasar con un rango nuevo: decide la regla del pais y,
    # al mandar, Meta.
    assert not phonenumbers.is_valid_number(phonenumbers.parse("+573990000001"))
    assert normalize_bulk_phone("+57 399 000 0001", "57") == BulkPhone("573990000001", "ok")


def test_mexico_fixed_or_mobile_stays_in() -> None:
    # En Mexico (y EE. UU.) phonenumbers no distingue fijo de movil: no descarta.
    number = phonenumbers.parse("+525512345678")
    assert phonenumbers.number_type(number) == phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE
    assert normalize_bulk_phone("+52 55 1234 5678", "52").reason == BULK_PHONE_OK


def test_every_reason_is_declared() -> None:
    raws = [c[0] for c in KIT_CASES] + ["+57 601 2345678", "+52 55 1", "+54 11 1", "+999 1", "+34 912 345 678"]
    reasons = {normalize_bulk_phone(raw, "57").reason for raw in raws}
    assert reasons - {BULK_PHONE_OK} <= set(BULK_PHONE_REASONS)
    assert len(set(BULK_PHONE_REASONS)) == len(BULK_PHONE_REASONS)


@pytest.mark.parametrize("raw", [c[0] for c in KIT_CASES if c[2] == "ok"])
def test_ends_in_the_delivery_form(raw: str) -> None:
    # La forma que sale es la de whatsapp_delivery_phone, y normalizarla otra
    # vez la deja igual: la respuesta cae en la misma conversacion que el resto.
    phone = normalize_bulk_phone(raw, "57").phone
    assert phone is not None
    assert whatsapp_delivery_phone(phone) == phone
    assert normalize_bulk_phone("+" + phone, None).phone == phone
    assert phone in equivalent_whatsapp_phones(phone)


def test_kit_variants_are_the_equivalent_forms() -> None:
    # masivo.py, variantes(): las dos formas del mismo movil en Chatwoot.
    assert set(equivalent_whatsapp_phones("5215512345678")) == {"5215512345678", "525512345678"}
    assert set(equivalent_whatsapp_phones("541123456789")) == {"541123456789", "5491123456789"}


def test_existing_functions_do_not_change() -> None:
    # La normalizacion nueva no toca las de siempre (D13, D14).
    assert canonical_whatsapp_phone("5215512345678") == "525512345678"
    assert whatsapp_delivery_phone("525512345678") == "5215512345678"
    assert whatsapp_delivery_phone("5491123456789") == "541123456789"
    assert whatsapp_delivery_phone("573001234567") == "573001234567"
