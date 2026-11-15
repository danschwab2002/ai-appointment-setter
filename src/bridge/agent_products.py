"""El producto que suma el Automation Expert: validar el minimo, advertir el esqueleto (product-card-v1, E-569, MP-07).

Contrato: ``docs/contracts/product-card-v1.md``. Plan: «Multiproducto: el plan
de implementacion» (vault del OS), MP-07, con D1 de Dan del 2026-11-13: el
conocimiento de un producto nuevo **no es una ficha con campos**; es un minimo
obligatorio que el bridge valida (lo que el codigo lee para rutear, cobrar,
cortar casos o la guarda de precio), un esqueleto recomendado que solo advierte
y un texto libre con tope de bytes.

Suma A-0 (decision de Dan del 2026-11-14, «el foco lo decide el sistema»): el
minimo lleva ``alias``, las palabras o frases con que la persona nombra el
producto. Son los que va a usar la preseleccion del foco (MP-12, MP-13); aca se
validan: lista no vacia de textos cortos, sin duplicados normalizados y sin
chocar con el nombre, la clave o los alias de otro producto de la instancia.

Este modulo es puro (sin red ni base):

* ``parse_product_card``: lo que manda el agente, contra la instancia. Junta
  **todos** los bloqueos de una vez (para que el AE corrija todo junto) y las
  advertencias del esqueleto; con cero bloqueos devuelve la ficha normalizada
  (``minimo`` + ``texto_libre``), que es lo que guarda la fila.
* ``ProductContext``: lo que ya usa la instancia (el principal y
  ``[[productos]]`` del manifiesto, mas los productos vigentes de la base que
  devuelve ``list_agent_products_v1`` en ``taken``).
* ``render_card``: el render del contrato §6 (cabecera, ``no_se_puede_prometer``
  y el texto libre tal cual), para medir el tope de 6.000 bytes. El turno lo
  usa recien en MP-11.

Con ``AGENT_PRODUCTS_ENABLED`` apagado el bridge lo importa (lo importa
``agent_product_api`` al arrancar) pero no lo llama: solo lo usan las rutas
del AE, que con el flag apagado no existen. Ninguna funcion de aca corre en
el turno.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from . import price_guard
from .campaign_inbound_entry import normalize_entry_text
from .commercial_knowledge import _SECRET_SHAPES
from .instance_manifest import InstanceManifest

__all__ = [
    "CONTRACT",
    "RULES_VERSION",
    "ParsedCard",
    "ProductCardInvalid",
    "ProductContext",
    "TakenProduct",
    "normalize_alias",
    "parse_product_card",
    "render_card",
]

CONTRACT = "product-card-v1"
RULES_VERSION = 1

MAX_KEY_CHARS = 40
MAX_NAME_CHARS = 80
MAX_ALIASES = 12
MIN_ALIAS_CHARS = 3
MAX_ALIAS_CHARS = 60
MAX_OFFERS = 20
MAX_OTHER_AMOUNTS = 12
MAX_WHAT_IS_CHARS = 80
MAX_PROMISES = 20
MAX_PROMISE_CHARS = 200
MAX_RELATIONS = 20
MAX_CONDITION_CHARS = 200
MAX_FREE_TEXT_BYTES = 5_500
MAX_RENDER_BYTES = 6_000

RESERVED_KEYS = frozenset({"principal", "todos", "ninguno"})
# En las relaciones, «principal» nombra al principal de la instancia: sin
# [[productos]] el principal no tiene otra clave (E-563, una sola capa).
PRINCIPAL_REF = "principal"

_KEY = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_HOTLINK = re.compile(r"[A-Za-z0-9]{4,32}")
_OFFER_CODE = re.compile(r"[A-Za-z0-9]{4,32}")
_PRICE = re.compile(r"[0-9]{1,9}(?:\.[0-9]{1,2})?")
# Las monedas que el extractor de la guarda de precio sabe leer: una moneda que
# no lee dejaria montos del texto libre sin comparar (D3 del PR).
CURRENCIES = frozenset(price_guard._ISO_CODES)  # noqa: SLF001 - la misma lista, a proposito

_TOP_KEYS = frozenset({"clave", "nombre", "alias", "hotmart", "ofertas", "no_se_puede_prometer", "relaciones",
                       "texto_libre"})
_HOTMART_KEYS = frozenset({"producto_id", "hotlink"})
_OFFER_KEYS = frozenset({"codigo", "precio", "moneda", "por_defecto", "otros_montos"})
_OTHER_AMOUNT_KEYS = frozenset({"monto", "moneda", "que_es"})
_RELATION_KEYS = ("incluye_productos", "no_se_vende_si_tiene", "venta_cruzada")
_CROSS_SELL_KEYS = frozenset({"producto", "condicion"})

# El esqueleto recomendado (contrato §3): la seccion, como se compara y su
# nombre en la advertencia.
SKELETON: tuple[tuple[str, str], ...] = (
    ("Qué es", "que_es"),
    ("Para quién es", "para_quien_es"),
    ("Qué incluye", "que_incluye"),
    ("Precio y cómo se paga", "precio_y_como_se_paga"),
    ("Garantía", "garantia"),
    ("Preguntas frecuentes", "preguntas_frecuentes"),
    ("Qué hacer si ya es alumna", "que_hacer_si_ya_es_alumna"),
    ("Tono", "tono"),
)

# Lo que no puede ir en el texto libre (contrato §4).
# Un mail: la parte local empieza despues de un espacio o un signo, nunca de una
# barra (``instagram.com/@ana.lucia`` es un usuario, no un mail).
_EMAIL = re.compile(r"(?<![/\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")
# Un telefono: 9 o mas cifras con separadores de telefono. Un monto con miles
# (1.397.000) no es un telefono: sus grupos son de 3 con punto o coma.
_PHONE = re.compile(r"(?<![\w.,])\+?\d[\d\s().-]{7,}\d(?![\w])")
_THOUSANDS = re.compile(r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?")
# Fechas, horas y rangos de anios: se sacan antes de buscar un telefono
# («2026-11-19 20:00», «19.11.2026», «2024-2025-2026» no son telefonos).
_DATES_AND_TIMES = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\b\d{1,2}:\d{2}\b"
    r"|\b(?:19|20)\d{2}(?:\s*[-/]\s*(?:19|20)\d{2})+\b")
_PAYMENT_URL = re.compile(
    r"pay\.hotmart\.com|hotmart\.com/\S*checkout|[?&]off=|go\.hotmart\.com", re.IGNORECASE)


class ProductCardInvalid(ValueError):
    """Lo que mando el agente no es una ficha (no es un objeto, o trae claves que no existen).

    ``reason`` lleva nombres de campos, nunca valores.
    """

    def __init__(self, code: str, reason: Mapping[str, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.reason = dict(reason)


@dataclass(frozen=True)
class TakenProduct:
    """Un producto que ya existe en la instancia: lo que otro no puede repetir."""

    key: str | None
    name: str | None
    aliases: tuple[str, ...] = ()
    hotmart_product_id: int | None = None
    offer_codes: tuple[str, ...] = ()
    includes: tuple[str, ...] = ()
    origin: str = "base"  # principal | manifiesto | base


@dataclass(frozen=True)
class ProductContext:
    """La instancia contra la que se valida una ficha."""

    products: tuple[TakenProduct, ...] = ()
    # Claves que un producto nuevo no puede tomar aunque no sean de un producto
    # (el funnel_ref del principal y de los de [[productos]]: el de un producto
    # nuevo se deriva de su clave, MP-08).
    reserved_keys: frozenset[str] = frozenset()

    @classmethod
    def from_manifest(cls, manifest: InstanceManifest | None,
                      stored: Iterable[Mapping[str, Any]] = ()) -> ProductContext:
        """El principal y [[productos]] del manifiesto, mas ``taken`` de ``list_agent_products_v1``."""
        products: list[TakenProduct] = []
        reserved: set[str] = set()
        if manifest is not None:
            principal_key = manifest.products[0].key if manifest.products else PRINCIPAL_REF
            products.append(TakenProduct(
                key=principal_key, name=manifest.product_name,
                hotmart_product_id=manifest.hotmart_product_id,
                offer_codes=tuple(offer.code for offer in manifest.offers), origin="principal"))
            reserved.add(manifest.funnel_ref)
            for product in manifest.products[1:]:
                commerce = product.commerce
                products.append(TakenProduct(
                    key=product.key,
                    name=commerce.product_name if commerce is not None else None,
                    hotmart_product_id=commerce.hotmart_product_id if commerce is not None else None,
                    offer_codes=tuple(offer.code for offer in commerce.offers) if commerce is not None else (),
                    origin="manifiesto"))
                if commerce is not None:
                    reserved.add(commerce.funnel_ref)
        for row in stored:
            products.append(_taken_from_row(row))
        return cls(products=tuple(products), reserved_keys=frozenset(reserved))


def _taken_from_row(row: Mapping[str, Any]) -> TakenProduct:
    """Una fila de ``taken`` (list_agent_products_v1). Lo que no tiene la forma se ignora campo por campo."""
    def strings(value: Any) -> tuple[str, ...]:
        return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()

    product_id = row.get("hotmart_product_id")
    return TakenProduct(
        key=row.get("product_key") if isinstance(row.get("product_key"), str) else None,
        name=row.get("nombre") if isinstance(row.get("nombre"), str) else None,
        aliases=strings(row.get("alias")),
        hotmart_product_id=product_id if type(product_id) is int else None,
        offer_codes=strings(row.get("offer_codes")),
        includes=strings(row.get("incluye_productos")),
        origin="base")


@dataclass(frozen=True)
class ParsedCard:
    """El resultado de validar una ficha.

    ``card`` es la ficha normalizada (``minimo`` + ``texto_libre``) solo si no
    hay bloqueos. ``blocks`` y ``warnings`` son listas de
    ``{"code", "field"?, "detail"?}``; ``detail`` lleva solo lo que el contrato
    pone despues de los dos puntos (``free_text_amount_not_declared:149 USD``,
    ``skeleton_section_missing:garantia``), nunca texto del usuario.
    """

    blocks: tuple[dict[str, str], ...]
    warnings: tuple[dict[str, str], ...]
    minimum: dict[str, Any] | None = None
    free_text: str | None = None
    render: str | None = None
    render_bytes: int | None = None
    render_sha256: str | None = None

    @property
    def ok(self) -> bool:
        return not self.blocks


def normalize_alias(text: str) -> str:
    """Como se comparan nombres, claves y alias: sin mayusculas, acentos, guiones ni signos.

    Es la misma normalizacion que la palabra clave de una campana
    (``normalize_entry_text``), la que va a usar la preseleccion del foco.
    """
    return normalize_entry_text(text)


@dataclass
class _Findings:
    blocks: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)

    def block(self, code: str, field_name: str | None = None, detail: str | None = None) -> None:
        finding = {"code": code}
        if field_name is not None:
            finding["field"] = field_name
        if detail is not None:
            finding["detail"] = detail
        if finding not in self.blocks:
            self.blocks.append(finding)

    def warn(self, code: str, field_name: str | None = None, detail: str | None = None) -> None:
        finding = {"code": code}
        if field_name is not None:
            finding["field"] = field_name
        if detail is not None:
            finding["detail"] = detail
        self.warnings.append(finding)


def _clean_line(value: Any, *, limit: int) -> str | None:
    """Un texto de una linea, sin caracteres de control, recortado; None si no sirve."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > limit or "\n" in text:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        return None
    return text


