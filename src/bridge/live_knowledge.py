"""El conocimiento vivo: una version del conocimiento base editada por el AE (live-knowledge-v1).

Contrato: ``docs/contracts/live-knowledge-v1.md``. Pieza 5.5 de la lista definitiva:
hoy cambiar el conocimiento es un archivo nuevo en el repo de la instancia, la firma y
un redeploy. Con ``AGENT_LIVE_KNOWLEDGE_ENABLED`` el AE redacta con el duenio un cambio
por secciones sobre lo vigente, el bridge lo valida con el MISMO cargador
(``CommercialKnowledge.from_mapping``) y contra la instancia, una persona de la lista de
aprobadores lo aprueba en la pagina y el bridge lo sirve en vivo.

Este modulo tiene dos partes:

* Puro: ``parse_change`` (la forma del cambio), ``apply_change``, ``build_knowledge``,
  ``instance_blocks`` (las reglas contra la instancia), ``evaluate`` (todo junto, lo que
  usan la vista previa, proponer y aprobar) y ``render_diff`` (lo que muestra la pagina).
* ``LiveKnowledgeSource``: lo que lee el turno. Se refresca de la base con cache corta,
  se queda con lo ultimo bueno si la base no contesta y, si nunca hubo una version viva
  (o se volvio al archivo), sirve el archivo del manifiesto como hoy.

El render y el hash son los del cargador: deterministas. La procedencia del turno dice
cual se uso (``ServedKnowledge.ref``).

D-55.4 (nunca a la vez con un segmento): el ``chequeo_sha`` de un segmento se calcula
contra el hash del conocimiento base. Mientras haya un segmento en vigencia (del
manifiesto o una campana del AE servida) la version viva no se sirve y el agente recibe
el archivo, contra el que se firmo el segmento; aprobar tambien se bloquea.

Los logs llevan el id, la version y el motivo; nunca el texto del conocimiento.
"""

from __future__ import annotations

import asyncio
import copy
import difflib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from .commercial_knowledge import MAX_RENDERED_BYTES, CommercialKnowledge, KnowledgeError
from .price_guard import (
    ANY_CURRENCY,
    Amount,
    PriceGuard,
    allowed_text,
    amount_of,
    extract_amounts,
    guard_from_manifest,
)

__all__ = [
    "CONTRACT",
    "FLAG_ENV",
    "KnowledgeChangeInvalid",
    "KnowledgeContext",
    "LiveKnowledgeSource",
    "ServedKnowledge",
    "apply_change",
    "build_knowledge",
    "evaluate",
    "file_knowledge",
    "parse_change",
    "render_diff",
]

logger = logging.getLogger(__name__)

CONTRACT = "live-knowledge-v1"
RULES_VERSION = 1
FLAG_ENV = "AGENT_LIVE_KNOWLEDGE_ENABLED"
REFRESH_SECONDS = 30.0
# El cambio viaja en el cuerpo de la ruta (256 KB); el conocimiento entero cabe holgado.
MAX_CHANGE_BYTES = 128 * 1024
# La pagina muestra hasta esto del diff; lo demas se corta y se dice.
MAX_DIFF_LINES = 600
NEAR_LIMIT_RATIO = 0.9

ORIGIN_FILE = "archivo"
ORIGIN_LIVE = "vivo"

# Las tablas de primer nivel que el AE puede reemplazar. La cabecera la arma el bridge.
SECTIONS = ("identidad", "voz", "oferta", "contenido", "no_confirmado", "promesas_prohibidas",
            "limites_sensibles", "faq", "pagos", "se_puede_decir")
REMOVABLE = frozenset({"faq", "pagos", "se_puede_decir"})
PRODUCT_PREFIX = "productos."
_PRODUCT_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")

# Por que una version publicada no se sirve. Los dice la pagina, en palabras.
NOT_SERVED_FILE_CHANGED = "archivo_cambio"
NOT_SERVED_RENDER = "render_no_calza"
NOT_SERVED_INVALID = "contenido_invalido"
NOT_SERVED_SEGMENTS = "segmentos_vigentes"


class KnowledgeChangeInvalid(ValueError):
    """El cambio no tiene la forma del contrato. ``reason`` nombra el campo, nunca un valor."""

    def __init__(self, problem: str, field_name: str = "cambios") -> None:
        super().__init__(problem)
        self.code = "knowledge_change_invalid"
        self.reason = {"field": field_name, "problem": problem}


