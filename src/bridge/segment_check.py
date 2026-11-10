"""El chequeo del alta de un segmento: ``setter segmento chequear <segmento.toml>``.

Diseno: E-310, «Segmentos: una etiqueta le abre al agente un conocimiento extra», §3
(el alta), con la correccion de Dan del 2026-11-03 (E-312b): el chequeo lo hace un
modelo, no reglas fijas. Contrato: docs/contracts/segment-check-v1.md.

Antes de que un segmento (p. ej. una campana) entre a produccion, un juez de modelo lo
lee junto con todo lo que el agente ya sabe (el SOUL comun, el conocimiento base de la
aliada, los otros segmentos con vigencias que se pisan, las plantillas de los flujos que
ponen su etiqueta y los casos del arnes) y devuelve contradicciones (C), huecos (H) y
dudas (D, lo que no le queda claro), cada una con la cita textual de cada fuente y una
pregunta lista para el equipo. Lo que el juez no tiene claro va como duda, nunca como
«esta bien».

El codigo no decide si hay una contradiccion. Lo que hace el codigo:

- arma las fuentes, cada una con un id, y se las pasa al juez enteras;
- le pasa como insumo los datos concretos que encuentra un extractor de expresiones
  regulares (montos, porcentajes, fechas, cupones, garantia, medios): una ayuda para
  que no se le escapen, no una decision;
- valida la salida: el JSON tiene que tener la forma pedida, y cada cita tiene que
  aparecer literalmente en la fuente que nombra. Una cita que no esta se descarta y se
  reporta;
- corre el juez N veces (3 por defecto) y junta los items: uno que sale en una sola
  corrida se pregunta igual;
- con 0 items en las N corridas validas, calcula el ``chequeo_sha`` que exige el
  cargador (:func:`verificar_chequeo`).

Las respuestas del equipo van en ``[[preguntas_del_alta]]`` del segmento, y la corrida
siguiente las lee como parte del segmento.

El esquema del archivo del segmento (``segment-knowledge-v1``) lo carga en el bridge
``segment_knowledge.py`` (PR-A del mismo diseno). Este modulo lee el TOML crudo con su
propio lector, a proposito: no depende del cargador y el enchufe es una linea. Al
mergear los dos, el lector se unifica.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tomllib
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import httpx

from .commercial_knowledge import CommercialKnowledge, KnowledgeError

REPORT_SCHEMA = "segment-check-v2"
SEGMENT_SCHEMA = "segment-knowledge-v1"

# La version del chequeo (instrucciones del juez y validacion de su salida). Cambiarla
# cambia todos los chequeo_sha: un segmento aprobado con otra version vuelve al chequeo.
CHECK_RULES_VERSION = "segment-check-juez-v2"

SOUL_RELATIVE_PATH = Path("profiles") / "agente-comercial-comun" / "SOUL.md"

# El juez. moonshotai/kimi-k2.6 es el respaldo que eligio Dan el 2026-10-14 para los
# profiles de ATT1: otra familia que la del agente comercial (z-ai/glm-5.3-flash), asi
# que no comparte sus puntos ciegos, y la cuenta de OpenRouter, que tiene retencion cero
# (ZDR) y excluye los modelos que no la cumplen, ya le hizo un pedido con 200 ese dia.
DEFAULT_MODEL = "moonshotai/kimi-k2.6"
DEFAULT_RUNS = 3
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY_VARIABLE = "OPENROUTER_API_KEY"
_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)

MAX_INSTRUCTION_CHARS = 600

_CHEQUEO_SHA = re.compile(r"[0-9a-f]{64}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")

# Los campos de [oferta] que un segmento puede cambiar a proposito.
REPLACEABLE_FIELDS = ("precio", "moneda", "garantia", "incluye", "medios")  # los de PR-A

# Lo que no entra en la huella del contenido: el chequeo_sha (seria circular) y la
# aprobacion, que se escribe despues del chequeo y no cambia lo que dice el segmento.
_HEADER_OUTSIDE_DIGEST = ("chequeo_sha", "estado", "aprobado_por", "aprobado_el")

_TOP_REQUIRED = {"cabecera", "vigencia", "asesoramiento"}
_TOP_OPTIONAL = {
    "instruccion",
    "oferta",
    "se_puede_decir",
    "no_confirmado",
    "promesas_prohibidas",
    "faq",
    "preguntas_del_alta",
}
_OFFER_KEYS = {
    "producto",
    "precio",
    "moneda",
    "precio_antes",
    "cupon",
    "codigo_oferta",
    "incluye",
    "garantia",
    "medios",
    "reemplaza",
}

class SegmentCheckError(ValueError):
    """El segmento (o una fuente) no se puede leer. El mensaje dice que y donde."""


class ChequeoError(ValueError):
    """El cargador no puede aceptar el segmento: falta o no coincide el chequeo_sha."""


# --------------------------------------------------------------------------- lectura


@dataclass(frozen=True)
class Segment:
    """Lo que el chequeo necesita de un archivo de segmento, leido del TOML crudo."""

    raw: Mapping[str, Any]
    key: str
    version: int
    status: str
    ally_ref: str
    chequeo_sha: str | None
    starts: datetime
    ends: datetime
    instruction: str | None
    product: str | None
    price: str | None
    currency: str | None
    price_before: str | None
    coupon: str | None
    includes: tuple[str, ...]
    guarantee: str | None
    methods: tuple[str, ...]
    replaces: tuple[str, ...]
    advice: tuple[str, ...]
    statements: tuple[str, ...]
    unconfirmed: tuple[str, ...]
    forbidden: tuple[str, ...]
    faq: tuple[tuple[str, str, str], ...]
    answers: tuple[tuple[str, str, str], ...]
    # La oferta de Hotmart de la campana (E-410): no entra al prompt del juez como
    # un dato a contrastar; la frase de JUDGE_INSTRUCTIONS dice que no es un item.
    offer_code: str | None = None

    def overlaps(self, other: "Segment") -> bool:
        return self.starts < other.ends and other.starts < self.ends



def read_segment_file(path: Path) -> Segment:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SegmentCheckError(f"no se pudo leer {path}") from exc
    try:
        payload = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SegmentCheckError(f"{path} no es TOML valido: {exc}") from exc
    return parse_segment(payload)


def parse_segment(payload: Mapping[str, Any]) -> Segment:
    """Lee el esquema segment-knowledge-v1 (E-310 §2.1). Cerrado: una clave de mas es error."""

    _exact("segmento", payload, _TOP_REQUIRED, _TOP_OPTIONAL)
    header = _table(payload, "cabecera")
    _exact(
        "cabecera",
        header,
        {"segment_version", "estado", "ally_ref", "clave"},
        {"nombre", "aprobado_por", "aprobado_el", "chequeo_sha"},
    )
    _optional_text(header, "nombre", "cabecera.nombre")
    version = header["segment_version"]
    if type(version) is not int or version < 1:
        raise SegmentCheckError("cabecera.segment_version debe ser un entero positivo")
    status = _text(header, "estado", "cabecera.estado")
    if status not in ("borrador", "aprobado"):
        raise SegmentCheckError("cabecera.estado debe ser borrador o aprobado")
    key = _text(header, "clave", "cabecera.clave")
    if _SLUG.fullmatch(key) is None:
        raise SegmentCheckError("cabecera.clave debe ser un slug en minusculas (a-z, 0-9, guiones)")
    sha = header.get("chequeo_sha")
    if sha is not None and (not isinstance(sha, str) or _CHEQUEO_SHA.fullmatch(sha) is None):
        raise SegmentCheckError("cabecera.chequeo_sha debe ser un sha256 en hexadecimal (64 caracteres)")

    window = _table(payload, "vigencia")
    _exact("vigencia", window, {"desde", "hasta"})
    starts = _aware(window["desde"], "vigencia.desde")
    ends = _aware(window["hasta"], "vigencia.hasta")
    if not starts < ends:
        raise SegmentCheckError("vigencia.desde tiene que ser anterior a vigencia.hasta")

    instruction = None
    if "instruccion" in payload:
        table = _table(payload, "instruccion")
        _exact("instruccion", table, set(), {"texto"})
        instruction = _optional_text(table, "texto", "instruccion.texto")
        if instruction is not None and len(instruction) > MAX_INSTRUCTION_CHARS:
            raise SegmentCheckError(
                f"instruccion.texto tiene {len(instruction)} caracteres; el maximo es {MAX_INSTRUCTION_CHARS}"
            )

    offer: Mapping[str, Any] = {}
    if "oferta" in payload:
        offer = _table(payload, "oferta")
        _exact("oferta", offer, set(), _OFFER_KEYS)
        _offer_rules(offer)
    replaces = _list(offer, "reemplaza", "oferta.reemplaza")
    unknown = sorted(set(replaces) - set(REPLACEABLE_FIELDS))
    if unknown:
        raise SegmentCheckError(
            f"oferta.reemplaza nombra campos que no se reemplazan: {', '.join(unknown)} "
            f"(se puede: {', '.join(REPLACEABLE_FIELDS)})"
        )
    present = {
        "precio": "precio" in offer,
        "moneda": "moneda" in offer,
        "garantia": "garantia" in offer,
        "incluye": bool(offer.get("incluye")),
        "medios": bool(offer.get("medios")),
    }
    for name in replaces:
        if not present[name]:
            raise SegmentCheckError(f"oferta.reemplaza nombra '{name}' y la oferta no lo trae")
    if len(set(replaces)) != len(replaces):
        raise SegmentCheckError("oferta.reemplaza repite un campo")

    advice_table = _table(payload, "asesoramiento")
    _exact("asesoramiento", advice_table, {"items"})

    return Segment(
        raw=payload,
        key=key,
        version=version,
        status=status,
        ally_ref=_text(header, "ally_ref", "cabecera.ally_ref"),
        chequeo_sha=sha,
        starts=starts,
        ends=ends,
        instruction=instruction,
        product=_optional_text(offer, "producto", "oferta.producto"),
        price=_optional_text(offer, "precio", "oferta.precio"),
        currency=_optional_text(offer, "moneda", "oferta.moneda"),
        price_before=_optional_text(offer, "precio_antes", "oferta.precio_antes"),
        coupon=_optional_text(offer, "cupon", "oferta.cupon"),
        offer_code=_optional_text(offer, "codigo_oferta", "oferta.codigo_oferta"),
        includes=_list(offer, "incluye", "oferta.incluye"),
        guarantee=_optional_text(offer, "garantia", "oferta.garantia"),
        methods=_list(offer, "medios", "oferta.medios"),
        replaces=replaces,
        advice=_list(advice_table, "items", "asesoramiento.items", allow_empty=False),
        statements=_items(payload, "se_puede_decir"),
        unconfirmed=_items(payload, "no_confirmado"),
        forbidden=_items(payload, "promesas_prohibidas"),
        faq=_qa(payload, "faq"),
        answers=_qa(payload, "preguntas_del_alta"),
    )


_COUPON_FORM = re.compile(r"[A-Za-z0-9_-]{2,64}")
_OFFER_CODE_FORM = re.compile(r"[A-Za-z0-9]{4,32}")


def _offer_rules(offer: Mapping[str, Any]) -> None:
    """Las reglas de [oferta] del cargador de PR-A (segment_knowledge._offer), las mismas."""

    if not offer:
        raise SegmentCheckError("oferta no puede estar vacia: si no cambia nada de la oferta, no va")
    product = _optional_text(offer, "producto", "oferta.producto")
    if product is not None and _SLUG.fullmatch(product) is None:
        raise SegmentCheckError("oferta.producto debe ser un slug en minusculas (a-z, 0-9, guiones)")
    if ("precio" in offer) != ("moneda" in offer):
        raise SegmentCheckError("oferta.precio y oferta.moneda van juntos")
    if "precio_antes" in offer and "precio" not in offer:
        raise SegmentCheckError("oferta.precio_antes va con oferta.precio")
    coupon = _optional_text(offer, "cupon", "oferta.cupon")
    if coupon is not None and _COUPON_FORM.fullmatch(coupon) is None:
        raise SegmentCheckError("oferta.cupon debe ser un codigo (letras, numeros, guiones; 2 a 64)")
    offer_code = _optional_text(offer, "codigo_oferta", "oferta.codigo_oferta")
    if offer_code is not None and _OFFER_CODE_FORM.fullmatch(offer_code) is None:
        raise SegmentCheckError(
            "oferta.codigo_oferta debe ser un codigo de oferta de Hotmart (letras y numeros, 4 a 32)"
        )


def segment_content_sha(payload: Mapping[str, Any]) -> str:
    """La huella de lo que dice el segmento, sin el chequeo_sha ni la aprobacion."""

    content = {key: value for key, value in payload.items() if key != "cabecera"}
    header = payload.get("cabecera")
    if isinstance(header, Mapping):
        content["cabecera"] = {
            key: value for key, value in header.items() if key not in _HEADER_OUTSIDE_DIGEST
        }
    return _sha(_canonical(content))


def calcular_chequeo_sha(
    segmento: Mapping[str, Any],
    *,
    base_sha256: str,
    otros: Sequence[Mapping[str, Any]] = (),
) -> str:
    """El chequeo_sha de un segmento: lo que el cargador recalcula al arrancar.

    Cubre lo que el bridge tiene a mano al arrancar: el contenido del segmento, el sha
    del render del conocimiento base, el contenido de los otros segmentos declarados
    cuyas vigencias se pisan con este y la version de las reglas del chequeo. Las
    plantillas, los flujos y los casos del arnes son entradas del chequeo (pueden
    abrir items) pero no de la huella: el bridge no los tiene al arrancar. El sha solo
    lo imprime un chequeo con 0 abiertos.
    """

    this = parse_segment(segmento)
    overlapping = []
    for raw in otros:
        other = parse_segment(raw)
        if other.key != this.key and this.overlaps(other):
            overlapping.append([other.key, segment_content_sha(raw)])
    return _sha(
        _canonical(
            {
                "esquema": REPORT_SCHEMA,
                "reglas": CHECK_RULES_VERSION,
                "segmento": segment_content_sha(segmento),
                "base": base_sha256,
                "otros": sorted(overlapping),
                "abiertos": 0,
            }
        )
    )


def verificar_chequeo(
    segmento: Mapping[str, Any],
    *,
    base: CommercialKnowledge,
    otros: Sequence[Mapping[str, Any]] = (),
) -> None:
    """El enchufe del cargador: lanza ChequeoError si un segmento aprobado no se puede cargar.

    Un ``borrador`` pasa (el cargador decide aparte si carga borradores). Un
    ``aprobado`` tiene que traer ``cabecera.chequeo_sha`` y tiene que ser el que da
    :func:`calcular_chequeo_sha` sobre el contenido actual, el base y los otros.
    """

    try:
        this = parse_segment(segmento)
    except SegmentCheckError as exc:
        raise ChequeoError(str(exc)) from exc
    if this.status != "aprobado":
        return
    if this.chequeo_sha is None:
        raise ChequeoError(
            f"el segmento {this.key} esta aprobado sin chequeo_sha: "
            "falta correr setter segmento chequear con 0 abiertos"
        )
    expected = calcular_chequeo_sha(segmento, base_sha256=base.rendered_sha256, otros=otros)
    if this.chequeo_sha != expected:
        raise ChequeoError(
            f"el chequeo_sha del segmento {this.key} no es el del contenido actual "
            "(cambio el segmento, el conocimiento base, otro segmento que se pisa o las reglas): "
            "hay que volver a correr setter segmento chequear"
        )


# ------------------------------------------------------------------- normalizacion


def _norm(text: str) -> str:
    """Minusculas, sin acentos y con los espacios colapsados."""

    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", plain.lower()).strip()


_NEGATIONS = {"no", "nunca", "ni", "sin", "jamas", "evita", "evitar", "evites", "prohibido"}


def _negated(normalized: str, start: int, lookback: int = 3) -> bool:
    """Si una de las ``lookback`` palabras anteriores a ``start`` es una negacion."""

    before = re.findall(r"[a-z0-9]+", normalized[:start])[-lookback:]
    return any(word in _NEGATIONS for word in before)


def _decimal(text: str) -> Decimal | None:
    cleaned = text.strip().replace(" ", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d{1,2})?", cleaned):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if match is None:
        return None
    try:
        return Decimal(match.group(0)).normalize()
    except InvalidOperation:
        return None


def _fmt_decimal(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


# ----------------------------------------------------------------------- extractor


@dataclass(frozen=True)
class Datum:
    """Un dato concreto sacado de un texto: tipo, valor normalizado y la cita."""

    kind: str
    value: str
    quote: str
    topic: str = ""


_CURRENCIES = {
    "usd": "USD",
    "us$": "USD",
    "u$s": "USD",
    "dolares": "USD",
    "dolar": "USD",
    "mxn": "MXN",
    "cop": "COP",
    "clp": "CLP",
    "pen": "PEN",
    "ars": "ARS",
    "eur": "EUR",
}
_CUR = r"(usd|us\$|u\$s|mxn|cop|clp|pen|ars|eur|dolares|dolar)"
_NUM = r"(\d{1,3}(?:[.,]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_AMOUNT_PATTERNS = (
    re.compile(_CUR + r"\s?\$?\s?" + _NUM + r"(?![\d%])"),
    re.compile(r"\$\s?" + _NUM + r"(?:\s?" + _CUR + r")?(?![\d%])"),
    re.compile(r"(?<![\w$])" + _NUM + r"\s?" + _CUR + r"\b"),
)
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s?(?:%|por ciento)")
_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
_DATE_WORDS = re.compile(
    r"\b(\d{1,2}) de (" + "|".join(_MONTHS) + r")(?: (?:de|del) (\d{4}))?\b"
)
_DATE_SLASH = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d/])")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_HOUR = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_NUMBER_WORDS = {
    "un": 1,
    "uno": 1,
    "una": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
    "quince": 15,
    "treinta": 30,
    "sesenta": 60,
    "noventa": 90,
}
_UNITS = {
    "dia": "dia",
    "dias": "dia",
    "hora": "hora",
    "horas": "hora",
    "semana": "semana",
    "semanas": "semana",
    "mes": "mes",
    "meses": "mes",
    "ano": "ano",
    "anos": "ano",
}
_DURATION = re.compile(
    r"\b(\d+|" + "|".join(_NUMBER_WORDS) + r")\s+(" + "|".join(_UNITS) + r")\b(?!\s+sin\s+intereses)"
)
_GUARANTEE_WORDS = re.compile(r"garanti|reembols|devoluci|devolvemos")
_MSI = re.compile(r"(?:meses|cuotas)\s+sin\s+intereses|\bmsi\b|son\s+sin\s+intereses")
_COUPON_SHAPE = re.compile(r"(?<![A-Za-z0-9])(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{5,}(?![A-Za-z0-9])")
_COUPON_KEYWORD = re.compile(r"(?:cup[oó]n|c[oó]digo)\s+(?:es\s+|de\s+descuento\s+)?[«\"'“]?([A-Z0-9]{4,})\b")
# Lista cerrada de medios de pago: nombre canonico -> patron sobre el texto normalizado.
PAYMENT_METHODS = {
    "tarjeta": r"\btarjetas?\b",
    "paypal": r"\bpaypal\b",
    "oxxo": r"\boxxo\b",
    "spei": r"\bspei\b",
    "mercado pago": r"\bmercado ?pago\b",
    "yape": r"\byape\b",
    "pagoefectivo": r"\bpago ?efectivo\b",
    "nequi": r"\bnequi\b",
    "bancolombia": r"\bbancolombia\b",
    "pse": r"\bpse\b",
    "efecty": r"\befecty\b",
    "mach": r"\bmach\b",
    "sencillito": r"\bsencillito\b",
    "servipag": r"\bservipag\b",
    "venmo": r"\bvenmo\b",
    "daviplata": r"\bdaviplata\b",
    "pay later": r"\bpay later\b|\bpagar despues\b",
}
_BONUS_CONTEXT = re.compile(r"bono|bonus|regalo|masterclass|clase|modulo|e-?book|guia|recetario|incluye")
_QUOTED = re.compile(r"[«“\"]([^«»“”\"]{3,80})[»”\"]")


def extract(text: str) -> list[Datum]:
    """Los datos concretos de un texto. Puro: el mismo texto da la misma lista."""

    data: list[Datum] = []
    normalized = _norm(text)

    taken: list[tuple[int, int]] = []
    for pattern in _AMOUNT_PATTERNS:
        for match in pattern.finditer(normalized):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in taken):
                continue
            taken.append(span)
            groups = [g for g in match.groups() if g]
            currency = next((_CURRENCIES[g] for g in groups if g in _CURRENCIES), None)
            amount = next((_decimal(g) for g in groups if g not in _CURRENCIES), None)
            if amount is None:
                continue
            value = f"{currency or '?'} {_fmt_decimal(amount)}"
            data.append(Datum("monto", value, match.group(0), "precio"))

    for match in _PERCENT.finditer(normalized):
        amount = _decimal(match.group(1))
        if amount is not None:
            data.append(Datum("porcentaje", _fmt_decimal(amount), match.group(0), "descuento"))

    for match in _DATE_WORDS.finditer(normalized):
        day, month, year = int(match.group(1)), _MONTHS[match.group(2)], match.group(3)
        data.append(Datum("fecha", _date_value(day, month, year), match.group(0), "vigencia"))
    for match in _DATE_SLASH.finditer(normalized):
        day, month, year = int(match.group(1)), int(match.group(2)), match.group(3)
        if 1 <= day <= 31 and 1 <= month <= 12:
            if year is not None and len(year) == 2:
                year = "20" + year
            data.append(Datum("fecha", _date_value(day, month, year), match.group(0), "vigencia"))
    for match in _DATE_ISO.finditer(normalized):
        data.append(
            Datum(
                "fecha",
                _date_value(int(match.group(3)), int(match.group(2)), match.group(1)),
                match.group(0),
                "vigencia",
            )
        )
    for match in _HOUR.finditer(normalized):
        data.append(Datum("hora", f"{int(match.group(1)):02d}:{match.group(2)}", match.group(0), "vigencia"))

    for match in _DURATION.finditer(normalized):
        window = normalized[max(0, match.start() - 60) : match.end() + 60]
        if not _GUARANTEE_WORDS.search(window):
            continue
        raw_number = match.group(1)
        number = int(raw_number) if raw_number.isdigit() else _NUMBER_WORDS[raw_number]
        data.append(Datum("duracion", f"{number} {_UNITS[match.group(2)]}", match.group(0), "garantia"))

    for match in _MSI.finditer(normalized):
        value = "no" if _negated(normalized, match.start(), lookback=4) else "si"
        data.append(Datum("msi", value, match.group(0), "pagos"))

    codes = {m.group(0) for m in _COUPON_SHAPE.finditer(text)}
    codes |= {m.group(1) for m in _COUPON_KEYWORD.finditer(text) if m.group(1).isupper()}
    for code in sorted(codes):
        data.append(Datum("cupon", code, code, "cupon"))

    for name, pattern in PAYMENT_METHODS.items():
        for match in re.finditer(pattern, normalized):
            if not _negated(normalized, match.start(), lookback=2):
                data.append(Datum("medio", name, match.group(0), "medios"))
                break

    for match in _QUOTED.finditer(text):
        window = _norm(text[max(0, match.start() - 80) : match.end() + 20])
        if _BONUS_CONTEXT.search(window):
            data.append(Datum("bono", _norm(match.group(1)), match.group(0), "incluye"))
    return data


def _date_value(day: int, month: int, year: str | None) -> str:
    return f"{year}-{month:02d}-{day:02d}" if year else f"--{month:02d}-{day:02d}"




# --------------------------------------------------------------- las preguntas fijas


@dataclass(frozen=True)
class FixedQuestion:
    key: str
    text: str
    field: str


# Las 12 preguntas del alta (E-310 §3.3). El juez las recibe como la fuente
# preguntas_fijas: cada una que el segmento no conteste es un hueco.
FIXED_QUESTIONS = (
    FixedQuestion("alta-01", "¿Desde y hasta cuándo va la campaña, con hora y zona?", "[vigencia].desde y .hasta"),
    FixedQuestion("alta-02", "¿Qué producto o productos entran en la campaña?", "[oferta].producto"),
    FixedQuestion("alta-03", "¿Cuál es el precio y la moneda de la campaña? ¿Y el precio de antes?", "[oferta].precio y .moneda (y .precio_antes)"),
    FixedQuestion("alta-04", "¿Cómo se obtiene el precio: con un cupón (qué código) o con una oferta distinta (qué link)?", "[oferta].cupon o [oferta].codigo_oferta"),
    FixedQuestion("alta-05", "¿Qué incluye, y hay bonos que existen solo en la campaña?", "[oferta].incluye"),
    FixedQuestion("alta-06", "¿Cambia la garantía en la campaña?", "[oferta].garantia (y reemplaza si cambia)"),
    FixedQuestion("alta-07", "¿Qué medios de pago hay en la campaña, y hay meses sin intereses?", "[oferta].medios"),
    FixedQuestion(
        "alta-08",
        "¿La campaña se combina con el cupón del carrito (el cupón personal o el código del carrito)?",
        "[[faq]] o [[preguntas_del_alta]]",
    ),
    FixedQuestion(
        "alta-09",
        "¿La campaña aplica a quien ya es clienta? ¿Qué se le dice?",
        "[[faq]] o [[preguntas_del_alta]]",
    ),
    FixedQuestion(
        "alta-10",
        "¿Qué se le dice a quien escribe después de que termina la campaña?",
        "[[faq]] o [[preguntas_del_alta]]",
    ),
    FixedQuestion("alta-11", "¿Qué preguntas de asesoramiento esperan, y qué se recomienda en cada caso?", "[asesoramiento].items"),
    FixedQuestion(
        "alta-12",
        "¿Quién entra al segmento: qué flujo o qué acción del equipo pone la etiqueta?",
        "un flujo con etiquetar, o [[preguntas_del_alta]]",
    ),
)


@dataclass(frozen=True)
class Template:
    name: str
    text: str


@dataclass(frozen=True)
class LabelingFlow:
    """Un flujo que pone la etiqueta del segmento, con las plantillas que manda."""

    where: str
    turned_on: bool
    templates: tuple[str, ...]


# ------------------------------------------------------------- lo que lee la CLI


def find_labeling_flows(flows_dir: Path, label: str) -> list[LabelingFlow]:
    """Los flujos de ``flows_dir`` (setter-flujo/v1) que tienen un paso etiquetar = label.

    Si hay un ``indice.json`` (flujos-publicados/v1), mira solo la version activa de cada
    flujo y toma su estado; si no, todos los .json, como apagados.
    """

    index_path = flows_dir / "indice.json"
    candidates: list[tuple[Path, bool]] = []
    if index_path.is_file():
        index = _read_json(index_path)
        for entry in index.get("flujos", []):
            active = entry.get("version_activa")
            for version in entry.get("versiones", []):
                archivo = version.get("archivo")
                if version.get("version") == active and isinstance(archivo, str):
                    # El indice nombra el archivo desde flujos/ («publicados/att1/...»).
                    relative = archivo.removeprefix(f"{flows_dir.name}/")
                    candidates.append((flows_dir / relative, entry.get("estado") == "prendido"))
    else:
        candidates = [(path, False) for path in sorted(flows_dir.rglob("*.json"))]
    found = []
    for path, on in candidates:
        if not path.is_file():
            continue
        definition = _read_json(path)
        steps = list(_walk_steps(definition.get("pasos", [])))
        if any(step.get("etiquetar") == label for step in steps):
            templates = tuple(step["plantilla"] for step in steps if isinstance(step.get("plantilla"), str))
            found.append(LabelingFlow(f"flujo {path.relative_to(flows_dir).as_posix()}", on, templates))
    return found


def _walk_steps(steps: Any) -> Iterable[Mapping[str, Any]]:
    if not isinstance(steps, list):
        return
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        yield step
        for branch in ("entonces", "si_no"):
            yield from _walk_steps(step.get(branch))


def read_template_catalog(path: Path) -> dict[str, Template]:
    """El catalogo exportado de Chatwoot (message_templates): nombre -> texto, sin los ejemplos."""

    payload = _read_json(path)
    raw = payload.get("message_templates", payload) if isinstance(payload, Mapping) else payload
    out: dict[str, Template] = {}
    for template in raw if isinstance(raw, list) else []:
        parts = []
        for component in template.get("components", []):
            if isinstance(component.get("text"), str):
                parts.append(component["text"])
            for button in component.get("buttons", []) or []:
                if isinstance(button.get("text"), str):
                    parts.append(button["text"])
        out[str(template.get("name"))] = Template(str(template.get("name")), "\n".join(parts))
    return out


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SegmentCheckError(f"no se pudo leer {path}: {exc}") from exc


@dataclass(frozen=True)
class HarnessCase:
    """Un caso del arnes que nombra al segmento: su id y su texto entero (espera y turnos)."""

    case_id: str
    text: str


def read_cases(path: Path, key: str) -> list[HarnessCase]:
    payload = _read_json(path)
    cases = payload if isinstance(payload, list) else payload.get("casos", [])
    return [
        HarnessCase(str(case.get("id")), json.dumps(case, ensure_ascii=False, indent=1))
        for case in cases
        if isinstance(case, Mapping) and key in (case.get("segmentos") or [])
    ]


# ------------------------------------------------------------------- las fuentes


@dataclass(frozen=True)
class Source:
    """Una fuente que lee el juez. ``source_id`` es lo que el juez cita."""

    source_id: str
    title: str
    text: str


@dataclass(frozen=True)
class Answered:
    """Una cuestion que el equipo ya contesto: una entrada de ``[[preguntas_del_alta]]``
    cuya ``pregunta`` empieza con ``[clave]``."""

    key: str
    question: str
    answer: str
    source: str

    @property
    def core(self) -> str:
        """La clave sin el tipo ni la huella (``c-cupon-x-55810e78`` → ``cupon-x``): el slug
        que el juez le pondria si la volviera a preguntar."""

        core = re.sub(r"^[chd]-", "", self.key)
        return re.sub(r"-[0-9a-f]{8}x*$", "", core)


_ANSWER_KEY = re.compile(r"\[([a-z0-9][a-z0-9-]{0,127})\]\s*")


def resolved_answers(segment: "Segment") -> list[Answered]:
    """Las respuestas del equipo que llevan su clave. Una sin clave sigue en el segmento,
    pero no se puede reabrir por nombre."""

    out = []
    for question, answer, source in segment.answers:
        match = _ANSWER_KEY.match(question)
        if match:
            out.append(Answered(match.group(1), question[match.end():].strip(), answer, source))
    return out


def build_sources(
    *,
    segment_text: str,
    segment: "Segment",
    base: CommercialKnowledge,
    soul_text: str,
    others: Sequence[tuple[str, str]] = (),
    templates: Mapping[str, Template] | None = None,
    flows: Sequence[LabelingFlow] | None = None,
    cases: Sequence[HarnessCase] | None = None,
    now: datetime,
) -> list[Source]:
    """Las fuentes del juez, en un orden fijo. ``others`` son (clave, texto TOML) de los
    otros segmentos cuyas vigencias se pisan con este."""

    sources = [
        Source("segmento", f"El segmento a chequear ({segment.key} v{segment.version}), el archivo TOML entero", segment_text),
    ]
    answered = resolved_answers(segment)
    if answered:
        sources.append(
            Source(
                "respuestas",
                "Las cuestiones que el equipo ya contesto en [[preguntas_del_alta]], con su clave: estan resueltas",
                "\n\n".join(f"[{a.key}] Pregunta: {a.question}\nRespuesta: {a.answer}\nFuente: {a.source}" for a in answered),
            )
        )
    sources += [
        Source("soul", "El SOUL comun del agente comercial (las reglas que el segmento no puede pisar)", soul_text),
        Source("base", f"El conocimiento base de la aliada, v{base.version}, tal como lo recibe el agente", base.render()),
    ]
    for key, text in others:
        sources.append(Source(f"segmento:{key}", f"Otro segmento vigente a la vez ({key})", text))
    context_lines = [
        f"Fecha y hora del chequeo: {now.isoformat()}.",
        f"Vigencia del segmento: desde {segment.starts.isoformat()} hasta {segment.ends.isoformat()}.",
    ]
    if segment.coupon:
        context_lines.append(
            f"oferta.cupon = {segment.coupon} es el cupon de esta campana. oferta.reemplaza solo admite "
            f"{', '.join(REPLACEABLE_FIELDS)}: el cupon no se nombra ahi, y no hace falta."
        )
    if flows is None:
        context_lines.append("No se buscaron los flujos que ponen la etiqueta del segmento: no se sabe quien entra.")
    elif not flows:
        context_lines.append("Ningun flujo publicado pone la etiqueta del segmento.")
    for flow in flows or ():
        state = "prendido" if flow.turned_on else "apagado"
        context_lines.append(
            f"El {flow.where} esta {state}, pone la etiqueta del segmento y manda las plantillas: "
            + (", ".join(flow.templates) or "ninguna")
            + "."
        )
        for name in flow.templates:
            if templates is None or name not in templates:
                context_lines.append(f"La plantilla {name} no esta en el catalogo exportado: su texto no se pudo leer.")
    if cases is None:
        context_lines.append("No se leyeron los casos del arnes.")
    elif not cases:
        context_lines.append("El arnes no tiene casos que nombren a este segmento.")
    sources.append(Source("contexto", "Lo que el sistema sabe del alta (flujos, plantillas y casos)", "\n".join(context_lines)))
    for flow in flows or ():
        for name in flow.templates:
            if templates is not None and name in templates and not any(s.source_id == f"plantilla:{name}" for s in sources):
                sources.append(Source(f"plantilla:{name}", f"Plantilla de WhatsApp {name}, de un flujo que pone la etiqueta", templates[name].text))
    for case in cases or ():
        sources.append(Source(f"caso:{case.case_id}", f"Caso del arnes {case.case_id}", case.text))
    sources.append(
        Source(
            "preguntas_fijas",
            "Las preguntas que todo segmento tiene que contestar antes del alta",
            "\n".join(f"{q.key}: {q.text}" for q in FIXED_QUESTIONS),
        )
    )
    return sources


def extracted_hints(sources: Sequence[Source]) -> str:
    """Los datos que encuentra el extractor, por fuente. Insumo para el juez, no decision."""

    lines = []
    for source in sources:
        if source.source_id in ("soul", "preguntas_fijas", "contexto", "respuestas"):
            continue
        data = sorted({f"{d.kind} {d.quote}" for d in extract(source.text) if d.kind != "bono"})
        if data:
            lines.append(f"- {source.source_id}: " + "; ".join(data))
    return "\n".join(lines)


# ---------------------------------------------------------------------- el juez


JUDGE_INSTRUCTIONS = """Sos el revisor del alta de un segmento de conocimiento de un agente comercial de WhatsApp.

