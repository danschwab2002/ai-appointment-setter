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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

from .campaign_inbound_entry import (
    CampaignEntry,
    CampaignEntryConfigError,
    EntrySegment,
    normalize_entry_text,
)
from .commercial_knowledge import CommercialKnowledge
from .flow_change_api import looks_like_identifier
from .instance_manifest import RESERVED_CHATWOOT_LABELS, InstanceManifest
from .segment_check import ChequeoError, SegmentCheckError, calcular_chequeo_sha
from .segment_check import verificar_chequeo as verificar_huella
from .segment_knowledge import (
    MAX_INSTRUCTION_CHARS,
    LoadedSegment,
    SegmentCatalog,
    SegmentError,
    SegmentKnowledge,
    SegmentSelection,
    build_catalog,
)

__all__ = [
    "CONTRACT",
    "RULES_VERSION",
    "CampaignContext",
    "CampaignInvalid",
    "LiveSegmentCatalog",
    "expected_check_sha",
    "instance_blocks",
    "parse_campaign",
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
_OFFER_KEYS = {"producto", "precio", "moneda", "precio_antes", "cupon", "codigo_oferta", "incluye", "garantia",
               "medios", "reemplaza"}
_LIST_SECTIONS = ("se_puede_decir", "no_confirmado", "promesas_prohibidas")
_QA_SECTIONS = ("faq", "preguntas_del_alta")
# Las etiquetas que no pueden abrir una campana: las mismas que un segmento del manifiesto.
_FORBIDDEN_LABELS = frozenset((*RESERVED_CHATWOOT_LABELS, "cliente"))


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


def parse_campaign(raw: Any) -> dict[str, Any]:
    """Lo que manda el agente, normalizado. Lo que no cierra se rechaza con el campo, sin el valor."""
    if not isinstance(raw, Mapping):
        raise _refused("campaign", "not_an_object")
    missing, extra = _REQUIRED - set(raw), set(raw) - _REQUIRED - _OPTIONAL
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
    return draft


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

    @property
    def product_names(self) -> dict[str, str]:
        return {product.key: product.offer_name for product in self.base.products if product.key is not None}

    @property
    def others(self) -> list[dict[str, Any]]:
        return [dict(segment.raw) for segment in self.manifest_catalog.segments]


def load_campaign_segment(draft: Mapping[str, Any], *, version: int, context: CampaignContext, status: str = "borrador",
                          check_sha: str | None = None, approved_on: date | None = None) -> LoadedSegment:
    """La campana como un segmento cargado. Lanza ``SegmentError`` si el esquema no la acepta."""
    raw = segment_mapping(draft, ally_ref=context.manifest.ally_ref, version=version, status=status,
                          check_sha=check_sha, approved_on=approved_on)
    knowledge = SegmentKnowledge.from_mapping(raw, time_zone=context.manifest.time_zone,
                                              product_names=context.product_names,
                                              principal_name=context.base.products[0].offer_name,
                                              require_approved=status == "aprobado")
    rendered = knowledge.render()
    return LoadedSegment(key=knowledge.key, label=draft["etiqueta"], priority=CAMPAIGN_PRIORITY,
                         knowledge_path=f"agent_campaigns/{knowledge.key}-v{knowledge.version}",
                         label_macro_id=draft["macro_etiqueta"], knowledge=knowledge, rendered=rendered,
                         sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(), raw=MappingProxyType(raw))


def expected_check_sha(draft: Mapping[str, Any], *, version: int, context: CampaignContext) -> str:
    """El ``chequeo_sha`` que tiene que tener hoy esta campana (la misma funcion que el cargador)."""
    raw = segment_mapping(draft, ally_ref=context.manifest.ally_ref, version=version)
    return calcular_chequeo_sha(raw, base_sha256=context.base.rendered_sha256, otros=context.others)


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
            if codes is None or offer_code not in codes:
                blocks.append(_finding("campaign_offer_unknown", "oferta.codigo_oferta"))
            warnings.append(_finding("campaign_offer_not_verified", "oferta.codigo_oferta"))
    return blocks, warnings, loaded


# Lo que hace que una campana publicada no se sirva. Los dice la pagina, en palabras.
NOT_SERVED_DRAFT = "contenido_invalido"
NOT_SERVED_CHECK = "chequeo_no_calza"
NOT_SERVED_CONFLICT = "choca_con_otro_segmento"
_Notify = Callable[[str, str], Awaitable[None]]


class _NotServable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LiveSegmentCatalog:
    """El catalogo del bridge con la campana de la base. Misma interfaz que ``SegmentCatalog``.

    ``fetch`` devuelve ``{"campaigns": [...]}`` de ``get_live_agent_campaigns_v1``. Si
    falla, se sirve lo ultimo bueno (o solo el manifiesto, si nunca leyo). Una campana
    que no se puede servir no toca al resto: los segmentos del manifiesto siguen como
    siempre y el bridge no se cae.
    """

    def __init__(self, *, context: CampaignContext, fetch: Callable[[], Awaitable[Mapping[str, Any]]],
                 notify: _Notify | None = None, refresh_seconds: float = REFRESH_SECONDS) -> None:
        self._context = context
        self._fetch = fetch
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

    # -- la interfaz de SegmentCatalog --------------------------------------------

    @property
    def segments(self) -> tuple[LoadedSegment, ...]:
        return self._catalog.segments

    @property
    def base_bytes(self) -> int:
        return self._catalog.base_bytes

    def select(self, *, labels: Any, now: datetime) -> SegmentSelection:
        return self._catalog.select(labels=labels, now=now)

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

    def bind_entry(self, entry: CampaignEntry, *, make_labeler: Callable[[str, int], Any]) -> None:
        """La entrada por palabra clave y pregunta: se recarga con cada cambio de la fila."""
        self._entry = entry
        self._entry_base = tuple(entry.segments)
        self._entry_base_labelers = dict(entry.labelers)
        self._make_labeler = make_labeler

    def _labeler(self, label: str, macro: int) -> Any:
        assert self._make_labeler is not None
        # El mismo objeto entre recargas: recuerda el macro verificado y las conversaciones etiquetadas.
        return self._labelers.setdefault((label, macro), self._make_labeler(label, macro))

    def _entry_segment(self, row: Mapping[str, Any], segment: LoadedSegment) -> EntrySegment:
        draft = row["draft"]
        return EntrySegment(key=segment.key, label=segment.label, name=segment.knowledge.name,
                            version=segment.version, valid_from=segment.knowledge.valid_from,
                            valid_until=segment.knowledge.valid_until,
                            keywords=tuple(normalize_entry_text(keyword) for keyword in draft.get("palabras_clave", ())),
                            question=draft.get("pregunta_entrada"))

    def _load_row(self, row: Mapping[str, Any]) -> LoadedSegment:
        try:
            draft = parse_campaign(row["draft"])
            approved = datetime.fromisoformat(str(row["approved_at"])).date()
            check_sha = row["check_sha"]
            if not isinstance(check_sha, str):
                raise _NotServable(NOT_SERVED_CHECK)
            segment = load_campaign_segment(draft, version=row["segment_version"], context=self._context,
                                            status="aprobado", check_sha=check_sha, approved_on=approved)
        except (CampaignInvalid, SegmentError, KeyError, TypeError, ValueError) as error:
            raise _NotServable(NOT_SERVED_DRAFT) from error
        if draft["macro_etiqueta"] in self._context.reserved_macros:
            raise _NotServable(NOT_SERVED_CONFLICT)
        try:
            verificar_huella(dict(segment.raw), base=self._context.base, otros=self._context.others)
        except (ChequeoError, SegmentCheckError) as error:
            raise _NotServable(NOT_SERVED_CHECK) from error
        return segment

    def _rebuild(self, rows: Sequence[Mapping[str, Any]]) -> None:
        loaded: list[tuple[str, Mapping[str, Any], LoadedSegment]] = []
        not_served: dict[str, str] = {}
        manifest_catalog = self._context.manifest_catalog
        for row in rows:
            campaign_id = str(row.get("campaign_id"))
            try:
                segment = self._load_row(row)
                build_catalog([*manifest_catalog.segments, *(item[2] for item in loaded), segment],
                              base_bytes=manifest_catalog.base_bytes)
            except _NotServable as error:
                not_served[campaign_id] = error.reason
                continue
            except SegmentError:
                not_served[campaign_id] = NOT_SERVED_CONFLICT
                continue
            loaded.append((campaign_id, row, segment))
        if self._entry is not None:
            kept: list[tuple[str, Mapping[str, Any], LoadedSegment]] = []
            for item in loaded:
                try:
                    self._reload_entry([*kept, item])
                except CampaignEntryConfigError:
                    not_served[item[0]] = NOT_SERVED_CONFLICT
                    continue
                kept.append(item)
            loaded = kept
            self._reload_entry(loaded)
        self._catalog = build_catalog([*manifest_catalog.segments, *(item[2] for item in loaded)],
                                      base_bytes=manifest_catalog.base_bytes)
        self._served = {item[0] for item in loaded}
        self._not_served = not_served

    def _reload_entry(self, loaded: Sequence[tuple[str, Mapping[str, Any], LoadedSegment]]) -> None:
        assert self._entry is not None
        segments = list(self._entry_base)
        labelers = dict(self._entry_base_labelers)
        for _, row, segment in loaded:
            segments.append(self._entry_segment(row, segment))
            labelers[segment.label] = self._labeler(segment.label, int(segment.label_macro_id or 0))
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
        if key == self._applied:
            return True
        try:
            self._rebuild(rows)
        except Exception as error:  # noqa: BLE001 - lo ultimo bueno sigue en pie
            self._failures += 1
            logger.warning("agent_campaigns_rebuild_failed error=%s", type(error).__name__)
            return False
        self._applied = key
        await self._after_change(rows)
        return True

    async def _after_change(self, rows: Sequence[Mapping[str, Any]]) -> None:
        keys = {str(row.get("campaign_id")): row for row in rows}
        for campaign_id in self._served:
            row = keys[campaign_id]
            logger.info("agent_campaign_served key=%s version=%s", row.get("campaign_key"), row.get("segment_version"))
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
