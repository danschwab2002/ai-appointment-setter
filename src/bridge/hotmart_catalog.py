"""La lectura de Hotmart: productos y ofertas (hotmart-catalog-read-v1, E-574, MP-16).

Lee de la API de productos de Hotmart lo que el minimo de ``product-card-v1``
necesita y el Automation Expert no tiene que tipear: el ID del producto, su
nombre, el hotlink del checkout y cada oferta con su codigo, precio y moneda,
y cual es la principal. Lo usa la sincronizacion diaria (MP-17) y, despues,
la herramienta ``read_hotmart_product`` del AE (MP-18).

**En este PR no lo llama ningun camino vivo.** El flag
``HOTMART_CATALOG_READ_ENABLED`` (apagado por defecto) decide si
``build_catalog_reader`` arma un lector; apagado, devuelve ``None`` sin abrir
el archivo de credenciales. Solo lectura: dos ``GET`` y nada mas.

Los dos endpoints (documentacion publica de Hotmart, leida el 2026-11-15):

- ``GET /products/api/v1/products`` (https://developers.hotmart.com/docs/en/v1/product/product-list/):
  ``items[]`` con ``id``, ``name``, ``ucode``, ``status``, ``format``,
  ``is_subscription``, ``warranty_period``; filtros ``id``, ``status``,
  ``format``; paginado por ``page_token``.
- ``GET /products/api/v1/products/:ucode/offers`` (https://developers.hotmart.com/docs/en/v1/product/product-offers/):
  ``items[]`` con ``code``, ``name``, ``price.value``, ``price.currency_code``,
  ``payment_mode``, ``is_main_offer`` y ``direct_offer_link_for_creator``
  (``https://pay.hotmart.com/<hotlink>?off=<code>``: de ahi sale el hotlink,
  que la lista de productos no trae).

**[sin verificar contra una respuesta real]:** los fixtures son los ejemplos de
la documentacion, no capturas (ver el PR y el contrato §6).

Reusa ``HotmartApiClient`` (el token, que invalida ante un 401, la
clasificacion de 401, 429 y 5xx como transitorios y errores sin URL ni
cuerpo). Ninguna credencial ni token sale de aca.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

import httpx

from bridge.hotmart_api import (
    HotmartApiClient,
    HotmartPermanentError,
    HotmartTransientError,
    load_hotmart_api_credentials,
)

CATALOG_READ_ENV = "HOTMART_CATALOG_READ_ENABLED"

PRODUCTS_PATH = "/products/api/v1/products"
OFFERS_PATH = "/products/api/v1/products/{ucode}/offers"

# La documentacion dice 50 por pagina por defecto en la lista de productos y no
# publica el maximo: se pide 50 y se acepta lo que venga.
DEFAULT_PAGE_SIZE = 50
# Tope de paginas por lectura. Pasarlo es un error, no una lista cortada: una
# lista de ofertas incompleta haria creer a la sincronizacion (MP-17) que una
# oferta desaparecio.
DEFAULT_MAX_PAGES = 20

_TRUE = frozenset({"1", "true", "yes", "on"})
_UCODE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# Los mismos de instance_manifest.py (_OFFER_CODE, _HOTLINK) y de
# product-card-v1 §2: lo que no tenga esta forma no entra al minimo.
_OFFER_CODE = re.compile(r"[A-Za-z0-9]{4,32}")
_HOTLINK = re.compile(r"[A-Za-z0-9]{4,32}")
_CURRENCY = re.compile(r"[A-Z]{3}")
# Hasta dos decimales: «1.397» como texto puede ser mil trescientos noventa y
# siete con punto de miles (MXN, BRL), y un precio de lista no tiene tres.
_DECIMAL_TEXT = re.compile(r"[0-9]{1,12}(\.[0-9]{1,2})?")
_MAX_DECIMALS = 2
_NAME_MAX = 80  # product-card-v1 §2
_SINGLE_PAYMENT_MODES = frozenset({"PAY_IN_FULL", "UNIQUE_PAYMENT"})
_PAGE_TOKEN_MAX = 2048
_CHECKOUT_HOSTS = frozenset({"pay.hotmart.com"})


def catalog_read_enabled(env: Mapping[str, str]) -> bool:
    """El flag, leido de un mapping (el entorno del proceso o un dict de test)."""
    value = env.get(CATALOG_READ_ENV, "")
    return isinstance(value, str) and value.strip().lower() in _TRUE


@dataclass(frozen=True)
class HotmartProduct:
    product_id: int
    name: str
    ucode: str
    status: str | None
    format: str | None
    is_subscription: bool | None
    warranty_days: int | None


@dataclass(frozen=True)
class HotmartOffer:
    code: str
    name: str | None
    price: str
    currency: str
    payment_mode: str | None
    is_main_offer: bool
    hotlink: str | None


@dataclass(frozen=True)
class HotmartProductReading:
    """Un producto con todas sus ofertas, tal como lo dio Hotmart.

    ``warnings`` lista lo que se descarto o no cierra (una oferta sin precio,
    un link de checkout que no se lee), con codigos cortos y sin datos.
    """

    product: HotmartProduct
    offers: tuple[HotmartOffer, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class MinimoDraft:
    """Lo que la lectura le aporta al minimo de product-card-v1 (§2).

    No es un minimo completo: falta la ``clave``, ``no_se_puede_prometer`` y
    el ``texto_libre``, que son del usuario. ``warnings`` son las de la
    lectura mas las del armado (sin oferta principal, hotlink ambiguo).
    """

    fields: dict[str, Any]
    warnings: tuple[str, ...]


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def price_text(value: Any) -> str | None:
    """El precio como texto decimal de product-card-v1 (``"10"``, ``"1397.5"``).

    La documentacion lo da como ``double``. Se convierte por ``repr`` (el
    decimal mas corto que vuelve al mismo double) y se rechaza lo que no sea un
    numero finito mayor que 0 con hasta dos decimales, un booleano incluido. Un
    texto se acepta solo con punto decimal y hasta dos decimales.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = Decimal(repr(value))
        except InvalidOperation:
            return None
    elif isinstance(value, str) and _DECIMAL_TEXT.fullmatch(value.strip()):
        number = Decimal(value.strip())
    else:
        return None
    if not number.is_finite() or number <= 0:
        return None
    exponent = number.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > _MAX_DECIMALS:
        # Ruido binario (0.30000000000000004) o un precio que no es de lista.
        return None
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")