Un segmento es un bloque extra (una campana, por ejemplo) que el agente recibe, ademas del conocimiento base y del SOUL, cuando la conversacion tiene una etiqueta y el segmento esta vigente. Donde el segmento dice algo distinto del conocimiento base, manda el segmento, pero solo si el cambio es a proposito: un campo de [oferta] de los que se reemplazan (__REEMPLAZABLES__) que cambia frente al base tiene que estar en [oferta].reemplaza. Los otros campos de [oferta] (cupon, precio_antes, producto) no se pueden nombrar en reemplaza y no hace falta: son de la campana. oferta.cupon es el cupon de la campana: que exista, o que sea distinto de un cupon del base, no es un item. oferta.codigo_oferta es la oferta de Hotmart de la campana: que exista, o que sea distinta de la del base, no es un item; el link de esa campana lo arma el sistema con esa oferta. Si es un item que un texto del segmento ofrezca otro cupon, nombre como el de la campana uno distinto del de oferta.cupon, o diga que se suma con otro cuando el base o el segmento dicen que no se suman. El SOUL no lo puede pisar ningun segmento.

Tu trabajo es encontrar todo lo que pueda hacer que el agente diga algo falso, contradictorio o no confirmado a una persona de este segmento, antes de que entre en produccion. Nada entra con algo abierto, asi que lo que no tengas claro no lo das por bueno: lo preguntas.

