"""La guarda de precio (docs/contracts/price-guard-v1.md, MP-04, E-564).

Que el agente nunca le diga a una persona un precio que nadie le enseno. La
guarda corre despues del modelo y antes de entregar, dentro del bucle de
intentos de `bridge.hermes`, igual que hoy pasa con las URLs (`url_in_reply`).

Tres piezas, todas puras:

1. El **extractor** (`extract_amounts`): de un texto, los montos que el texto
   ata a una moneda (codigo ISO, simbolo, palabra o cuota), con los
   separadores de Mexico y Colombia. Es la misma funcion que va a usar la
   validacion de `product-card-v1` (MP-07).
2. Los **permitidos del turno** (`PriceGuard.allowed_for_turn`): lo que el
   extractor encuentra en los textos que el bridge le manda al modelo en ese
   turno (el conocimiento base, los segmentos y el bloque de la campana, el
   contexto que arma el bridge sin los mensajes ni lo que dijo la persona),
   mas los datos estructurados (el precio del manifiesto, el de la oferta de
   la campana y `[guarda_precio] montos_extra`).
3. La **comparacion** (`PriceGuard.blocked`): los montos del `reply` que no
   estan en los permitidos, con la misma moneda. Un monto con `$` sin codigo
   pasa si el numero esta permitido en cualquier moneda; un permitido con `$`
   vale para cualquier moneda (D5 del arnes A-0). «$1,397 USD» es USD: el
   codigo despues del signo manda.

Con `AGENT_PRICE_GUARD_MODE=off` (o ausente) el bridge no arma la guarda y
nada de este modulo corre: el pedido a Hermes y la propuesta son byte a byte
los de siempre.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

PRICE_GUARD_MODE_ENV = "AGENT_PRICE_GUARD_MODE"
MODE_OFF = "off"
MODE_BLOCK = "bloquea"
MODES = (MODE_OFF, MODE_BLOCK)

# La regla del intento, el `reason_code` de la derivacion y el `reason` del
# resultado `failed` del procesador (price-guard-v1 §11).
PRICE_NOT_IN_CATALOG = "price_not_in_catalog"
CONFIGURATION_INVALID = "price_guard_configuration_invalid"

OUTCOME_PASSED = "passed"
OUTCOME_RETRIED_OK = "retried_ok"
OUTCOME_HANDOFF = "handoff"

# La nota privada que deja la derivacion cuando el agente habia pedido el
# link y la guarda no lo dejo salir (price-guard-v1 §4).
PRICE_GUARD_LINK_NOTE_MARKER_PREFIX = "[supportmagician-guarda-precio:"

# Cuantos permitidos se nombran en la correccion del reintento. El
# conocimiento de una aliada trae pocos montos (ATT1: 1, Maribel: 2), pero un
# texto largo no tiene que inflar el mensaje.
_MAX_ALLOWED_IN_CORRECTION = 12


class PriceGuardConfigurationError(ValueError):
    """`AGENT_PRICE_GUARD_MODE` tiene un valor que no es `off` ni `bloquea`."""


def parse_mode(raw: str | None) -> str:
    """El modo del entorno. Ausente o vacio = `off`; otro valor no arranca."""
    value = (raw or "").strip().lower()
    if not value:
        return MODE_OFF
    if value not in MODES:
        raise PriceGuardConfigurationError(CONFIGURATION_INVALID)
    return value


# --------------------------------------------------------------------------
# El extractor (price-guard-v1 §2)

# Los codigos ISO 4217 de las aliadas y sus paises. Un codigo que no esta aca
# no ata un numero: «20 abc» no es un monto.
_ISO_CODES = (
    "USD",
    "MXN",
    "COP",
    "ARS",
    "CLP",
    "PEN",
    "EUR",
    "BRL",
    "UYU",
    "GTQ",
    "CRC",
    "DOP",
    "BOB",
    "PYG",
)
_ISO = "|".join(_ISO_CODES)
# Antes del numero: codigo (tambien pegado a «$»: USD$, MXN$), US$, U$S, MX$,
# COL$, € o $. Los largos primero, para que «USD$» no se lea como «USD».
_BEFORE = rf"(?:(?:{_ISO})\s?\$|U\$S|US\$|MX\$|COL\$|{_ISO}|€|\$)"
# Despues del numero: codigo, «$MXN», €, $ o una palabra de moneda.
_AFTER = (
    rf"(?:\$\s?(?:{_ISO})|{_ISO}|€|\$|dlls?|dls|"
    r"pesos?\s+mexicanos?|pesos?\s+colombianos?|pesos?\s+argentinos?|"
    r"pesos?\s+chilenos?|pesos?|d[óo]lares|d[óo]lar|euros?|soles)"
)
# Un numero con separadores de miles (1,397 · 1.397.000 · 1.204,31) o sin
# ellos (47 · 1397.50).
_NUMBER = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
# Despues del numero: que no siga otro digito (ni tras un separador ni tras
# un espacio, «$1 397»), ni un porcentaje. Sin esto el motor retrocede y
# parte el numero («$1397.505» se leia $1397).
_NUMBER_END = r"(?![.,]?\d)(?!\s\d{3}(?!\d))(?!\s?%)"
# Las unidades que dicen que un «N x M» o «N meses de M» no es un precio.
_NOT_MONEY_AFTER = (
    r"(?!\s*(?:min\b|minutos?|segundos?|horas?|h\b|d[ií]as?|semanas?|meses|mes\b|"
    r"a[ñn]os?|cuotas|pagos|mensualidades|personas|clases|m[óo]dulos|sesiones))"
)

_RE_BEFORE = re.compile(
    rf"(?<![A-Za-z0-9])({_BEFORE})\s?({_NUMBER}){_NUMBER_END}"
    rf"(?:\s?({_ISO})(?![A-Za-z]))?",
    re.IGNORECASE,
)
_RE_AFTER = re.compile(
    rf"(?<![\w.,$€])(?<!\d\s)({_NUMBER})\s?({_AFTER})(?![A-Za-z])",
    re.IGNORECASE,
)
# Cuotas con moneda o sin ella: «12 cuotas de $166», «12 pagos de 166 USD»,
# «12x 166», «12 meses de 166». El monto es la cuota, no el numero de cuotas.
_RE_INSTALLMENT = re.compile(
    rf"(?<![\w.,])\d{{1,2}}\s*(?:x|cuotas\s+de|pagos\s+de|meses\s+de|"
    rf"mensualidades\s+de)\s*({_BEFORE})?\s?({_NUMBER}){_NUMBER_END}"
    rf"(?:\s?({_AFTER})(?![A-Za-z]))?{_NOT_MONEY_AFTER}",
    re.IGNORECASE,
)
# «12 de 166»: solo si cerca dice cuotas, meses, pagos o mensualidades (si no,
# «15 de noviembre» o «8 de 10» serian montos). «1 de 12 cuotas» es el total
# de cuotas, no un monto: lo descarta _NOT_MONEY_AFTER.
_RE_INSTALLMENT_BARE = re.compile(
    rf"(?<![\w.,])\d{{1,2}}\s+de\s+({_BEFORE})?\s?({_NUMBER}){_NUMBER_END}{_NOT_MONEY_AFTER}",
    re.IGNORECASE,
)
_RE_INSTALLMENT_CONTEXT = re.compile(r"cuota|meses|pagos|mensualidad", re.IGNORECASE)
# Una cuota sin moneda escrita por debajo de esto no cuenta (D3 del PR), y un
# año («los primeros 3 meses de 2026») tampoco.
_MIN_BARE_INSTALLMENT = Decimal(10)
_YEARS = (Decimal(1990), Decimal(2100))

ANY_CURRENCY = "$"


@dataclass(frozen=True, order=True)
class Amount:
    """Un monto con su moneda. `currency` es un codigo ISO o `$` (sin codigo)."""

    currency: str
    value: Decimal

    def label(self) -> str:
        """Como se nombra en la correccion: `USD 47`, `MXN 1.397`, `$ 166`."""
        return f"{self.currency} {format_decimal(self.value)}"

    def as_json(self) -> dict[str, str]:
        return {"monto": str(self.value), "moneda": self.currency}


def format_decimal(value: Decimal) -> str:
    """Miles con punto y decimales con coma, como el conocimiento de Maribel."""
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent >= 0:
        integer, fraction = f"{int(value):d}", ""
    else:
        text = f"{value:f}"
        integer, fraction = text.split(".")
    groups = []
    while len(integer) > 3:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]
    groups.insert(0, integer)
    formatted = ".".join(groups)
    return f"{formatted},{fraction}" if fraction else formatted


def normalize_decimal(value: Decimal) -> Decimal | None:
    """Un valor positivo y finito, sin ceros de mas: 199.00 es 199."""
    if not value.is_finite() or value <= 0:
        return None
    if value == value.to_integral_value():
        return value.quantize(Decimal(1))
    return value.normalize()


def parse_number(text: str) -> Decimal | None:
    """Un numero con los separadores de Mexico y Colombia (price-guard-v1 §2).

    Un punto o una coma seguidos de exactamente tres digitos son miles; con
    uno o dos digitos, decimales. Con los dos signos, el ultimo es el decimal.
    """
    raw = text.strip().replace(" ", "")
    if "," in raw and "." in raw:
        decimal_sign = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands = "." if decimal_sign == "," else ","
        raw = raw.replace(thousands, "").replace(decimal_sign, ".")
    elif "," in raw or "." in raw:
        separator = "," if "," in raw else "."
        parts = raw.split(separator)
        if all(len(part) == 3 for part in parts[1:]):
            raw = raw.replace(separator, "")
        elif len(parts) == 2:
            raw = raw.replace(separator, ".")
        else:
            return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return normalize_decimal(value)


def _currency(token: str | None) -> str:
    text = re.sub(r"\s+", "", (token or "").strip().lower())
    if not text or text in ("$", "peso", "pesos"):
        return ANY_CURRENCY
    if text in ("us$", "u$s") or text.startswith("d"):
        return "USD"
    if text == "mx$" or "mexican" in text:
        return "MXN"
    if text == "col$" or "colombian" in text:
        return "COP"
    if len(text) == 4 and text.endswith("$"):
        # USD$, MXN$: el codigo pegado al signo.
        return text[:3].upper()
    if len(text) == 4 and text.startswith("$"):
        # $MXN despues del numero.
        return text[1:].upper()
    if "argentin" in text:
        return "ARS"
    if "chilen" in text:
        return "CLP"
    if text == "soles":
        return "PEN"
    if text == "€" or text.startswith("euro"):
        return "EUR"
    return text.upper()


def extract_amounts(text: str | None) -> frozenset[Amount]:
    """Los montos de un texto (price-guard-v1 §2). Vacio si no hay ninguno.

    No es un monto: un numero sin moneda («8 programas», «30 dias»), un
    porcentaje («10 %»), una hora, una fecha o un numero de pedido. Una cuota
    sin moneda escrita sale con `$` (D4 del arnes A-0). Si el mismo numero se
    leyo con moneda y como cuota sin moneda, queda el de la moneda.
    """
    if not text:
        return frozenset()
    found: set[Amount] = set()

    def add(number: str, currency: str | None, *, installment: bool = False) -> None:
        value = parse_number(number)
        if value is None:
            return
        if installment and currency is None and (
            value < _MIN_BARE_INSTALLMENT or _YEARS[0] <= value <= _YEARS[1]
        ):
            # «2x1», «4 x 4», «3 meses de 2026»: no es un precio.
            return
        found.add(Amount(currency=_currency(currency), value=value))

    for match in _RE_BEFORE.finditer(text):
        # «$1,397 USD»: el codigo despues del numero manda sobre un «$» suelto.
        symbol = match.group(1)
        if symbol == "$" and match.group(3):
            symbol = match.group(3)
        add(match.group(2), symbol)
    for match in _RE_AFTER.finditer(text):
        add(match.group(1), match.group(2))
    for match in _RE_INSTALLMENT.finditer(text):
        add(match.group(2), match.group(1) or match.group(3), installment=True)
    for match in _RE_INSTALLMENT_BARE.finditer(text):
        window = text[max(0, match.start() - 40) : match.end() + 40]
        if _RE_INSTALLMENT_CONTEXT.search(window):
            add(match.group(2), match.group(1), installment=True)
    with_currency = {amount.value for amount in found if amount.currency != ANY_CURRENCY}
    return frozenset(
        amount
        for amount in found
        if amount.currency != ANY_CURRENCY or amount.value not in with_currency
    )


# Las secciones de un texto del turno cuyos montos NO se permiten: lo que el
# agente nunca puede decir y lo que no esta confirmado. El render del
# conocimiento base y el de un segmento o campana usan estos titulos. Medido el
# 14/11: la campana Black de ATT1 (r3) dice «Citar el precio tachado de USD 97»
# en sus promesas prohibidas; sin esto, USD 97 pasaba la guarda (D2 del PR).
_SECTIONS_NOT_ALLOWED = frozenset({"## Promesas prohibidas", "## No confirmado"})


def allowed_text(text: str | None) -> str:
    """El texto sin sus secciones de promesas prohibidas ni de no confirmado."""
    if not text:
        return ""
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        heading = line.strip()
        if heading.startswith("#"):
            skipping = heading in _SECTIONS_NOT_ALLOWED
        if not skipping:
            kept.append(line)
    return "\n".join(kept)


# Lo que el contexto del turno trae y NO cuenta como permitido: los mensajes
# (lo que escribio la persona y lo que contesto antes el agente) y los campos
# que el agente capturo de la persona (price-guard-v1 §3, «No son permitidos
# los montos que escribio la persona»).
_CONTEXT_KEYS_NOT_ALLOWED = frozenset({"messages", "known_fields"})


def _context_texts(context: Mapping[str, object]) -> list[str]:
    texts: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    for key, value in context.items():
        if key not in _CONTEXT_KEYS_NOT_ALLOWED:
            walk(value)
    return texts


# --------------------------------------------------------------------------
# Los permitidos y la comparacion (price-guard-v1 §3)


def amount_of(value: object, currency: object) -> Amount | None:
    """Un dato estructurado (precio + moneda) como monto, o None si no sirve."""
    if not isinstance(currency, str) or not currency.strip():
        return None
    if isinstance(value, Decimal):
        number = normalize_decimal(value)
    elif isinstance(value, (int, str)) and not isinstance(value, bool):
        number = parse_number(str(value))
    else:
        return None
    if number is None:
        return None
    return Amount(currency=_currency(currency), value=number)


def block_offer_amounts(block: object) -> frozenset[Amount]:
    """`oferta.precio` y `oferta.precio_antes` del segmento o la campana de un bloque.

    Un `TurnSegment` lleva el `LoadedSegment`, que lleva el conocimiento con su
    oferta. Un bloque sin oferta (el aviso de entrada de una campana) no suma.
    """
    segment = getattr(block, "segment", None)
    knowledge = getattr(segment, "knowledge", None)
    offer = getattr(knowledge, "offer", None)
    if offer is None:
        return frozenset()
    currency = getattr(offer, "currency", None)
    found = {
        amount
        for amount in (
            amount_of(getattr(offer, "price", None), currency),
            amount_of(getattr(offer, "price_before", None), currency),
        )
        if amount is not None
    }
    return frozenset(found)


@dataclass(frozen=True)
class PriceGuard:
    """La guarda de una instancia, en modo `bloquea`.

    ``structured``: los montos fijos de la instancia (el precio del principal y
    de cada producto del manifiesto, y `[guarda_precio] montos_extra`).
    """

    structured: frozenset[Amount] = frozenset()
    mode: str = MODE_BLOCK

    def allowed_for_turn(
        self,
        *,
        texts: Iterable[str | None],
        context: Mapping[str, object] | None = None,
        blocks: Iterable[object] = (),
    ) -> frozenset[Amount]:
        allowed: set[Amount] = set(self.structured)
        for text in texts:
            allowed |= extract_amounts(allowed_text(text))
        for block in blocks:
            rendered = getattr(block, "rendered", None)
            allowed |= extract_amounts(allowed_text(rendered if isinstance(rendered, str) else None))
            allowed |= block_offer_amounts(block)
        if context is not None:
            for text in _context_texts(context):
                allowed |= extract_amounts(text)
        return frozenset(allowed)

    @staticmethod
    def blocked(reply: object, allowed: frozenset[Amount]) -> tuple[Amount, ...]:
        """Los montos del `reply` fuera de los permitidos, ordenados. Vacio = pasa."""
        if not isinstance(reply, str):
            return ()
        values = {amount.value for amount in allowed}
        wildcard = {amount.value for amount in allowed if amount.currency == ANY_CURRENCY}
        out = []
        for amount in extract_amounts(reply):
            if (
                amount in allowed
                or amount.value in wildcard
                or (amount.currency == ANY_CURRENCY and amount.value in values)
            ):
                continue
            out.append(amount)
        return tuple(sorted(out))


def guard_from_manifest(manifest: object) -> PriceGuard:
    """La guarda de una instancia con manifiesto: sus precios estructurados.

    El precio del principal (`[hotmart]`), el de cada producto de
    `[[productos]]` con Hotmart propio y `[guarda_precio] montos_extra`.
    """
    structured: set[Amount] = set()
    principal = amount_of(getattr(manifest, "price", None), getattr(manifest, "currency", None))
    if principal is not None:
        structured.add(principal)
    for product in getattr(manifest, "products", ()) or ():
        commerce = getattr(product, "commerce", None)
        if commerce is None:
            continue
        amount = amount_of(getattr(commerce, "price", None), getattr(commerce, "currency", None))
        if amount is not None:
            structured.add(amount)
    for extra in getattr(manifest, "price_guard_extra_amounts", ()) or ():
        amount = amount_of(extra.value, extra.currency)
        if amount is not None:
            structured.add(amount)
    return PriceGuard(structured=frozenset(structured))


def correction_detail(blocked: Iterable[Amount], allowed: Iterable[Amount]) -> str:
    """El detalle de la correccion del reintento. Solo montos de catalogo.

    Nunca lleva texto de la persona ni del modelo: los montos bloqueados son
    numeros y los permitidos salen de lo que el bridge le paso al modelo.
    """
    blocked_labels = ", ".join(amount.label() for amount in sorted(set(blocked)))
    allowed_sorted = sorted(set(allowed))
    if allowed_sorted:
        shown = allowed_sorted[:_MAX_ALLOWED_IN_CORRECTION]
        allowed_part = "usa solo estos: " + ", ".join(amount.label() for amount in shown)
    else:
        allowed_part = "no escribas ningun monto"
    noun = "el monto" if len(set(blocked)) == 1 else "los montos"
    verb = "no esta" if len(set(blocked)) == 1 else "no estan"
    return f"{noun} {blocked_labels} {verb} en lo que te pasamos; {allowed_part}."


def provenance_entry(
    attempts: list[dict[str, object]], outcome: str
) -> dict[str, object]:
    """La clave `price_guard` de la procedencia del turno (price-guard-v1 §5)."""
    return {"mode": MODE_BLOCK, "attempts": attempts, "outcome": outcome}


def link_note_marker(handoff_request_id: str) -> str:
    """El marcador de la nota del link que no salio: una por derivacion."""
    return f"{PRICE_GUARD_LINK_NOTE_MARKER_PREFIX}{handoff_request_id}]"


def link_note_body(product_name: str | None) -> str:
    """La nota privada para el equipo. Texto fijo, sin nada de la persona."""
    product = f" de {product_name}" if product_name else ""
    return (
        f"El agente pidió el link de pago{product} y no salió: su respuesta "
        "traía un monto que no está en el catálogo (guarda de precio, "
        "price_not_in_catalog). Si la persona quiere comprar, mándale el link "
        "vos."
    )