def hotlink_from_offer_link(link: Any, offer_code: str) -> str | None:
    """El hotlink de ``https://pay.hotmart.com/<hotlink>?off=<codigo>``.

    Solo si el host es el del checkout, la ruta es un solo segmento con la
    forma del hotlink y el ``off=`` es el codigo de esa misma oferta.
    """
    if not isinstance(link, str) or not link:
        return None
    try:
        parts = urlsplit(link.strip())
    except ValueError:
        return None
    if parts.scheme != "https" or (parts.hostname or "").lower() not in _CHECKOUT_HOSTS:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 1 or _HOTLINK.fullmatch(segments[0]) is None:
        return None
    offs = parse_qs(parts.query).get("off", [])
    if offs != [offer_code]:
        return None
    return segments[0]


def _page(body: Any, operation: str) -> tuple[list[Any], str | None]:
    if not isinstance(body, dict):
        raise HotmartTransientError(f"{operation}_invalid_shape")
    items = body.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise HotmartTransientError(f"{operation}_invalid_shape")
    page_info = body.get("page_info")
    token: str | None = None
    if isinstance(page_info, dict):
        raw = page_info.get("next_page_token")
        if isinstance(raw, str) and raw:
            if len(raw) > _PAGE_TOKEN_MAX:
                raise HotmartTransientError(f"{operation}_invalid_page_token")
            token = raw
    elif page_info is not None:
        raise HotmartTransientError(f"{operation}_invalid_shape")
    return items, token


def parse_product_item(item: Any) -> HotmartProduct | None:
    """Un item de la lista de productos, o ``None`` si no se puede usar."""
    if not isinstance(item, dict):
        return None
    product_id = _positive_int(item.get("id"))
    name = item.get("name")
    ucode = item.get("ucode")
    if product_id is None or not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(ucode, str) or _UCODE.fullmatch(ucode) is None:
        return None
    warranty = item.get("warranty_period")
    return HotmartProduct(
        product_id=product_id,
        name=name.strip(),
        ucode=ucode,
        status=_str_or_none(item.get("status")),
        format=_str_or_none(item.get("format")),
        is_subscription=_bool_or_none(item.get("is_subscription")),
        warranty_days=(
            warranty
            if isinstance(warranty, int) and not isinstance(warranty, bool) and warranty >= 0
            else None
        ),
    )