Devuelve tres tipos de item:
- "contradiccion": dos fuentes (o dos partes de la misma) dicen cosas que no pueden ser verdad a la vez para una persona de este segmento. Por ejemplo: otro precio, moneda, garantia, cupon, fecha, medio de pago o bono; un campo de [oferta] distinto del base sin estar en reemplaza; algo que el base tiene en promesas prohibidas o no confirmado y el segmento afirma; algo del segmento que va contra una regla del SOUL (escribir una URL o el link, no derivar, prometer, pedir datos de pago, descuentos sin cupon, dosis o diagnosticos, mas de una pregunta por respuesta); una plantilla de un flujo que pone la etiqueta que dice otra cosa que el segmento; otro segmento vigente a la vez que dice otra cosa; un flujo prendido que pone la etiqueta fuera de la vigencia.
- "hueco": falta algo que el agente va a necesitar. Cada pregunta de la fuente preguntas_fijas que el segmento no contesta (ni con un campo, ni con una [[faq]], ni con una respuesta en [[preguntas_del_alta]]) es un hueco. Tambien lo es un caso del arnes cuya respuesta esperada no sale del segmento ni del base, y una plantilla que no se pudo leer.
- "duda": algo que no esta claro y que puede estar bien o mal; una frase ambigua; algo que el agente podria entender de dos maneras. Si dudas entre si algo es un problema o no, es una duda.

