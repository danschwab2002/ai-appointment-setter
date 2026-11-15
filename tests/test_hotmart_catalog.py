"""La lectura de Hotmart: productos y ofertas (E-574, MP-16).

FIXTURES: los dos ``tests/fixtures/hotmart_api_*_doc_20261114.json`` son
**sinteticos segun la documentacion, no capturados**: los ejemplos de respuesta
de https://developers.hotmart.com/docs/en/v1/product/product-list/ y
https://developers.hotmart.com/docs/en/v1/product/product-offers/, copiados tal
cual el 2026-11-15 (ver ``_origen`` de cada archivo). La captura real es deuda
del PR (F6 del plan de multiproducto). Las paginas 2 y los casos rotos se
DERIVAN de esos ejemplos cambiando un solo campo, y se nombran asi.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import json
from pathlib import Path
from typing import Any
import warnings

import httpx
import pytest

from bridge.hotmart_api import (
    HotmartApiClient,
    HotmartApiCredentials,
    HotmartPermanentError,
    HotmartTransientError,
)
from bridge.hotmart_catalog import (
    CATALOG_READ_ENV,
    HotmartCatalogReader,
    HotmartOffer,
    HotmartProduct,
    HotmartProductReading,
    build_catalog_reader,
    catalog_read_enabled,
    hotlink_from_offer_link,
    minimo_from_reading,
    parse_offer_item,
    parse_product_item,
    price_text,
)

FIXTURES = Path(__file__).parent / "fixtures"
SRC = Path(__file__).resolve().parents[1] / "src" / "bridge"
TOKEN = "token-de-prueba"


def _doc(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert data["_origen"]["clase"] == "sintetico segun doc, no capturado"
    return data["respuestas"]


PRODUCTS = _doc("hotmart_api_products_list_doc_20261114.json")
OFFERS = _doc("hotmart_api_product_offers_doc_20261114.json")
PRODUCT_A_UCODE = "f2b3be1f-313f-4a2d-b5b7-1c39d67dd3ee"


def _credentials() -> HotmartApiCredentials:
    return HotmartApiCredentials(client_id="id", client_secret="secreto", basic="b")


SECRET = "secreto-unico-de-prueba-e574"


class FakeHotmart:
    """Un Hotmart de mentira que contesta con los ejemplos de la doc."""

    def __init__(self, routes: dict[tuple[str, str | None], list[httpx.Response]]):
        # (path, page_token) -> respuestas en orden
        self.routes = routes
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if "oauth/token" in request.url.path:
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 172799})
        assert request.method == "GET", "la lectura nunca escribe"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        key = (request.url.path, request.url.params.get("page_token"))
        queue = self.routes.get(key)
        assert queue, f"pedido inesperado: {key}"
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def reader(self, **kwargs: Any) -> HotmartCatalogReader:
        client = HotmartApiClient(
            credentials=_credentials(), transport=httpx.MockTransport(self.handler)
        )
        return HotmartCatalogReader(client, **kwargs)

    def api_calls(self) -> list[httpx.Request]:
        return [c for c in self.calls if "oauth/token" not in c.url.path]


def _last_page(body: dict[str, Any]) -> dict[str, Any]:
    """DERIVADO: el mismo ejemplo de la doc como ultima pagina (sin next)."""
    derived = copy.deepcopy(body)
    derived["page_info"]["next_page_token"] = None
    return derived


OFFERS_PATH_A = f"/products/api/v1/products/{PRODUCT_A_UCODE}/offers"


# --- el flag y que nada vivo lo llama ---------------------------------------


def test_flag_off_by_default_and_does_not_open_credentials(tmp_path: Path) -> None:
    missing = tmp_path / "no-existe"
    assert catalog_read_enabled({}) is False
    assert catalog_read_enabled({CATALOG_READ_ENV: "false"}) is False
    assert build_catalog_reader({}, credentials_path=missing) is None
    assert build_catalog_reader({CATALOG_READ_ENV: "0"}, credentials_path=None) is None


def test_flag_on_builds_reader_and_needs_credentials(tmp_path: Path) -> None:
    creds = tmp_path / "hotmart"
    creds.write_text(
        f"HOTMART_CLIENT_ID=a\nHOTMART_CLIENT_SECRET={SECRET}\nHOTMART_BASIC=c\n",
        encoding="utf-8",
    )
    reader = build_catalog_reader({CATALOG_READ_ENV: "true"}, credentials_path=creds)
    assert isinstance(reader, HotmartCatalogReader)
    assert SECRET not in repr(reader)
    with pytest.raises(ValueError, match="credentials_path_missing"):
        build_catalog_reader({CATALOG_READ_ENV: "true"}, credentials_path=None)


def test_no_live_module_imports_the_catalog_reader() -> None:
    """En este PR ningun modulo del bridge usa la lectura (la usa MP-17)."""
    users = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "hotmart_catalog.py":
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "bridge.hotmart_catalog":
                users.append(path.name)
            elif isinstance(node, ast.Import) and any(
                alias.name == "bridge.hotmart_catalog" for alias in node.names
            ):
                users.append(path.name)
            elif isinstance(node, ast.ImportFrom) and node.module in ("bridge", None) and any(
                alias.name == "hotmart_catalog" for alias in node.names
            ):
                users.append(path.name)
            elif isinstance(node, ast.ImportFrom) and node.level and node.module == "hotmart_catalog":
                users.append(path.name)
    assert users == []
    # y por texto, por si alguien lo carga con importlib
    mentions = [
        path.name
        for path in SRC.rglob("*.py")
        if path.name != "hotmart_catalog.py" and "hotmart_catalog" in path.read_text(encoding="utf-8")
    ]
    assert mentions == []


# --- el parser contra los ejemplos de la doc ----------------------------------


def test_products_doc_example_parses_every_item() -> None:
    products = [parse_product_item(item) for item in PRODUCTS["200"]["items"]]
    assert all(products)
    first = products[0]
    assert first == HotmartProduct(
        product_id=698441,
        name="Product A",
        ucode=PRODUCT_A_UCODE,
        status="DRAFT",
        format="EBOOK",
        is_subscription=False,
        warranty_days=7,
    )
    assert [p.product_id for p in products] == [698441, 1117869, 486869, 4319408]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", None),
        ("id", True),
        ("id", "698441"),
        ("id", 0),
        ("name", ""),
        ("name", None),
        ("ucode", "no-es-un-uuid"),
        ("ucode", "../../coupon"),
        ("ucode", None),
    ],
)
def test_product_item_derived_broken_is_dropped(field: str, value: Any) -> None:
    item = copy.deepcopy(PRODUCTS["200"]["items"][0])
    item[field] = value
    assert parse_product_item(item) is None


def test_offers_doc_example_parses_with_hotlink() -> None:
    offer, warning = parse_offer_item(OFFERS["200"]["items"][0])
    assert warning is None
    assert offer == HotmartOffer(
        code="02mhofjd",
        name=None,
        price="10",
        currency="BRL",
        payment_mode="PAY_IN_FULL",
        is_main_offer=True,
        hotlink="A00000000A",
    )


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda o: o.update(code="a"), "offer_skipped:code_invalid"),
        (lambda o: o.update(code=None), "offer_skipped:code_invalid"),
        (lambda o: o.pop("price"), "offer_skipped:02mhofjd:price_missing"),
        (lambda o: o["price"].update(value=0), "offer_skipped:02mhofjd:price_invalid"),
        (lambda o: o["price"].update(value=-5), "offer_skipped:02mhofjd:price_invalid"),
        (lambda o: o["price"].update(value=True), "offer_skipped:02mhofjd:price_invalid"),
        (lambda o: o["price"].update(value=None), "offer_skipped:02mhofjd:price_invalid"),
        (lambda o: o["price"].update(value=float("nan")), "offer_skipped:02mhofjd:price_invalid"),
        (lambda o: o["price"].update(currency_code="reales"), "offer_skipped:02mhofjd:currency_invalid"),
        (lambda o: o["price"].update(currency_code=None), "offer_skipped:02mhofjd:currency_invalid"),
    ],
)
def test_offer_item_derived_broken_is_skipped_with_reason(mutate: Any, reason: str) -> None:
    item = copy.deepcopy(OFFERS["200"]["items"][0])
    mutate(item)
    offer, warning = parse_offer_item(item)
    assert offer is None
    assert warning == reason


@pytest.mark.parametrize(
    "value,expected",
    [
        (10, "10"),
        (10.0, "10"),
        (47, "47"),
        (1397.0, "1397"),
        (1397.5, "1397.5"),
        (19.9, "19.9"),
        (0.1 + 0.2, None),
        (19.999, None),
        ("199", "199"),
        ("1397.00", "1397"),
        (float("inf"), None),
        ("1.397", None),
        ("1397.5", "1397.5"),
        ("1,397", None),
        ("", None),
    ],
)
def test_price_text(value: Any, expected: str | None) -> None:
    assert price_text(value) == expected


@pytest.mark.parametrize(
    "link,code,expected",
    [
        ("https://pay.hotmart.com/A00000000A?off=02mhofjd", "02mhofjd", "A00000000A"),
        ("https://pay.hotmart.com/A00000000A?off=02mhofjd&checkoutMode=10", "02mhofjd", "A00000000A"),
        ("https://pay.hotmart.com/A00000000A?off=otraoferta", "02mhofjd", None),
        ("https://pay.hotmart.com/A00000000A", "02mhofjd", None),
        ("http://pay.hotmart.com/A00000000A?off=02mhofjd", "02mhofjd", None),
        ("https://pay.hotmart.com.evil.example/A00000000A?off=02mhofjd", "02mhofjd", None),
        ("https://pay.hotmart.com/a/b?off=02mhofjd", "02mhofjd", None),
        ("https://pay.hotmart.com/A0?off=02mhofjd", "02mhofjd", None),
        ("https://pay.hotmart.com/A00000000A?off=02mhofjd&off=x", "02mhofjd", None),
        (None, "02mhofjd", None),
        ("", "02mhofjd", None),
    ],
)
def test_hotlink_from_offer_link(link: Any, code: str, expected: str | None) -> None:
    assert hotlink_from_offer_link(link, code) == expected


# --- el cliente: paginado, filtro y errores -----------------------------------


def test_read_product_follows_pagination_and_builds_the_minimo() -> None:
    page1 = PRODUCTS["200"]  # trae next_page_token, como en la doc
    page2 = _last_page(PRODUCTS["200"])  # DERIVADO: ultima pagina, con los otros tres
    page2["items"] = page2["items"][1:]
    offers_last = _last_page(OFFERS["200"])
    token1 = page1["page_info"]["next_page_token"]
    fake = FakeHotmart(
        {
            ("/products/api/v1/products", None): [httpx.Response(200, json=page1)],
            ("/products/api/v1/products", token1): [httpx.Response(200, json=page2)],
            (OFFERS_PATH_A, None): [httpx.Response(200, json=offers_last)],
        }
    )
    reading = asyncio.run(fake.reader().read_product(698441))
    assert isinstance(reading, HotmartProductReading)
    assert reading.product.ucode == PRODUCT_A_UCODE
    assert [o.code for o in reading.offers] == ["02mhofjd"]
    calls = fake.api_calls()
    assert [c.url.params.get("id") for c in calls[:2]] == ["698441", "698441"]
    assert calls[1].url.params["page_token"] == token1
    assert all(c.url.params["max_results"] == "50" for c in calls)
    draft = minimo_from_reading(reading)
    assert draft.fields == {
        "nombre": "Product A",
        "hotmart": {"producto_id": 698441, "hotlink": "A00000000A"},
        "ofertas": [
            {"codigo": "02mhofjd", "precio": "10", "moneda": "BRL", "por_defecto": True}
        ],
    }
    assert draft.warnings == ("product_status:DRAFT",)


def test_find_product_requires_the_same_id_even_if_the_filter_is_ignored() -> None:
    fake = FakeHotmart(
        {("/products/api/v1/products", None): [httpx.Response(200, json=_last_page(PRODUCTS["200"]))]}
    )
    assert asyncio.run(fake.reader().find_product(5071808)) is None


def test_find_product_404_means_not_in_the_account() -> None:
    fake = FakeHotmart(
        {("/products/api/v1/products", None): [httpx.Response(404, json={"error": "not_found"})]}
    )
    assert asyncio.run(fake.reader().read_product(698441)) is None


def test_offers_404_after_the_product_is_found_is_an_error() -> None:
    fake = FakeHotmart(
        {
            ("/products/api/v1/products", None): [httpx.Response(200, json=_last_page(PRODUCTS["200"]))],
            (OFFERS_PATH_A, None): [httpx.Response(404, json={})],
        }
    )
    with pytest.raises(HotmartPermanentError) as excinfo:
        asyncio.run(fake.reader().read_product(698441))
    assert excinfo.value.code == "hotmart_offers_list_http_404"


@pytest.mark.parametrize(
    "status,error_type,code",
    [
        ("400", HotmartPermanentError, "hotmart_offers_list_http_400"),
        ("401", HotmartTransientError, "hotmart_offers_list_http_401"),
        ("500", HotmartTransientError, "hotmart_offers_list_http_500"),
    ],
)
def test_offers_doc_error_responses(status: str, error_type: type, code: str) -> None:
    fake = FakeHotmart({(OFFERS_PATH_A, None): [httpx.Response(int(status), json=OFFERS[status])]})
    with pytest.raises(error_type) as excinfo:
        asyncio.run(fake.reader().list_offers(PRODUCT_A_UCODE))
    assert excinfo.value.code == code
    # el error no lleva el cuerpo ni la URL
    assert OFFERS[status]["error"] not in str(excinfo.value)
    assert "hotmart.com" not in str(excinfo.value)


@pytest.mark.parametrize(
    "status,error_type",
    [
        ("400", HotmartPermanentError),
        ("401", HotmartTransientError),
        ("500", HotmartTransientError),
        # DERIVADO: la doc no publica el 429; el cuerpo es el del 500.
        ("429", HotmartTransientError),
        # DERIVADO: un 403 (sin permiso de lectura de productos), cuerpo del 400.
        ("403", HotmartPermanentError),
    ],
)
def test_products_doc_error_responses(status: str, error_type: type) -> None:
    body = PRODUCTS.get(status) or PRODUCTS["500" if status == "429" else "400"]
    fake = FakeHotmart(
        {("/products/api/v1/products", None): [httpx.Response(int(status), json=body)]}
    )
    with pytest.raises(error_type) as excinfo:
        asyncio.run(fake.reader().list_products())
    assert type(excinfo.value) is error_type
    assert excinfo.value.code == f"hotmart_products_list_http_{status}"


def test_find_product_404_on_page_two_is_an_error_not_a_short_list() -> None:
    page1 = PRODUCTS["200"]  # con next_page_token, como en la doc
    token1 = page1["page_info"]["next_page_token"]
    fake = FakeHotmart(
        {
            ("/products/api/v1/products", None): [httpx.Response(200, json=page1)],
            ("/products/api/v1/products", token1): [httpx.Response(404, json={})],
        }
    )
    with pytest.raises(HotmartPermanentError) as excinfo:
        asyncio.run(fake.reader().find_product(698441))
    assert excinfo.value.code == "hotmart_products_find_http_404"


def test_401_invalidates_the_token_for_the_next_call() -> None:
    offers_last = _last_page(OFFERS["200"])
    fake = FakeHotmart(
        {
            (OFFERS_PATH_A, None): [
                httpx.Response(401, json=OFFERS["401"]),
                httpx.Response(200, json=offers_last),
            ]
        }
    )
    reader = fake.reader()
    with pytest.raises(HotmartTransientError):
        asyncio.run(reader.list_offers(PRODUCT_A_UCODE))
    offers, _ = asyncio.run(reader.list_offers(PRODUCT_A_UCODE))
    assert [o.code for o in offers] == ["02mhofjd"]
    tokens = [c for c in fake.calls if "oauth/token" in c.url.path]
    assert len(tokens) == 2


def test_pagination_loop_is_an_error_not_an_endless_read() -> None:
    page = OFFERS["200"]  # DERIVADO en uso: Hotmart devolviendo siempre el mismo token
    token = page["page_info"]["next_page_token"]
    fake = FakeHotmart(
        {
            (OFFERS_PATH_A, None): [httpx.Response(200, json=page)],
            (OFFERS_PATH_A, token): [httpx.Response(200, json=page)],
        }
    )
    with pytest.raises(HotmartTransientError) as excinfo:
        asyncio.run(fake.reader().list_offers(PRODUCT_A_UCODE))
    assert excinfo.value.code == "hotmart_offers_list_pagination_loop"


def test_too_many_pages_fails_instead_of_returning_a_partial_list() -> None:
    def page(n: int) -> dict[str, Any]:
        body = copy.deepcopy(OFFERS["200"])  # DERIVADO: un token distinto por pagina
        body["page_info"]["next_page_token"] = f"pagina-{n + 1}"
        return body

    routes: dict[tuple[str, str | None], list[httpx.Response]] = {
        (OFFERS_PATH_A, None): [httpx.Response(200, json=page(1))]
    }
    for n in range(2, 6):
        routes[(OFFERS_PATH_A, f"pagina-{n}")] = [httpx.Response(200, json=page(n))]
    fake = FakeHotmart(routes)
    with pytest.raises(HotmartPermanentError) as excinfo:
        asyncio.run(fake.reader(max_pages=3).list_offers(PRODUCT_A_UCODE))
    assert excinfo.value.code == "hotmart_offers_list_too_many_pages"
    assert len(fake.api_calls()) == 3


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"items": {"code": "x"}},
        {"items": [], "page_info": "x"},
        {"items": [], "page_info": {"next_page_token": "a" * 3000}},
    ],
)
def test_invalid_shapes_are_transient_errors(body: Any) -> None:
    fake = FakeHotmart({(OFFERS_PATH_A, None): [httpx.Response(200, json=body)]})
    with pytest.raises(HotmartTransientError):
        asyncio.run(fake.reader().list_offers(PRODUCT_A_UCODE))


def test_invalid_json_is_transient() -> None:
    fake = FakeHotmart({(OFFERS_PATH_A, None): [httpx.Response(200, content=b"<html>")]})
    with pytest.raises(HotmartTransientError) as excinfo:
        asyncio.run(fake.reader().list_offers(PRODUCT_A_UCODE))
    assert excinfo.value.code == "hotmart_offers_list_invalid_json"


def test_empty_items_null_is_an_empty_list() -> None:
    fake = FakeHotmart(
        {(OFFERS_PATH_A, None): [httpx.Response(200, json={"items": None, "page_info": {}})]}
    )
    offers, warnings = asyncio.run(fake.reader().list_offers(PRODUCT_A_UCODE))
    assert offers == () and warnings == ()


def test_bad_inputs_never_reach_hotmart() -> None:
    fake = FakeHotmart({})
    reader = fake.reader()
    with pytest.raises(HotmartPermanentError):
        asyncio.run(reader.list_offers("../../coupon"))
    with pytest.raises(HotmartPermanentError):
        asyncio.run(reader.find_product(0))
    with pytest.raises(HotmartPermanentError):
        asyncio.run(reader.find_product(True))  # type: ignore[arg-type]
    with pytest.raises(HotmartPermanentError):
        asyncio.run(reader.list_products(status="ACTIVE&x=1"))
    assert fake.calls == []


def test_list_offers_skips_broken_and_duplicated_with_warnings() -> None:
    body = _last_page(OFFERS["200"])
    good = body["items"][0]
    broken = copy.deepcopy(good)  # DERIVADO: sin precio
    broken["code"] = "sinprecio"
    broken.pop("price")
    body["items"] = [good, copy.deepcopy(good), broken]
    fake = FakeHotmart({(OFFERS_PATH_A, None): [httpx.Response(200, json=body)]})
    offers, warnings = asyncio.run(fake.reader().list_offers(PRODUCT_A_UCODE))
    assert [o.code for o in offers] == ["02mhofjd"]
    assert warnings == ("offer_duplicated:02mhofjd", "offer_skipped:sinprecio:price_missing")


# --- el armado del minimo -----------------------------------------------------


def _reading(*offers: HotmartOffer, status: str = "ACTIVE") -> HotmartProductReading:
    product = parse_product_item(PRODUCTS["200"]["items"][0])
    assert product is not None
    product = HotmartProduct(**{**product.__dict__, "status": status})
    return HotmartProductReading(product=product, offers=offers, warnings=())


def _offer(code: str, *, main: bool, hotlink: str | None = "A00000000A") -> HotmartOffer:
    base, _ = parse_offer_item(OFFERS["200"]["items"][0])
    assert base is not None
    return HotmartOffer(**{**base.__dict__, "code": code, "is_main_offer": main, "hotlink": hotlink})


def test_minimo_without_exactly_one_main_offer_marks_none() -> None:
    for offers, count in [
        ((_offer("aaaa1111", main=False), _offer("bbbb2222", main=False)), 0),
        ((_offer("aaaa1111", main=True), _offer("bbbb2222", main=True)), 2),
    ]:
        draft = minimo_from_reading(_reading(*offers))
        assert [o["por_defecto"] for o in draft.fields["ofertas"]] == [False, False]
        assert f"main_offer_count:{count}" in draft.warnings


def test_minimo_hotlink_ambiguous_or_missing() -> None:
    ambiguous = minimo_from_reading(
        _reading(_offer("aaaa1111", main=True), _offer("bbbb2222", main=False, hotlink="B00000000B"))
    )
    assert ambiguous.fields["hotmart"]["hotlink"] is None
    assert "hotlink_ambiguous" in ambiguous.warnings
    missing = minimo_from_reading(_reading(_offer("aaaa1111", main=True, hotlink=None)))
    assert missing.fields["hotmart"]["hotlink"] is None
    assert "hotlink_missing" in missing.warnings
    partial = minimo_from_reading(
        _reading(_offer("aaaa1111", main=True), _offer("bbbb2222", main=False, hotlink=None))
    )
    assert partial.fields["hotmart"]["hotlink"] == "A00000000A"
    assert "hotlink_missing_in_some_offers" in partial.warnings


def test_minimo_without_offers_and_active_product() -> None:
    draft = minimo_from_reading(_reading())
    assert draft.fields["ofertas"] == []
    assert draft.warnings == ("no_offers",)
    ok = minimo_from_reading(_reading(_offer("aaaa1111", main=True)))
    assert ok.warnings == ()


def test_find_product_with_two_items_of_the_same_id_is_an_error() -> None:
    body = _last_page(PRODUCTS["200"])  # DERIVADO: el producto A repetido
    body["items"] = [body["items"][0], copy.deepcopy(body["items"][0])]
    fake = FakeHotmart({("/products/api/v1/products", None): [httpx.Response(200, json=body)]})
    with pytest.raises(HotmartTransientError) as excinfo:
        asyncio.run(fake.reader().find_product(698441))
    assert excinfo.value.code == "hotmart_products_find_ambiguous"


def test_offer_item_variants_the_parser_normalizes() -> None:
    item = copy.deepcopy(OFFERS["200"]["items"][0])  # DERIVADO: moneda en minuscula
    item["price"]["currency_code"] = "brl"
    offer, _ = parse_offer_item(item)
    assert offer is not None and offer.currency == "BRL"
    item = copy.deepcopy(OFFERS["200"]["items"][0])  # DERIVADO: precio como texto
    item["price"]["value"] = "1397.00"
    offer, _ = parse_offer_item(item)
    assert offer is not None and offer.price == "1397"
    item = copy.deepcopy(OFFERS["200"]["items"][0])  # DERIVADO: off= en mayuscula
    item["direct_offer_link_for_creator"] = "https://pay.hotmart.com/A00000000A?off=02MHOFJD"
    offer, _ = parse_offer_item(item)
    # a proposito: el off= tiene que ser el mismo codigo, byte a byte
    assert offer is not None and offer.hotlink is None
    item = copy.deepcopy(OFFERS["200"]["items"][0])  # DERIVADO: is_main_offer como texto
    item["is_main_offer"] = "true"
    offer, warning = parse_offer_item(item)
    assert offer is not None and offer.is_main_offer is False
    assert warning == "offer:02mhofjd:is_main_offer_invalid"


def test_minimo_warns_long_name_and_non_single_payment() -> None:
    reading = _reading(_offer("aaaa1111", main=True))
    long_name = HotmartProduct(**{**reading.product.__dict__, "name": "x" * 81})
    subscription = HotmartOffer(**{**reading.offers[0].__dict__, "payment_mode": "SUBSCRIPTION"})
    draft = minimo_from_reading(
        HotmartProductReading(product=long_name, offers=(subscription,), warnings=())
    )
    assert draft.warnings == ("name_too_long", "payment_mode:aaaa1111:SUBSCRIPTION")