def parse_offer_item(item: Any) -> tuple[HotmartOffer | None, str | None]:
    """Un item de la lista de ofertas: la oferta, o ``None`` y el motivo.

    Con la oferta tambien puede venir una advertencia (``is_main_offer`` que
    no es booleano: se lee como ``False``).
    """
    if not isinstance(item, dict):
        return None, "offer_skipped:not_an_object"
    code = item.get("code")
    if not isinstance(code, str) or _OFFER_CODE.fullmatch(code) is None:
        return None, "offer_skipped:code_invalid"
    price = item.get("price")
    if not isinstance(price, dict):
        return None, f"offer_skipped:{code}:price_missing"
    value = price_text(price.get("value"))
    if value is None:
        return None, f"offer_skipped:{code}:price_invalid"
    currency = price.get("currency_code")
    if not isinstance(currency, str) or _CURRENCY.fullmatch(currency.strip().upper()) is None:
        return None, f"offer_skipped:{code}:currency_invalid"
    name = item.get("name")
    main = item.get("is_main_offer")
    warning = None
    if main is not None and not isinstance(main, bool):
        warning = f"offer:{code}:is_main_offer_invalid"
    return (
        HotmartOffer(
            code=code,
            name=name.strip() if isinstance(name, str) and name.strip() else None,
            price=value,
            currency=currency.strip().upper(),
            payment_mode=_str_or_none(item.get("payment_mode")),
            is_main_offer=item.get("is_main_offer") is True,
            hotlink=hotlink_from_offer_link(
                item.get("direct_offer_link_for_creator"), code
            ),
        ),
        warning,
    )


def minimo_from_reading(reading: HotmartProductReading) -> MinimoDraft:
    """Vuelca la lectura a los campos del minimo que salen de Hotmart.

    ``nombre``, ``hotmart.producto_id``, ``hotmart.hotlink`` y ``ofertas[]``
    con ``codigo``, ``precio``, ``moneda`` y ``por_defecto``. La oferta por
    defecto es la principal de Hotmart (``is_main_offer``); si no hay
    exactamente una, ninguna queda marcada y se advierte: lo decide el
    usuario, porque product-card-v1 exige exactamente una.
    """
    warnings = list(reading.warnings)
    offers = reading.offers
    mains = [offer for offer in offers if offer.is_main_offer]
    if not offers:
        warnings.append("no_offers")
    elif len(mains) != 1:
        warnings.append("main_offer_count:" + str(len(mains)))
    hotlinks = {offer.hotlink for offer in offers if offer.hotlink is not None}
    hotlink: str | None = None
    if len(hotlinks) == 1:
        hotlink = next(iter(hotlinks))
    elif len(hotlinks) > 1:
        warnings.append("hotlink_ambiguous")
    elif offers:
        warnings.append("hotlink_missing")
    if hotlink is not None and any(offer.hotlink is None for offer in offers):
        warnings.append("hotlink_missing_in_some_offers")
    product = reading.product
    fields: dict[str, Any] = {
        "nombre": product.name,
        "hotmart": {"producto_id": product.product_id, "hotlink": hotlink},
        "ofertas": [
            {
                "codigo": offer.code,
                "precio": offer.price,
                "moneda": offer.currency,
                "por_defecto": len(mains) == 1 and offer.is_main_offer,
            }
            for offer in offers
        ],
    }
    if len(product.name) > _NAME_MAX:
        warnings.append("name_too_long")
    for offer in offers:
        if offer.payment_mode is not None and offer.payment_mode not in _SINGLE_PAYMENT_MODES:
            # En una suscripcion o en cuotas el precio puede ser por periodo.
            warnings.append(f"payment_mode:{offer.code}:{offer.payment_mode}")
    if product.status is not None and product.status != "ACTIVE":
        warnings.append("product_status:" + product.status)
    return MinimoDraft(fields=fields, warnings=tuple(warnings))