@dataclass(frozen=True)
class ServedKnowledge:
    """El conocimiento que recibe el turno: el del archivo o una version viva."""

    origin: str
    knowledge: CommercialKnowledge
    sections: Mapping[str, Any]
    rendered: str
    sha256: str
    file_version: int
    file_sha256: str
    knowledge_id: str | None = None
    live_version: int | None = None

    def ref(self) -> dict[str, Any]:
        """La procedencia del turno: con que conocimiento contesto el agente."""
        if self.origin == ORIGIN_LIVE:
            return {"origen": ORIGIN_LIVE, "knowledge_id": self.knowledge_id, "live_version": self.live_version,
                    "version": self.file_version, "sha256": self.sha256}
        return {"origen": ORIGIN_FILE, "version": self.file_version, "sha256": self.sha256}


@dataclass(frozen=True)
class KnowledgeContext:
    """Lo que las reglas miran de la instancia: el manifiesto, el archivo y los montos del catalogo."""

    manifest: Any
    file: ServedKnowledge
    structured: frozenset[Amount] = field(default_factory=frozenset)

    @classmethod
    def build(cls, manifest: Any, file: ServedKnowledge) -> KnowledgeContext:
        return cls(manifest=manifest, file=file, structured=guard_from_manifest(manifest).structured)


