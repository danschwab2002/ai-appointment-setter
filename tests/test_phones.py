"""La equivalencia de telefonos de WhatsApp en Python (bridge.phones).

Es el espejo de _whatsapp_phone_canonical y _whatsapp_phone_variants de la
migracion 20261001000100: la misma tabla que recorre
tests/sql/followup_engine/validate_whatsapp_phone_equivalence.mjs sobre la
funcion real de la base. Los numeros son de rangos de prueba (digitos
repetidos), no de personas.

La forma de entrega (whatsapp_delivery_phone) sale de la medicion del
2026-10-01 sobre el Chatwoot de produccion (decisiones D13 y D14): Mexico se
manda con el 1, Argentina sin el 9.
"""

from __future__ import annotations

import pytest

from bridge.phones import (
    canonical_whatsapp_phone,
    equivalent_whatsapp_phones,
    same_whatsapp_phone,
    whatsapp_delivery_phone,
    whatsapp_phone_region,
)

MX_FORM = "525512345678"  # 52 + 10: lo que guarda el formulario
MX_WHATSAPP = "5215512345678"  # 521 + 10: Hotmart y el wa_id
AR_FORM = "541112345678"  # 54 + 10
AR_WHATSAPP = "5491112345678"  # 549 + 10


@pytest.mark.parametrize(
    ("phone", "canonical"),
    [
        # Mexico y Argentina, en los dos sentidos.
        (MX_WHATSAPP, MX_FORM),
        (MX_FORM, MX_FORM),
        (AR_WHATSAPP, AR_FORM),
        (AR_FORM, AR_FORM),
        # Solo cuentan los digitos: un "+", espacios, guiones o parentesis.
        ("+52 1 55 1234 5678", MX_FORM),
        ("+54 9 (11) 1234-5678", AR_FORM),
        ("+525512345678", MX_FORM),
        # La regla va anclada por largo. Un nacional de 10 digitos que empieza
        # con 1 o con 9 (12 digitos en total) queda igual a si mismo.
        ("521551234567", "521551234567"),
        ("549111234567", "549111234567"),
        # 14 digitos con el prefijo tampoco se tocan.
        ("52155123456789", "52155123456789"),
        # Brasil (el noveno digito) queda afuera a proposito (D16).
        ("5531999999999", "5531999999999"),
        ("553199999999", "553199999999"),
        # Otros paises.
        ("12025550123", "12025550123"),
        ("573001234567", "573001234567"),
    ],
)
def test_canonical_form(phone: str, canonical: str) -> None:
    assert canonical_whatsapp_phone(phone) == canonical
    # Idempotente: la canonica de la canonica es ella misma.
    assert canonical_whatsapp_phone(canonical) == canonical


@pytest.mark.parametrize("phone", [None, "", "   ", "+", "sin digitos", 525512345678])
def test_canonical_form_without_digits_is_none(phone: object) -> None:
    assert canonical_whatsapp_phone(phone) is None  # type: ignore[arg-type]
    assert equivalent_whatsapp_phones(phone) == ()  # type: ignore[arg-type]
    assert whatsapp_delivery_phone(phone) is None  # type: ignore[arg-type]
    assert whatsapp_phone_region(phone) == "other"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("phone", "forms"),
    [
        # La canonica primero y la forma de WhatsApp despues, igual que la base.
        (MX_FORM, (MX_FORM, MX_WHATSAPP)),
        (MX_WHATSAPP, (MX_FORM, MX_WHATSAPP)),
        (AR_FORM, (AR_FORM, AR_WHATSAPP)),
        (AR_WHATSAPP, (AR_FORM, AR_WHATSAPP)),
        ("+52 1 55 1234 5678", (MX_FORM, MX_WHATSAPP)),
        # Un 52 + 10 cuyo nacional empieza con 1: es canonico, y su otra forma
        # lleva el 1 delante de los diez.
        ("521551234567", ("521551234567", "5211551234567")),
        # Una sola forma para todo lo demas.
        ("5531999999999", ("5531999999999",)),
        ("12025550123", ("12025550123",)),
    ],
)
def test_equivalent_forms(phone: str, forms: tuple[str, ...]) -> None:
    assert equivalent_whatsapp_phones(phone) == forms
    # Todas las formas comparten canonica.
    assert {canonical_whatsapp_phone(form) for form in forms} == {forms[0]}


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        (MX_FORM, MX_WHATSAPP, True),
        (MX_WHATSAPP, MX_FORM, True),
        (AR_FORM, AR_WHATSAPP, True),
        ("+" + AR_WHATSAPP, AR_FORM, True),
        (MX_FORM, MX_FORM, True),
        # Otros diez digitos: otra persona.
        (MX_FORM, "5215512345679", False),
        (AR_FORM, "5491112345679", False),
        # Brasil con y sin el 9 no equivale.
        ("5531999999999", "553199999999", False),
        # Mexico no equivale a Argentina aunque coincidan los diez digitos.
        ("525512345678", "545512345678", False),
        (None, None, False),
        ("", MX_FORM, False),
    ],
)
def test_same_phone(first: str | None, second: str | None, same: bool) -> None:
    assert same_whatsapp_phone(first, second) is same


@pytest.mark.parametrize(
    ("phone", "delivery", "region"),
    [
        # Mexico: con el 1, que es donde cae la respuesta en Chatwoot.
        (MX_FORM, MX_WHATSAPP, "MX"),
        (MX_WHATSAPP, MX_WHATSAPP, "MX"),
        # Argentina: sin el 9, la forma que Chatwoot normaliza.
        (AR_FORM, AR_FORM, "AR"),
        (AR_WHATSAPP, AR_FORM, "AR"),
        # Cualquier otro: igual a si mismo.
        ("5531999999999", "5531999999999", "other"),
        ("12025550123", "12025550123", "other"),
        ("+57 300 123 4567", "573001234567", "other"),
    ],
)
def test_delivery_form_and_region(phone: str, delivery: str, region: str) -> None:
    assert whatsapp_delivery_phone(phone) == delivery
    assert whatsapp_phone_region(phone) == region
    # La forma de entrega es siempre el mismo telefono.
    assert same_whatsapp_phone(delivery, phone)
