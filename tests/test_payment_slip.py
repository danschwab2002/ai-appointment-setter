"""La ficha de pago: tienda, vencimiento y boton, contra las fichas reales de ATT1.

Fixtures capturados, no inventados:

- ``tests/fixtures/hotmart_payment_slip_printed_att1_20261018.json``: los 13
  ``PURCHASE_BILLET_PRINTED`` guardados en att1-db desde el 12/10, anonimizados
  (captura del 2026-10-18 ~19:40Z). Del hash de ``billet_url`` queda el largo
  (48) y de ``billet_barcode`` solo los digitos 19-26, el vencimiento.
- ``tests/fixtures/chatwoot_message_templates_inbox_11_ficha_20261018.json``:
  la plantilla ``att1_ficha_efectivo_01`` tal como esta en el catalogo del
  inbox 11 (captura del 2026-10-18 ~20:05Z).

Las derivaciones (una ficha sin referencia, una fila de Chile) estan marcadas
en cada test.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bridge.payment_slip import (
    PAYMENT_SLIP_BUTTON_SUFFIX_INVALID,
    PAYMENT_SLIP_DUE_DATE_UNKNOWN,
    PAYMENT_SLIP_METHOD_NOT_ENABLED,
    PAYMENT_SLIP_REASONS,
    PAYMENT_SLIP_URL_PREFIX_MISMATCH,
    PaymentSlipFacts,
    PaymentSlipResolution,
    format_due_date_es,
    parse_payment_slip_methods,
    payment_slip_facts,
    resolve_payment_slip,
)

FIXTURES = Path(__file__).parent / "fixtures"
SLIPS = json.loads(
    (FIXTURES / "hotmart_payment_slip_printed_att1_20261018.json").read_text(encoding="utf-8")
)["eventos"]
CATALOG = json.loads(
    (FIXTURES / "chatwoot_message_templates_inbox_11_ficha_20261018.json").read_text(
        encoding="utf-8"
    )
)["message_templates"]

OXXO_PREFIX = "https://customer-local-latam.ebanx.com/print/oxxo?hash="
CDMX = ZoneInfo("America/Mexico_City")

# La fila del diseno (2026-10-18-diseno-c1-la-ficha-de-oxxo.md, seccion A), tal
# como saldria de ``tomllib`` (las listas del TOML llegan como ``list``).
MX_ROW: dict[str, object] = {
    "medio": "CASHPAYMENT",
    "pais": "MX",
    "tienda": "OXXO",
    "zona_horaria": "America/Mexico_City",
    "referencia_largo": 34,
    "vencimiento_digitos": [19, 26],
    "vencimiento_dias_si_falta": 3,
    "boton_prefijo": OXXO_PREFIX,
    "habilitada": True,
}
MX_ONLY = parse_payment_slip_methods([MX_ROW])


def _payloads(*, medio: str, pais: str | None = None) -> list[dict[str, object]]:
    found = []
    for event in SLIPS:
        purchase = event["payload"]["data"]["purchase"]
        if purchase["payment"]["type"] != medio:
            continue
        if pais is not None and purchase["checkout_country"]["iso"] != pais:
            continue
        found.append(event["payload"])
    return found


MX_CASH = _payloads(medio="CASHPAYMENT", pais="MX")
CL_CASH = _payloads(medio="CASHPAYMENT", pais="CL")
OTHER_METHODS = [
    event["payload"]
    for event in SLIPS
    if event["payload"]["data"]["purchase"]["payment"]["type"] != "CASHPAYMENT"
]


def test_the_capture_has_the_shape_the_contract_relies_on() -> None:
    assert len(SLIPS) == 13
    assert len(MX_CASH) == 5
    assert len(CL_CASH) == 1
    assert sorted(
        p["data"]["purchase"]["payment"]["type"] for p in OTHER_METHODS
    ) == ["MERCADO_PAGO"] * 4 + ["NEQUI"] * 2 + ["YAPE"]
    for payload in OTHER_METHODS:
        payment = payload["data"]["purchase"]["payment"]
        assert "billet_url" not in payment and "billet_barcode" not in payment


# ── Los hechos ───────────────────────────────────────────────────────


def test_the_facts_of_a_mexican_slip_are_read_as_they_arrive() -> None:
    payload = MX_CASH[0]
    facts = payment_slip_facts(payload)

    purchase = payload["data"]["purchase"]
    assert facts.payment_type == "CASHPAYMENT"
    assert facts.checkout_country_iso == "MX"
    assert facts.billet_url == purchase["payment"]["billet_url"]
    assert facts.billet_barcode == purchase["payment"]["billet_barcode"]
    assert facts.generated_at is not None
    assert facts.generated_at.utcoffset() == timedelta(0)
    assert int(facts.generated_at.timestamp() * 1000) == purchase["order_date"]


def test_the_country_falls_back_to_the_buyer_address() -> None:
    # Derivado: se saca data.purchase.checkout_country de una ficha real.
    payload = deepcopy(MX_CASH[0])
    del payload["data"]["purchase"]["checkout_country"]

    assert payment_slip_facts(payload).checkout_country_iso == "MX"


@pytest.mark.parametrize(
    "payload",
    [None, [], "x", {}, {"data": None}, {"data": {"purchase": {"payment": 3}}}],
)
def test_missing_facts_are_none_and_never_raise(payload: object) -> None:
    assert payment_slip_facts(payload) == PaymentSlipFacts(None, None, None, None, None)


def test_a_wrongly_typed_order_date_is_unknown_not_an_error() -> None:
    # Derivado: order_date como texto y como booleano.
    for value in ("1791863778000", True, -1):
        payload = deepcopy(MX_CASH[0])
        payload["data"]["purchase"]["order_date"] = value
        assert payment_slip_facts(payload).generated_at is None


# ── La resolucion ────────────────────────────────────────────────────


def test_the_five_mexican_slips_resolve_to_oxxo_with_the_reference_due_date() -> None:
    resolutions = [resolve_payment_slip(payment_slip_facts(p), MX_ONLY) for p in MX_CASH]

    assert all(isinstance(r, PaymentSlipResolution) for r in resolutions)
    assert [r.due_date for r in resolutions] == [  # type: ignore[union-attr]
        date(2026, 10, 15),
        date(2026, 10, 16),
        date(2026, 10, 17),
        date(2026, 10, 19),
        date(2026, 10, 20),
    ]
    for payload, resolution in zip(MX_CASH, resolutions, strict=True):
        assert isinstance(resolution, PaymentSlipResolution)
        assert resolution.tienda == "OXXO"
        assert resolution.due_date_source == "reference"
        assert len(resolution.button_suffix) == 48
        assert resolution.button_suffix == payload["data"]["purchase"]["payment"][
            "billet_url"
        ].removeprefix(OXXO_PREFIX)
        assert resolution.method == MX_ONLY[0]
        # Lo medido: el vencimiento es el dia de generada en CDMX mas 3.
        generated = datetime.fromtimestamp(
            payload["data"]["purchase"]["order_date"] / 1000, tz=CDMX
        ).date()
        assert resolution.due_date == generated + timedelta(days=3)


def test_the_button_template_is_the_one_approved_in_the_catalog() -> None:
    [template] = [t for t in CATALOG if t["name"] == "att1_ficha_efectivo_01"]
    [buttons] = [c["buttons"] for c in template["components"] if c["type"] == "BUTTONS"]
    url_button = buttons[0]
    assert url_button["type"] == "URL"

    resolution = resolve_payment_slip(payment_slip_facts(MX_CASH[0]), MX_ONLY)

    assert isinstance(resolution, PaymentSlipResolution)
    assert resolution.button_url_template == url_button["url"]
    assert MX_ONLY[0].button_url_template == url_button["url"]
    # El ejemplo que Meta aprobo tambien pasa el patron del sufijo.
    [example] = url_button["example"]
    example_facts = PaymentSlipFacts("CASHPAYMENT", "MX", example, None, None)
    example_resolution = resolve_payment_slip(
        PaymentSlipFacts(
            "CASHPAYMENT", "MX", example, None, payment_slip_facts(MX_CASH[0]).generated_at
        ),
        MX_ONLY,
    )
    assert isinstance(example_resolution, PaymentSlipResolution)
    assert example_resolution.button_suffix == "EJEMPLO0123456789"
    assert resolve_payment_slip(example_facts, MX_ONLY) == PAYMENT_SLIP_DUE_DATE_UNKNOWN


def test_the_chilean_slip_is_not_enabled_with_the_mexico_only_table() -> None:
    [payload] = CL_CASH
    assert resolve_payment_slip(payment_slip_facts(payload), MX_ONLY) == (
        PAYMENT_SLIP_METHOD_NOT_ENABLED
    )


def test_a_chilean_row_with_the_oxxo_prefix_does_not_match_its_link() -> None:
    # Derivado: una fila de Chile habilitada por error con el prefijo de OXXO. El
    # link real de Chile es https://api.ebanx.com/ws/redirect?hash=...
    [payload] = CL_CASH
    methods = parse_payment_slip_methods([MX_ROW, {**MX_ROW, "pais": "CL"}])

    assert resolve_payment_slip(payment_slip_facts(payload), methods) == (
        PAYMENT_SLIP_URL_PREFIX_MISMATCH
    )


@pytest.mark.parametrize(
    "payload",
    OTHER_METHODS,
    ids=[
        f"{p['data']['purchase']['payment']['type']}-{p['data']['purchase']['checkout_country']['iso']}"
        for p in OTHER_METHODS
    ],
)
def test_the_other_methods_are_not_enabled(payload: dict[str, object]) -> None:
    assert resolve_payment_slip(payment_slip_facts(payload), MX_ONLY) == (
        PAYMENT_SLIP_METHOD_NOT_ENABLED
    )


def test_a_disabled_row_does_not_enable_its_method() -> None:
    methods = parse_payment_slip_methods([{**MX_ROW, "habilitada": False}])
    assert resolve_payment_slip(payment_slip_facts(MX_CASH[0]), methods) == (
        PAYMENT_SLIP_METHOD_NOT_ENABLED
    )


def test_an_empty_table_enables_nothing() -> None:
    assert parse_payment_slip_methods([]) == ()
    assert resolve_payment_slip(payment_slip_facts(MX_CASH[0]), ()) == (
        PAYMENT_SLIP_METHOD_NOT_ENABLED
    )


def _with_payment(**changes: object) -> PaymentSlipFacts:
    payload = deepcopy(MX_CASH[0])
    payment = payload["data"]["purchase"]["payment"]
    for key, value in changes.items():
        if value is None:
            payment.pop(key, None)
        else:
            payment[key] = value
    return payment_slip_facts(payload)


def test_a_slip_without_link_is_a_prefix_mismatch() -> None:
    # Derivado: una ficha real de OXXO sin billet_url.
    assert resolve_payment_slip(_with_payment(billet_url=None), MX_ONLY) == (
        PAYMENT_SLIP_URL_PREFIX_MISMATCH
    )


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "abc",  # demasiado corto
        "f" * 48 + "&utm_source=x",
        "f" * 48 + "#frag",
        "f" * 24 + "/" + "f" * 23,
        "f" * 48 + "%20",
        "f" * 129,
    ],
)
def test_an_unsafe_suffix_is_rejected(suffix: str) -> None:
    # Derivado: el link real de OXXO con otro sufijo.
    assert resolve_payment_slip(_with_payment(billet_url=OXXO_PREFIX + suffix), MX_ONLY) == (
        PAYMENT_SLIP_BUTTON_SUFFIX_INVALID
    )


@pytest.mark.parametrize(
    "barcode",
    [
        None,  # sin referencia
        "0",  # la forma de la de Chile
        "0" * 33,  # un digito menos
        "0" * 18 + "20261399" + "0" * 8,  # mes 13
        "0" * 18 + "2026101X" + "0" * 8,  # no son digitos
        "0" * 18 + "20261010" + "0" * 8,  # antes de generada (12/10)
        "0" * 18 + "20270101" + "0" * 8,  # mas de 31 dias despues
    ],
)
def test_without_a_usable_reference_the_due_date_is_generated_plus_days(
    barcode: str | None,
) -> None:
    # Derivado: la primera ficha real de OXXO (generada el 12/10 en CDMX) con
    # otra referencia.
    resolution = resolve_payment_slip(_with_payment(billet_barcode=barcode), MX_ONLY)

    assert isinstance(resolution, PaymentSlipResolution)
    assert resolution.due_date_source == "generated_plus_days"
    assert resolution.due_date == date(2026, 10, 15)


def test_without_reference_nor_generation_date_the_due_date_is_unknown() -> None:
    # Derivado: sin billet_barcode ni order_date.
    payload = deepcopy(MX_CASH[0])
    del payload["data"]["purchase"]["payment"]["billet_barcode"]
    del payload["data"]["purchase"]["order_date"]

    assert resolve_payment_slip(payment_slip_facts(payload), MX_ONLY) == (
        PAYMENT_SLIP_DUE_DATE_UNKNOWN
    )


def test_a_reference_alone_is_enough_without_generation_date() -> None:
    # Derivado: la referencia real, sin order_date.
    payload = deepcopy(MX_CASH[0])
    del payload["data"]["purchase"]["order_date"]

    resolution = resolve_payment_slip(payment_slip_facts(payload), MX_ONLY)

    assert isinstance(resolution, PaymentSlipResolution)
    assert resolution.due_date == date(2026, 10, 15)
    assert resolution.due_date_source == "reference"


def test_the_reasons_are_exported_and_distinct() -> None:
    assert PAYMENT_SLIP_REASONS == {
        "payment_slip_method_not_enabled",
        "payment_slip_url_prefix_mismatch",
        "payment_slip_button_suffix_invalid",
        "payment_slip_due_date_unknown",
    }


# ── La fecha en espanol ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (date(2026, 10, 22), "jueves 22 de octubre"),
        (date(2026, 10, 18), "domingo 18 de octubre"),
        (date(2027, 1, 4), "lunes 4 de enero"),
        (date(2026, 12, 2), "miércoles 2 de diciembre"),
        (date(2026, 10, 17), "sábado 17 de octubre"),
    ],
)
def test_the_due_date_is_written_in_spanish(value: date, text: str) -> None:
    assert format_due_date_es(value) == text


def test_the_catalog_example_date_has_the_same_shape() -> None:
    [template] = [t for t in CATALOG if t["name"] == "att1_ficha_efectivo_01"]
    [body] = [c for c in template["components"] if c["type"] == "BODY"]
    assert body["example"]["body_text"][0][3] == format_due_date_es(date(2026, 10, 22))


# ── La tabla ─────────────────────────────────────────────────────────


def test_the_mexico_row_of_the_design_is_accepted() -> None:
    [method] = MX_ONLY
    assert method.medio == "CASHPAYMENT"
    assert method.pais == "MX"
    assert method.vencimiento_digitos == (19, 26)
    assert method.habilitada is True


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"medio": "cashpayment"}, "medio"),
        ({"pais": "MEX"}, "pais"),
        ({"tienda": "  "}, "tienda"),
        ({"tienda": "OXXO\n"}, "tienda"),
        ({"zona_horaria": "America/Ciudad_Inventada"}, "zona_horaria"),
        ({"zona_horaria": 3}, "zona_horaria"),
        ({"referencia_largo": True}, "referencia_largo"),
        ({"referencia_largo": 0}, "referencia_largo"),
        ({"referencia_largo": 25}, "vencimiento_digitos"),
        ({"vencimiento_digitos": [19, 25]}, "vencimiento_digitos"),
        ({"vencimiento_digitos": [0, 7]}, "vencimiento_digitos"),
        ({"vencimiento_digitos": [19]}, "vencimiento_digitos"),
        ({"vencimiento_digitos": "19-26"}, "vencimiento_digitos"),
        ({"vencimiento_dias_si_falta": -1}, "vencimiento_dias_si_falta"),
        ({"vencimiento_dias_si_falta": "3"}, "vencimiento_dias_si_falta"),
        ({"boton_prefijo": "http://customer-local-latam.ebanx.com/print/oxxo?hash="}, "boton_prefijo"),
        ({"boton_prefijo": "https://customer-local-latam.ebanx.com/print/oxxo?hash"}, "boton_prefijo"),
        ({"boton_prefijo": "https://customer-local-latam.ebanx.com/print/oxxo?hash={{1}}"}, "boton_prefijo"),
        ({"boton_prefijo": "https:///print/oxxo?hash="}, "boton_prefijo"),
        ({"boton_prefijo": "https://ebanx.com/a b?hash="}, "boton_prefijo"),
        ({"habilitada": "true"}, "habilitada"),
    ],
)
def test_a_malformed_row_is_rejected(change: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_payment_slip_methods([{**MX_ROW, **change}])


def test_a_row_with_missing_or_unknown_keys_is_rejected() -> None:
    missing = dict(MX_ROW)
    del missing["tienda"]
    with pytest.raises(ValueError, match=r"missing: \['tienda'\]"):
        parse_payment_slip_methods([missing])
    with pytest.raises(ValueError, match=r"unknown: \['extra'\]"):
        parse_payment_slip_methods([{**MX_ROW, "extra": 1}])


def test_a_repeated_method_and_country_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"ficha.medios\[1\] repeats"):
        parse_payment_slip_methods([MX_ROW, {**MX_ROW, "habilitada": False}])


@pytest.mark.parametrize("raw", [None, {"medio": "CASHPAYMENT"}, "x", [None], [[]]])
def test_the_table_must_be_a_list_of_tables(raw: object) -> None:
    with pytest.raises(ValueError, match="ficha.medios"):
        parse_payment_slip_methods(raw)


def test_the_error_names_the_row() -> None:
    with pytest.raises(ValueError, match=r"ficha.medios\[1\]: .*pais"):
        parse_payment_slip_methods([MX_ROW, {**MX_ROW, "pais": "mx"}])