Antes de marcar un item de redaccion o de tratamiento, comproba que el texto esta mal: «dile que escriba» o «pidele que elija» son subjuntivo de tu, no de usted.

Las respuestas del equipo estan en [[preguntas_del_alta]] del segmento, y repetidas en la fuente respuestas con su clave entre corchetes. Una cuestion respondida ahi esta resuelta: no la vuelvas a preguntar, ni con otras palabras, ni desde otro campo o fuente, ni como otro tipo de item, aunque prefieras otra forma de resolverla. Es la misma cuestion si la respuesta ya contesta lo que preguntarias. Solo se reabre si la respuesta no la resuelve, es ambigua, o un texto del segmento o de otra fuente dice otra cosa que la respuesta: entonces el item lleva en "reabre" la clave de esa respuesta y cita las dos cosas, un fragmento de la respuesta y el texto, fuera de [[preguntas_del_alta]], que la contradice o que queda sin contestar. Un item que reabre una respuesta sin citar las dos cosas no se cuenta. Un item que no reabre ninguna respuesta lleva "reabre": "".

Reglas de la salida:
1. Cada item cita textualmente, de cada fuente que lo origina, el fragmento exacto: copialo caracter por caracter de la fuente, sin resumir ni corregir, de una frase o menos. Una cita que no aparece tal cual en su fuente se descarta. Las fuentes se nombran por su id (segmento, respuestas, soul, base, contexto, preguntas_fijas, segmento:<clave>, plantilla:<nombre>, caso:<id>). Un hueco de una pregunta fija cita esa pregunta de preguntas_fijas.
2. Cada item lleva una pregunta para el equipo, en espanol neutro, cerrada si se puede, que se pueda mandar sola por Slack, y el campo del segmento que se va a escribir con la respuesta.
3. Los datos que encontro el extractor son una ayuda para no pasar por alto un numero: pueden estar mal, y no son una fuente que se cita.
4. Si no encontras nada, devuelve items vacio. Solo si de verdad no hay nada que preguntar.