class HotmartCatalogReader:
    """Lee productos y ofertas con un ``HotmartApiClient`` ya armado."""

    def __init__(
        self,
        client: HotmartApiClient,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        if page_size <= 0 or max_pages <= 0:
            raise ValueError("hotmart_catalog_limits_invalid")
        self._client = client
        self._page_size = page_size
        self._max_pages = max_pages

    def __repr__(self) -> str:
        return "HotmartCatalogReader(<redacted>)"

    async def _pages(
        self,
        path: str,
        params: dict[str, str],
        operation: str,
        *,
        not_found_is_empty: bool = False,
    ) -> list[Any]:
        collected: list[Any] = []
        seen: set[str] = set()
        token: str | None = None
        for _ in range(self._max_pages):
            query = dict(params)
            query["max_results"] = str(self._page_size)
            if token is not None:
                query["page_token"] = token
            # _api pide el token, invalida el cacheado ante un 401 y levanta
            # transitorio en 429/5xx (hotmart_api.py). Los errores no llevan
            # la URL ni el cuerpo.
            response = await self._client._api(
                "GET", path, params=query, operation=operation
            )
            if response.status_code == 404 and not_found_is_empty and token is None:
                # D3: la documentacion dice que un valor que no existe responde
                # 404 (sandbox). Solo para la busqueda por ID y en la primera
                # pagina: un 404 en la segunda es un error, no una lista cortada.
                return collected
            if response.status_code != 200:
                raise HotmartPermanentError(
                    f"{operation}_http_{response.status_code}",
                    status_code=response.status_code,
                )
            try:
                body = response.json()
            except ValueError:
                raise HotmartTransientError(f"{operation}_invalid_json") from None
            items, token = _page(body, operation)
            collected.extend(items)
            if token is None:
                return collected
            if token in seen:
                raise HotmartTransientError(f"{operation}_pagination_loop")
            seen.add(token)
        raise HotmartPermanentError(f"{operation}_too_many_pages")

    async def list_products(self, *, status: str | None = None) -> tuple[HotmartProduct, ...]:
        params: dict[str, str] = {}
        if status is not None:
            if not re.fullmatch(r"[A-Z_]{1,40}", status):
                raise HotmartPermanentError("hotmart_products_status_invalid")
            params["status"] = status
        items = await self._pages(PRODUCTS_PATH, params, "hotmart_products_list")
        products = [parse_product_item(item) for item in items]
        return tuple(product for product in products if product is not None)

    async def find_product(self, product_id: int) -> HotmartProduct | None:
        """El producto por su ID de 7 digitos, o ``None`` si la cuenta no lo tiene.

        Se filtra con ``id`` y ademas se exige el ID igual en la respuesta: un
        filtro que Hotmart ignore no devuelve otro producto.
        """
        if _positive_int(product_id) is None:
            raise HotmartPermanentError("hotmart_product_id_invalid")
        items = await self._pages(
            PRODUCTS_PATH,
            {"id": str(product_id)},
            "hotmart_products_find",
            not_found_is_empty=True,
        )
        matches = [
            product
            for product in (parse_product_item(item) for item in items)
            if product is not None and product.product_id == product_id
        ]
        if len(matches) > 1:
            raise HotmartTransientError("hotmart_products_find_ambiguous")
        return matches[0] if matches else None

    async def list_offers(self, ucode: str) -> tuple[tuple[HotmartOffer, ...], tuple[str, ...]]:
        """Todas las ofertas del producto y las advertencias de las descartadas."""
        if not isinstance(ucode, str) or _UCODE.fullmatch(ucode) is None:
            raise HotmartPermanentError("hotmart_product_ucode_invalid")
        items = await self._pages(
            OFFERS_PATH.format(ucode=ucode), {}, "hotmart_offers_list"
        )
        offers: list[HotmartOffer] = []
        warnings: list[str] = []
        codes: set[str] = set()
        for item in items:
            offer, warning = parse_offer_item(item)
            if offer is None:
                warnings.append(warning or "offer_skipped")
                continue
            if warning is not None:
                warnings.append(warning)
            if offer.code in codes:
                warnings.append(f"offer_duplicated:{offer.code}")
                continue
            codes.add(offer.code)
            offers.append(offer)
        return tuple(offers), tuple(warnings)

    async def read_product(self, product_id: int) -> HotmartProductReading | None:
        """El producto con sus ofertas, o ``None`` si la cuenta no lo tiene."""
        product = await self.find_product(product_id)
        if product is None:
            return None
        offers, warnings = await self.list_offers(product.ucode)
        return HotmartProductReading(product=product, offers=offers, warnings=warnings)


def build_catalog_reader(
    env: Mapping[str, str],
    *,
    credentials_path: Path | str | None,
    transport: httpx.AsyncBaseTransport | None = None,
    api_base_url: str | None = None,
) -> HotmartCatalogReader | None:
    """El lector, o ``None`` con el flag apagado.

    Apagado no abre el archivo de credenciales ni arma un cliente. Prendido y
    sin ruta, falla con un codigo corto (como el worker del cupon).
    """
    if not catalog_read_enabled(env):
        return None
    if credentials_path is None or not str(credentials_path).strip():
        raise ValueError("hotmart_catalog_credentials_path_missing")
    credentials = load_hotmart_api_credentials(credentials_path)
    kwargs: dict[str, Any] = {"credentials": credentials, "transport": transport}
    if api_base_url is not None:
        kwargs["api_base_url"] = api_base_url
    return HotmartCatalogReader(HotmartApiClient(**kwargs))
