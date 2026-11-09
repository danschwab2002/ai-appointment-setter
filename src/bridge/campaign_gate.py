"""Que frena la publicacion de una campana (criterio ``frenos-v1``, decision de Dan del 2026-11-09).

El juez de ``segment_check`` sigue devolviendo todo. Esto decide, en codigo y sobre las citas que ya
pasaron la verificacion literal, cuales de sus observaciones frenan: solo las que contradicen un
valor o una promesa escrita (precio, cupon, garantia, promesas). Lo demas se muestra como aviso y
no frena. Una duda, un hueco o un item sin cita real son aviso siempre.

Va en codigo y no en el prompt a proposito: cambiar lo que devuelve el juez sube
``CHECK_RULES_VERSION``, que cambia todos los ``chequeo_sha`` y deja de servir las publicadas, y en
las corridas reales el juez marco como contradiccion cosas que su propio prompt dice que no lo son.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal

from .segment_check import (
    _CURRENCIES,
    Citation,
    GateContext,
    Item,
    Segment,
    _decimal,
    _fmt_decimal,
    _negated,
    _norm,
    _plain,
    extract,
)

__all__ = ["CRITERIO", "razon_de_freno"]

CRITERIO = "frenos-v1"

_ALTA = "preguntas_del_alta"
_LISTS = {"promesas_prohibidas", "no_confirmado"}
_UNKNOWN_REGION = "?"

# Una tabla del TOML, en su propia linea: lo que usa render_toml y lo que se escribe a mano.
_HEADER = re.compile(r"^\s*\[{1,2}\s*([A-Za-z0-9_.-]+)\s*\]{1,2}\s*(?:#.*)?$")

_LITERAL_PRICE = re.compile(r"^\s*precio\s*=\s*\"?([\d.,]+)\"?\s*$")
_PRICE_BEFORE_LINE = re.compile(r"\bprecio_antes\b")
# Un monto que es el de antes no es otro precio: «USD 31 (antes USD 47)». El marcador va pegado al
# monto: «solo antes del viernes: USD 35» es un precio de hoy.
_SAID_BEFORE = re.compile(
    r"(?:\bantes(?:\s+(?:costaba|valia|era(?:\s+de)?))?|\bprecio\s+(?:regular|normal|de\s+lista)(?:\s+de)?"
    r"|\ben\s+(?:lugar|vez)\s+de(?:\s+pagar)?|\bde\s+siempre)[\s:,(]*$"
)
# Montos que miden otra cosa que lo que se paga: lo que vale un bono, lo que se ahorra, una cuota.
_NOT_THE_PRICE = re.compile(
    r"(?:\bvalorad[oa]s?\s+en|\bvaluad[oa]s?\s+en|\bahorr\w+(?:\s+(?:de|hasta))?|\bdescuento\s+de"
    r"|\b(?:cuotas?|pagos|mensualidades|meses)\s+de)[\s:(]*$"
)
_CURRENCY_ALIASES = {"dolares americanos": "USD", "dolares estadounidenses": "USD", "pesos mexicanos": "MXN",
                     "peso mexicano": "MXN", "mx$": "MXN"}

_GUARANTEE_WORD = re.compile(r"\bgarantias?\b|\breembols\w*|\bdevoluci\w+|\bdevolvemos\b")
# Cuanto tarda en llegar la plata no es el plazo de la garantia.
_REFUND_TIMING = re.compile(r"\b(?:tard\w+|demor\w+|proces\w+|acredit\w+|lleg\w+|reflej\w+)\b")
# La palabra y su negacion despues: «la garantia no aplica». «No aplica a compras anteriores» acota, no niega.
_DENIED_AFTER = re.compile(
    r"[\s:=\"]*(?:ya\s+)?no\s+(?:aplica|esta\s+disponible|existe|se\s+ofrece|es\s+valida|vale)\b"
    r"(?!\s+(?:a|al|para|si|cuando|con|sobre|en\s+(?!esta\b)))"
)
# Lo que vale un plazo en dias. Dos calendarios: 12 meses y 1 ano son lo mismo, y 4 semanas y 1 mes tambien.
_DAYS: dict[str, tuple[Decimal, ...]] = {
    "hora": (Decimal(1) / Decimal(24),),
    "dia": (Decimal(1),),
    "semana": (Decimal(7),),
    "mes": (Decimal(30), Decimal(28)),
    "ano": (Decimal(365), Decimal(360)),
}

# El cupon como lo escribe la gente: mayusculas y numeros, y el + del final es parte del codigo
# (PROTOCOLO10+ no es PROTOCOLO10). El extractor del juez se lo come; por eso va este y no aquel.
# Una forma de codigo sola no alcanza (GLP-1, T3-T4, 48HRS): tiene que estar al lado de la palabra
# cupon o codigo, de «promo», o de un verbo como usa o escribe.
_TOKEN = r"[«\"'“]?((?=[A-Z0-9_-]*[A-Z])[A-Z0-9][A-Z0-9_-]{2,}\+?)(?![A-Za-z0-9+])"
_NAMED_CODE = re.compile(r"(?i:\b(?:cup[oó]n|c[oó]digo)(?:\s+(?:es|de\s+descuento))?)\s*:?\s*" + _TOKEN)
_PROMO_CODE = re.compile(r"(?i:\bpromo)\s*:?\s*(?=[«\"'“]?[A-Z0-9_-]*\d)" + _TOKEN)
_VERB_CODE = re.compile(
    r"(?i:\b(?:us[aá]r?|utiliz[aá]r?|aplic[aá]r?|escrib[eií]r?|ingres[aá]r?|coloc[aá]r?)\s+(?:(?:el|la)\s+)?)" + _TOKEN
)
# Tras un verbo, una palabra sola y corta no es un cupon: «escribe QUIERO» es la palabra clave.
_MIN_BARE_WORD_CHARS = 8
_NOT_CODES = {"USD", "MXN", "COP", "CLP", "PEN", "ARS", "EUR", "MSI", "SPEI", "OXXO", "PAYPAL", "HOTMART", "WHATSAPP",
              "GRATIS", "PROMO", "CUPON", "CODIGO"}
_NOT_A_CODE = re.compile(r"\d{4}-\d{2}")  # una fecha ISO: 2026-11-20T00
_COUPON_WORD = re.compile(r"\bcupon(?:es)?\b|\bcodigos?\b|\bdescuentos?\b")
_BONUS_WORD = re.compile(r"\bbon(?:o|os|us)\b|\bregalos?\b|\bguias?\b|\bclases?\b|\bmodulos?\b")
_COMBINES = re.compile(r"\bse suman?\b|\bsumar(?:se|lo|la|los|las)?\b|\bcombin\w+|\bacumul\w+")
_DISPLACES = re.compile(r"\b(?:reemplaza|sustituye|en (?:vez|lugar) de)\b")
_NEGATED_AFTER = re.compile(r"(?:ya )?(?:no|nunca)\b")
# Una frase corta: lo que niega una palabra, y lo que se dice junto.
_PHRASE_END = re.compile(r"[.;:,!?()]")
_CLAUSE_END = re.compile(r"[.;:!?()]")

_SPEAKS_OF_PROMISING = re.compile(r"\bpromet|\bpromesa")
# Un monto o un numero suelto («USD 47») esta en cualquier lado: citar eso no es citar la promesa.
_MIN_PROMISE_WORDS = 2
_MIN_WORD_CHARS = 3

# Las que hablan de lo que el agente le dice a alguien. El alta y el contexto son del equipo.
_VALUE_SOURCES_EXACT = {"segmento", "base"}
_VALUE_SOURCES_PREFIX = ("segmento:", "plantilla:", "caso:")


def _regions(text: str) -> list[tuple[str, str]]:
    """El TOML partido por tabla: (nombre, texto para comparar). Lo de antes de la primera, sin nombre."""
    regions: list[tuple[str, str]] = []
    name = ""
    lines: list[str] = []
    for line in text.splitlines():
        header = _HEADER.match(line)
        if header:
            regions.append((name, _plain("\n".join(lines))))
            name, lines = header.group(1), []
        else:
            lines.append(line)
    regions.append((name, _plain("\n".join(lines))))
    return regions


def _where(citation: Citation, regions: Sequence[tuple[str, str]]) -> set[str]:
    needle = _plain(citation.text)
    found = {name for name, text in regions if needle in text}
    return found or {_UNKNOWN_REGION}


def _speaks_outside_lists(citation: Citation, regions: Sequence[tuple[str, str]]) -> bool:
    """Si es texto de la campana que el agente usa: ni lista de lo que no se dice, ni respuesta del
    alta, que el agente no recibe (segment_knowledge la guarda aparte y no la renderiza)."""
    if citation.source != "segmento":
        return citation.source.startswith("plantilla:")
    return bool(_where(citation, regions) - _LISTS - {_ALTA})


def _confirmed_by_the_owner(citation: Citation, regions: Sequence[tuple[str, str]], ctx: GateContext) -> bool:
    """Si la cita sale solo de una respuesta del alta (la pregunta puede repetir el valor equivocado)."""
    if citation.source != "segmento" or _where(citation, regions) - _LISTS != {_ALTA}:
        return False
    needle = _plain(citation.text)
    return any(needle in _plain(answer) for _, answer, _ in ctx.segment.answers)


def _is_value_source(citation: Citation, regions: Sequence[tuple[str, str]], ctx: GateContext) -> bool:
    if citation.source not in _VALUE_SOURCES_EXACT and not citation.source.startswith(_VALUE_SOURCES_PREFIX):
        return False
    if citation.source != "segmento":
        return True
    where = _where(citation, regions) - _LISTS  # lo que no se dice o no esta confirmado no es un valor
    return _confirmed_by_the_owner(citation, regions, ctx) if where == {_ALTA} else bool(where)


def _speaks_for_the_campaign(citation: Citation) -> bool:
    return citation.source == "segmento" or citation.source.startswith("plantilla:")


def _skip_owner_pair(first: Citation, second: Citation, regions: Sequence[tuple[str, str]], ctx: GateContext) -> bool:
    """Lo que confirmo el duenio se contrapone al valor de la campana; contra el base o contra otra respuesta, no."""
    confirmed = [_confirmed_by_the_owner(first, regions, ctx), _confirmed_by_the_owner(second, regions, ctx)]
    if not any(confirmed):
        return False
    return all(confirmed) or not _speaks_for_the_campaign(second if confirmed[0] else first)


def _is_replaced_pair(first: Citation, second: Citation, ctx: GateContext, field_name: str, *,
                      is_the_offer: Callable[[Citation, GateContext], bool]) -> bool:
    """El base contra la campana en un campo que reemplaza a proposito, si lo citado es el valor de la oferta.
    Un tercer valor es otra cosa, y frena."""
    if field_name not in ctx.segment.replaces or "base" not in (first.source, second.source):
        return False
    own = second if first.source == "base" else first
    return _speaks_for_the_campaign(own) and is_the_offer(own, ctx)


def _offer_currency(segment: Segment) -> str:
    name = _norm(segment.currency or "")
    return _CURRENCIES.get(name) or _CURRENCY_ALIASES.get(name) or name.upper()


def _prices(citation: Citation, ctx: GateContext) -> set[tuple[str, str]]:
    """Los montos y porcentajes de una cita: (moneda o ? o %, valor)."""
    if _PRICE_BEFORE_LINE.search(citation.text):
        return set()
    literal = _LITERAL_PRICE.match(citation.text) if citation.source == "segmento" else None
    if literal:
        amount = _decimal(literal.group(1))
        return {(_offer_currency(ctx.segment), _fmt_decimal(amount))} if amount is not None else set()
    normalized = _norm(citation.text)
    found: set[tuple[str, str]] = set()
    for datum in extract(citation.text):
        if datum.kind not in ("monto", "porcentaje"):
            continue
        before = normalized[: normalized.find(datum.quote)]
        if _SAID_BEFORE.search(before) or (datum.kind == "monto" and _NOT_THE_PRICE.search(before)):
            continue
        if datum.kind == "porcentaje":
            found.add(("%", datum.value))
        else:
            currency, value = datum.value.split(" ", 1)
            found.add((currency, value))
    return found


def _is_the_offer_price(citation: Citation, ctx: GateContext) -> bool:
    amount = _decimal(ctx.segment.price) if ctx.segment.price else None
    if amount is None:
        return True
    currency, value = _offer_currency(ctx.segment), _fmt_decimal(amount)
    prices = [price for price in _prices(citation, ctx) if price[0] == currency]
    return not prices or any(price[1] == value for price in prices)


def _prices_differ(first: set[tuple[str, str]], second: set[tuple[str, str]]) -> bool:
    """Hay un par de la misma moneda y ninguno da igual: si comparten un valor, es otra redaccion.

    ``$`` sin moneda no se compara con una moneda explicita: en Mexico son pesos."""
    pairs = [(a, b) for a in first for b in second if a[0] == b[0]]
    return bool(pairs) and all(a[1] != b[1] for a, b in pairs)


def _pairs(citations: Sequence[Citation]) -> Iterable[tuple[Citation, Citation]]:
    for index, first in enumerate(citations):
        for second in citations[index + 1:]:
            yield first, second


def _other_price(citations: Sequence[Citation], ctx: GateContext, regions: Sequence[tuple[str, str]]) -> bool:
    for first, second in _pairs(citations):
        if _skip_owner_pair(first, second, regions, ctx) or _is_replaced_pair(first, second, ctx, "precio", is_the_offer=_is_the_offer_price):
            continue
        if _prices_differ(_prices(first, ctx), _prices(second, ctx)):
            return True
    return False


def _phrase_before(text: str, end: int) -> str:
    return _PHRASE_END.split(text[:end])[-1]


def _clause_around(text: str, position: int) -> str:
    start = max((match.end() for match in _CLAUSE_END.finditer(text, 0, position)), default=0)
    next_end = _CLAUSE_END.search(text, position)
    return text[start : next_end.start() if next_end else len(text)]


def _denied(normalized: str, position: int, lookback: int = 3) -> bool:
    """Una negacion justo antes, sin cruzar una coma ni un punto: «si no lo tienes, usa X» no niega a X."""
    phrase = _phrase_before(normalized, position)
    return _negated(phrase, len(phrase), lookback=lookback)


def _codes(text: str) -> list[tuple[int, int, str]]:
    """Los cupones de un texto: los que tienen forma de codigo y contexto de cupon al lado."""
    found: dict[int, tuple[int, int, str]] = {}
    for pattern in (_NAMED_CODE, _PROMO_CODE, _VERB_CODE):
        for match in pattern.finditer(text):
            code = match.group(1)
            bare_after_a_verb = pattern is _VERB_CODE and not any(c.isdigit() for c in code) and len(code) < _MIN_BARE_WORD_CHARS
            if code not in _NOT_CODES and not bare_after_a_verb and not _NOT_A_CODE.match(code):
                found[match.start(1)] = (match.start(1), match.end(1), code)
    return [found[start] for start in sorted(found)]


def _says_it_adds_up(citation: Citation, own: str | None) -> bool:
    """Que dice que se suma con otro cupon o descuento. Sumar un bono es otra cosa."""
    normalized = _norm(citation.text)
    for match in _COMBINES.finditer(normalized):
        clause = _clause_around(normalized, match.start())
        names_a_coupon = _COUPON_WORD.search(clause) is not None or bool(own and _norm(own) in clause)
        if names_a_coupon and not _BONUS_WORD.search(clause) and not _denied(normalized, match.start()):
            return True
    return False


def _code_is_denied(text: str, start: int, end: int) -> bool:
    """Si la negacion toca a este codigo: «No uses X», «X no aplica», «no se combina con X», «reemplaza a X»."""
    phrase = _norm(_phrase_before(text, start))
    if _negated(phrase, len(phrase), lookback=4) or _DISPLACES.search(phrase) is not None:
        return True
    if any(_negated(phrase, match.start(), lookback=3) for match in _COMBINES.finditer(phrase)):
        return True
    return _NEGATED_AFTER.match(_norm(text[end:])) is not None


def _offers_another_code(citation: Citation, own: str | None) -> bool:
    """Si la cita nombra como vigente un codigo distinto del de la campana. «No ofrezcas X» no lo ofrece."""
    return any((own is None or code.upper() != own.upper()) and not _code_is_denied(citation.text, start, end)
               for start, end, code in _codes(citation.text))


def _other_coupon(citations: Sequence[Citation], ctx: GateContext, regions: Sequence[tuple[str, str]]) -> bool:
    for citation in citations:
        if not _speaks_outside_lists(citation, regions):
            continue
        if _offers_another_code(citation, ctx.segment.coupon) or _says_it_adds_up(citation, ctx.segment.coupon):
            return True
    return False


def _guarantee_terms(text: str) -> set[Decimal]:
    """Los plazos de garantia de un texto, en dias (los dos valores posibles de un mes o un ano)."""
    terms: set[Decimal] = set()
    if _is_refund_timing(_norm(text)):
        return terms
    # El extractor mira 60 caracteres alrededor por la palabra: aca ya se sabe que habla de la garantia.
    for datum in extract(f"garantia {text}"):
        if datum.kind == "duracion":
            number, unit = datum.value.split(" ", 1)
            terms |= {Decimal(number) * days for days in _DAYS[unit]}
    return terms


def _is_refund_timing(normalized: str) -> bool:
    return "garantia" not in normalized and _REFUND_TIMING.search(normalized) is not None


def _guarantee_stance(text: str) -> str | None:
    """``no`` si el texto niega la garantia, ``si`` si la afirma, None si no habla de ella o da las dos."""
    normalized = _norm(text)
    if _is_refund_timing(normalized):
        return None
    matches = list(_GUARANTEE_WORD.finditer(normalized))
    if not matches:
        return None
    negated = {_denied(normalized, match.start()) or _DENIED_AFTER.match(normalized, match.end()) is not None for match in matches}
    if negated == {True}:
        return "no"
    return "si" if negated == {False} else None


def _is_the_offer_guarantee(citation: Citation, ctx: GateContext) -> bool:
    offer = ctx.segment.guarantee
    if offer is None:
        return True
    if _guarantee_stance(citation.text) == "no":
        return _guarantee_stance(offer if _GUARANTEE_WORD.search(_norm(offer)) else f"garantia {offer}") == "no"
    terms, offered = _guarantee_terms(citation.text), _guarantee_terms(offer)
    return not (terms and offered) or bool(terms & offered)


def _other_guarantee(citations: Sequence[Citation], ctx: GateContext, regions: Sequence[tuple[str, str]]) -> bool:
    for first, second in _pairs(citations):
        if _skip_owner_pair(first, second, regions, ctx) or _is_replaced_pair(first, second, ctx, "garantia", is_the_offer=_is_the_offer_guarantee):
            continue
        stances = {_guarantee_stance(first.text), _guarantee_stance(second.text)}
        if None in stances:
            continue
        if stances == {"no", "si"}:
            return True
        terms = _guarantee_terms(first.text), _guarantee_terms(second.text)
        if terms[0] and terms[1] and not terms[0] & terms[1]:
            return True
    return False


def _base_lists(ctx: GateContext) -> list[str]:
    lists = list(ctx.base.forbidden_promises) + list(ctx.base.unconfirmed)
    for product in ctx.base.products:
        lists += list(product.forbidden_promises) + list(product.unconfirmed)
    return [_plain(entry) for entry in lists]


def _is_offer_literal(citation: Citation) -> bool:
    return citation.source == "segmento" and (_LITERAL_PRICE.match(citation.text) is not None or _PRICE_BEFORE_LINE.search(citation.text) is not None)


def _is_a_real_quote(needle: str, entries: Sequence[str]) -> bool:
    """La entrada entera de la lista o una parte con sentido. Un monto o un numero solo, no."""
    words = [word for word in re.findall(r"[a-z]+", _norm(needle)) if word not in _CURRENCIES]
    if not words:
        return False
    return needle in entries or sum(len(word) >= _MIN_WORD_CHARS for word in words) >= _MIN_PROMISE_WORDS


def _is_a_written_promise(citation: Citation, regions: Sequence[tuple[str, str]], ctx: GateContext, lists: Sequence[str]) -> bool:
    needle = _plain(citation.text)
    if citation.source == "segmento":
        own = [_plain(entry) for entry in (*ctx.segment.forbidden, *ctx.segment.unconfirmed)]
        return bool(_where(citation, regions) & _LISTS) and _is_a_real_quote(needle, own)
    if citation.source == "base":
        return _is_a_real_quote(needle, lists) and any(needle in entry or entry in needle for entry in lists)
    return citation.source == "soul" and _SPEAKS_OF_PROMISING.search(_norm(citation.text)) is not None


def _breaks_a_promise(citations: Sequence[Citation], ctx: GateContext, regions: Sequence[tuple[str, str]]) -> bool:
    lists = _base_lists(ctx)
    promises = [c for c in citations if _is_a_written_promise(c, regions, ctx, lists)]
    # Un valor suelto de la oferta (``precio = "31"``) no dice nada que rompa una promesa.
    speaks = [c for c in citations if _speaks_outside_lists(c, regions) and c not in promises and not _is_offer_literal(c)]
    return bool(promises) and bool(speaks)


def razon_de_freno(item: Item, ctx: GateContext) -> str | None:
    """Por que esta observacion frena la publicacion, o None si queda como aviso.

    Solo una contradiccion con cita real. Devuelve la primera regla que de: precio, cupon, garantia, promesa."""
    if item.kind != "C" or not item.verified:
        return None
    regions = _regions(ctx.segment_text)
    values = [c for c in item.citations if _is_value_source(c, regions, ctx)]
    if _other_price(values, ctx, regions):
        return "precio"
    if _other_coupon(item.citations, ctx, regions):
        return "cupon"
    if _other_guarantee(values, ctx, regions):
        return "garantia"
    if _breaks_a_promise(item.citations, ctx, regions):
        return "promesa"
    return None