Responde SOLO un objeto JSON, sin texto alrededor, con esta forma:
{"items": [{"tipo": "contradiccion" | "hueco" | "duda", "clave": "slug-corto-en-minusculas", "titulo": "una linea", "citas": [{"fuente": "<id>", "texto": "<fragmento literal>"}], "pregunta": "...", "campo": "...", "reabre": "" | "<clave de la respuesta>"}]}""".replace(
    "__REEMPLAZABLES__", ", ".join(REPLACEABLE_FIELDS)
)


def build_messages(sources: Sequence[Source]) -> list[dict[str, str]]:
    blocks = []
    for source in sources:
        blocks.append(f'<fuente id="{source.source_id}" titulo="{source.title}">\n{source.text}\n</fuente>')
    hints = extracted_hints(sources)
    user = "\n\n".join(blocks)
    if hints:
        user += "\n\nDatos que encontro el extractor en cada fuente (insumo, puede tener errores):\n" + hints
    user += "\n\nDevuelve el JSON."
    return [
        {"role": "system", "content": JUDGE_INSTRUCTIONS},
        {"role": "user", "content": user},
    ]


@dataclass(frozen=True)
class JudgeAnswer:
    model: str
    content: str


Judge = Callable[[list[dict[str, str]]], JudgeAnswer]


class JudgeError(RuntimeError):
    """El juez no se pudo correr (sin clave, error de red o de OpenRouter)."""


def openrouter_judge(
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
    temperature: float = 0.4,
) -> Judge:
    """El juez por OpenRouter. La clave sale de ``api_key`` o de la variable OPENROUTER_API_KEY,
    nunca de la linea de comandos, y no se imprime."""

    key = api_key if api_key is not None else os.environ.get(API_KEY_VARIABLE)
    if not key:
        raise JudgeError(f"falta la variable {API_KEY_VARIABLE} en el entorno (la clave no va en la linea de comandos)")

    def judge(messages: list[dict[str, str]]) -> JudgeAnswer:
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        try:
            with httpx.Client(timeout=_TIMEOUT, transport=transport) as client:
                response = client.post(
                    OPENROUTER_URL,
                    json=body,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise JudgeError(f"OpenRouter no respondio: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise JudgeError(f"OpenRouter respondio HTTP {response.status_code}")
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise JudgeError("OpenRouter respondio sin choices[0].message.content") from None
        return JudgeAnswer(str(data.get("model") or model), str(content or ""))

    return judge


def recorded_judge(answers: Sequence[str], model: str = "grabado") -> Judge:
    """Un juez que devuelve respuestas grabadas, en orden: para las pruebas sin red."""

    queue = list(answers)

    def judge(messages: list[dict[str, str]]) -> JudgeAnswer:
        if not queue:
            raise JudgeError("se acabaron las respuestas grabadas")
        return JudgeAnswer(model, queue.pop(0))

    return judge


# ------------------------------------------------------------- validar la salida


ITEM_KINDS = {"contradiccion": "C", "hueco": "H", "duda": "D"}
_ITEM_KEYS = {"tipo", "clave", "titulo", "citas", "pregunta", "campo"}
# ``reabre`` es de las reglas v2; una salida sin ese campo (las grabadas con v1) se sigue leyendo.
_ITEM_OPTIONAL_KEYS = {"reabre"}


def _plain(text: str) -> str:
    """Para comparar citas: NFC, comillas y guiones unificados, espacios colapsados."""

    text = unicodedata.normalize("NFC", text)
    text = text.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "–": "-", "—": "-", " ": " "}))
    return re.sub(r"\s+", " ", text).strip()


def _json_object(content: str) -> Any:
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)


@dataclass
class Citation:
    source: str
    text: str


@dataclass
class Item:
    kind: str  # C, H o D
    key: str
    title: str
    citations: list[Citation]
    question: str
    field: str
    runs: list[int]
    number: str = ""
    discarded: list[Citation] = field(default_factory=list)
    reopens: str = ""  # la clave de la respuesta del equipo que este item reabre
    stop_reason: str | None = None  # por que frena, si corrio una compuerta y frena

    @property
    def verified(self) -> bool:
        return bool(self.citations)

    def slack(self) -> str:
        lines = [f"*{self.number}* · {self.title}"]
        lines += [f"> {c.source}: «{c.text}»" for c in self.citations]
        lines.append(self.question)
        lines.append(f"(con la respuesta se escribe: {self.field})")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "numero": self.number,
            "clave": self.key,
            "tipo": {"C": "contradiccion", "H": "hueco", "D": "duda"}[self.kind],
            "titulo": self.title,
            "citas": [{"fuente": c.source, "texto": c.text} for c in self.citations],
            "citas_descartadas": [{"fuente": c.source, "texto": c.text} for c in self.discarded],
            "sin_cita_verificable": not self.verified,
            "pregunta": self.question,
            "campo": self.field,
            "reabre": self.reopens,
            "corridas": list(self.runs),
            "para_registrar": f"[{self.key}] {self.question}",
            "mensaje_slack": self.slack(),
        }


@dataclass
class Run:
    number: int
    model: str
    valid: bool
    items: int
    error: str | None = None


def parse_judge_output(content: str, sources: Sequence[Source], run: int) -> list[Item]:
    """Valida la salida del juez. Lanza ValueError si la forma no es la pedida; las citas
    que no aparecen literalmente en su fuente se separan como descartadas."""

    payload = _json_object(content)
    if not isinstance(payload, dict) or set(payload) != {"items"} or not isinstance(payload["items"], list):
        raise ValueError('la salida no es {"items": [...]}')
    by_id = {source.source_id: _plain(source.text) for source in sources}
    items = []
    for index, raw in enumerate(payload["items"]):
        if not isinstance(raw, dict) or not _ITEM_KEYS <= set(raw) <= _ITEM_KEYS | _ITEM_OPTIONAL_KEYS:
            raise ValueError(f"items[{index}] no tiene exactamente {', '.join(sorted(_ITEM_KEYS))} (y reabre)")
        reopens = raw.get("reabre", "")
        if not isinstance(reopens, str):
            raise ValueError(f"items[{index}].reabre tiene que ser un texto (vacio si no reabre nada)")
        if raw["tipo"] not in ITEM_KINDS:
            raise ValueError(f"items[{index}].tipo no es contradiccion, hueco ni duda")
        for name in ("clave", "titulo", "pregunta", "campo"):
            if not isinstance(raw[name], str) or not raw[name].strip():
                raise ValueError(f"items[{index}].{name} tiene que ser un texto no vacio")
        if not isinstance(raw["citas"], list) or not raw["citas"]:
            raise ValueError(f"items[{index}].citas tiene que ser una lista no vacia")
        good, bad = [], []
        for cite in raw["citas"]:
            if not isinstance(cite, dict) or set(cite) != {"fuente", "texto"} or not all(
                isinstance(cite[k], str) for k in ("fuente", "texto")
            ):
                raise ValueError(f"items[{index}].citas lleva objetos {{fuente, texto}}")
            citation = Citation(cite["fuente"].strip(), cite["texto"].strip())
            haystack = by_id.get(citation.source)
            needle = _plain(citation.text)
            (good if haystack is not None and needle and needle in haystack else bad).append(citation)
        slug = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", raw["clave"]).encode("ascii", "ignore").decode().lower()).strip("-")[:40] or "item"
        items.append(
            Item(
                kind=ITEM_KINDS[raw["tipo"]],
                key=slug,
                title=raw["titulo"].strip(),
                citations=good,
                question=raw["pregunta"].strip(),
                field=raw["campo"].strip(),
                runs=[run],
                discarded=bad,
                reopens=reopens.strip().strip("[]").strip(),
            )
        )
    return items


def gate_reopened(items: Sequence[Item], answered: Sequence[Answered]) -> tuple[list[Item], list[str]]:
    """Una cuestion que el equipo ya contesto no vuelve a abrirse sola. Un item reabre una
    respuesta si lo dice en ``reabre`` o si el juez le pone el mismo slug que tenia la
    pregunta contestada. Para quedar abierto tiene que citar (verificado) un fragmento de
    esa respuesta y un texto que no este en ninguna entrada de [[preguntas_del_alta]]: lo
    que la contradice o lo que queda sin contestar. Si no, sale como aviso y no cuenta.
    Devuelve (los items que siguen, los avisos)."""

    by_key = {a.key: a for a in answered}
    by_core = {a.core: a for a in answered}
    team_texts = [_plain(text) for a in answered for text in (a.question, a.answer)]
    kept: list[Item] = []
    notes: list[str] = []
    for item in items:
        target = by_key.get(item.reopens) or by_core.get(item.key)
        if target is None:
            kept.append(item)
            continue
        item.reopens = target.key
        answer = _plain(target.answer)
        cites_answer = any(_plain(c.text) in answer for c in item.citations)
        cites_new = any(not any(_plain(c.text) in text for text in team_texts) for c in item.citations if c.source != "respuestas")
        if cites_answer and cites_new:
            kept.append(item)
            continue
        missing = " y ".join(
            part for part, ok in (("un fragmento de la respuesta", cites_answer), ("un texto que la contradiga", cites_new)) if not ok
        )
        notes.append(
            f"corrida {item.runs[0]}: el juez volvio a preguntar [{target.key}], que el equipo ya contesto, "
            f"sin citar {missing} («{item.title}»): no cuenta como abierto"
        )
    return kept, notes


def merge_items(runs: Sequence[list[Item]]) -> list[Item]:
    """Junta los items de varias corridas. Dos items se juntan si son del mismo tipo y
    comparten una cita verificada; si no, quedan los dos. Ninguno se pierde."""

    merged: list[Item] = []
    for items in runs:
        for item in items:
            cites = {(c.source, _plain(c.text)) for c in item.citations}
            twin = next(
                (
                    other
                    for other in merged
                    if other.kind == item.kind and cites & {(c.source, _plain(c.text)) for c in other.citations}
                ),
                None,
            )
            if twin is None:
                merged.append(item)
                continue
            twin.runs = sorted(set(twin.runs) | set(item.runs))
            known = {(c.source, _plain(c.text)) for c in twin.citations}
            twin.citations += [c for c in item.citations if (c.source, _plain(c.text)) not in known]
            twin.discarded += item.discarded
    order = {"C": 0, "H": 1, "D": 2}
    merged.sort(key=lambda item: (order[item.kind], -len(item.runs), item.key))
    counters = {"C": 0, "H": 0, "D": 0}
    used: set[str] = set()
    for item in merged:
        counters[item.kind] += 1
        item.number = f"{item.kind}{counters[item.kind]}"
        # La clave lleva la huella de sus citas: estable si el juez cita lo mismo.
        digest = hashlib.sha256(
            "\x1f".join(sorted(f"{c.source}\x1e{_plain(c.text)}" for c in item.citations) or [item.title]).encode()
        ).hexdigest()[:8]
        key = f"{item.kind.lower()}-{item.key}-{digest}"
        while key in used:
            key += "x"
        used.add(key)
        item.key = key
    return merged


# ---------------------------------------------------------------------- el reporte


@dataclass
class Report:
    segment: "Segment"
    base: CommercialKnowledge
    items: list[Item]
    runs: list[Run]
    requested_runs: int
    warnings: list[str]
    sources: dict[str, Any]
    chequeo_sha: str | None
    notices: list[Item] = field(default_factory=list)  # con compuerta: lo que el juez dijo y no frena

    @property
    def valid_runs(self) -> int:
        return sum(1 for run in self.runs if run.valid)

    @property
    def complete(self) -> bool:
        return self.valid_runs == self.requested_runs

    @property
    def clean(self) -> bool:
        return self.complete and not self.items

    def to_json(self) -> dict[str, Any]:
        return {
            "esquema": REPORT_SCHEMA,
            "reglas": CHECK_RULES_VERSION,
            "segmento": {
                "clave": self.segment.key,
                "version": self.segment.version,
                "estado": self.segment.status,
                "contenido_sha256": segment_content_sha(self.segment.raw),
                "vigencia": {"desde": self.segment.starts.isoformat(), "hasta": self.segment.ends.isoformat()},
                "chequeo_sha_en_cabecera": self.segment.chequeo_sha,
                "respuestas_del_equipo": len(self.segment.answers),
            },
            "fuentes": self.sources,
            "corridas": [
                {"numero": r.number, "modelo": r.model, "valida": r.valid, "items": r.items, "error": r.error}
                for r in self.runs
            ],
            "corridas_pedidas": self.requested_runs,
            "abiertos": len(self.items),
            "items": [item.to_json() for item in self.items],
            "avisos": list(self.warnings),
            "chequeo_sha": self.chequeo_sha,
        }

    def to_markdown(self) -> str:
        seg = self.segment
        lines = [
            f"# Chequeo del alta · segmento {seg.key} v{seg.version}",
            "",
            f"- Estado del archivo: {seg.status}. Vigencia: {seg.starts.isoformat()} → {seg.ends.isoformat()}.",
            f"- Conocimiento base: v{self.base.version}, sha256 {self.base.rendered_sha256[:12]}.",
            f"- Juez: {', '.join(sorted({r.model for r in self.runs})) or '-'}; corridas válidas "
            f"{self.valid_runs} de {self.requested_runs}. Abiertos: **{len(self.items)}**.",
        ]
        if self.chequeo_sha:
            lines += ["", "**Limpio.** Para la cabecera del segmento:", "", "```toml", f'chequeo_sha = "{self.chequeo_sha}"', "```"]
        elif not self.complete:
            lines += ["", "**Sin veredicto:** no corrieron todas las corridas. Hay que volver a correrlo."]
        if self.items:
            lines += [
                "",
                "## Para preguntar al equipo",
                "",
                "Una pregunta por mensaje. Con la respuesta: se corrige el archivo si hace falta y se registra en "
                "`[[preguntas_del_alta]]` (`pregunta` empieza con la clave entre corchetes, `respuesta`, "
                '`fuente = "Slack, <quien>, <fecha>"`). La corrida siguiente la lee.',
            ]
            for item in self.items:
                lines += ["", f"### {item.number} · `{item.key}` · corridas {', '.join(map(str, item.runs))}", "", item.slack()]
                if not item.verified:
                    lines.append("_Ninguna cita de este item aparece en las fuentes: el juez puede haberlo inventado. Se pregunta igual._")
                for c in item.discarded:
                    lines.append(f"_Cita descartada (no está en {c.source}): «{c.text}»_")
        failed = [r for r in self.runs if not r.valid]
        if failed:
            lines += ["", "## Corridas que fallaron", ""] + [f"- corrida {r.number} ({r.model}): {r.error}" for r in failed]
        if self.warnings:
            lines += ["", "## Avisos", ""] + [f"- {w}" for w in self.warnings]
        return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class GateContext:
    """Lo que una compuerta puede mirar, ademas de las citas del item."""

    segment: Segment
    segment_text: str
    base: CommercialKnowledge


Gate = Callable[[Item, GateContext], "str | None"]


def chequear(
    segmento_texto: str,
    *,
    base: CommercialKnowledge,
    soul_texto: str,
    juez: Judge,
    otros: Sequence[str] = (),
    plantillas: Mapping[str, Template] | None = None,
    flujos: Sequence[LabelingFlow] | None = None,
    casos: Sequence[HarnessCase] | None = None,
    corridas: int = DEFAULT_RUNS,
    ahora: datetime | None = None,
    fuentes: Mapping[str, Any] | None = None,
    compuerta: Gate | None = None,
) -> Report:
    """El chequeo entero, sin leer archivos: el juez corre ``corridas`` veces sobre las mismas
    fuentes. ``otros`` son los textos TOML de los otros segmentos declarados.

    Con ``compuerta`` (la usa el comando de una campana), ``items`` son los que frenan y el resto
    va a ``notices``. Sin compuerta, todo item frena: el alta por archivo sigue igual."""

    if corridas < 1:
        raise SegmentCheckError("corridas tiene que ser 1 o mas")
    try:
        raw = tomllib.loads(segmento_texto)
    except tomllib.TOMLDecodeError as exc:
        raise SegmentCheckError(f"el segmento no es TOML valido: {exc}") from exc
    seg = parse_segment(raw)
    others_raw = []
    overlapping: list[tuple[str, str]] = []
    for text in otros:
        try:
            other_raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise SegmentCheckError(f"otro segmento no es TOML valido: {exc}") from exc
        other = parse_segment(other_raw)
        others_raw.append(other_raw)
        if other.key != seg.key and seg.overlaps(other):
            overlapping.append((other.key, text))
    now = ahora or datetime.now(timezone.utc)
    warnings: list[str] = []
    if seg.ally_ref != base.ally_ref:
        warnings.append(f"el segmento es de '{seg.ally_ref}' y el conocimiento base de '{base.ally_ref}'")
    sources = build_sources(
        segment_text=segmento_texto,
        segment=seg,
        base=base,
        soul_text=soul_texto,
        others=overlapping,
        templates=plantillas,
        flows=flujos,
        cases=casos,
        now=now,
    )
    messages = build_messages(sources)
    answered = resolved_answers(seg)
    runs: list[Run] = []
    found: list[list[Item]] = []
    for number in range(1, corridas + 1):
        last_error = None
        model = "-"
        # Un reintento si la salida no tiene la forma pedida; un error de red no se reintenta.
        for _attempt in range(2):
            try:
                answer = juez(messages)
            except JudgeError as exc:
                last_error = str(exc)
                break
            model = answer.model
            try:
                items = parse_judge_output(answer.content, sources, number)
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = f"salida invalida: {exc}"
                continue
            runs.append(Run(number, model, True, len(items)))
            items, reopened = gate_reopened(items, answered)
            warnings += reopened
            found.append(items)
            last_error = None
            break
        if last_error is not None:
            runs.append(Run(number, model, False, 0, last_error))
    items = merge_items(found)
    discarded = sum(len(item.discarded) for item in items)
    if discarded:
        warnings.append(f"se descartaron {discarded} citas que no aparecen literalmente en su fuente")
    notices: list[Item] = []
    if compuerta is not None:
        gate_context = GateContext(seg, segmento_texto, base)
        stopping: list[Item] = []
        for item in items:
            item.stop_reason = compuerta(item, gate_context)
            (stopping if item.stop_reason else notices).append(item)
        items = stopping
    report_sources = dict(fuentes or {})
    report_sources.setdefault("base", {"version": base.version, "sha256": base.rendered_sha256})
    report_sources["leidas_por_el_juez"] = [s.source_id for s in sources]
    report = Report(seg, base, items, runs, corridas, warnings, report_sources, None, notices)
    if report.clean:
        report.chequeo_sha = calcular_chequeo_sha(raw, base_sha256=base.rendered_sha256, otros=others_raw)
        if seg.chequeo_sha is not None and seg.chequeo_sha != report.chequeo_sha:
            warnings.append("la cabecera trae un chequeo_sha que no es el de este contenido: reemplazalo por el de este reporte")
    elif seg.chequeo_sha is not None:
        warnings.append("la cabecera trae un chequeo_sha pero el chequeo no salio limpio: el cargador lo va a rechazar")
    return report


def check_files(
    segment_path: Path,
    *,
    judge: Judge,
    instance_dir: Path | None = None,
    soul_path: Path | None = None,
    catalog_path: Path | None = None,
    cases_path: Path | None = None,
    flows_dir: Path | None = None,
    runs: int = DEFAULT_RUNS,
    now: datetime | None = None,
    label: str | None = None,
    compuerta: Gate | None = None,
) -> Report:
    """Arma las fuentes desde la carpeta de la instancia y corre :func:`chequear`.

    ``label`` es la etiqueta de Chatwoot del segmento cuando no esta declarado en el manifiesto
    (una campana de la base): con ella se buscan los flujos que la ponen. Sin ``label``, la del
    manifiesto y si no, la clave."""

    try:
        segment_text = segment_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SegmentCheckError(f"no se pudo leer {segment_path}") from exc
    segment = read_segment_file(segment_path)
    expected_name = f"{segment.key}-v{segment.version}.toml"
    if segment_path.name != expected_name:
        # El cargador de PR-A no carga un archivo con otro nombre (D-E311-6).
        raise SegmentCheckError(f"el archivo se tiene que llamar {expected_name}, no {segment_path.name}")
    if instance_dir is None:
        parents = segment_path.resolve().parents
        guess = parents[2] if len(parents) > 2 else None
        if guess is not None and (guess / "instancia.toml").is_file():
            instance_dir = guess
    if instance_dir is None:
        raise SegmentCheckError("falta la carpeta de la instancia (--instancia): ahi esta el conocimiento base")
    try:
        manifest = tomllib.loads((instance_dir / "instancia.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SegmentCheckError(f"no se pudo leer {instance_dir / 'instancia.toml'}: {exc}") from exc
    agent = manifest.get("agente", {}) if isinstance(manifest.get("agente"), Mapping) else {}
    knowledge_rel = agent.get("conocimiento")
    if not isinstance(knowledge_rel, str):
        raise SegmentCheckError("instancia.toml no tiene agente.conocimiento")
    try:
        base = CommercialKnowledge.from_toml_file(instance_dir / knowledge_rel, require_approved=False)
    except KnowledgeError as exc:
        raise SegmentCheckError(f"{knowledge_rel}: {exc}") from exc

    declared = [entry for entry in agent.get("segmentos", []) if isinstance(entry, Mapping)]
    mine = next((entry for entry in declared if entry.get("clave") == segment.key), None)
    if label is None:
        label = str(mine.get("etiqueta")) if mine and mine.get("etiqueta") else segment.key
    others: list[str] = []
    for entry in declared:
        if entry.get("clave") == segment.key or not isinstance(entry.get("conocimiento"), str):
            continue
        path = instance_dir / entry["conocimiento"]
        try:
            others.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise SegmentCheckError(f"no se pudo leer {path}") from exc
    sources: dict[str, Any] = {
        "segmento": {"archivo": str(segment_path)},
        "base": {"archivo": knowledge_rel, "version": base.version, "sha256": base.rendered_sha256},
    }

    if soul_path is None:
        soul_path = Path(__file__).resolve().parents[2] / SOUL_RELATIVE_PATH
    try:
        soul_bytes = soul_path.read_bytes()
    except OSError as exc:
        raise SegmentCheckError(f"no se pudo leer el SOUL comun {soul_path} (--soul)") from exc
    sources["soul"] = {"archivo": str(soul_path), "sha256": hashlib.sha256(soul_bytes).hexdigest()}

    if flows_dir is None and (instance_dir / "flujos" / "publicados").is_dir():
        flows_dir = instance_dir / "flujos" / "publicados"
    flows = find_labeling_flows(flows_dir, label) if flows_dir is not None else None
    if flows is not None:
        sources["flujos"] = {"carpeta": str(flows_dir), "etiqueta": label, "que_etiquetan": [f.where for f in flows]}
    if catalog_path is None:
        catalogs = sorted((instance_dir / "evidencia").glob("*catalogo-plantillas*.json"))
        catalog_path = catalogs[-1] if catalogs else None
    templates = read_template_catalog(catalog_path) if catalog_path is not None else None
    if catalog_path is not None:
        sources["plantillas"] = {"archivo": str(catalog_path), "sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest()}
    if cases_path is None and (instance_dir / "conocimiento" / "prueba" / "casos.json").is_file():
        cases_path = instance_dir / "conocimiento" / "prueba" / "casos.json"
    cases = read_cases(cases_path, segment.key) if cases_path is not None else None
    if cases_path is not None:
        sources["casos"] = {"archivo": str(cases_path), "del_segmento": [c.case_id for c in cases or []]}

    return chequear(
        segment_text,
        base=base,
        soul_texto=soul_bytes.decode("utf-8"),
        juez=judge,
        otros=others,
        plantillas=templates,
        flujos=flows,
        casos=cases,
        corridas=runs,
        ahora=now,
        fuentes=sources,
        compuerta=compuerta,
    )


def _recording(judge: Judge, directory: Path, *, on_error: Callable[[OSError], None] | None = None) -> Judge:
    """Graba cada respuesta cruda. Con ``on_error``, un disco que falla avisa y la corrida sigue."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        if on_error is None:
            raise
        on_error(error)
        return judge
    counter = {"n": 0}

    def wrapped(messages: list[dict[str, str]]) -> JudgeAnswer:
        answer = judge(messages)
        counter["n"] += 1
        record = {"_grabado": datetime.now(timezone.utc).isoformat(), "modelo": answer.model, "content": answer.content}
        try:
            (directory / f"respuesta-{counter['n']}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as error:
            if on_error is None:
                raise
            on_error(error)
        return answer

    return wrapped


def read_recorded(paths: Sequence[Path]) -> Judge:
    """Un juez que reproduce respuestas guardadas con --grabar (o escritas con esa forma)."""

    records = [_read_json(path) for path in paths]
    answers = [str(r["content"]) for r in records]
    model = str(records[0].get("modelo", "grabado")) if records else "grabado"
    return recorded_judge(answers, model=model)


# --------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setter segmento",
        description="Chequea un segmento antes del alta con un juez de modelo (E-310 §3, E-312b). No escribe el segmento.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("chequear", help="contradicciones, huecos y dudas de un segmento, con las preguntas para el equipo")
    check.add_argument("segmento", type=Path, help="conocimiento/segmentos/<clave>-v<N>.toml")
    check.add_argument("--instancia", type=Path, help="carpeta de la instancia (por defecto, la que contiene al segmento)")
    check.add_argument("--soul", type=Path, help="SOUL comun (por defecto, el del producto)")
    check.add_argument("--catalogo", type=Path, help="catalogo de plantillas exportado (por defecto, el mas nuevo de evidencia/)")
    check.add_argument("--casos", type=Path, help="casos del arnes (por defecto, conocimiento/prueba/casos.json)")
    check.add_argument("--flujos", type=Path, help="carpeta de flujos setter-flujo/v1 (por defecto, flujos/publicados)")
    check.add_argument("--modelo", default=os.environ.get("SEGMENT_CHECK_MODEL", DEFAULT_MODEL), help=f"modelo de OpenRouter (por defecto {DEFAULT_MODEL})")
    check.add_argument("--corridas", type=int, default=DEFAULT_RUNS, help=f"veces que corre el juez (por defecto {DEFAULT_RUNS})")
    check.add_argument("--grabar", type=Path, help="carpeta donde guardar la respuesta cruda de cada corrida")
    check.add_argument("--grabadas", type=Path, nargs="+", help="reproduce respuestas guardadas con --grabar, sin red")
    check.add_argument("--ahora", help="fecha y hora con zona del chequeo (por defecto, ahora)")
    check.add_argument("--json", action="store_true", help="imprime el reporte en JSON")
    check.add_argument("--salida", type=Path, help="carpeta donde escribir <clave>-v<N>-chequeo.json y .md")
    args = parser.parse_args(argv)

    now = None
    if args.ahora:
        try:
            now = datetime.fromisoformat(args.ahora)
        except ValueError:
            print(f"--ahora no es una fecha ISO: {args.ahora}", file=sys.stderr)
            return 2
        if now.tzinfo is None:
            print("--ahora lleva zona (p. ej. 2026-11-20T00:00:00-06:00)", file=sys.stderr)
            return 2
    try:
        judge = read_recorded(args.grabadas) if args.grabadas else openrouter_judge(model=args.modelo)
    except (JudgeError, SegmentCheckError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    if args.grabar is not None:
        judge = _recording(judge, args.grabar)
    try:
        report = check_files(
            args.segmento,
            judge=judge,
            instance_dir=args.instancia,
            soul_path=args.soul,
            catalog_path=args.catalogo,
            cases_path=args.casos,
            flows_dir=args.flujos,
            runs=args.corridas,
            now=now,
        )
    except SegmentCheckError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    payload = report.to_json()
    markdown = report.to_markdown()
    if args.salida is not None:
        args.salida.mkdir(parents=True, exist_ok=True)
        stem = f"{report.segment.key}-v{report.segment.version}-chequeo"
        (args.salida / f"{stem}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.salida / f"{stem}.md").write_text(markdown, encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else markdown, end="\n" if args.json else "")
    if report.clean:
        print(f"LIMPIO chequeo_sha={report.chequeo_sha}", file=sys.stderr)
        return 0
    if not report.complete:
        print(f"SIN VEREDICTO: {report.valid_runs} de {report.requested_runs} corridas validas", file=sys.stderr)
        return 3 if report.valid_runs == 0 else 1
    print(f"ABIERTOS {len(report.items)}", file=sys.stderr)
    return 1


# ------------------------------------------------------------------------ helpers


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    raise TypeError(f"no se puede serializar {type(value).__name__}")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _aware(value: Any, where: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise SegmentCheckError(f"{where} debe ser fecha y hora con zona (2026-11-20T00:00:00-06:00)") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SegmentCheckError(f"{where} debe ser fecha y hora con zona (2026-11-20T00:00:00-06:00)")
    return value


def _exact(where: str, table: Mapping[str, Any], required: set[str], optional: set[str] | None = None) -> None:
    optional = optional or set()
    missing = sorted(required - set(table))
    extra = sorted(set(table) - required - optional)
    if missing:
        raise SegmentCheckError(f"{where}: faltan {', '.join(missing)}")
    if extra:
        raise SegmentCheckError(f"{where}: claves no soportadas {', '.join(extra)}")


def _table(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise SegmentCheckError(f"{key} debe ser una tabla")
    return value


def _text(table: Mapping[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SegmentCheckError(f"{where} debe ser un texto no vacio")
    return value.strip()


def _optional_text(table: Mapping[str, Any], key: str, where: str) -> str | None:
    if key not in table:
        return None
    return _text(table, key, where)


def _list(table: Mapping[str, Any], key: str, where: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise SegmentCheckError(f"{where} debe ser una lista de textos no vacios")
    if not allow_empty and not value:
        raise SegmentCheckError(f"{where} no puede estar vacia")
    return tuple(item.strip() for item in value)


def _items(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    if key not in payload:
        return ()
    table = _table(payload, key)
    _exact(key, table, {"items"})
    # Si la tabla esta, items no puede estar vacia (como en el cargador de PR-A).
    return _list(table, "items", f"{key}.items", allow_empty=False)


def _qa(payload: Mapping[str, Any], key: str) -> tuple[tuple[str, str, str], ...]:
    raw = payload.get(key, [])
    if not isinstance(raw, list):
        raise SegmentCheckError(f"{key} debe ser una lista de tablas [[{key}]]")
    out = []
    for index, entry in enumerate(raw):
        where = f"{key}[{index}]"
        if not isinstance(entry, dict):
            raise SegmentCheckError(f"{where} debe ser una tabla")
        _exact(where, entry, {"pregunta", "respuesta", "fuente"})
        out.append(
            (
                _text(entry, "pregunta", f"{where}.pregunta"),
                _text(entry, "respuesta", f"{where}.respuesta"),
                _text(entry, "fuente", f"{where}.fuente"),
            )
        )
    return tuple(out)


if __name__ == "__main__":
    raise SystemExit(main())
