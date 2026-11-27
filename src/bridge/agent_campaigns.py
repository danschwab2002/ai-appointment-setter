"""La campana que nace como dato: forma, armado del segmento y catalogo vivo (agent-campaigns-v1, E-355).

Contrato: ``docs/contracts/agent-campaigns-v1.md``. Alcance: opcion B del
2026-11-08 («Le pedis la campana al AE, la aprobas en una pagina y queda
andando, sin subir nada al servidor»).

Hasta aca un segmento era un archivo del repo de la instancia, declarado en el
manifiesto y leido al arrancar. Una campana del Automation Expert es una fila de
``agent_campaigns``: mismo esquema (``segment-knowledge-v1``), mismo render, mismo
``chequeo_sha`` (``segment_check``), distinta procedencia. Este modulo es el
traductor y el sosten:

* ``parse_campaign``: lo que manda el agente, normalizado; es lo que guarda la fila.
* ``segment_mapping`` y ``render_toml``: la fila como el dict y el TOML que
  entienden ``SegmentKnowledge.from_mapping`` y el juez.
* ``instance_blocks``: las reglas contra la instancia (vista previa y validacion).
* ``LiveSegmentCatalog``: la misma interfaz que ``SegmentCatalog``
  (``select``, ``blocks_for``, ``readiness``, ``segments``) para que sus consumidores
  no cambien. Se refresca de la base con cache corta y se queda con lo ultimo bueno
  si la base no contesta.

Fases del lanzamiento (4.8, ``AGENT_CAMPAIGN_PHASES_ENABLED``, apagado por defecto;
contrato ``docs/contracts/agent-campaign-phases-v1.md``): con el flag, una campana
puede traer ``fases`` fechadas y un ``bloque_minimo``. Cada fase y el bloque minimo
son una *pieza*: un segmento con su clave (``<clave>--<fase>``, ``<clave>--minimo``),
su vigencia y su render, validados, juzgados y aprobados juntos. El catalogo sirve
la fase cuya ventana contiene ahora y, fuera de toda fase (entre fases, despues del
cierre o con la campana pausada), el bloque minimo. Sin el flag una campana con
fases no se guarda ni se sirve, y una sin fases es byte a byte la de siempre.

Decisiones de Dan del 2026-11-08: un ``chequeo_sha`` que no calza en una campana de
la base **no se sirve, avisa y el bridge sigue** (la regla del 06/11 que frena el
arranque queda solo para los archivos del manifiesto), y una sola campana viva por
instancia (lo hace cumplir la base).

Los logs llevan la clave, la version y el motivo; nunca el texto de la campana.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

from .campaign_inbound_entry import (
    CampaignEntry,
    CampaignEntryConfigError,
    EntrySegment,
    normalize_entry_text,
)
from .campaign_entry import CampaignEntry as OfferCampaignEntry
from .campaign_entry import agent_campaign_entries
from .segment_labels import ORIGIN_AGENT_CAMPAIGN, SegmentLabel
from .commercial_knowledge import CommercialKnowledge
from .flow_change_api import looks_like_identifier
from .instance_manifest import RESERVED_CHATWOOT_LABELS, InstanceManifest
from .segment_check import ChequeoError, SegmentCheckError, _canonical, _sha, calcular_chequeo_sha
from .segment_check import verificar_chequeo as verificar_huella
from .segment_knowledge import (
    MAX_INSTRUCTION_CHARS,
    LoadedSegment,
    SegmentCatalog,
    SegmentError,
    SegmentKnowledge,
    STATE_ACTIVE,
    SegmentSelection,
    build_catalog,
    knowledge_product_names,
)

__all__ = [
    "CONTRACT",
    "RULES_VERSION",
    "CampaignContext",
    "CampaignInvalid",
    "LiveSegmentCatalog",
    "campaign_pieces",
    "expected_check_sha",
    "has_pieces",
    "instance_blocks",
    "parse_campaign",
    "phased_check_sha",
    "render_toml",
    "segment_mapping",
    "wa_me_link",
]

logger = logging.getLogger(__name__)

CONTRACT = "agent-campaigns-v1"
RULES_VERSION = 1
# Una campana de la base va despues de los segmentos del manifiesto.
CAMPAIGN_PRIORITY = 1000
REFRESH_SECONDS = 30.0
MAX_KEYWORDS = 8
MAX_KEYWORD_CHARS = 80
MAX_QUESTION_CHARS = 200
MAX_NAME_CHARS = 120
MAX_LIST_ITEMS = 100
MAX_LABEL_CHARS = 40
_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")

_REQUIRED = {"clave", "nombre", "etiqueta", "macro_etiqueta", "desde", "hasta", "asesoramiento"}
_OPTIONAL = {"palabras_clave", "pregunta_entrada", "instruccion", "oferta", "se_puede_decir", "no_confirmado",
             "promesas_prohibidas", "faq", "preguntas_del_alta"}
_OFFER_KEYS = {"producto", "precio", "moneda", "precio_antes", "cupon", "codigo_oferta", "frenar_descuento_carrito",
               "incluye", "garantia", "medios", "reemplaza"}
_LIST_SECTIONS = ("se_puede_decir", "no_confirmado", "promesas_prohibidas")
_QA_SECTIONS = ("faq", "preguntas_del_alta")
# Las etiquetas que no pueden abrir una campana: las mismas que un segmento del manifiesto.
_FORBIDDEN_LABELS = frozenset((*RESERVED_CHATWOOT_LABELS, "cliente"))

# -- Fases del lanzamiento (4.8, agent-campaign-phases-v1) --------------------------
PHASES_CONTRACT = "agent-campaign-phases-v1"
# D-48.2: de 1 a 6 fases; cada una con su clave corta, que va en la clave del segmento.
MAX_PHASES = 6
MAX_PHASE_KEY_CHARS = 24
# La clave de la campana deja lugar para «--» y la clave de la fase (128 en total).
MAX_PHASED_CAMPAIGN_KEY_CHARS = 128 - 2 - MAX_PHASE_KEY_CHARS
PIECE_SEPARATOR = "--"
MINIMAL_PIECE = "minimo"
# D-48.4: el bloque minimo sigue sirviendose hasta 14 dias despues del cierre.
MINIMAL_GRACE = timedelta(days=14)
# Lo que cabe en la fila (_agent_campaign_draft_is_valid, 32768 bytes) y en la vista previa (65536).
MAX_DRAFT_BYTES = 32_768
MAX_PREVIEW_BYTES = 60_000
# Las prioridades: la fase y la campana van en 1000; el bloque minimo despues, asi se pisa con
# la fase sin chocar (se elige en el turno: nunca van los dos); los de las campanas en reposo, detras.
MINIMAL_PRIORITY = CAMPAIGN_PRIORITY + 1
REST_PRIORITY = CAMPAIGN_PRIORITY + 2
MAX_RESTS = 5
_PHASE_REQUIRED = {"clave", "nombre", "desde", "hasta"}
_PHASE_SECTIONS = {"instruccion", "oferta", "asesoramiento", "se_puede_decir", "no_confirmado",
                   "promesas_prohibidas", "faq"}
_MINIMAL_REQUIRED = {"asesoramiento"}
_MINIMAL_OPTIONAL = {"instruccion", "oferta", "se_puede_decir", "no_confirmado", "promesas_prohibidas", "faq"}
# D-48.3: el bloque minimo no ofrece nada del lanzamiento: ni cupon, ni oferta propia, ni precio tachado.
_MINIMAL_OFFER_FORBIDDEN = {"cupon", "codigo_oferta", "precio_antes", "frenar_descuento_carrito"}
# D-48.3: si el AE no la escribe, el bloque minimo lleva esta instruccion (va en la fila: se juzga y se aprueba).
MINIMAL_DEFAULT_INSTRUCTION = (
    "Ahora no hay ninguna fase del lanzamiento abierta. No ofrezcas precio de lanzamiento, cupón, "
    "bonos ni fechas del lanzamiento. Contesta sobre el producto con lo que dice este bloque y, si "
    "preguntan por el lanzamiento, di que las novedades se informan por este mismo chat."
)


class CampaignInvalid(ValueError):
    """Lo que mando el agente no sirve. ``reason`` lleva nombres de campos, nunca valores."""

    def __init__(self, code: str, reason: Mapping[str, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.reason = dict(reason)


def _refused(field: str, problem: str) -> CampaignInvalid:
    return CampaignInvalid("campaign_invalid", {"field": field, "problem": problem})


def _text(raw: Mapping[str, Any], key: str, field: str, *, limit: int = 4000) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _refused(field, "not_a_text")
    text = value.strip()
    if len(text) > limit:
        raise _refused(field, f"longer_than_{limit}")
    if any((ord(character) < 32 and character != "\n") or ord(character) == 127 for character in text):
        raise _refused(field, "control_characters")
    return text


def _line(raw: Mapping[str, Any], key: str, field: str, *, limit: int) -> str:
    text = _text(raw, key, field, limit=limit)
    if "\n" in text:
        raise _refused(field, "more_than_one_line")
    return text


def _texts(raw: Mapping[str, Any], key: str, field: str, *, required: bool = False) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS or (required and not value):
        raise _refused(field, "not_a_list_of_texts")
    return [_text({"x": item}, "x", field) for item in value]


def _instant(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise _refused(key, "not_a_date_with_zone")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _refused(key, "not_a_date_with_zone") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _refused(key, "not_a_date_with_zone")
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _offer(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or not raw or set(raw) - _OFFER_KEYS:
        raise _refused("oferta", "unknown_or_empty")
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in ("incluye", "medios", "reemplaza"):
            out[key] = _texts(raw, key, f"oferta.{key}")
        elif key == "frenar_descuento_carrito":
            # E-410 C8: un si/no. Solo se guarda en true: false es lo de hoy.
            if type(value) is not bool:
                raise _refused("oferta.frenar_descuento_carrito", "not_a_boolean")
            if value:
                out[key] = True
        else:
            if not isinstance(value, str):
                raise _refused(f"oferta.{key}", "not_a_text")
            out[key] = _line(raw, key, f"oferta.{key}", limit=2000)
    return out


def _qa(raw: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > MAX_LIST_ITEMS:
        raise _refused(field, "not_a_list")
    out = []
    for index, entry in enumerate(raw):
        where = f"{field}[{index}]"
        if not isinstance(entry, Mapping) or set(entry) != {"pregunta", "respuesta", "fuente"}:
            raise _refused(where, "pregunta_respuesta_fuente")
        out.append({key: _text(entry, key, f"{where}.{key}") for key in ("pregunta", "respuesta", "fuente")})
    return out


def parse_campaign(raw: Any, *, phases: bool = False) -> dict[str, Any]:
    """Lo que manda el agente, normalizado. Lo que no cierra se rechaza con el campo, sin el valor.

    ``phases`` es ``AGENT_CAMPAIGN_PHASES_ENABLED``: sin el, ``fases`` y ``bloque_minimo`` son
    claves de mas (D-48.1: una campana con fases no se guarda ni se sirve con el flag apagado)."""
    if not isinstance(raw, Mapping):
        raise _refused("campaign", "not_an_object")
    optional = _OPTIONAL | ({"fases", "bloque_minimo"} if phases else set())
    missing, extra = _REQUIRED - set(raw), set(raw) - _REQUIRED - optional
    if missing or extra:
        reason: dict[str, Any] = {"field": "campaign"}
        if missing:
            reason["missing"] = sorted(missing)
        if extra:
            reason["extra"] = sorted(key if isinstance(key, str) and key.isascii() and len(key) <= 40 else "<otra>"
                                      for key in extra)[:10]
        raise CampaignInvalid("campaign_invalid", reason)
    key = raw.get("clave")
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or set(key) - _KEY_CHARS or key[0] == "-":
        raise _refused("clave", "lowercase_slug")
    label = raw.get("etiqueta")
    if not isinstance(label, str) or not 1 <= len(label) <= MAX_LABEL_CHARS or set(label) - _LABEL_CHARS \
            or label[0] in "-_":
        raise _refused("etiqueta", "chatwoot_label_lowercase")
    macro = raw.get("macro_etiqueta")
    if type(macro) is not int or macro < 1 or macro >= 10**12:
        raise _refused("macro_etiqueta", "positive_integer")
    draft: dict[str, Any] = {
        "clave": key, "nombre": _line(raw, "nombre", "nombre", limit=MAX_NAME_CHARS), "etiqueta": label,
        "macro_etiqueta": macro, "desde": _instant(raw, "desde"), "hasta": _instant(raw, "hasta"),
        "asesoramiento": _texts(raw, "asesoramiento", "asesoramiento", required=True),
    }
    if datetime.fromisoformat(draft["desde"]) >= datetime.fromisoformat(draft["hasta"]):
        raise _refused("desde", "must_be_before_hasta")
    if "palabras_clave" in raw:
        keywords = [_line({"x": item}, "x", "palabras_clave", limit=MAX_KEYWORD_CHARS)
                    for item in _as_list(raw["palabras_clave"], "palabras_clave")]
        normalized = [normalize_entry_text(keyword) for keyword in keywords]
        if not 1 <= len(keywords) <= MAX_KEYWORDS or not all(normalized) \
                or len(set(normalized)) != len(normalized):
            raise _refused("palabras_clave", "one_to_eight_with_letters_and_distinct")
        draft["palabras_clave"] = keywords
    if "pregunta_entrada" in raw:
        draft["pregunta_entrada"] = _line(raw, "pregunta_entrada", "pregunta_entrada", limit=MAX_QUESTION_CHARS)
    if "palabras_clave" not in draft and "pregunta_entrada" not in draft:
        raise _refused("palabras_clave", "keyword_or_question_required")
    if "instruccion" in raw:
        draft["instruccion"] = _text(raw, "instruccion", "instruccion", limit=MAX_INSTRUCTION_CHARS)
    if "oferta" in raw:
        draft["oferta"] = _offer(raw["oferta"])
    for section in _LIST_SECTIONS:
        if section in raw:
            draft[section] = _texts(raw, section, section, required=True)
    for section in _QA_SECTIONS:
        if section in raw:
            draft[section] = _qa(raw[section], section)
    for field in ("nombre", "pregunta_entrada"):
        if isinstance(draft.get(field), str) and looks_like_identifier(draft[field]):
            raise CampaignInvalid("identifier_not_allowed", {"field": field, "problem": "phone_or_mail"})
    if "fases" in raw or "bloque_minimo" in raw:
        _parse_pieces(raw, draft)
    return draft


def _sections(raw: Mapping[str, Any], out: dict[str, Any], where: str, keys: set[str]) -> None:
    """Las secciones de contenido de una fase o del bloque minimo, con las reglas de la campana."""
    if "instruccion" in keys and "instruccion" in raw:
        out["instruccion"] = _text(raw, "instruccion", f"{where}.instruccion", limit=MAX_INSTRUCTION_CHARS)
    if "oferta" in keys and "oferta" in raw:
        try:
            out["oferta"] = _offer(raw["oferta"])
        except CampaignInvalid as error:
            raise _refused(f"{where}.{error.reason.get('field', 'oferta')}", str(error.reason.get("problem"))) \
                from None
    if "asesoramiento" in keys and "asesoramiento" in raw:
        out["asesoramiento"] = _texts(raw, "asesoramiento", f"{where}.asesoramiento", required=True)
    for section in _LIST_SECTIONS:
        if section in keys and section in raw:
            out[section] = _texts(raw, section, f"{where}.{section}", required=True)
    if "faq" in keys and "faq" in raw:
        out["faq"] = _qa(raw["faq"], f"{where}.faq")


def _parse_pieces(raw: Mapping[str, Any], draft: dict[str, Any]) -> None:
    """``fases`` y ``bloque_minimo`` (D-48.1 a D-48.3). Sin fases se puede traer solo el bloque minimo."""
    if "fases" in raw and "bloque_minimo" not in raw:
        raise _refused("bloque_minimo", "required_with_fases")
    if len(draft["clave"]) > MAX_PHASED_CAMPAIGN_KEY_CHARS:
        raise _refused("clave", f"longer_than_{MAX_PHASED_CAMPAIGN_KEY_CHARS}_with_pieces")
    start, end = datetime.fromisoformat(draft["desde"]), datetime.fromisoformat(draft["hasta"])
    if "fases" in raw:
        phases = raw["fases"]
        if not isinstance(phases, list) or not 1 <= len(phases) <= MAX_PHASES:
            raise _refused("fases", f"one_to_{MAX_PHASES}")
        parsed: list[dict[str, Any]] = []
        for index, phase in enumerate(phases):
            where = f"fases[{index}]"
            if not isinstance(phase, Mapping) or _PHASE_REQUIRED - set(phase) \
                    or set(phase) - _PHASE_REQUIRED - _PHASE_SECTIONS:
                raise _refused(where, "clave_nombre_desde_hasta_and_sections")
            key = phase.get("clave")
            if not isinstance(key, str) or not 1 <= len(key) <= MAX_PHASE_KEY_CHARS or set(key) - _KEY_CHARS \
                    or key[0] == "-" or key[-1] == "-" or PIECE_SEPARATOR in key or key == MINIMAL_PIECE:
                raise _refused(f"{where}.clave", "short_lowercase_slug")
            out: dict[str, Any] = {"clave": key, "nombre": _line(phase, "nombre", f"{where}.nombre",
                                                                  limit=MAX_NAME_CHARS)}
            try:
                out["desde"], out["hasta"] = _instant(phase, "desde"), _instant(phase, "hasta")
            except CampaignInvalid as error:
                raise _refused(f"{where}.{error.reason['field']}", "not_a_date_with_zone") from None
            phase_start, phase_end = datetime.fromisoformat(out["desde"]), datetime.fromisoformat(out["hasta"])
            if phase_start >= phase_end:
                raise _refused(f"{where}.desde", "must_be_before_hasta")
            if phase_start < start or phase_end > end:
                raise _refused(where, "outside_the_campaign_window")
            if looks_like_identifier(out["nombre"]):
                raise CampaignInvalid("identifier_not_allowed", {"field": f"{where}.nombre", "problem": "phone_or_mail"})
            _sections(phase, out, where, _PHASE_SECTIONS)
            parsed.append(out)
        if len({phase["clave"] for phase in parsed}) != len(parsed):
            raise _refused("fases", "repeated_clave")
        ordered = sorted(parsed, key=lambda phase: datetime.fromisoformat(phase["desde"]))
        if ordered != parsed:
            raise _refused("fases", "not_in_order")
        for first, second in zip(parsed, parsed[1:]):
            if datetime.fromisoformat(second["desde"]) < datetime.fromisoformat(first["hasta"]):
                raise _refused("fases", "windows_overlap")
        draft["fases"] = parsed
    minimal = raw["bloque_minimo"]
    if not isinstance(minimal, Mapping) or _MINIMAL_REQUIRED - set(minimal) \
            or set(minimal) - _MINIMAL_REQUIRED - _MINIMAL_OPTIONAL:
        raise _refused("bloque_minimo", "asesoramiento_and_sections")
    if isinstance(minimal.get("oferta"), Mapping) and set(minimal["oferta"]) & _MINIMAL_OFFER_FORBIDDEN:
        raise _refused("bloque_minimo.oferta", "no_coupon_no_offer_code_no_old_price")
    out_minimal: dict[str, Any] = {}
    _sections(minimal, out_minimal, "bloque_minimo", _MINIMAL_REQUIRED | _MINIMAL_OPTIONAL)
    out_minimal.setdefault("instruccion", MINIMAL_DEFAULT_INSTRUCTION)
    draft["bloque_minimo"] = {key: out_minimal[key] for key in ("instruccion", *sorted(set(out_minimal) - {"instruccion"}))}
    if len(json.dumps(draft, ensure_ascii=False).encode("utf-8")) > MAX_DRAFT_BYTES:
        raise _refused("campaign", f"longer_than_{MAX_DRAFT_BYTES}_bytes")


def has_pieces(draft: Mapping[str, Any]) -> bool:
    """Si la campana trae fases o bloque minimo: entonces se sirve por piezas."""
    return "fases" in draft or "bloque_minimo" in draft


@dataclass(frozen=True)
class CampaignPiece:
    """Una pieza de una campana con fases: una fase, la campana entera (sin fases) o el bloque minimo.

    ``draft`` tiene la forma de una campana sin fases, para que ``segment_mapping`` la arme igual."""

    key: str
    kind: str  # "fase", "campana" o "minimo"
    phase: str | None
    draft: Mapping[str, Any]
    # Lo que muestra la pagina: el nombre de la fase, o el de la campana.
    title: str = ""

    @property
    def minimal(self) -> bool:
        return self.kind == MINIMAL_PIECE


_SHARED = ("instruccion", "oferta", "asesoramiento", *_LIST_SECTIONS, "faq")


def campaign_pieces(draft: Mapping[str, Any]) -> tuple[CampaignPiece, ...]:
    """Las piezas en orden: las fases (o la campana entera) y al final el bloque minimo.

    Una fase hereda de la campana cada seccion que no trae y reemplaza entera la que trae
    (D-48.2). El bloque minimo tiene su propio contenido (no hereda la oferta ni el cupon de la
    campana) y corre desde el inicio de la campana hasta 14 dias despues del cierre (D-48.4)."""
    if not has_pieces(draft):
        return (CampaignPiece(key=draft["clave"], kind="campana", phase=None, draft=draft, title=draft["nombre"]),)
    common = {key: draft[key] for key in ("etiqueta", "macro_etiqueta") if key in draft}
    if "preguntas_del_alta" in draft:
        common["preguntas_del_alta"] = draft["preguntas_del_alta"]
    pieces: list[CampaignPiece] = []
    if "fases" in draft:
        for phase in draft["fases"]:
            # El bloque se titula con la campana y la fase: el agente sabe de que lanzamiento es.
            flat: dict[str, Any] = {**common, "clave": f"{draft['clave']}{PIECE_SEPARATOR}{phase['clave']}",
                                    "nombre": f"{draft['nombre']} · {phase['nombre']}", "desde": phase["desde"],
                                    "hasta": phase["hasta"]}
            for section in _SHARED:
                if section in phase:
                    flat[section] = phase[section]
                elif section in draft:
                    flat[section] = draft[section]
            pieces.append(CampaignPiece(key=flat["clave"], kind="fase", phase=phase["clave"], draft=flat,
                                        title=phase["nombre"]))
    else:
        flat = {key: value for key, value in draft.items() if key not in ("fases", "bloque_minimo")}
        pieces.append(CampaignPiece(key=draft["clave"], kind="campana", phase=None, draft=flat, title=draft["nombre"]))
    minimal = draft["bloque_minimo"]
    until = datetime.fromisoformat(draft["hasta"]) + MINIMAL_GRACE
    flat_minimal: dict[str, Any] = {**common, "clave": f"{draft['clave']}{PIECE_SEPARATOR}{MINIMAL_PIECE}",
                                    "nombre": draft["nombre"], "desde": draft["desde"],
                                    "hasta": until.isoformat(timespec="seconds").replace("+00:00", "Z")}
    flat_minimal.update({key: minimal[key] for key in _SHARED if key in minimal})
    pieces.append(CampaignPiece(key=flat_minimal["clave"], kind=MINIMAL_PIECE, phase=None, draft=flat_minimal,
                                title=draft["nombre"]))
    return tuple(pieces)


def phased_check_sha(piece_shas: Sequence[tuple[str, str]]) -> str:
    """El ``chequeo_sha`` de una campana por piezas: la huella de los de cada pieza, en orden (D-48.5)."""
    return _sha(_canonical({"esquema": PHASES_CONTRACT, "piezas": [[key, sha] for key, sha in piece_shas]}))


def _as_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise _refused(field, "not_a_list")
    return value


def segment_mapping(draft: Mapping[str, Any], *, ally_ref: str, version: int, status: str = "borrador",
                    check_sha: str | None = None, approved_on: date | None = None) -> dict[str, Any]:
    """La fila como el dict de tomllib de un archivo de segmento. Mismas claves, mismos tipos."""
    header: dict[str, Any] = {"segment_version": version, "estado": status, "ally_ref": ally_ref,
                              "clave": draft["clave"], "nombre": draft["nombre"]}
    if status == "aprobado":
        header["aprobado_por"] = "pagina de aprobacion"
        header["aprobado_el"] = approved_on
    if check_sha is not None:
        header["chequeo_sha"] = check_sha
    mapping: dict[str, Any] = {
        "cabecera": header,
        "vigencia": {"desde": datetime.fromisoformat(draft["desde"]),
                     "hasta": datetime.fromisoformat(draft["hasta"])},
    }
    if "instruccion" in draft:
        mapping["instruccion"] = {"texto": draft["instruccion"]}
    if "oferta" in draft:
        mapping["oferta"] = dict(draft["oferta"])
    mapping["asesoramiento"] = {"items": list(draft["asesoramiento"])}
    for section in _LIST_SECTIONS:
        if section in draft:
            mapping[section] = {"items": list(draft[section])}
    for section in _QA_SECTIONS:
        if section in draft:
            mapping[section] = [dict(entry) for entry in draft[section]]
    return mapping


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):  # pragma: no cover - el esquema no tiene booleanos
        raise TypeError("bool")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(type(value).__name__)


def render_toml(mapping: Mapping[str, Any]) -> str:
    """El archivo que lee el juez (``segment_check.check_files``). tomllib lo devuelve igual."""
    lines: list[str] = []
    tables = [(key, value) for key, value in mapping.items() if isinstance(value, Mapping)]
    arrays = [(key, value) for key, value in mapping.items() if isinstance(value, list)]
    for name, table in tables:
        lines.append(f"[{name}]")
        lines += [f"{key} = {_toml_value(value)}" for key, value in table.items()]
        lines.append("")
    for name, entries in arrays:
        for entry in entries:
            lines.append(f"[[{name}]]")
            lines += [f"{key} = {_toml_value(value)}" for key, value in entry.items()]
            lines.append("")
    return "\n".join(lines)


def wa_me_link(phone: object, keyword: str | None) -> str | None:
    """El link de WhatsApp con la palabra clave escrita: lo que va en el boton de la campana."""
    digits = "".join(character for character in phone if character.isdigit()) if isinstance(phone, str) else ""
    if not digits:
        return None
    return f"https://wa.me/{digits}" + (f"?text={quote(keyword)}" if keyword else "")


@dataclass(frozen=True)
class CampaignContext:
    """Contra que se mide una campana: la instancia y lo que ya carga el bridge."""

    manifest: InstanceManifest
    base: CommercialKnowledge
    manifest_catalog: SegmentCatalog
    reserved_macros: frozenset[int] = frozenset()
    # E-410: AGENT_CAMPAIGN_OFFER_LINK_ENABLED. Sin el, una campana con
    # oferta.codigo_oferta no se valida ni se sirve: su bloque prometeria un link
    # con esa oferta que no sale.
    offer_link_enabled: bool = False
    # 4.8: AGENT_CAMPAIGN_PHASES_ENABLED. Sin el, una campana con fases o bloque minimo
    # no se valida, no se guarda y no se sirve (D-48.1).
    phases_enabled: bool = False

    @property
    def product_names(self) -> dict[str, str]:
        # E-563: con [[productos]], tambien el principal de un conocimiento de una capa y
        # los productos sin seccion (por su product_name de Hotmart).
        return knowledge_product_names(self.manifest, self.base)

    @property
    def others(self) -> list[dict[str, Any]]:
        return [dict(segment.raw) for segment in self.manifest_catalog.segments]


def load_campaign_segment(draft: Mapping[str, Any], *, version: int, context: CampaignContext, status: str = "borrador",
                          check_sha: str | None = None, approved_on: date | None = None,
                          priority: int = CAMPAIGN_PRIORITY) -> LoadedSegment:
    """La campana como un segmento cargado. Lanza ``SegmentError`` si el esquema no la acepta."""
    raw = segment_mapping(draft, ally_ref=context.manifest.ally_ref, version=version, status=status,
                          check_sha=check_sha, approved_on=approved_on)
    knowledge = SegmentKnowledge.from_mapping(raw, time_zone=context.manifest.time_zone,
                                              product_names=context.product_names,
                                              principal_name=context.base.products[0].offer_name,
                                              require_approved=status == "aprobado")
    rendered = knowledge.render()
    return LoadedSegment(key=knowledge.key, label=draft["etiqueta"], priority=priority,
                         knowledge_path=f"agent_campaigns/{knowledge.key}-v{knowledge.version}",
                         label_macro_id=draft["macro_etiqueta"], knowledge=knowledge, rendered=rendered,
                         sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(), raw=MappingProxyType(raw))


def expected_check_sha(draft: Mapping[str, Any], *, version: int, context: CampaignContext) -> str:
    """El ``chequeo_sha`` que tiene que tener hoy esta campana (la misma funcion que el cargador).

    Con fases o bloque minimo, la huella de los ``chequeo_sha`` de cada pieza (D-48.5)."""
    if has_pieces(draft):
        return phased_check_sha(piece_check_shas(draft, version=version, context=context))
    raw = segment_mapping(draft, ally_ref=context.manifest.ally_ref, version=version)
    return calcular_chequeo_sha(raw, base_sha256=context.base.rendered_sha256, otros=context.others)


def piece_check_shas(draft: Mapping[str, Any], *, version: int, context: CampaignContext) -> list[tuple[str, str]]:
    """El ``chequeo_sha`` de cada pieza, en orden: lo que imprime el juez para cada una."""
    out = []
    for piece in campaign_pieces(draft):
        raw = segment_mapping(piece.draft, ally_ref=context.manifest.ally_ref, version=version)
        out.append((piece.key, calcular_chequeo_sha(raw, base_sha256=context.base.rendered_sha256,
                                                    otros=context.others)))
    return out


def declared_offers(manifest: InstanceManifest) -> tuple[Any, ...]:
    """Todas las ofertas declaradas de la instancia: las del principal y las de [[productos]]."""
    offers = list(manifest.offers)
    for product in manifest.products:
        if product.commerce is not None:
            offers.extend(product.commerce.offers)
    return tuple(offers)


def default_offer_codes(manifest: InstanceManifest) -> frozenset[str]:
    """Las ofertas por defecto de la instancia: la del principal y la de cada producto."""
    return frozenset(offer.code for offer in declared_offers(manifest) if offer.default)


def offer_entry_taken(manifest: InstanceManifest, offer_code: str) -> bool:
    """Si la oferta, o la landing de esa oferta, ya es de una ``[[campanas]]`` del manifiesto (D3 de E-411)."""
    landings = {offer.landing_id for offer in declared_offers(manifest) if offer.code == offer_code}
    for campaign in getattr(manifest, "campaigns", ()) or ():
        if offer_code in campaign.offers or landings & set(campaign.landings):
            return True
    return False


def _finding(code: str, field: str | None = None, detail: str | None = None) -> dict[str, Any]:
    finding: dict[str, Any] = {"code": code}
    if field is not None:
        finding["field"] = field
    if detail is not None:
        finding["detail"] = detail[:300]
    return finding


def instance_blocks(draft: Mapping[str, Any], *, context: CampaignContext, now: datetime, version: int = 1,
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], LoadedSegment | None]:
    """Las reglas de la campana contra la instancia: (bloqueos, avisos, el segmento armado)."""
    blocks: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    manifest = context.manifest
    label = draft["etiqueta"]
    product_labels = {product.chatwoot_label for product in manifest.products if product.chatwoot_label}
    if label in _FORBIDDEN_LABELS or label.startswith("automation_"):
        blocks.append(_finding("campaign_label_reserved", "etiqueta"))
    if label in product_labels:
        blocks.append(_finding("campaign_label_is_a_product", "etiqueta"))
    declared = manifest.agent_segments
    if label in {segment.label for segment in declared}:
        blocks.append(_finding("campaign_label_taken", "etiqueta"))
    if draft["clave"] in {segment.key for segment in declared}:
        blocks.append(_finding("campaign_key_taken", "clave"))
    if draft["macro_etiqueta"] in context.reserved_macros \
            or draft["macro_etiqueta"] in {segment.label_macro_id for segment in declared}:
        blocks.append(_finding("campaign_macro_reserved", "macro_etiqueta"))
    theirs = {keyword for segment in declared for keyword in segment.entry_keywords}
    if {normalize_entry_text(keyword) for keyword in draft.get("palabras_clave", ())} & theirs:
        blocks.append(_finding("campaign_keyword_taken", "palabras_clave"))
    if datetime.fromisoformat(draft["hasta"]) <= now:
        blocks.append(_finding("campaign_window_over", "hasta"))
    if has_pieces(draft):
        return _piece_blocks(draft, context=context, version=version, blocks=blocks, warnings=warnings)
    loaded = None
    try:
        loaded = load_campaign_segment(draft, version=version, context=context)
    except SegmentError as error:
        blocks.append(_finding("campaign_segment_invalid", detail=str(error)))
    if loaded is not None:
        try:
            build_catalog([*context.manifest_catalog.segments, loaded], base_bytes=context.manifest_catalog.base_bytes)
        except SegmentError as error:
            blocks.append(_finding("campaign_does_not_fit", detail=str(error)))
        coupon = draft.get("oferta", {}).get("cupon")
        if coupon is not None:
            # El producto no lo puede verificar: la API de Hotmart no esta en este camino.
            warnings.append(_finding("campaign_coupon_not_verified", "oferta.cupon"))
        offer_code = loaded.knowledge.offer.offer_code if loaded.knowledge.offer is not None else None
        if offer_code is not None:
            # E-410: la oferta del link tiene que ser una oferta declarada del producto
            # de la campana (el principal, o el de oferta.producto). Si esta activa en
            # Hotmart y en el catalogo de la base no lo ve este camino: lo dice la
            # reserva (blocked_campaign_offer, y se deriva).
            codes = manifest.offer_codes_of_product(loaded.knowledge.offer.product)
            if not context.offer_link_enabled:
                blocks.append(_finding("campaign_offer_link_disabled", "oferta.codigo_oferta"))
            if codes is None or offer_code not in codes:
                blocks.append(_finding("campaign_offer_unknown", "oferta.codigo_oferta"))
            elif offer_code in default_offer_codes(manifest):
                # Hallazgo 3 de la revision: la oferta por defecto ya es la del link de
                # siempre, y como entrada por oferta etiquetaria todo carrito de su landing.
                blocks.append(_finding("campaign_offer_is_default", "oferta.codigo_oferta"))
            elif offer_entry_taken(manifest, offer_code):
                # E-411, D3: el carrito de esa oferta ya lo etiqueta una campana del
                # manifiesto; el AE no le saca la oferta.
                blocks.append(_finding("campaign_offer_entry_taken", "oferta.codigo_oferta"))
            warnings.append(_finding("campaign_offer_not_verified", "oferta.codigo_oferta"))
    return blocks, warnings, loaded


def _offer_findings(loaded: LoadedSegment, *, context: CampaignContext, field: str, blocks: list[dict[str, Any]],
                    warnings: list[dict[str, Any]]) -> None:
    """Las reglas de la oferta (E-410, E-411) para una pieza, con el campo de la pieza."""
    manifest = context.manifest
    offer = loaded.knowledge.offer
    if offer is None:
        return
    if offer.coupon is not None:
        warnings.append(_finding("campaign_coupon_not_verified", f"{field}.cupon"))
    code = offer.offer_code
    if code is None:
        return
    codes = manifest.offer_codes_of_product(offer.product)
    if not context.offer_link_enabled:
        blocks.append(_finding("campaign_offer_link_disabled", f"{field}.codigo_oferta"))
    if codes is None or code not in codes:
        blocks.append(_finding("campaign_offer_unknown", f"{field}.codigo_oferta"))
    elif code in default_offer_codes(manifest):
        blocks.append(_finding("campaign_offer_is_default", f"{field}.codigo_oferta"))
    elif offer_entry_taken(manifest, code):
        blocks.append(_finding("campaign_offer_entry_taken", f"{field}.codigo_oferta"))
    warnings.append(_finding("campaign_offer_not_verified", f"{field}.codigo_oferta"))


def _piece_field(draft: Mapping[str, Any], piece: CampaignPiece) -> str:
    if piece.minimal:
        return "bloque_minimo"
    if piece.kind == "fase":
        index = [phase["clave"] for phase in draft["fases"]].index(piece.phase)
        return f"fases[{index}]"
    return "campaign"


def _piece_blocks(draft: Mapping[str, Any], *, context: CampaignContext, version: int, blocks: list[dict[str, Any]],
                  warnings: list[dict[str, Any]],
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], LoadedSegment | None]:
    """Las reglas de una campana por piezas: cada pieza se arma y se mide sola, y todas juntas entran
    con el catalogo del manifiesto. Devuelve como segmento la primera pieza (el ``render`` de la vista
    previa); todas van en ``piezas`` (``pieces_preview``)."""
    if not context.phases_enabled:
        # D-48.1: el parser ya no la deja pasar sin el flag; esto es por si alguien la arma a mano.
        blocks.append(_finding("campaign_phases_disabled", "fases"))
        return blocks, warnings, None
    loaded: list[LoadedSegment] = []
    invalid = False
    if "fases" in draft and "oferta" in draft:
        # La oferta de arriba la heredan las fases que no traen la suya y es la de la entrada por oferta:
        # pasa por las mismas reglas aunque ninguna fase la herede.
        top = {key: value for key, value in draft.items() if key not in ("fases", "bloque_minimo")}
        try:
            _offer_findings(load_campaign_segment(top, version=version, context=context), context=context,
                            field="oferta", blocks=blocks, warnings=warnings)
        except SegmentError as error:
            blocks.append(_finding("campaign_segment_invalid", "oferta", detail=str(error)))
            invalid = True
    for piece in campaign_pieces(draft):
        field = _piece_field(draft, piece)
        try:
            segment = load_campaign_segment(piece.draft, version=version, context=context,
                                            priority=MINIMAL_PRIORITY if piece.minimal else CAMPAIGN_PRIORITY)
        except SegmentError as error:
            blocks.append(_finding("campaign_segment_invalid", field, detail=str(error)))
            invalid = True
            continue
        _offer_findings(segment, context=context, field="oferta" if field == "campaign" else f"{field}.oferta",
                        blocks=blocks, warnings=warnings)
        loaded.append(segment)
    if loaded and not invalid:
        try:
            build_catalog([*context.manifest_catalog.segments, *loaded], base_bytes=context.manifest_catalog.base_bytes)
        except SegmentError as error:
            blocks.append(_finding("campaign_does_not_fit", detail=str(error)))
    return blocks, warnings, (loaded[0] if loaded else None)


def pieces_preview(draft: Mapping[str, Any], *, context: CampaignContext, version: int = 1,
                   ) -> list[dict[str, Any]] | None:
    """Lo que la pagina muestra de cada pieza: clave, tipo, nombre, ventana y el render.

    None para una campana sin piezas: la vista previa queda como siempre."""
    if not has_pieces(draft) or not context.phases_enabled:
        return None
    out = []
    for piece in campaign_pieces(draft):
        try:
            rendered: str | None = load_campaign_segment(piece.draft, version=version, context=context).rendered
        except SegmentError:
            rendered = None
        out.append({"clave": piece.phase or piece.kind, "tipo": piece.kind, "nombre": piece.title,
                    "desde": piece.draft["desde"], "hasta": piece.draft["hasta"], "render": rendered})
    return out


class _LiveLabels:
    """La fuente viva de las etiquetas de segmento (``SegmentLabelSource``)."""

    live = True

    def __init__(self, catalog: "LiveSegmentCatalog") -> None:
        self._catalog = catalog

    def segments(self) -> tuple[SegmentLabel, ...]:
        return self._catalog._label_segments

    def labeler(self, label: str) -> Any:
        return self._catalog._label_labelers.get(label)


class _LiveEntries:
    """La fuente viva de la entrada por landing u oferta (``CampaignEntrySource``)."""

    live = True

    def __init__(self, catalog: "LiveSegmentCatalog") -> None:
        self._catalog = catalog

    def campaigns(self) -> tuple[OfferCampaignEntry, ...]:
        return self._catalog._entry_campaigns

    def labeler(self, label: str) -> Any:
        return self._catalog._label_labelers.get(label)


# Lo que hace que una campana publicada no se sirva. Los dice la pagina, en palabras.
NOT_SERVED_DRAFT = "contenido_invalido"
NOT_SERVED_CHECK = "chequeo_no_calza"
NOT_SERVED_CONFLICT = "choca_con_otro_segmento"
_Notify = Callable[[str, str], Awaitable[None]]


class _NotServable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Served:
    """Una fila que se sirve: sus segmentos (una pieza o varias) y lo que leen la entrada y las etiquetas.

    Sin piezas, ``segments`` es el segmento de siempre y el resto sale de el: nada cambia."""

    campaign_id: str
    row: Mapping[str, Any]
    draft: Mapping[str, Any]
    segments: tuple[LoadedSegment, ...]
    key: str
    label: str
    name: str
    version: int
    macro: int | None
    valid_from: datetime
    valid_until: datetime
    offer_code: str | None
    pieced: bool = False
    # 4.8: la campana pausada o terminada de la que solo se sirve el bloque minimo.
    resting: bool = False

    @classmethod
    def of(cls, campaign_id: str, row: Mapping[str, Any], draft: Mapping[str, Any],
           segment: LoadedSegment) -> "_Served":
        offer = segment.knowledge.offer
        return cls(campaign_id=campaign_id, row=row, draft=draft, segments=(segment,), key=segment.key,
                   label=segment.label, name=segment.knowledge.name, version=segment.version,
                   macro=segment.label_macro_id, valid_from=segment.knowledge.valid_from,
                   valid_until=segment.knowledge.valid_until,
                   offer_code=offer.offer_code if offer is not None else None)


class LiveSegmentCatalog:
    """El catalogo del bridge con la campana de la base. Misma interfaz que ``SegmentCatalog``.

    ``fetch`` devuelve ``{"campaigns": [...]}`` de ``get_live_agent_campaigns_v1``. Si
    falla, se sirve lo ultimo bueno (o solo el manifiesto, si nunca leyo). Una campana
    que no se puede servir no toca al resto: los segmentos del manifiesto siguen como
    siempre y el bridge no se cae.
    """

    def __init__(self, *, context: CampaignContext, fetch: Callable[[], Awaitable[Mapping[str, Any]]],
                 notify: _Notify | None = None, refresh_seconds: float = REFRESH_SECONDS,
                 fetch_rests: Callable[[], Awaitable[Mapping[str, Any]]] | None = None) -> None:
        self._context = context
        self._fetch = fetch
        # 4.8: las campanas en reposo (``get_agent_campaign_rests_v1``). Solo con el flag de fases.
        self._fetch_rests = fetch_rests if context.phases_enabled else None
        self._rest_rows: list[Mapping[str, Any]] = []
        self._rest_failures = 0
        self._resting: set[str] = set()
        self._rest_not_served: dict[str, str] = {}
        # clave del bloque minimo -> las claves de las otras piezas de su campana (para no servir los dos).
        self._minimal_of: dict[str, frozenset[str]] = {}
        self._notify = notify
        self._refresh_seconds = refresh_seconds
        self._catalog = context.manifest_catalog
        self._applied: tuple[tuple[str, str, str], ...] | None = None
        self._served: set[str] = set()
        self._not_served: dict[str, str] = {}
        self._announced: set[tuple[str, str]] = set()
        self._failures = 0
        self._entry: CampaignEntry | None = None
        self._entry_base: tuple[EntrySegment, ...] = ()
        self._entry_base_labelers: dict[str, Any] = {}
        self._make_labeler: Callable[[str, int], Any] | None = None
        self._labelers: dict[tuple[str, int], Any] = {}
        self._task: asyncio.Task[None] | None = None
        # E-411: las etiquetas de segmento y la entrada por oferta, con las del AE.
        self._labels_bound = False
        self._label_base: tuple[SegmentLabel, ...] = ()
        self._label_base_labelers: dict[str, Any] = {}
        self._entry_campaigns_base: tuple[OfferCampaignEntry, ...] = ()
        self._label_segments: tuple[SegmentLabel, ...] = ()
        self._label_labelers: dict[str, Any] = {}
        self._entry_campaigns: tuple[OfferCampaignEntry, ...] = ()
        self._label_ae: tuple[SegmentLabel, ...] = ()
        self._label_ae_labelers: dict[str, Any] = {}
        self._entry_campaigns_ae: tuple[OfferCampaignEntry, ...] = ()

    # -- la interfaz de SegmentCatalog --------------------------------------------

    @property
    def segments(self) -> tuple[LoadedSegment, ...]:
        return self._catalog.segments

    @property
    def base_bytes(self) -> int:
        return self._catalog.base_bytes

    def select(self, *, labels: Any, now: datetime) -> SegmentSelection:
        if not self._minimal_of:
            return self._catalog.select(labels=labels, now=now)
        # 4.8 (D-48.4): el bloque minimo de una campana va solo si ninguna otra pieza suya esta vigente.
        active = {segment.key for segment in self._catalog.segments if segment.state_at(now) == STATE_ACTIVE}
        hidden = {key for key, others in self._minimal_of.items() if others & active}
        if not hidden:
            return self._catalog.select(labels=labels, now=now)
        visible = SegmentCatalog(segments=tuple(segment for segment in self._catalog.segments
                                                if segment.key not in hidden),
                                 base_bytes=self._catalog.base_bytes)
        return visible.select(labels=labels, now=now)

    def blocks_for(self, entries: object) -> tuple[LoadedSegment, ...]:
        return self._catalog.blocks_for(entries)

    def readiness(self, now: datetime) -> str:
        return self._catalog.readiness(now)

    # -- lo propio -----------------------------------------------------------------

    @property
    def context(self) -> CampaignContext:
        return self._context

    def serving_state(self, campaign_id: str) -> str | None:
        """``served``, el motivo de por que no se sirve, o None si el catalogo no la conoce."""
        if campaign_id in self._served:
            return "served"
        return self._not_served.get(campaign_id)

    def status_line(self) -> str:
        """Para ``/ready``: cuantas se sirven, cuantas no y si la lectura viene andando."""
        state = "ok" if self._failures == 0 else f"failing:{self._failures}"
        return f"served={len(self._served)},not_served={len(self._not_served)},refresh={state}"

    def phases_status_line(self) -> str:
        """Para ``/ready`` (clave ``agent_campaign_phases``): ``disabled`` sin el flag; si no, cuantas
        se sirven por piezas, cuantas en reposo con su bloque minimo y si la lectura del reposo anda."""
        if not self._context.phases_enabled:
            return "disabled"
        state = "ok" if self._rest_failures == 0 else f"failing:{self._rest_failures}"
        return (f"pieced={len(self._minimal_of)},resting={len(self._resting)},"
                f"rest_not_served={len(self._rest_not_served)},rests_refresh={state}")

    def bind_entry(self, entry: CampaignEntry, *, make_labeler: Callable[[str, int], Any]) -> None:
        """La entrada por palabra clave y pregunta: se recarga con cada cambio de la fila."""
        self._entry = entry
        self._entry_base = tuple(entry.segments)
        self._entry_base_labelers = dict(entry.labelers)
        self._make_labeler = make_labeler

    def bind_labels(self, *, segments: Sequence[SegmentLabel] = (), labelers: Mapping[str, Any] | None = None,
                    campaigns: Sequence[OfferCampaignEntry] = (),
                    make_labeler: Callable[[str, int], Any]) -> None:
        """La herencia, el ``etiquetar`` del flujo y la entrada por oferta (E-411).

        ``segments``/``labelers``: los del manifiesto (lo de hoy). ``campaigns``: las
        ``[[campanas]]``. En cada recarga se suman las campanas servidas del AE, con el
        mismo etiquetador que usa la palabra clave.
        """
        self._labels_bound = True
        self._label_base = tuple(segments)
        self._label_base_labelers = {**self._label_base_labelers, **dict(labelers or {})}
        self._entry_campaigns_base = tuple(campaigns) or self._entry_campaigns_base
        if self._make_labeler is None:
            self._make_labeler = make_labeler
        self._label_segments = (*self._label_base, *self._label_ae)
        self._label_labelers = {**self._label_base_labelers, **self._label_ae_labelers}
        self._entry_campaigns = (*self._entry_campaigns_base, *self._entry_campaigns_ae)

    def bind_entry_campaigns(self, campaigns: Sequence[OfferCampaignEntry], *, labelers: Mapping[str, Any],
                             make_labeler: Callable[[str, int], Any]) -> None:
        """La entrada por oferta (E-411): las ``[[campanas]]`` y sus etiquetadores, sin tocar los segmentos."""
        self.bind_labels(segments=self._label_base, labelers=labelers, campaigns=campaigns,
                         make_labeler=make_labeler)

    def label_segments(self) -> tuple[SegmentLabel, ...]:
        return self._label_segments

    def label_source(self) -> "_LiveLabels":
        return _LiveLabels(self)

    def entry_source(self) -> "_LiveEntries":
        return _LiveEntries(self)

    def _rebuild_labels(self, loaded: Sequence[_Served]) -> None:
        segments: list[SegmentLabel] = []
        labelers: dict[str, Any] = {}
        served: list[tuple[SegmentLabel, str | None]] = []
        for item in loaded:
            if item.resting:
                # 4.8: una campana en reposo no hereda ni etiqueta por flujo ni por oferta.
                continue
            macro = item.macro
            label = SegmentLabel(key=item.key, label=item.label, macro_id=macro,
                                 valid_from=item.valid_from,
                                 valid_until=item.valid_until, origin=ORIGIN_AGENT_CAMPAIGN)
            segments.append(label)
            if macro is not None:
                labelers[item.label] = self._labeler(item.label, int(macro))
            code = item.offer_code
            if code is not None and code in default_offer_codes(self._context.manifest):
                # La oferta por defecto no entra por oferta: etiquetaria todo carrito de su landing.
                code = None
            if code is not None and offer_entry_taken(self._context.manifest, code):
                # D3 de E-411: se sigue sirviendo (bloque y palabra clave), sin la entrada por oferta.
                logger.warning("agent_campaign_entry_skipped key=%s reason=offer_taken", item.key)
                code = None
            served.append((label, code))
        entries = agent_campaign_entries(served, declared_offers(self._context.manifest))
        self._label_ae = tuple(segments)
        self._label_ae_labelers = labelers
        self._entry_campaigns_ae = entries
        self._label_segments = (*self._label_base, *self._label_ae)
        self._label_labelers = {**self._label_base_labelers, **self._label_ae_labelers}
        self._entry_campaigns = (*self._entry_campaigns_base, *self._entry_campaigns_ae)

    def _labeler(self, label: str, macro: int) -> Any:
        assert self._make_labeler is not None
        # El mismo objeto entre recargas: recuerda el macro verificado y las conversaciones etiquetadas.
        return self._labelers.setdefault((label, macro), self._make_labeler(label, macro))

    def _entry_segments(self, item: _Served) -> list[EntrySegment]:
        """La entrada de una fila. Sin piezas, la de siempre: palabra clave y pregunta en la vigencia.

        4.8 (D-48.4): con piezas, la palabra clave sigue etiquetando hasta 14 dias despues del cierre
        (para que quien llega por la pauta reciba el bloque minimo) y la pregunta solo en la vigencia;
        en reposo, solo la palabra clave."""
        draft = item.draft
        keywords = tuple(normalize_entry_text(keyword) for keyword in draft.get("palabras_clave", ()))
        question = draft.get("pregunta_entrada")
        if not item.pieced:
            return [EntrySegment(key=item.key, label=item.label, name=item.name, version=item.version,
                                 valid_from=item.valid_from, valid_until=item.valid_until,
                                 keywords=keywords, question=question)]
        out = []
        if keywords:
            out.append(EntrySegment(key=item.key, label=item.label, name=item.name, version=item.version,
                                    valid_from=item.valid_from, valid_until=item.valid_until + MINIMAL_GRACE,
                                    keywords=keywords))
        if question is not None and not item.resting:
            out.append(EntrySegment(key=item.key, label=item.label, name=item.name, version=item.version,
                                    valid_from=item.valid_from, valid_until=item.valid_until, question=question))
        return out

    def _load_row(self, row: Mapping[str, Any], *, resting: bool = False) -> _Served:
        campaign_id = str(row.get("campaign_id"))
        phases = self._context.phases_enabled
        try:
            draft = parse_campaign(row["draft"], phases=phases)
            approved = datetime.fromisoformat(str(row["approved_at"])).date()
            check_sha = row["check_sha"]
            if not isinstance(check_sha, str):
                raise _NotServable(NOT_SERVED_CHECK)
            version = row["segment_version"]
            pieced = has_pieces(draft)
            if resting and not pieced:
                raise _NotServable(NOT_SERVED_DRAFT)
            if not pieced:
                segment = load_campaign_segment(draft, version=version, context=self._context,
                                                status="aprobado", check_sha=check_sha, approved_on=approved)
                pieces: tuple[CampaignPiece, ...] = ()
            else:
                pieces = campaign_pieces(draft)
        except (CampaignInvalid, SegmentError, KeyError, TypeError, ValueError) as error:
            raise _NotServable(NOT_SERVED_DRAFT) from error
        if draft["macro_etiqueta"] in self._context.reserved_macros:
            raise _NotServable(NOT_SERVED_CONFLICT)
        offers = [draft.get("oferta", {})] + [piece.draft.get("oferta", {}) for piece in pieces]
        if any(offer.get("codigo_oferta") is not None for offer in offers) and not self._context.offer_link_enabled:
            # E-410: sin AGENT_CAMPAIGN_OFFER_LINK_ENABLED el bloque prometeria un link que no sale.
            raise _NotServable(NOT_SERVED_DRAFT)
        if not pieced:
            try:
                verificar_huella(dict(segment.raw), base=self._context.base, otros=self._context.others)
            except (ChequeoError, SegmentCheckError) as error:
                raise _NotServable(NOT_SERVED_CHECK) from error
            return _Served.of(campaign_id, row, draft, segment)
        # D-48.5: la huella de la campana es la de sus piezas; cada pieza lleva la suya en la cabecera.
        try:
            shas = piece_check_shas(draft, version=version, context=self._context)
        except (ChequeoError, SegmentCheckError, SegmentError, ValueError) as error:
            raise _NotServable(NOT_SERVED_CHECK) from error
        if phased_check_sha(shas) != check_sha:
            raise _NotServable(NOT_SERVED_CHECK)
        segments = []
        try:
            for index, (piece, (_, piece_sha)) in enumerate(zip(pieces, shas)):
                if resting and not piece.minimal:
                    continue
                priority = (REST_PRIORITY if resting else MINIMAL_PRIORITY) if piece.minimal else CAMPAIGN_PRIORITY
                segments.append(load_campaign_segment(piece.draft, version=version, context=self._context,
                                                      status="aprobado", check_sha=piece_sha, approved_on=approved,
                                                      priority=priority))
        except SegmentError as error:
            raise _NotServable(NOT_SERVED_DRAFT) from error
        offer = draft.get("oferta", {})
        return _Served(campaign_id=campaign_id, row=row, draft=draft, segments=tuple(segments), key=draft["clave"],
                       label=draft["etiqueta"], name=draft["nombre"], version=version,
                       macro=draft["macro_etiqueta"], valid_from=datetime.fromisoformat(draft["desde"]),
                       valid_until=datetime.fromisoformat(draft["hasta"]), offer_code=offer.get("codigo_oferta"),
                       pieced=True, resting=resting)

    def _rebuild(self, rows: Sequence[Mapping[str, Any]], rests: Sequence[Mapping[str, Any]] = ()) -> None:
        loaded: list[_Served] = []
        not_served: dict[str, str] = {}
        manifest_catalog = self._context.manifest_catalog

        def fits(item: _Served) -> None:
            build_catalog([*manifest_catalog.segments, *(segment for other in loaded for segment in other.segments),
                           *item.segments], base_bytes=manifest_catalog.base_bytes)

        for row in rows:
            campaign_id = str(row.get("campaign_id"))
            try:
                item = self._load_row(row)
                fits(item)
            except _NotServable as error:
                not_served[campaign_id] = error.reason
                continue
            except SegmentError:
                not_served[campaign_id] = NOT_SERVED_CONFLICT
                continue
            loaded.append(item)
        rest_not_served: dict[str, str] = {}
        if rests:
            # D-48.6: en reposo, solo si ninguna servida tiene su etiqueta o su clave; una por clave
            # (la version mas alta) y hasta MAX_RESTS, la mas reciente primero.
            taken_labels = {item.label for item in loaded}
            taken_keys = {item.key for item in loaded}
            ordered = sorted(rests, key=lambda row: (str(row.get("approved_at")), int(row.get("segment_version") or 0)),
                             reverse=True)
            seen_keys: set[str] = set()
            for row in ordered:
                campaign_id = str(row.get("campaign_id"))
                key = str(row.get("campaign_key"))
                if key in taken_keys or key in seen_keys or str(row.get("label")) in taken_labels:
                    continue
                seen_keys.add(key)
                if len([item for item in loaded if item.resting]) >= MAX_RESTS:
                    break
                try:
                    item = self._load_row(row, resting=True)
                    index = len([other for other in loaded if other.resting])
                    item = replace(item, segments=tuple(replace(segment, priority=REST_PRIORITY + index)
                                                        for segment in item.segments))
                    fits(item)
                except _NotServable as error:
                    rest_not_served[campaign_id] = error.reason
                    continue
                except SegmentError:
                    rest_not_served[campaign_id] = NOT_SERVED_CONFLICT
                    continue
                taken_labels.add(item.label)
                loaded.append(item)
        if self._entry is not None:
            kept: list[_Served] = []
            for item in loaded:
                try:
                    self._reload_entry([*kept, item])
                except CampaignEntryConfigError:
                    if item.resting:
                        # La palabra clave ya es de otra: el bloque minimo sigue para quien ya tiene la etiqueta.
                        kept.append(replace(item, draft={**item.draft, "palabras_clave": []}))
                        continue
                    not_served[item.campaign_id] = NOT_SERVED_CONFLICT
                    continue
                kept.append(item)
            loaded = kept
            self._reload_entry(loaded)
        if self._labels_bound:
            self._rebuild_labels(loaded)
        self._catalog = build_catalog([*manifest_catalog.segments,
                                       *(segment for item in loaded for segment in item.segments)],
                                      base_bytes=manifest_catalog.base_bytes)
        self._served = {item.campaign_id for item in loaded if not item.resting}
        self._not_served = not_served
        self._resting = {item.campaign_id for item in loaded if item.resting}
        self._rest_not_served = rest_not_served
        minimal_of: dict[str, frozenset[str]] = {}
        for item in loaded:
            if item.pieced and not item.resting:
                minimal = [segment.key for segment in item.segments if segment.key.endswith(
                    f"{PIECE_SEPARATOR}{MINIMAL_PIECE}")]
                others = frozenset(segment.key for segment in item.segments if segment.key not in minimal)
                minimal_of.update({key: others for key in minimal})
        self._minimal_of = minimal_of

    def _reload_entry(self, loaded: Sequence[_Served]) -> None:
        assert self._entry is not None
        segments = list(self._entry_base)
        labelers = dict(self._entry_base_labelers)
        for item in loaded:
            entries = self._entry_segments(item)
            segments.extend(entries)
            if entries:
                labelers[item.label] = self._labeler(item.label, int(item.macro or 0))
        self._entry.reload(segments, labelers)

    async def refresh(self) -> bool:
        """Lee la base y actualiza. Nunca lanza. False si no se pudo leer."""
        try:
            payload = await self._fetch()
            rows = payload["campaigns"]
            if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
                raise TypeError("campaigns")
        except Exception as error:  # noqa: BLE001 - la base caida no tira el turno de nadie
            self._failures += 1
            logger.warning("agent_campaigns_refresh_failed failures=%s error=%s", self._failures,
                           type(error).__name__)
            return False
        self._failures = 0
        key = tuple(sorted((str(row.get("campaign_id")), str(row.get("fingerprint")), str(row.get("check_sha")))
                           for row in rows))
        rests: list[Mapping[str, Any]] = []
        if self._fetch_rests is not None:
            rests = await self._read_rests()
            key = (*key, ("rests",), *sorted(
                (str(row.get("campaign_id")), str(row.get("status")), str(row.get("fingerprint")),
                 str(row.get("check_sha"))) for row in rests))
        if key == self._applied:
            return True
        try:
            self._rebuild(rows, rests)
        except Exception as error:  # noqa: BLE001 - lo ultimo bueno sigue en pie
            self._failures += 1
            logger.warning("agent_campaigns_rebuild_failed error=%s", type(error).__name__)
            return False
        self._applied = key
        await self._after_change(rows)
        return True

    async def _read_rests(self) -> list[Mapping[str, Any]]:
        """Las campanas en reposo. Si la base no contesta, las ultimas buenas: el resto sigue igual."""
        assert self._fetch_rests is not None
        try:
            payload = await self._fetch_rests()
            rows = payload["campaigns"]
            if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
                raise TypeError("campaigns")
        except Exception as error:  # noqa: BLE001 - sin el reposo, se sirve lo de siempre
            self._rest_failures += 1
            logger.warning("agent_campaign_rests_refresh_failed failures=%s error=%s", self._rest_failures,
                           type(error).__name__)
            return self._rest_rows
        self._rest_failures = 0
        self._rest_rows = list(rows)
        return self._rest_rows

    async def _after_change(self, rows: Sequence[Mapping[str, Any]]) -> None:
        keys = {str(row.get("campaign_id")): row for row in rows}
        for campaign_id in self._served:
            row = keys[campaign_id]
            logger.info("agent_campaign_served key=%s version=%s", row.get("campaign_key"), row.get("segment_version"))
        for campaign_id in self._resting:
            row = next((item for item in self._rest_rows if str(item.get("campaign_id")) == campaign_id), {})
            logger.info("agent_campaign_resting key=%s version=%s status=%s", row.get("campaign_key"),
                        row.get("segment_version"), row.get("status"))
        for campaign_id, reason in self._rest_not_served.items():
            row = next((item for item in self._rest_rows if str(item.get("campaign_id")) == campaign_id), {})
            logger.warning("agent_campaign_rest_not_served key=%s version=%s reason=%s", row.get("campaign_key"),
                           row.get("segment_version"), reason)
        for campaign_id, reason in self._not_served.items():
            row = keys[campaign_id]
            logger.warning("agent_campaign_not_served key=%s version=%s reason=%s", row.get("campaign_key"),
                           row.get("segment_version"), reason)
            if (campaign_id, reason) in self._announced or self._notify is None:
                continue
            self._announced.add((campaign_id, reason))
            try:
                await self._notify(str(row.get("campaign_key")), reason)
            except Exception as error:  # noqa: BLE001 - el aviso no cambia lo que se sirve
                if type(error).__name__ == "ConnectorSemanticConflict" and str(error) == "connector_semantic_conflict":
                    # E-572: el id del aviso sale de la campana y el motivo, pero la hora es la
                    # del arranque. Despues de un reinicio el conector ya lo tiene, con otra
                    # hora, y contesta 409: el aviso ya salio una vez, como pide el contrato.
                    # Un tenant que no calza (connector_tenant_mismatch) sigue siendo una falla.
                    logger.info("agent_campaign_notice_already_admitted key=%s reason=%s",
                                row.get("campaign_key"), reason)
                    continue
                logger.warning("agent_campaign_notice_failed error=%s", type(error).__name__)

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="agent-campaigns-refresh")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            await self.refresh()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


def utc_now() -> datetime:
    return datetime.now(UTC)