def _decimal_text(value: Any) -> str | None:
    """Un decimal mayor que 0, escrito como texto (``"199"``, ``"1397.00"``), normalizado (``"1397"``)."""
    if not isinstance(value, str) or not _PRICE.fullmatch(value.strip()):
        return None
    try:
        number = price_guard.normalize_decimal(Decimal(value.strip()))
    except InvalidOperation:
        return None
    if number is None:
        return None
    return format(number, "f")


def _currency(value: Any) -> str | None:
    return value if isinstance(value, str) and value in CURRENCIES else None


def _check_keys(raw: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ProductCardInvalid("product_card_invalid", {"field": where, "problem": "unknown_keys",
                                                          "keys": unknown[:10]})


def _offers(raw: Any, found: _Findings) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        found.block("offers_missing", "ofertas")
        return []
    if len(raw) > MAX_OFFERS:
        found.block("offers_missing", "ofertas", f"mas_de_{MAX_OFFERS}")
        return []
    offers: list[dict[str, Any]] = []
    codes: set[str] = set()
    defaults = 0
    for index, item in enumerate(raw):
        where = f"ofertas[{index}]"
        if not isinstance(item, Mapping):
            found.block("offer_code_invalid", where)
            continue
        _check_keys(item, _OFFER_KEYS, where)
        code = item.get("codigo")
        if not isinstance(code, str) or not _OFFER_CODE.fullmatch(code):
            found.block("offer_code_invalid", f"{where}.codigo")
            code = None
        elif code in codes:
            found.block("offer_code_taken", f"{where}.codigo", "repetida_en_la_ficha")
        else:
            codes.add(code)
        price = _decimal_text(item.get("precio"))
        if price is None:
            found.block("offer_price_invalid", f"{where}.precio")
        currency = _currency(item.get("moneda"))
        if currency is None:
            found.block("offer_currency_invalid", f"{where}.moneda")
        default = item.get("por_defecto")
        if type(default) is not bool:
            found.block("default_offer_count", f"{where}.por_defecto", "no_es_booleano")
            default = False
        defaults += int(default)
        others: list[dict[str, str]] = []
        raw_others = item.get("otros_montos", [])
        if not isinstance(raw_others, list) or len(raw_others) > MAX_OTHER_AMOUNTS:
            found.block("other_amount_invalid", f"{where}.otros_montos")
            raw_others = []
        for other_index, other in enumerate(raw_others):
            other_where = f"{where}.otros_montos[{other_index}]"
            if not isinstance(other, Mapping):
                found.block("other_amount_invalid", other_where)
                continue
            _check_keys(other, _OTHER_AMOUNT_KEYS, other_where)
            amount = _decimal_text(other.get("monto"))
            other_currency = _currency(other.get("moneda"))
            what = _clean_line(other.get("que_es"), limit=MAX_WHAT_IS_CHARS)
            if amount is None or other_currency is None or what is None or _personal(what):
                found.block("other_amount_invalid", other_where)
                continue
            others.append({"monto": amount, "moneda": other_currency, "que_es": what})
        if code is not None and price is not None and currency is not None:
            offers.append({"codigo": code, "precio": price, "moneda": currency, "por_defecto": default,
                           "otros_montos": others})
    if defaults != 1:
        found.block("default_offer_count", "ofertas", str(defaults))
    return offers


def _promises(raw: Any, found: _Findings) -> list[str]:
    if not isinstance(raw, list) or not raw:
        found.block("promises_missing", "no_se_puede_prometer")
        return []
    if len(raw) > MAX_PROMISES:
        found.block("promise_invalid", "no_se_puede_prometer", f"mas_de_{MAX_PROMISES}")
        return []
    promises: list[str] = []
    for index, item in enumerate(raw):
        text = _clean_line(item, limit=MAX_PROMISE_CHARS)
        if text is None or _personal(text):
            found.block("promise_invalid", f"no_se_puede_prometer[{index}]")
            continue
        promises.append(text)
    return promises


def _aliases(raw: Any, found: _Findings) -> list[str]:
    """Contrato §2.1 (E-570, 0.2.0): de 1 a 12, de 3 a 60 caracteres, con una letra, sin duplicados."""
    if raw is None or raw == []:
        found.block("aliases_missing", "alias")
        return []
    if not isinstance(raw, list):
        found.block("alias_invalid", "alias", "no_es_lista")
        return []
    if len(raw) > MAX_ALIASES:
        found.block("alias_invalid", "alias", f"mas_de_{MAX_ALIASES}")
        return []
    aliases: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"alias[{index}]"
        raw_text = item.strip() if isinstance(item, str) else None
        text = _clean_line(item, limit=MAX_ALIAS_CHARS)
        normalized = normalize_alias(text) if text is not None else ""
        if (text is None or raw_text is None or len(raw_text) < MIN_ALIAS_CHARS or len(normalized) < MIN_ALIAS_CHARS
                or not any(character.isalpha() for character in normalized) or normalized in RESERVED_KEYS):
            found.block("alias_invalid", where)
            continue
        # Un alias pasa por el filtro de datos personales y secretos del texto libre, con sus codigos (§2.1).
        if any(shape.search(text) for shape in _SECRET_SHAPES):
            found.block("free_text_secret", where)
            continue
        if _personal(text):
            found.block("free_text_personal_data", where)
            continue
        if normalized in seen:
            found.block("alias_duplicate", where)
            continue
        seen.add(normalized)
        aliases.append(text)
    return aliases


def _relations(raw: Any, found: _Findings) -> dict[str, list[Any]]:
    relations: dict[str, list[Any]] = {key: [] for key in _RELATION_KEYS}
    if raw is None:
        return relations
    if not isinstance(raw, Mapping):
        raise ProductCardInvalid("product_card_invalid", {"field": "relaciones", "problem": "not_an_object"})
    _check_keys(raw, frozenset(_RELATION_KEYS), "relaciones")
    for name in ("incluye_productos", "no_se_vende_si_tiene"):
        items = raw.get(name, [])
        if not isinstance(items, list) or len(items) > MAX_RELATIONS:
            found.block("relation_unknown_product", f"relaciones.{name}")
            continue
        for index, item in enumerate(items):
            if not isinstance(item, str) or not _KEY.fullmatch(item):
                found.block("relation_unknown_product", f"relaciones.{name}[{index}]")
                continue
            if item not in relations[name]:
                relations[name].append(item)
    items = raw.get("venta_cruzada", [])
    if not isinstance(items, list) or len(items) > MAX_RELATIONS:
        found.block("relation_unknown_product", "relaciones.venta_cruzada")
        return relations
    for index, item in enumerate(items):
        where = f"relaciones.venta_cruzada[{index}]"
        if not isinstance(item, Mapping):
            found.block("relation_unknown_product", where)
            continue
        _check_keys(item, _CROSS_SELL_KEYS, where)
        product = item.get("producto")
        if not isinstance(product, str) or not _KEY.fullmatch(product):
            found.block("relation_unknown_product", f"{where}.producto")
            continue
        entry: dict[str, str] = {"producto": product}
        if "condicion" in item:
            condition = _clean_line(item.get("condicion"), limit=MAX_CONDITION_CHARS)
            if condition is None or _personal(condition):
                found.block("relation_unknown_product", f"{where}.condicion", "condicion_invalida")
                continue
            entry["condicion"] = condition
        relations["venta_cruzada"].append(entry)
    return relations


def _looks_like_phone(text: str) -> bool:
    for match in _PHONE.finditer(_DATES_AND_TIMES.sub(" ", text)):
        token = match.group(0).strip()
        if _THOUSANDS.fullmatch(token):
            continue
        if sum(character.isdigit() for character in token) >= 9:
            return True
    return False


def _personal(text: str) -> bool:
    """Un mail o un telefono. Un monto con miles, una fecha o una hora no lo son (E-569, revision)."""
    return bool(_EMAIL.search(text)) or _looks_like_phone(text)


def _free_text(raw: Any, offers: Sequence[Mapping[str, Any]], found: _Findings) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        found.block("free_text_empty", "texto_libre")
        return None
    text = raw.strip()
    if len(text.encode("utf-8")) > MAX_FREE_TEXT_BYTES:
        found.block("free_text_too_long", "texto_libre", str(len(text.encode("utf-8"))))
        return None
    if any((ord(character) < 32 and character not in "\n\t") or ord(character) == 127 for character in text):
        found.block("free_text_control_characters", "texto_libre")
        return None
    if _personal(text):
        found.block("free_text_personal_data", "texto_libre")
    if any(shape.search(text) for shape in _SECRET_SHAPES):
        found.block("free_text_secret", "texto_libre")
    if _PAYMENT_URL.search(text):
        found.block("free_text_payment_url", "texto_libre")
    allowed: set[price_guard.Amount] = set()
    for offer in offers:
        amount = price_guard.amount_of(offer["precio"], offer["moneda"])
        if amount is not None:
            allowed.add(amount)
        for other in offer["otros_montos"]:
            other_amount = price_guard.amount_of(other["monto"], other["moneda"])
            if other_amount is not None:
                allowed.add(other_amount)
    for blocked in price_guard.PriceGuard.blocked(text, frozenset(allowed)):
        found.block("free_text_amount_not_declared", "texto_libre",
                    f"{format(blocked.value, 'f')} {blocked.currency}")
    headings = {normalize_alias(line.lstrip()[3:]) for line in text.splitlines()
                if line.lstrip().startswith("## ")}
    for title, slug in SKELETON:
        if normalize_alias(title) not in headings:
            found.warn("skeleton_section_missing", "texto_libre", slug)
    return text


def render_card(minimum: Mapping[str, Any], free_text: str) -> str:
    """El render del contrato §6: cabecera, lo que no se puede prometer y el texto libre tal cual.

    No lleva ``hotmart``, los codigos de oferta, ``alias``, ``relaciones`` (la
    venta cruzada queda apagada, §5) ni las advertencias. Los montos van con
    los separadores de Mexico (``MXN 1.397``), como el conocimiento firmado.
    """
    lines = [f"# Producto: {minimum['nombre']}", ""]
    for offer in minimum["ofertas"]:
        price = price_guard.format_decimal(Decimal(offer["precio"]))
        kind = "Oferta por defecto" if offer["por_defecto"] else "Otra oferta"
        lines.append(f"- {kind}: {offer['moneda']} {price} (precio de lista).")
        for other in offer["otros_montos"]:
            amount = price_guard.format_decimal(Decimal(other["monto"]))
            lines.append(f"  - Otro monto: {other['moneda']} {amount} ({other['que_es']}).")
    lines.extend(["", "No se puede prometer:", ""])
    lines.extend(f"- {item}" for item in minimum["no_se_puede_prometer"])
    lines.extend(["", free_text])
    return "\n".join(lines) + "\n"


def _instance_checks(minimum: Mapping[str, Any], context: ProductContext, found: _Findings) -> None:
    """Lo que choca con otro producto de la instancia. Los de la misma clave son versiones del mismo producto."""
    key = minimum["clave"]
    others = [product for product in context.products if product.key != key or product.origin != "base"]
    if key in RESERVED_KEYS or key in context.reserved_keys or any(
            product.key == key for product in others):
        found.block("product_key_taken", "clave")
    product_id = minimum["hotmart"]["producto_id"]
    if any(product.hotmart_product_id == product_id for product in others):
        found.block("hotmart_product_taken", "hotmart.producto_id")
    taken_codes = {code for product in others for code in product.offer_codes}
    for index, offer in enumerate(minimum["ofertas"]):
        if offer["codigo"] in taken_codes:
            found.block("offer_code_taken", f"ofertas[{index}].codigo")
    # Los nombres de cada producto: su nombre, su clave (sin guiones) y sus alias.
    taken_names: set[str] = set()
    for product in others:
        for text in (product.name, product.key, *product.aliases):
            if isinstance(text, str) and normalize_alias(text):
                taken_names.add(normalize_alias(text))
    if normalize_alias(minimum["nombre"]) in taken_names:
        found.block("alias_taken", "nombre")
    # La clave tambien es un alias implicito (§2.1): «el-protocolo-black» choca con el alias «el protocolo black».
    if normalize_alias(key) in taken_names and not any(finding["code"] == "product_key_taken"
                                                        for finding in found.blocks):
        found.block("alias_taken", "clave")
    for index, alias in enumerate(minimum["alias"]):
        if normalize_alias(alias) in taken_names:
            found.block("alias_taken", f"alias[{index}]")
    known = {product.key for product in others if product.key is not None} | {PRINCIPAL_REF}
    relations = minimum["relaciones"]
    for name in ("incluye_productos", "no_se_vende_si_tiene"):
        for index, item in enumerate(relations[name]):
            if item == key or item not in known:
                found.block("relation_unknown_product", f"relaciones.{name}[{index}]")
    for index, item in enumerate(relations["venta_cruzada"]):
        if item["producto"] == key or item["producto"] not in known:
            found.block("relation_unknown_product", f"relaciones.venta_cruzada[{index}].producto")
    # Sin ciclos en incluye_productos: desde lo que este incluye, nada vuelve a el.
    includes: dict[str, set[str]] = {}
    for product in others:
        if product.key is not None:
            includes.setdefault(product.key, set()).update(product.includes)
    pending = list(relations["incluye_productos"])
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current == key:
            found.block("relation_cycle", "relaciones.incluye_productos")
            break
        if current in seen:
            continue
        seen.add(current)
        pending.extend(includes.get(current, ()))


def parse_product_card(raw: Any, *, context: ProductContext | None = None) -> ParsedCard:
    """Valida una ficha de ``product-card-v1`` contra la instancia.

    Levanta ``ProductCardInvalid`` solo si lo que llega no es una ficha (no es
    un objeto o trae claves desconocidas). Todo lo demas vuelve como bloqueos
    con los codigos del contrato §10 (los alias, §2.1, desde la 0.2.0 de
    E-570) mas ``free_text_control_characters``.
    """
    if not isinstance(raw, Mapping):
        raise ProductCardInvalid("product_card_invalid", {"field": None, "problem": "not_an_object"})
    _check_keys(raw, _TOP_KEYS, "producto")
    found = _Findings()

    key = raw.get("clave")
    if not isinstance(key, str) or len(key) > MAX_KEY_CHARS or not _KEY.fullmatch(key):
        found.block("product_key_invalid", "clave")
        key = None
    name = _clean_line(raw.get("nombre"), limit=MAX_NAME_CHARS)
    if name is None or _personal(name):
        found.block("product_name_invalid", "nombre")
        name = None
    aliases = _aliases(raw.get("alias"), found)

    hotmart = raw.get("hotmart")
    product_id: int | None = None
    hotlink: str | None = None
    if not isinstance(hotmart, Mapping):
        found.block("hotmart_product_invalid", "hotmart")
    else:
        _check_keys(hotmart, _HOTMART_KEYS, "hotmart")
        candidate = hotmart.get("producto_id")
        if type(candidate) is int and 0 < candidate < 10**12:
            product_id = candidate
        else:
            found.block("hotmart_product_invalid", "hotmart.producto_id")
        link = hotmart.get("hotlink")
        if isinstance(link, str) and _HOTLINK.fullmatch(link):
            hotlink = link
        else:
            found.block("hotlink_invalid", "hotmart.hotlink")

    offers = _offers(raw.get("ofertas"), found)
    promises = _promises(raw.get("no_se_puede_prometer"), found)
    relations = _relations(raw.get("relaciones"), found)
    free_text = _free_text(raw.get("texto_libre"), offers, found)

    complete = (key is not None and name is not None and aliases and product_id is not None
                and hotlink is not None and offers and promises and free_text is not None)
    minimum: dict[str, Any] | None = None
    if complete:
        minimum = {
            "clave": key,
            "nombre": name,
            "alias": aliases,
            "hotmart": {"producto_id": product_id, "hotlink": hotlink},
            "ofertas": offers,
            "no_se_puede_prometer": promises,
            "relaciones": relations,
        }
        _instance_checks(minimum, context or ProductContext(), found)
    render = render_bytes = render_sha = None
    if minimum is not None and free_text is not None:
        render = render_card(minimum, free_text)
        render_bytes = len(render.encode("utf-8"))
        render_sha = hashlib.sha256(render.encode("utf-8")).hexdigest()
        if render_bytes > MAX_RENDER_BYTES:
            found.block("render_too_long", "texto_libre", str(render_bytes))
    if found.blocks:
        return ParsedCard(blocks=tuple(found.blocks), warnings=tuple(found.warnings), render=render,
                          render_bytes=render_bytes, render_sha256=render_sha)
    return ParsedCard(blocks=(), warnings=tuple(found.warnings), minimum=minimum, free_text=free_text,
                      render=render, render_bytes=render_bytes, render_sha256=render_sha)
