"""Segmentos: una etiqueta de Chatwoot le abre al agente un conocimiento extra (E-311).

Diseno: ``documentacion/2026-11-03-e310-diseno-segmentos-conocimiento-por-etiqueta.md``
(E-310, PR-A, con los defaults D1 a D15). Contrato:
``docs/contracts/segment-knowledge-v1.md``.

Un segmento es una etiqueta de la conversacion de Chatwoot con un archivo de
conocimiento aprobado y una vigencia. Vive en el repo de la instancia, al lado
del conocimiento base (``conocimiento/segmentos/<clave>-v<N>.toml``), y se
declara en el manifiesto con ``[[agente.segmentos]]``. El bridge lo carga al
arrancar con ``AGENT_SEGMENTS_ENABLED``.

En cada turno, si la conversacion tiene la etiqueta y el segmento esta vigente
(``desde <= ahora < hasta``), el agente recibe un mensaje ``system`` mas,
despues del conocimiento base, con el render del segmento encabezado por un
preambulo fijo del producto. El SOUL comun no se toca (D4).

Este modulo es la parte pura: el esquema, el render, el catalogo y la seleccion
por turno. El cableado (el flag, la entrada del agente, ``/ready`` y los logs)
vive en ``bridge.app``; el pedido a Hermes, en ``bridge.hermes``.

EL ENCHUFE DEL CHEQUEO DEL ALTA (PR-B, E-337). ``verificar_chequeo`` es el punto
donde el cargador exige el ``chequeo_sha`` de la cabecera: llama a
``bridge.segment_check.verificar_chequeo`` y convierte su ``ChequeoError`` en
``SegmentError``. Un ``aprobado`` sin ``chequeo_sha``, o con uno que no es el del
contenido actual (el segmento, el base, otro segmento que se pisa o las reglas del
chequeo), no carga y el bridge no arranca, como un segmento en borrador.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .commercial_knowledge import _SECRET_SHAPES, CommercialKnowledge

if TYPE_CHECKING:
    from .instance_manifest import AgentSegment, InstanceManifest

SEGMENT_SCHEMA = "segment-knowledge-v1"
# La clave de la entrada del agente (shadow-context-v6): que segmentos fueron.
SEGMENT_CONTEXT_KEY = "segments"

# Topes (D5). Un segmento lleno pesa ~2.000 tokens; con el v14 de ATT1 (29.091
# bytes) entran uno lleno o dos chicos.
MAX_SEGMENT_RENDERED_BYTES = 8_000
MAX_SEGMENTS_PER_TURN = 2
MAX_TURN_SYSTEM_BYTES = 40_000
MAX_INSTRUCTION_CHARS = 600

STATUSES = ("borrador", "aprobado")

STATE_NOT_STARTED = "por_empezar"
STATE_ACTIVE = "vigente"
STATE_EXPIRED = "vencido"

# Los motivos de un segmento etiquetado que no va en el turno (logs).
SKIP_NOT_STARTED = "not_started"
SKIP_EXPIRED = "expired"
SKIP_MAX_PER_TURN = "max_per_turn"
SKIP_SIZE = "size"

# Los campos de [oferta] que un segmento puede cambiar a proposito respecto del
# conocimiento base (D14). Sin ``reemplaza``, una diferencia es una
# contradiccion (la marca el chequeo del alta, PR-B).
REPLACEABLE_FIELDS = ("precio", "moneda", "garantia", "incluye", "medios")

# El preambulo lo pone el producto, no la instancia: asi una aliada sin
# segmentos sigue byte a byte igual y el SOUL comun no cambia (D4).
PREAMBLE = (
    "Esta persona está en esta campaña. Lo de este bloque vale para ella y, donde "
    "dice algo distinto del conocimiento general, manda este bloque. No cambia cómo "
    "respondes, el link de pago ni cuándo derivar."
)
# El de una campana con oferta propia ([oferta].codigo_oferta, E-410): el link
# no es el de siempre, es el de la oferta de la campana. Sin el campo, el
# preambulo de arriba, byte a byte.
PREAMBLE_CAMPAIGN_OFFER = (
    "Esta persona está en esta campaña. Lo de este bloque vale para ella y, donde "
    "dice algo distinto del conocimiento general, manda este bloque. No cambia cómo "
    "respondes ni cuándo derivar. El link de pago lo arma el sistema con la oferta "
    "de esta campaña."
)
CAMPAIGN_OFFER_LINE = "- El enlace de pago de esta campaña es el de su oferta: pedilo como siempre."

_REF = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COUPON = re.compile(r"[A-Za-z0-9_-]{2,64}")
# El codigo de una oferta de Hotmart, la forma del check de
# additional_offer_codes (20260928000200) y de la reserva por oferta.
OFFER_CODE = re.compile(r"[A-Za-z0-9]{4,32}")

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
    "frenar_descuento_carrito",
    "incluye",
    "garantia",
    "medios",
    "reemplaza",
}
_FIELD_LABELS = {
    "precio": "precio",
    "moneda": "moneda",
    "garantia": "garantía",
    "incluye": "qué incluye",
    "medios": "medios de pago",
}


class SegmentError(ValueError):
    """El segmento o su declaracion no son validos. El mensaje dice que y donde."""


@dataclass(frozen=True)
class SegmentOffer:
    """``[oferta]`` del segmento: lo que cambia en la campana."""

    product: str | None
    product_name: str | None
    price: str | None
    currency: str | None
    price_before: str | None
    coupon: str | None
    includes: tuple[str, ...]
    guarantee: str | None
    methods: tuple[str, ...]
    replaces: tuple[str, ...]
    # La oferta de Hotmart de la campana (E-410). No se nombra en ``reemplaza``:
    # no es un dato que el agente diga, es la oferta del link.
    offer_code: str | None = None
    # E-410 C8: la campana frena el descuento del carrito (los frenos de E-339)
    # aunque no traiga cupon. Por defecto False: sin el campo, como hoy. No va al
    # render: es una regla del producto, no algo que el agente diga.
    stop_cart_discount: bool = False


@dataclass(frozen=True)
class SegmentFaq:
    question: str
    answer: str
    source: str


@dataclass(frozen=True)
class SegmentKnowledge:
    """Un archivo de segmento, validado. El render depende de la zona de la aliada."""

    key: str
    name: str
    version: int
    status: str
    ally_ref: str
    approved_by: str | None
    approved_on: date | None
    # El sha del reporte del chequeo del alta que dio limpio. Lo exige el
    # cargador desde PR-B (``verificar_chequeo``); hoy solo se valida su forma.
    check_sha: str | None
    valid_from: datetime
    valid_until: datetime
    time_zone: str
    instruction: str | None
    offer: SegmentOffer | None
    advice: tuple[str, ...]
    statements: tuple[str, ...]
    unconfirmed: tuple[str, ...]
    forbidden_promises: tuple[str, ...]
    faq: tuple[SegmentFaq, ...]
    # Lo que contesto el equipo en el alta: es registro, no va al render.
    intake: tuple[SegmentFaq, ...]

    @property
    def approved(self) -> bool:
        return self.status == "aprobado"

    def state_at(self, now: datetime) -> str:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if now < self.valid_from:
            return STATE_NOT_STARTED
        if now >= self.valid_until:
            return STATE_EXPIRED
        return STATE_ACTIVE

    def render(self) -> str:
        """El bloque que recibe el agente, en un orden que no cambia."""

        # ``hasta`` es exclusivo: hasta 2026-12-01T00:00 se lee «hasta el 30/11 23:59».
        until = (self.valid_until - timedelta(seconds=1)).astimezone(ZoneInfo(self.time_zone))
        campaign_offer = self.offer is not None and self.offer.offer_code is not None
        lines = [
            f"# Segmento: {self.name} (vigente hasta el {until:%d/%m %H:%M}, hora de la aliada)",
            "",
            PREAMBLE_CAMPAIGN_OFFER if campaign_offer else PREAMBLE,
        ]
        if self.instruction:
            lines += ["", "## Cómo usar este bloque", "", self.instruction]
        if self.offer is not None:
            lines += _render_offer(self.offer)
        lines += ["", "## Asesoramiento", ""] + _bullets(self.advice)
        if self.statements:
            lines += ["", "## Lo que sí se puede decir", ""] + _bullets(self.statements)
        if self.unconfirmed:
            lines += ["", "## No confirmado", "", "Si preguntan por esto, deriva:", ""]
            lines += _bullets(self.unconfirmed)
        if self.forbidden_promises:
            lines += ["", "## Promesas prohibidas", ""] + _bullets(self.forbidden_promises)
        if self.faq:
            lines += ["", "## Preguntas frecuentes de la campaña", ""]
            for entry in self.faq:
                lines += [f"- P: {entry.question}", f"  R: {entry.answer}"]
        return "\n".join(lines).rstrip() + "\n"

    @property
    def rendered_sha256(self) -> str:
        return hashlib.sha256(self.render().encode("utf-8")).hexdigest()

    @classmethod
    def from_toml_file(
        cls,
        path: Path,
        *,
        time_zone: str,
        product_names: Mapping[str, str] | None = None,
        principal_name: str | None = None,
        require_approved: bool = True,
    ) -> SegmentKnowledge:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SegmentError(f"no se pudo leer {path}") from exc
        try:
            payload = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise SegmentError(f"{path} no es TOML valido: {exc}") from exc
        return cls.from_mapping(
            payload,
            time_zone=time_zone,
            product_names=product_names,
            principal_name=principal_name,
            require_approved=require_approved,
        )

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        time_zone: str,
        product_names: Mapping[str, str] | None = None,
        principal_name: str | None = None,
        require_approved: bool = True,
    ) -> SegmentKnowledge:
        """Valida el archivo.

        ``product_names``: clave -> nombre de la oferta de los productos del
        conocimiento base de varios productos (vacio en uno de un solo producto).
        ``principal_name``: el nombre de la oferta del principal, el que nombra
        ``oferta.producto`` con un base de un solo producto.
        """

        ZoneInfo(time_zone)
        _exact("segmento", payload, _TOP_REQUIRED, _TOP_OPTIONAL)

        header = _table(payload, "cabecera")
        _exact(
            "cabecera",
            header,
            {"segment_version", "estado", "ally_ref", "clave"},
            {"nombre", "aprobado_por", "aprobado_el", "chequeo_sha"},
        )
        version = header["segment_version"]
        if type(version) is not int or version < 1:
            raise SegmentError("cabecera.segment_version debe ser un entero positivo")
        status = _text(header, "estado", "cabecera.estado")
        if status not in STATUSES:
            raise SegmentError(f"cabecera.estado debe ser uno de {', '.join(STATUSES)}")
        key = _text(header, "clave", "cabecera.clave")
        if _REF.fullmatch(key) is None:
            raise SegmentError("cabecera.clave debe ser un slug en minusculas (a-z, 0-9, guiones)")
        approved_by = _optional_text(header, "aprobado_por", "cabecera.aprobado_por")
        approved_on = header.get("aprobado_el")
        if approved_on is not None and (
            not isinstance(approved_on, date) or isinstance(approved_on, datetime)
        ):
            raise SegmentError("cabecera.aprobado_el debe ser una fecha (2026-11-03)")
        if status == "aprobado" and (approved_by is None or approved_on is None):
            raise SegmentError("un segmento aprobado lleva aprobado_por y aprobado_el")
        if require_approved and status != "aprobado":
            raise SegmentError("el segmento esta en borrador: el bridge solo carga uno aprobado")
        check_sha = _optional_text(header, "chequeo_sha", "cabecera.chequeo_sha")
        if check_sha is not None and _SHA256.fullmatch(check_sha) is None:
            raise SegmentError("cabecera.chequeo_sha debe ser un sha256 en hexadecimal (64 minusculas)")

        window = _table(payload, "vigencia")
        _exact("vigencia", window, {"desde", "hasta"})
        valid_from = _aware(window.get("desde"), "vigencia.desde")
        valid_until = _aware(window.get("hasta"), "vigencia.hasta")
        if valid_from >= valid_until:
            raise SegmentError("vigencia.desde tiene que ser anterior a vigencia.hasta")

        instruction = None
        if "instruccion" in payload:
            raw = _table(payload, "instruccion")
            _exact("instruccion", raw, set(), {"texto"})
            instruction = _optional_text(raw, "texto", "instruccion.texto")
            if instruction is not None and len(instruction) > MAX_INSTRUCTION_CHARS:
                raise SegmentError(
                    f"instruccion.texto tiene {len(instruction)} caracteres; el maximo es "
                    f"{MAX_INSTRUCTION_CHARS}"
                )

        offer = (
            _offer(_table(payload, "oferta"), product_names or {}, principal_name)
            if "oferta" in payload
            else None
        )

        advice_table = _table(payload, "asesoramiento")
        _exact("asesoramiento", advice_table, {"items"})

        segment = cls(
            key=key,
            # El nombre titula el bloque; sin el, la clave (la tabla del diseno E-310
            # no lo traia y PR-B escribe sus fixtures con esa tabla).
            name=_optional_text(header, "nombre", "cabecera.nombre") or key,
            version=version,
            status=status,
            ally_ref=_text(header, "ally_ref", "cabecera.ally_ref"),
            approved_by=approved_by,
            approved_on=approved_on,
            check_sha=check_sha,
            valid_from=valid_from,
            valid_until=valid_until,
            time_zone=time_zone,
            instruction=instruction,
            offer=offer,
            advice=_list(advice_table, "items", "asesoramiento.items", allow_empty=False),
            statements=_items(payload, "se_puede_decir"),
            unconfirmed=_items(payload, "no_confirmado"),
            forbidden_promises=_items(payload, "promesas_prohibidas"),
            faq=_entries(payload, "faq"),
            intake=_entries(payload, "preguntas_del_alta"),
        )
        rendered = segment.render()
        size = len(rendered.encode("utf-8"))
        if size > MAX_SEGMENT_RENDERED_BYTES:
            raise SegmentError(
                f"el segmento renderizado pesa {size} bytes; el maximo es {MAX_SEGMENT_RENDERED_BYTES}"
            )
        for shape in _SECRET_SHAPES:
            if shape.search(rendered):
                raise SegmentError(
                    "el segmento contiene algo con forma de credencial; "
                    "los secretos no van en este archivo"
                )
        return segment


@dataclass(frozen=True)
class LoadedSegment:
    """Un segmento declarado en el manifiesto, con su archivo ya validado."""

    key: str
    label: str
    priority: int
    knowledge_path: str
    label_macro_id: int | None
    knowledge: SegmentKnowledge
    rendered: str
    sha256: str
    # El dict de tomllib del archivo, tal cual: es lo que lee el chequeo del alta
    # (PR-B, ``segment_check``), que tiene su propio lector del TOML.
    raw: Mapping[str, Any] = MappingProxyType({})

    @property
    def version(self) -> int:
        return self.knowledge.version

    @property
    def rendered_bytes(self) -> int:
        return len(self.rendered.encode("utf-8"))

    def state_at(self, now: datetime) -> str:
        return self.knowledge.state_at(now)

    def overlaps(self, other: LoadedSegment) -> bool:
        return (
            self.knowledge.valid_from < other.knowledge.valid_until
            and other.knowledge.valid_from < self.knowledge.valid_until
        )


@dataclass(frozen=True)
class TurnSegment:
    """Un segmento que va en un turno, con lo que el turno sabe de la conversacion.

    ``agent_replies_before``: cuantas respuestas del agente ya tenia la
    conversacion antes de este turno (despues del ultimo ``/nuevo``). Va a la
    procedencia: el primer turno con el segmento de una conversacion con
    respuestas antes es un segmento que llego tarde (la etiqueta se puso despues
    de que el agente ya hablo sin ella). Ver el contrato, «La etiqueta antes del
    primer turno».
    """

    segment: LoadedSegment
    agent_replies_before: int

    @property
    def key(self) -> str:
        return self.segment.key

    @property
    def version(self) -> int:
        return self.segment.version

    @property
    def sha256(self) -> str:
        return self.segment.sha256

    @property
    def rendered(self) -> str:
        return self.segment.rendered

    @property
    def activated_late(self) -> bool:
        """Si este turno, de ser el primero con el segmento, lo trae tarde."""

        return self.agent_replies_before > 0


@dataclass(frozen=True)
class SegmentSelection:
    """Lo que el turno lleva: los segmentos que van y por que no van los otros."""

    active: tuple[LoadedSegment, ...]
    # (clave, version, motivo) de cada segmento etiquetado que no fue.
    skipped: tuple[tuple[str, int, str], ...]

    def context_entries(self) -> list[dict[str, object]]:
        return [{"key": segment.key, "version": segment.version} for segment in self.active]


@dataclass(frozen=True)
class SegmentCatalog:
    """Los segmentos de la instancia, ordenados por prioridad (menor primero)."""

    segments: tuple[LoadedSegment, ...]
    base_bytes: int

    def select(self, *, labels: Iterable[str], now: datetime) -> SegmentSelection:
        """Etiqueta x vigencia x prioridad, con los topes del turno (D5, D6)."""

        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        present = set(labels)
        active: list[LoadedSegment] = []
        skipped: list[tuple[str, int, str]] = []
        used = self.base_bytes
        for segment in self.segments:
            if segment.label not in present:
                continue
            state = segment.state_at(now)
            if state == STATE_NOT_STARTED:
                skipped.append((segment.key, segment.version, SKIP_NOT_STARTED))
                continue
            if state == STATE_EXPIRED:
                skipped.append((segment.key, segment.version, SKIP_EXPIRED))
                continue
            if len(active) >= MAX_SEGMENTS_PER_TURN:
                skipped.append((segment.key, segment.version, SKIP_MAX_PER_TURN))
                continue
            if used + segment.rendered_bytes > MAX_TURN_SYSTEM_BYTES:
                skipped.append((segment.key, segment.version, SKIP_SIZE))
                continue
            active.append(segment)
            used += segment.rendered_bytes
        return SegmentSelection(active=tuple(active), skipped=tuple(skipped))

    def blocks_for(self, entries: object) -> tuple[LoadedSegment, ...]:
        """Los segmentos que nombra la entrada del agente (``segments``), en su orden."""

        if not isinstance(entries, list):
            return ()
        by_key = {segment.key: segment for segment in self.segments}
        blocks: list[LoadedSegment] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            segment = by_key.get(entry.get("key"))  # type: ignore[arg-type]
            if segment is not None and entry.get("version") == segment.version:
                blocks.append(segment)
        return tuple(blocks)

    def readiness(self, now: datetime) -> str:
        """``clave:vN:sha:estado`` de cada segmento, separados por coma; ``none`` sin ninguno."""

        if not self.segments:
            return "none"
        return ",".join(
            f"{segment.key}:v{segment.version}:{segment.sha256}:{segment.state_at(now)}"
            for segment in self.segments
        )


def verificar_chequeo(
    segmento: Mapping[str, Any],
    *,
    base: CommercialKnowledge,
    otros: Sequence[Mapping[str, Any]] = (),
) -> None:
    """El enchufe del chequeo del alta (PR-B del diseno E-310, §3.1; E-337).

    Misma firma que ``bridge.segment_check.verificar_chequeo`` de PR-B (E-312):
    ``segmento`` es el dict de tomllib del archivo, ``base`` el conocimiento base y
    ``otros`` los dicts de los otros segmentos declarados. Lo llama
    ``load_segment_catalog`` para cada segmento que el bridge va a cargar, despues
    de validar el archivo y el catalogo entero.

    Un ``borrador`` pasa (el cargador ya lo frena aparte). Un ``aprobado`` sin
    ``cabecera.chequeo_sha``, o con uno que no es el que da
    ``segment_check.calcular_chequeo_sha`` sobre el contenido actual, lanza
    ``SegmentError``: asi «nada entra con una contradiccion abierta» lo hace
    cumplir el codigo. Un otro segmento que el lector de PR-B no entiende tambien
    es ``SegmentError``, nunca otra excepcion.
    """

    from .segment_check import ChequeoError, SegmentCheckError
    from .segment_check import verificar_chequeo as verificar_huella

    try:
        verificar_huella(segmento, base=base, otros=otros)
    except (ChequeoError, SegmentCheckError) as exc:
        raise SegmentError(f"chequeo del alta: {exc}") from exc


def load_declared_segment(
    declaration: AgentSegment,
    *,
    instance_dir: Path,
    manifest: InstanceManifest,
    base: CommercialKnowledge,
    require_approved: bool = True,
) -> LoadedSegment:
    """Lee y valida el archivo de un segmento declarado, contra el manifiesto."""

    path = instance_dir / declaration.knowledge_path
    where = f"agente.segmentos '{declaration.key}'"
    product_names = {
        product.key: product.offer_name for product in base.products if product.key is not None
    }
    try:
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise SegmentError(f"no se pudo leer {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise SegmentError(f"{path} no es TOML valido: {exc}") from exc
        segment = SegmentKnowledge.from_mapping(
            raw,
            time_zone=manifest.time_zone,
            product_names=product_names,
            principal_name=base.products[0].offer_name,
            require_approved=require_approved,
        )
    except SegmentError as exc:
        raise SegmentError(f"{declaration.knowledge_path}: {exc}") from exc
    if segment.key != declaration.key:
        raise SegmentError(
            f"{where}: el archivo dice cabecera.clave '{segment.key}'"
        )
    if segment.ally_ref != manifest.ally_ref:
        raise SegmentError(
            f"{where}: el segmento es de '{segment.ally_ref}' y el manifiesto de '{manifest.ally_ref}'"
        )
    if segment.offer is not None and segment.offer.offer_code is not None:
        # La oferta de la campana tiene que ser una oferta declarada del producto
        # del segmento (el principal si no dice producto): si no, el link no se
        # podria emitir (E-410).
        codes = manifest.offer_codes_of_product(segment.offer.product)
        if codes is None or segment.offer.offer_code not in codes:
            raise SegmentError(
                f"{where}: oferta.codigo_oferta '{segment.offer.offer_code}' no es una oferta "
                "declarada del producto de la campana en el manifiesto"
            )
    expected_name = f"{segment.key}-v{segment.version}.toml"
    if Path(declaration.knowledge_path).name != expected_name:
        raise SegmentError(
            f"{where}: el archivo de la version {segment.version} se llama {expected_name}"
        )
    rendered = segment.render()
    return LoadedSegment(
        key=declaration.key,
        label=declaration.label,
        priority=declaration.priority,
        knowledge_path=declaration.knowledge_path,
        label_macro_id=declaration.label_macro_id,
        knowledge=segment,
        rendered=rendered,
        sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        raw=MappingProxyType(raw),
    )


def build_catalog(segments: Sequence[LoadedSegment], *, base_bytes: int) -> SegmentCatalog:
    """Arma el catalogo y falla si dos segmentos no pueden convivir (D5, D6).

    Un segmento que con el base no entra en el turno es un error. Dos con
    vigencias que se pisan no pueden tener la misma prioridad, ni sumar con el
    base mas de lo que entra: asi el recorte por tamano del turno no deberia
    pasar en produccion.
    """

    ordered = tuple(sorted(segments, key=lambda segment: (segment.priority, segment.key)))
    for segment in ordered:
        if base_bytes + segment.rendered_bytes > MAX_TURN_SYSTEM_BYTES:
            raise SegmentError(
                f"el segmento '{segment.key}' ({segment.rendered_bytes} bytes) con el conocimiento "
                f"base ({base_bytes} bytes) pasa los {MAX_TURN_SYSTEM_BYTES} bytes del turno"
            )
    for index, first in enumerate(ordered):
        for second in ordered[index + 1 :]:
            if not first.overlaps(second):
                continue
            if first.priority == second.priority:
                raise SegmentError(
                    f"los segmentos '{first.key}' y '{second.key}' tienen la misma prioridad "
                    f"({first.priority}) y vigencias que se pisan"
                )
            total = base_bytes + first.rendered_bytes + second.rendered_bytes
            if total > MAX_TURN_SYSTEM_BYTES:
                raise SegmentError(
                    f"los segmentos '{first.key}' y '{second.key}' tienen vigencias que se pisan "
                    f"y con el conocimiento base suman {total} bytes; el maximo del turno es "
                    f"{MAX_TURN_SYSTEM_BYTES}"
                )
    return SegmentCatalog(segments=ordered, base_bytes=base_bytes)


def load_segment_catalog(
    manifest: InstanceManifest,
    instance_dir: Path,
    base: CommercialKnowledge,
) -> SegmentCatalog:
    """El catalogo que carga el bridge al arrancar: solo segmentos aprobados."""

    loaded = tuple(
        load_declared_segment(
            declaration, instance_dir=instance_dir, manifest=manifest, base=base
        )
        for declaration in manifest.agent_segments
    )
    catalog = build_catalog(loaded, base_bytes=len(base.render().encode("utf-8")))
    for segment in catalog.segments:
        verificar_chequeo(
            dict(segment.raw),
            base=base,
            otros=[dict(other.raw) for other in catalog.segments if other.key != segment.key],
        )
    return catalog


def _offer(
    raw: Mapping[str, Any], product_names: Mapping[str, str], principal_name: str | None
) -> SegmentOffer:
    _exact("oferta", raw, set(), _OFFER_KEYS)
    if not raw:
        raise SegmentError("oferta no puede estar vacia: si no cambia nada de la oferta, no va")
    product = _optional_text(raw, "producto", "oferta.producto")
    product_name = None
    if product is not None:
        if _REF.fullmatch(product) is None:
            raise SegmentError("oferta.producto debe ser un slug en minusculas (a-z, 0-9, guiones)")
        if product_names:
            # Con [[productos]] en el base, la clave tiene que ser una de ellas.
            if product not in product_names:
                raise SegmentError(
                    f"oferta.producto '{product}' no es un producto del conocimiento base "
                    f"({', '.join(sorted(product_names))})"
                )
            product_name = product_names[product]
        else:
            # Con un solo producto, «el principal» (diseno E-310 §2.1): el render lo
            # nombra con la oferta del base. Si la clave dice otra cosa lo marca el
            # chequeo del alta (PR-B), no el cargador.
            product_name = principal_name or product
    price = _optional_text(raw, "precio", "oferta.precio")
    currency = _optional_text(raw, "moneda", "oferta.moneda")
    price_before = _optional_text(raw, "precio_antes", "oferta.precio_antes")
    if (price is None) != (currency is None):
        raise SegmentError("oferta.precio y oferta.moneda van juntos")
    if price_before is not None and price is None:
        raise SegmentError("oferta.precio_antes va con oferta.precio")
    coupon = _optional_text(raw, "cupon", "oferta.cupon")
    if coupon is not None and _COUPON.fullmatch(coupon) is None:
        raise SegmentError("oferta.cupon debe ser un codigo (letras, numeros, guiones; 2 a 64)")
    offer_code = _optional_text(raw, "codigo_oferta", "oferta.codigo_oferta")
    if offer_code is not None and OFFER_CODE.fullmatch(offer_code) is None:
        raise SegmentError(
            "oferta.codigo_oferta debe ser un codigo de oferta de Hotmart (letras y numeros, 4 a 32)"
        )
    stop_cart_discount = raw.get("frenar_descuento_carrito", False)
    if type(stop_cart_discount) is not bool:
        raise SegmentError("oferta.frenar_descuento_carrito debe ser true o false")
    includes = _list(raw, "incluye", "oferta.incluye")
    methods = _list(raw, "medios", "oferta.medios")
    replaces = _list(raw, "reemplaza", "oferta.reemplaza")
    present = {
        "precio": price is not None,
        "moneda": currency is not None,
        "garantia": "garantia" in raw,
        "incluye": bool(includes),
        "medios": bool(methods),
    }
    for field in replaces:
        if field not in REPLACEABLE_FIELDS:
            raise SegmentError(
                f"oferta.reemplaza: '{field}' no es un campo que se pueda reemplazar "
                f"({', '.join(REPLACEABLE_FIELDS)})"
            )
        if not present[field]:
            raise SegmentError(f"oferta.reemplaza nombra '{field}' y la oferta no lo trae")
    if len(set(replaces)) != len(replaces):
        raise SegmentError("oferta.reemplaza repite un campo")
    return SegmentOffer(
        product=product,
        product_name=product_name,
        price=price,
        currency=currency,
        price_before=price_before,
        coupon=coupon,
        includes=includes,
        guarantee=_optional_text(raw, "garantia", "oferta.garantia"),
        methods=methods,
        replaces=replaces,
        offer_code=offer_code,
        stop_cart_discount=stop_cart_discount,
    )


def _render_offer(offer: SegmentOffer) -> list[str]:
    lines = ["", "## Oferta de la campaña", ""]
    if offer.product_name:
        lines.append(f"- Producto: {offer.product_name}.")
    if offer.price is not None:
        lines.append(f"- Precio en esta campaña: {offer.currency} {offer.price}.")
    if offer.price_before is not None:
        lines.append(f"- Precio de antes: {offer.currency} {offer.price_before}.")
    if offer.coupon is not None:
        lines.append(f"- Cupón de la campaña: {offer.coupon}.")
    if offer.offer_code is not None:
        lines.append(CAMPAIGN_OFFER_LINE)
    if offer.guarantee is not None:
        lines.append(f"- Garantía en esta campaña: {offer.guarantee}")
    if offer.replaces:
        changed = ", ".join(_FIELD_LABELS[field] for field in offer.replaces)
        lines.append(f"- Cambia a propósito respecto del conocimiento general: {changed}.")
    if offer.includes:
        lines += ["", "Incluye en esta campaña:", ""] + _bullets(offer.includes)
    if offer.methods:
        lines += ["", "Medios de pago en esta campaña:", ""] + _bullets(offer.methods)
    return lines


def _aware(value: object, where: str) -> datetime:
    if not isinstance(value, datetime):
        raise SegmentError(f"{where} debe ser fecha y hora con zona (2026-11-20T00:00:00-06:00)")
    if value.tzinfo is None or value.utcoffset() is None:
        raise SegmentError(f"{where} debe llevar la zona (-06:00), no una hora local")
    return value


def _items(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    if key not in payload:
        return ()
    table = _table(payload, key)
    _exact(key, table, {"items"})
    return _list(table, "items", f"{key}.items", allow_empty=False)


def _entries(payload: Mapping[str, Any], key: str) -> tuple[SegmentFaq, ...]:
    raw_entries = payload.get(key, [])
    if not isinstance(raw_entries, list):
        raise SegmentError(f"{key} debe ser una lista de tablas [[{key}]]")
    entries = []
    for index, raw in enumerate(raw_entries):
        where = f"{key}[{index}]"
        if not isinstance(raw, dict):
            raise SegmentError(f"{where} debe ser una tabla")
        _exact(where, raw, {"pregunta", "respuesta", "fuente"})
        entries.append(
            SegmentFaq(
                question=_text(raw, "pregunta", f"{where}.pregunta"),
                answer=_text(raw, "respuesta", f"{where}.respuesta"),
                source=_text(raw, "fuente", f"{where}.fuente"),
            )
        )
    return tuple(entries)


def _bullets(items: Iterable[str]) -> list[str]:
    return [f"- {item}" for item in items]


def _exact(where: str, table: Mapping[str, Any], required: set[str], optional: set[str] | None = None) -> None:
    optional = optional or set()
    missing = sorted(required - set(table))
    extra = sorted(set(table) - required - optional)
    if missing:
        raise SegmentError(f"{where}: faltan {', '.join(missing)}")
    if extra:
        raise SegmentError(f"{where}: claves no soportadas {', '.join(extra)}")


def _table(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise SegmentError(f"{key} debe ser una tabla")
    return value


def _text(table: Mapping[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SegmentError(f"{where} debe ser un texto no vacio")
    return value.strip()


def _optional_text(table: Mapping[str, Any], key: str, where: str) -> str | None:
    if key not in table:
        return None
    return _text(table, key, where)


def _list(table: Mapping[str, Any], key: str, where: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise SegmentError(f"{where} debe ser una lista de textos no vacios")
    if not allow_empty and not value:
        raise SegmentError(f"{where} no puede estar vacia")
    return tuple(item.strip() for item in value)