def _json_safe(value: Any, where: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise KnowledgeError(f"{where} no se puede guardar como JSON") from error


def file_knowledge(knowledge: CommercialKnowledge, raw: Mapping[str, Any]) -> ServedKnowledge:
    """El archivo del manifiesto como ``ServedKnowledge``. ``raw`` es el dict de tomllib del mismo archivo."""
    again = CommercialKnowledge.from_mapping(raw, require_approved=False)
    if again.render() != knowledge.render():  # pragma: no cover - el mismo archivo leido dos veces
        raise KnowledgeError("el archivo de conocimiento cambio mientras se leia")
    sections = _json_safe({key: value for key, value in raw.items() if key != "cabecera"}, "el conocimiento")
    return ServedKnowledge(origin=ORIGIN_FILE, knowledge=knowledge, sections=sections, rendered=knowledge.render(),
                           sha256=knowledge.rendered_sha256, file_version=knowledge.version,
                           file_sha256=knowledge.rendered_sha256)


def parse_change(raw: Any) -> dict[str, Any]:
    """``{"cambios": {...}}`` normalizado (D-55.1). Lanza ``KnowledgeChangeInvalid``."""
    if not isinstance(raw, Mapping) or set(raw) != {"cambios"}:
        raise KnowledgeChangeInvalid("debe_ser_un_objeto_con_cambios", "body")
    changes = raw["cambios"]
    if not isinstance(changes, Mapping) or not changes:
        raise KnowledgeChangeInvalid("cambios_vacio_o_no_objeto")
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if not isinstance(key, str):
            raise KnowledgeChangeInvalid("clave_no_texto")
        if key.startswith(PRODUCT_PREFIX):
            product = key[len(PRODUCT_PREFIX):]
            if _PRODUCT_KEY.fullmatch(product) is None:
                raise KnowledgeChangeInvalid("clave_de_producto_invalida", "cambios.productos")
            if not isinstance(value, Mapping):
                raise KnowledgeChangeInvalid("producto_debe_ser_una_tabla", "cambios.productos")
            if value.get("clave") != product:
                raise KnowledgeChangeInvalid("la_clave_del_producto_no_cambia", "cambios.productos")
        elif key not in SECTIONS:
            raise KnowledgeChangeInvalid("seccion_no_editable", "cambios")
        elif value is None:
            if key not in REMOVABLE:
                raise KnowledgeChangeInvalid("seccion_obligatoria", f"cambios.{key}")
        elif not isinstance(value, (Mapping, list)):
            raise KnowledgeChangeInvalid("seccion_debe_ser_tabla_o_lista", f"cambios.{key}")
        out[key] = value
    normalized = {"cambios": out}
    try:
        encoded = json.dumps(normalized, ensure_ascii=False, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        raise KnowledgeChangeInvalid("no_es_json") from None
    if len(encoded.encode("utf-8")) > MAX_CHANGE_BYTES:
        raise KnowledgeChangeInvalid("cambio_demasiado_grande")
    return json.loads(encoded)


def apply_change(sections: Mapping[str, Any], change: Mapping[str, Any]) -> dict[str, Any]:
    """Las secciones vigentes con el cambio aplicado. Lo que no se nombra queda igual."""
    out = copy.deepcopy(dict(sections))
    for key, value in change["cambios"].items():
        if key.startswith(PRODUCT_PREFIX):
            products = out.get("productos")
            if not isinstance(products, list):
                raise KnowledgeChangeInvalid("el_conocimiento_no_es_por_producto", "cambios.productos")
            product = key[len(PRODUCT_PREFIX):]
            for index, entry in enumerate(products):
                if isinstance(entry, Mapping) and entry.get("clave") == product:
                    products[index] = copy.deepcopy(value)
                    break
            else:
                raise KnowledgeChangeInvalid("producto_inexistente", "cambios.productos")
        elif value is None:
            out.pop(key, None)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _header(file_version: int, ally_ref: str, *, approved_by: str | None = None,
            approved_on: date | None = None) -> dict[str, Any]:
    if approved_by is None or approved_on is None:
        return {"knowledge_version": file_version, "estado": "borrador", "ally_ref": ally_ref}
    return {"knowledge_version": file_version, "estado": "aprobado", "ally_ref": ally_ref,
            "aprobado_por": approved_by, "aprobado_el": approved_on}


def build_knowledge(sections: Mapping[str, Any], *, file_version: int, ally_ref: str,
                    approved_by: str | None = None, approved_on: date | None = None) -> CommercialKnowledge:
    """El cargador del contrato sobre las secciones, con la cabecera que arma el bridge.

    El render no usa el estado ni quien aprobo: el hash de lo validado es el de lo servido.
    """
    if "cabecera" in sections:
        raise KnowledgeChangeInvalid("la_cabecera_la_arma_el_bridge")
    header = _header(file_version, ally_ref, approved_by=approved_by, approved_on=approved_on)
    return CommercialKnowledge.from_mapping({**sections, "cabecera": header},
                                            require_approved=header["estado"] == "aprobado")


def _finding(code: str, field_name: str | None = None, detail: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"code": code}
    if field_name is not None:
        out["field"] = field_name
    if detail is not None:
        out["detail"] = detail
    return out


def instance_blocks(knowledge: CommercialKnowledge, *, context: KnowledgeContext) -> list[dict[str, Any]]:
    """Las reglas contra la instancia (contrato, «Las reglas contra la instancia»)."""
    blocks: list[dict[str, Any]] = []
    file = context.file.knowledge
    manifest = context.manifest
    if knowledge.by_product != file.by_product:
        blocks.append(_finding("knowledge_shape_changed"))
        return blocks
    keys = tuple(product.key for product in knowledge.products)
    error = manifest.knowledge_products_error(keys) if hasattr(manifest, "knowledge_products_error") else None
    if error is not None or keys != tuple(product.key for product in file.products):
        blocks.append(_finding("knowledge_products_mismatch", "productos", error))
    file_names = {product.key: product.offer_name for product in file.products}
    for product in knowledge.products:
        where = "oferta" if product.key is None else f"productos.{product.key}.oferta"
        if product.key in file_names and product.offer_name != file_names[product.key]:
            blocks.append(_finding("knowledge_offer_name_changed", f"{where}.nombre"))
        price = amount_of(product.offer_price, product.offer_currency)
        if price is None or not _in_catalog(price, context.structured):
            blocks.append(_finding("knowledge_price_not_in_catalog", f"{where}.precio"))
    allowed = context.structured | extract_amounts(allowed_text(context.file.rendered))
    for amount in PriceGuard.blocked(allowed_text(knowledge.render()), frozenset(allowed)):
        # El monto es del catalogo o del texto que escribio el AE: nunca de una persona.
        blocks.append(_finding("knowledge_amount_not_in_catalog", detail=amount.label()))
    return blocks


def _in_catalog(amount: Amount, structured: frozenset[Amount]) -> bool:
    """La misma regla que ``PriceGuard.blocked``, para un dato estructurado (precio + moneda)."""
    if amount in structured:
        return True
    if amount.currency == ANY_CURRENCY:
        return amount.value in {item.value for item in structured}
    return amount.value in {item.value for item in structured if item.currency == ANY_CURRENCY}


@dataclass(frozen=True)
class Evaluation:
    blocks: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    sections: dict[str, Any] | None = None
    knowledge: CommercialKnowledge | None = None

    @property
    def rendered(self) -> str | None:
        return self.knowledge.render() if self.knowledge is not None else None


def evaluate(change: Mapping[str, Any], *, base: ServedKnowledge, context: KnowledgeContext,
             harness: bool = False) -> Evaluation:
    """El cambio (ya normalizado) sobre ``base``: bloqueos, avisos y la version armada.

    ``harness`` (5.5 parte 2, ``AGENT_LIVE_KNOWLEDGE_HARNESS_ENABLED``): el bridge corre el arnes F-055
    sobre la version guardada, asi que no se avisa ``knowledge_harness_not_run``. Sin el flag, igual que antes."""
    try:
        sections = apply_change(base.sections, change)
    except KnowledgeChangeInvalid as error:
        return Evaluation(blocks=[_finding(error.code, error.reason["field"], error.reason["problem"])], warnings=[])
    try:
        knowledge = build_knowledge(sections, file_version=context.file.file_version,
                                    ally_ref=context.file.knowledge.ally_ref)
    except KnowledgeChangeInvalid as error:
        return Evaluation(blocks=[_finding(error.code, error.reason["field"], error.reason["problem"])], warnings=[])
    except KnowledgeError as error:
        # El mensaje del cargador nombra la seccion y la clave, no el contenido.
        return Evaluation(blocks=[_finding("knowledge_invalid", detail=str(error)[:300])], warnings=[])
    blocks = instance_blocks(knowledge, context=context)
    if knowledge.rendered_sha256 == base.sha256:
        blocks.append(_finding("knowledge_change_empty"))
    warnings = [] if harness else [_finding("knowledge_harness_not_run")]
    size = len(knowledge.render().encode("utf-8"))
    if size > MAX_RENDERED_BYTES * NEAR_LIMIT_RATIO:
        warnings.append(_finding("knowledge_near_size_limit", detail=f"{size}/{MAX_RENDERED_BYTES}"))
    return Evaluation(blocks=blocks, warnings=warnings, sections=sections, knowledge=knowledge)


def render_diff(before: str, after: str) -> dict[str, Any]:
    """El diff del render, linea a linea, para la pagina y el AE."""
    lines: list[dict[str, str]] = []
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=2):
        if line.startswith(("---", "+++")):
            continue
        kind = {"+": "agrega", "-": "saca", "@": "salto"}.get(line[:1], "igual")
        lines.append({"tipo": kind, "texto": "" if kind == "salto" else line[1:]})
    added = sum(1 for line in lines if line["tipo"] == "agrega")
    removed = sum(1 for line in lines if line["tipo"] == "saca")
    return {"lineas": lines[:MAX_DIFF_LINES], "truncado": len(lines) > MAX_DIFF_LINES,
            "agregadas": added, "sacadas": removed}


def changed_sections(change: Mapping[str, Any]) -> list[str]:
    return sorted(change["cambios"])


# ---------------------------------------------------------------------------- el servido


class _NotServable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def utc_now() -> datetime:
    return datetime.now(UTC)


class LiveKnowledgeSource:
    """Lo que el turno recibe. ``current()`` no espera a la base: la lectura corre aparte.

    ``fetch`` devuelve ``{"knowledge": <fila publicada o null>}`` de ``get_live_agent_knowledge_v1``.
    ``segments_in_force`` (D-55.4): las claves de los segmentos en vigencia; con alguna, se sirve
    el archivo.
    """

    def __init__(self, *, context: KnowledgeContext, fetch: Callable[[], Awaitable[Mapping[str, Any]]],
                 segments_in_force: Callable[[], tuple[str, ...]] | None = None,
                 refresh_seconds: float = REFRESH_SECONDS) -> None:
        self._context = context
        self._fetch = fetch
        self._segments_in_force = segments_in_force or (lambda: ())
        self._refresh_seconds = refresh_seconds
        self._live: ServedKnowledge | None = None
        self._published_id: str | None = None
        self._not_served: str | None = None
        self._applied: tuple[str, str, str] | None = None
        self._failures = 0
        self._read_once = False
        self._task: asyncio.Task[None] | None = None

    @property
    def context(self) -> KnowledgeContext:
        return self._context

    @property
    def file(self) -> ServedKnowledge:
        return self._context.file

    def segments_in_force(self) -> tuple[str, ...]:
        try:
            return tuple(self._segments_in_force())
        except Exception as error:  # noqa: BLE001 - sin poder mirarlo, lo seguro es el archivo
            logger.warning("live_knowledge_segments_unreadable error=%s", type(error).__name__)
            return ("desconocido",)

    def current(self) -> ServedKnowledge:
        live = self._live
        if live is None:
            return self._context.file
        if self.segments_in_force():
            return self._context.file
        return live

    def edit_base(self) -> ServedKnowledge:
        """Sobre que se arma un cambio: la viva publicada (aunque un segmento la tape) o el archivo."""
        return self._live or self._context.file

    def serving_state(self, knowledge_id: str) -> str | None:
        """``served``, el motivo de por que no se sirve, o None si no es la publicada."""
        if knowledge_id != self._published_id:
            return None
        if self._live is not None:
            return NOT_SERVED_SEGMENTS if self.segments_in_force() else "served"
        return self._not_served

    def status_line(self) -> str:
        served = self.current()
        origin = ORIGIN_FILE if served.origin == ORIGIN_FILE else f"{ORIGIN_LIVE}:v{served.live_version}"
        state = "ok" if self._failures == 0 else f"failing:{self._failures}"
        line = f"origen={origin},refresh={state}"
        reason = self._not_served or (NOT_SERVED_SEGMENTS if self._live is not None and served.origin == ORIGIN_FILE
                                      else None)
        return f"{line},no_servida={reason}" if reason else line

    def _load_row(self, row: Mapping[str, Any]) -> ServedKnowledge:
        file = self._context.file
        try:
            knowledge_id = str(UUID(str(row["knowledge_id"])))
            live_version = row["live_version"]
            if type(live_version) is not int or live_version < 1:
                raise TypeError("live_version")
            if row.get("file_sha256") != file.file_sha256 or row.get("file_version") != file.file_version:
                raise _NotServable(NOT_SERVED_FILE_CHANGED)
            sections = row["knowledge"]
            if not isinstance(sections, Mapping):
                raise TypeError("knowledge")
            approved_at = datetime.fromisoformat(str(row["approved_at"]).replace("Z", "+00:00"))
            approved_by = row.get("approved_by_ref")
            knowledge = build_knowledge(sections, file_version=file.file_version, ally_ref=file.knowledge.ally_ref,
                                        approved_by=str(approved_by or "slack"), approved_on=approved_at.date())
        except _NotServable:
            raise
        except (KnowledgeError, KnowledgeChangeInvalid, KeyError, TypeError, ValueError) as error:
            raise _NotServable(NOT_SERVED_INVALID) from error
        if knowledge.rendered_sha256 != row.get("rendered_sha256"):
            raise _NotServable(NOT_SERVED_RENDER)
        if instance_blocks(knowledge, context=self._context):
            raise _NotServable(NOT_SERVED_INVALID)
        return ServedKnowledge(origin=ORIGIN_LIVE, knowledge=knowledge, sections=dict(sections),
                               rendered=knowledge.render(), sha256=knowledge.rendered_sha256,
                               file_version=file.file_version, file_sha256=file.file_sha256,
                               knowledge_id=knowledge_id, live_version=live_version)

    async def refresh(self) -> bool:
        """Lee la base y actualiza. Nunca lanza. False si no se pudo leer."""
        try:
            payload = await self._fetch()
            row = payload["knowledge"]
            if row is not None and not isinstance(row, Mapping):
                raise TypeError("knowledge")
        except Exception as error:  # noqa: BLE001 - la base caida no tira el turno de nadie
            self._failures += 1
            logger.warning("live_knowledge_refresh_failed failures=%s error=%s", self._failures,
                           type(error).__name__)
            return False
        self._failures = 0
        self._read_once = True
        if row is None:
            if self._published_id is not None:
                logger.info("live_knowledge_back_to_file")
            self._live, self._published_id, self._not_served, self._applied = None, None, None, None
            return True
        key = (str(row.get("knowledge_id")), str(row.get("rendered_sha256")), str(row.get("approved_at")))
        if key == self._applied:
            return True
        self._applied = key
        self._published_id = key[0]
        try:
            self._live = self._load_row(row)
            self._not_served = None
            logger.info("live_knowledge_served id=%s live_version=%s", key[0], row.get("live_version"))
        except _NotServable as error:
            self._live = None
            self._not_served = error.reason
            logger.warning("live_knowledge_not_served id=%s live_version=%s reason=%s", key[0],
                           row.get("live_version"), error.reason)
        return True

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="live-knowledge-refresh")

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
