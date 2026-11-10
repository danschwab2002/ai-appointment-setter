"""La etiqueta de un segmento antes del primer turno del agente (E-316, PR-C).

Diseno: ``documentacion/2026-11-03-e310-diseno-segmentos-conocimiento-por-etiqueta.md``
(E-310, §2.7 y PR-C). Contrato: ``docs/contracts/segment-labels-v1.md``.

Un segmento (PR-A, ``segment-knowledge-v1``) se activa con una etiqueta de la
conversacion de Chatwoot: el bridge la lee de ``conversation.labels`` del webhook
en cada turno. Dan, el 03/11: «un inicio de conversacion sin esa etiqueta puede
ensuciar mucho la memoria del agente, por mas de que luego se le sume la
etiqueta». Este modulo hace que la etiqueta este antes del primer turno, por dos
caminos:

* **El motor nativo la pone antes del primer envio** (``NativeSegmentLabels``):
  un flujo nativo con ``etiquetar: <etiqueta de un segmento>`` le suma la
  etiqueta a la conversacion antes de mandar la plantilla, por el macro del
  segmento que solo suma esa etiqueta (el patron de ``ProductLabeler``). Cuando
  la persona contesta, el webhook ya la trae. Si no se pudo antes, se reintenta
  una vez despues del envio aceptado. Falla blanda: una etiqueta que no sale no
  frena el envio (D-E316-3).
* **El bridge la hereda de otra conversacion del contacto**
  (``SegmentLabelInheritance``): si la persona abre una conversacion nueva, la
  etiqueta de la vieja no pasa sola. La primera vez que el bridge ve una
  conversacion sin ninguna etiqueta de segmento, lee las otras conversaciones del
  contacto en el inbox (``list_contact_conversations``, una llamada) y, si alguna
  tiene la etiqueta de un segmento que no vencio, se la pone a esta por el macro
  y la suma al turno antes de que corra el agente. Lo que hereda se recuerda por
  conversacion, asi que un turno nunca paga una llamada extra salvo el primero que
  ve el proceso (D-E316-6).

**La declaracion de los segmentos es de PR-A** (``[[agente.segmentos]]`` en el
manifiesto, ``InstanceManifest.agent_segments``, con ``macro_etiqueta``) y la
vigencia vive en el archivo de cada segmento (``SegmentCatalog``). Mientras PR-A
no este en ``main`` este modulo la lee por atributo (``declared_segment_labels``):
sin PR-A no hay segmentos y nada de esto corre. Unificar al mergear los dos es
cambiar los ``getattr`` por el acceso tipado.

Los logs no llevan telefono, mail ni nombre: la conversacion, el contacto (id de
Chatwoot), la clave y la etiqueta del segmento.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .customer_status import CUSTOMER_LABEL, conversation_labels

logger = logging.getLogger(__name__)

# Los estados de un segmento a una hora, los mismos que ``SegmentKnowledge.state_at``
# de PR-A (``/ready``): por_empezar, vigente, vencido. ``sin_vigencia``: el bridge no
# tiene el archivo del segmento cargado (AGENT_SEGMENTS_ENABLED apagado) y no sabe.
STATE_NOT_STARTED = "por_empezar"
STATE_ACTIVE = "vigente"
STATE_EXPIRED = "vencido"
STATE_UNKNOWN = "sin_vigencia"
SEGMENT_STATES = frozenset({STATE_NOT_STARTED, STATE_ACTIVE, STATE_EXPIRED, STATE_UNKNOWN})
# Las etiquetas que nunca son de un segmento: la de cliente (sigue por
# customer_status, D13 del diseno) y las de la automatizacion.
NOT_SEGMENT_LABELS = frozenset({CUSTOMER_LABEL, "automation_paused", "automation_opted_out"})
# Cuantas conversaciones recuerda la herencia. Al reiniciar se olvidan, y la primera
# vuelta las vuelve a leer (una llamada por conversacion).
DEFAULT_REMEMBERED_CONVERSATIONS = 4096


class SegmentLabelsConfigError(ValueError):
    """La declaracion de los segmentos no sirve para etiquetar."""


# De donde viene un segmento (E-411): el manifiesto ([[agente.segmentos]]) o una
# campana del Automation Expert servida por el catalogo vivo de la base.
ORIGIN_MANIFEST = "manifiesto"
ORIGIN_AGENT_CAMPAIGN = "campana_ae"


@dataclass(frozen=True)
class SegmentLabel:
    """Un segmento visto desde su etiqueta: la clave, la etiqueta, su macro y su vigencia."""

    key: str
    label: str
    macro_id: int | None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    origin: str = ORIGIN_MANIFEST

    def state_at(self, now: datetime) -> str:
        """``hasta`` es exclusivo, como en PR-A (D-E311-2)."""
        if self.valid_from is None or self.valid_until is None:
            return STATE_UNKNOWN
        if now < self.valid_from:
            return STATE_NOT_STARTED
        if now >= self.valid_until:
            return STATE_EXPIRED
        return STATE_ACTIVE


def declared_segment_labels(manifest: object, catalog: object = None) -> tuple[SegmentLabel, ...]:
    """Los segmentos declarados, con su vigencia si el catalogo de PR-A esta cargado.

    La interfaz minima con PR-A (E-311): ``manifest.agent_segments`` es una tupla de
    ``AgentSegment`` (``key``, ``label``, ``label_macro_id``), y ``catalog.segments``
    de ``LoadedSegment`` (``key``, ``knowledge.valid_from``/``valid_until``). Sin el
    atributo (un ``main`` sin PR-A, o sin manifiesto) no hay segmentos: ``()``. Un
    valor con otra forma es ``SegmentLabelsConfigError``: nada a medias.
    """
    declared = getattr(manifest, "agent_segments", ()) if manifest is not None else ()
    if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)):
        raise SegmentLabelsConfigError("agent_segments_invalid")
    windows: dict[str, tuple[datetime, datetime]] = {}
    loaded = getattr(catalog, "segments", ()) if catalog is not None else ()
    if not isinstance(loaded, Sequence) or isinstance(loaded, (str, bytes)):
        raise SegmentLabelsConfigError("segment_catalog_invalid")
    for segment in loaded:
        key = getattr(segment, "key", None)
        knowledge = getattr(segment, "knowledge", None)
        start = getattr(knowledge, "valid_from", None)
        end = getattr(knowledge, "valid_until", None)
        if (
            not isinstance(key, str)
            or not isinstance(start, datetime)
            or not isinstance(end, datetime)
            or start.tzinfo is None
            or end.tzinfo is None
            or not start < end
        ):
            raise SegmentLabelsConfigError("segment_catalog_invalid")
        windows[key] = (start, end)
    out: list[SegmentLabel] = []
    for segment in declared:
        key = getattr(segment, "key", None)
        label = getattr(segment, "label", None)
        macro_id = getattr(segment, "label_macro_id", None)
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(label, str)
            or not label
            or label in NOT_SEGMENT_LABELS
            or label.startswith("automation_")
            or (macro_id is not None and (type(macro_id) is not int or macro_id < 1))
        ):
            raise SegmentLabelsConfigError("agent_segment_invalid")
        window = windows.get(key)
        out.append(
            SegmentLabel(
                key=key,
                label=label,
                macro_id=macro_id,
                valid_from=window[0] if window else None,
                valid_until=window[1] if window else None,
            )
        )
    if len({segment.label for segment in out}) != len(out):
        raise SegmentLabelsConfigError("agent_segment_label_repeated")
    return tuple(out)


def labelable(segments: Iterable[SegmentLabel]) -> tuple[SegmentLabel, ...]:
    """Los segmentos que el bridge puede etiquetar: los que tienen macro."""
    return tuple(segment for segment in segments if segment.macro_id is not None)


def validator_segment_labels(
    segments: Iterable[SegmentLabel], *, now: datetime
) -> list[dict[str, str]]:
    """Lo que el validador de flujos recibe en ``instance.segment_labels``.

    Solo los que tienen macro: un ``etiquetar`` con la etiqueta de un segmento sin
    macro no lo podria ejecutar nadie. El estado se calcula ahora: el validador no
    mira la hora (``segment_label_outside_window`` sale de este estado).
    """
    return [
        {"etiqueta": segment.label, "segmento": segment.key, "estado": segment.state_at(now)}
        for segment in labelable(segments)
    ]


class _Labeler(Protocol):
    @property
    def label(self) -> str: ...

    async def ensure(self, conversation_id: int, *, source: str) -> str: ...


class SegmentLabelSource(Protocol):
    """De donde leen los caminos de la etiqueta sus segmentos y etiquetadores (E-411).

    Se lee en cada llamada (``plan``, ``apply``): la fuente fija devuelve siempre lo
    mismo (lo de hoy, el manifiesto); la viva (el catalogo de las campanas del AE)
    cambia cuando cambia la fila de la base.
    """

    live: bool

    def segments(self) -> tuple[SegmentLabel, ...]: ...

    def labeler(self, label: str) -> _Labeler | None: ...


class StaticSegmentLabels:
    """La fuente fija: los segmentos y etiquetadores que se armaron al arrancar."""

    live = False

    def __init__(self, segments: Iterable[SegmentLabel], labelers: Mapping[str, _Labeler]) -> None:
        self._segments = tuple(segments)
        self._labelers = dict(labelers)

    def segments(self) -> tuple[SegmentLabel, ...]:
        return self._segments

    def labeler(self, label: str) -> _Labeler | None:
        return self._labelers.get(label)

    @property
    def labelers(self) -> Mapping[str, _Labeler]:
        return dict(self._labelers)


class _TailReader(Protocol):
    async def get_flow_engine_native_step_tail(self, action_id: str) -> Any: ...


LABEL_DONE = frozenset({"added", "present"})


@dataclass
class NativeSegmentLabelPlan:
    """Lo que el motor hace con la etiqueta de un segmento en un envio nativo.

    ``before_send`` corre con la conversacion antes de la plantilla (la llama el
    remitente cuando ya eligio o creo la conversacion, o el worker con la del
    seguimiento). ``after_send`` corre con el envio aceptado: si antes no se pudo,
    reintenta una vez. Nunca lanza.
    """

    action_id: str
    segment: SegmentLabel
    labeler: _Labeler
    before_outcome: str | None = None

    async def before_send(self, conversation_id: int) -> str:
        self.before_outcome = await self.labeler.ensure(
            conversation_id, source="flow_engine_native_segment"
        )
        logger.info(
            "flow_engine_native_segment_label when=before_send action_id=%s conversation=%s "
            "segment=%s label=%s outcome=%s",
            self.action_id,
            conversation_id,
            self.segment.key,
            self.segment.label,
            self.before_outcome,
        )
        return self.before_outcome

    async def after_send(self, conversation_id: int) -> str:
        if self.before_outcome in LABEL_DONE:
            return self.before_outcome
        outcome = await self.labeler.ensure(conversation_id, source="flow_engine_native_segment")
        log = logger.info if outcome in LABEL_DONE else logger.warning
        log(
            "flow_engine_native_segment_label when=after_send action_id=%s conversation=%s "
            "segment=%s label=%s before=%s outcome=%s",
            self.action_id,
            conversation_id,
            self.segment.key,
            self.segment.label,
            self.before_outcome or "no_corrio",
            outcome,
        )
        return outcome


class NativeSegmentLabels:
    """Las etiquetas de segmento que el motor nativo pone antes del primer envio."""

    def __init__(
        self,
        *,
        segments: Iterable[SegmentLabel] = (),
        labelers: Mapping[str, _Labeler] | None = None,
        source: SegmentLabelSource | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if source is None:
            # La fuente fija de hoy: se valida al construir, como siempre.
            labelers = dict(labelers or {})
            by_label = {segment.label: segment for segment in segments}
            if not labelers or set(labelers) - set(by_label):
                raise SegmentLabelsConfigError("segment_labelers_invalid")
            for label, labeler in labelers.items():
                if labeler.label != label:
                    raise SegmentLabelsConfigError("segment_labelers_invalid")
            source = StaticSegmentLabels(by_label.values(), labelers)
        self._source = source
        self._clock = clock

    def _labelable(self) -> tuple[dict[str, SegmentLabel], dict[str, _Labeler]]:
        segments: dict[str, SegmentLabel] = {}
        labelers: dict[str, _Labeler] = {}
        for segment in self._source.segments():
            labeler = self._source.labeler(segment.label)
            if labeler is None:
                continue
            segments[segment.label] = segment
            labelers[segment.label] = labeler
        return segments, labelers

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(self._labelable()[1])

    async def plan(self, supabase: _TailReader, *, action_id: str) -> NativeSegmentLabelPlan | None:
        """El plan de la etiqueta para la accion de un recorrido nativo, o None.

        Lee la cola del paso (``flow_engine_native_step_tail_v1``: el ``etiquetar``
        de la version fijada), una llamada por envio nativo y solo con el flag. Sin
        respuesta, ``None``: el envio sale igual (D-E316-3). Un segmento por empezar o
        vencido no se pone (``segment_label_outside_window``, D-E316-5).
        """
        try:
            tail = await supabase.get_flow_engine_native_step_tail(action_id)
        except Exception as exc:  # noqa: BLE001 - falla blanda, el log lleva el tipo
            logger.warning(
                "flow_engine_native_segment_label_unavailable action_id=%s error=%s",
                action_id,
                type(exc).__name__,
            )
            return None
        label = getattr(tail, "etiquetar", None) if tail is not None else None
        segments, labelers = self._labelable()
        if not isinstance(label, str) or label not in labelers:
            return None
        segment = segments[label]
        state = segment.state_at(self._clock())
        if state in (STATE_NOT_STARTED, STATE_EXPIRED):
            logger.info(
                "flow_engine_native_segment_label_skipped action_id=%s segment=%s reason=%s",
                action_id,
                segment.key,
                state,
            )
            return None
        return NativeSegmentLabelPlan(
            action_id=action_id, segment=segment, labeler=labelers[label]
        )


# ---------------------------------------------------------------------------
# La herencia: la etiqueta de otra conversacion del contacto.
# ---------------------------------------------------------------------------


class _ContactConversations(Protocol):
    async def list_contact_conversations(
        self, *, contact_id: int, inbox_id: int
    ) -> Sequence[Any]: ...


def webhook_contact_id(payload: Mapping[str, object]) -> int | None:
    """El contacto de Chatwoot del webhook ``message_created`` de un entrante.

    ``conversation.meta.sender`` con ``type: contact`` (captura
    ``chatwoot_message_created_buttons_inbox_11_conv_536_20261028.json``). Si el
    ``sender`` de arriba tambien trae ``id``, tiene que ser el mismo. Otra forma:
    ``None`` (no se hereda nada).
    """
    conversation = payload.get("conversation")
    meta = conversation.get("meta") if isinstance(conversation, Mapping) else None
    sender = meta.get("sender") if isinstance(meta, Mapping) else None
    if not isinstance(sender, Mapping) or sender.get("type") != "contact":
        return None
    contact_id = sender.get("id")
    if type(contact_id) is not int or contact_id < 1:
        return None
    top = payload.get("sender")
    if isinstance(top, Mapping) and "id" in top and top.get("id") != contact_id:
        return None
    return contact_id


def with_labels(payload: dict[str, Any], labels: Iterable[str]) -> dict[str, Any]:
    """El payload con ``conversation.labels`` sumando ``labels``, sin tocar el original."""
    extra = [label for label in labels if label not in conversation_labels(payload)]
    conversation = payload.get("conversation")
    if not extra or not isinstance(conversation, Mapping):
        return payload
    current = conversation.get("labels")
    base = list(current) if isinstance(current, list) else []
    return {**payload, "conversation": {**conversation, "labels": [*base, *extra]}}


class SegmentLabelInheritance:
    """Hereda a una conversacion las etiquetas de segmento de otra del mismo contacto."""

    def __init__(
        self,
        *,
        client: _ContactConversations,
        inbox_id: int,
        segments: Iterable[SegmentLabel] = (),
        labelers: Mapping[str, _Labeler] | None = None,
        source: SegmentLabelSource | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        remembered: int = DEFAULT_REMEMBERED_CONVERSATIONS,
    ) -> None:
        if type(inbox_id) is not int or inbox_id < 1 or remembered < 1:
            raise SegmentLabelsConfigError("segment_inheritance_invalid")
        if source is None:
            # La fuente fija de hoy: se valida al construir, como siempre.
            segments = tuple(segments)
            labelers = dict(labelers or {})
            if not segments:
                raise SegmentLabelsConfigError("segment_inheritance_invalid")
            if set(labelers) - {segment.label for segment in segments}:
                raise SegmentLabelsConfigError("segment_labelers_invalid")
            source = StaticSegmentLabels(segments, labelers)
        self._client = client
        self._inbox_id = inbox_id
        self._source = source
        self._clock = clock
        self._remembered = remembered
        # conversacion -> lo que heredo (vacio: se miro y no habia nada).
        self._seen: OrderedDict[int, tuple[str, ...]] = OrderedDict()
        # Con que etiquetas de segmento se miro cada una (solo cuenta con la fuente viva).
        self._seen_with: dict[int, frozenset[str]] = {}

    async def apply(self, payload: dict[str, Any], *, conversation_id: int) -> dict[str, Any]:
        """El payload del turno con las etiquetas heredadas. Nunca lanza.

        * La conversacion ya tiene una etiqueta de segmento en el webhook: nada
          (``present``), sin llamadas.
        * Ya se miro en este proceso: se suma lo que heredo, sin llamadas.
        * Si no, una llamada: las otras conversaciones del contacto en el inbox. Las
          etiquetas de segmento que tengan y que no vencieron se le ponen a esta por
          el macro y se suman al turno. Una lectura que falla no se recuerda: el
          proximo turno vuelve a intentar.
        """
        segments = {segment.label: segment for segment in self._source.segments()}
        labels = set(conversation_labels(payload))
        if labels & set(segments):
            return payload
        if conversation_id in self._seen and (
            not self._source.live
            or self._seen_with.get(conversation_id) == frozenset(segments)
        ):
            # Con la fuente viva, lo recordado vale mientras los segmentos sean los
            # mismos: una campana publicada despues se vuelve a mirar.
            self._seen.move_to_end(conversation_id)
            return with_labels(payload, self._seen[conversation_id])
        if self._source.live and all(
            segment.state_at(self._clock()) == STATE_EXPIRED for segment in segments.values()
        ):
            # E-411: con la fuente viva y ningun segmento sin vencer no hay nada que
            # heredar: no se llama a Chatwoot (lo de quien no esta en ninguna
            # campana). No se recuerda: si aparece una campana, se mira.
            logger.info(
                "segment_label_inheritance outcome=sin_segmentos conversation=%s", conversation_id
            )
            return payload
        contact_id = webhook_contact_id(payload)
        if contact_id is None:
            logger.info(
                "segment_label_inheritance outcome=sin_contacto conversation=%s", conversation_id
            )
            self._remember(conversation_id, ())
            return payload
        try:
            others = await self._client.list_contact_conversations(
                contact_id=contact_id, inbox_id=self._inbox_id
            )
        except Exception as exc:  # noqa: BLE001 - falla blanda, el log lleva el tipo
            logger.warning(
                "segment_label_inheritance outcome=lectura_fallida conversation=%s contact=%s error=%s",
                conversation_id,
                contact_id,
                type(exc).__name__,
            )
            return payload
        now = self._clock()
        found: list[str] = []
        sources: list[int] = []
        for other in others:
            other_id = getattr(other, "conversation_id", None)
            if other_id == conversation_id:
                continue
            for label in getattr(other, "labels", ()) or ():
                segment = segments.get(label)
                if segment is None or label in found:
                    continue
                if segment.state_at(now) == STATE_EXPIRED:
                    continue
                if segment.origin == ORIGIN_AGENT_CAMPAIGN and not _active_since(
                    other, segment.valid_from
                ):
                    # D1 de E-411: la etiqueta de una campana del AE se puede volver a
                    # usar en otra campana. Solo se hereda de una conversacion con
                    # actividad desde el desde de la campana vigente.
                    continue
                found.append(label)
                if isinstance(other_id, int):
                    sources.append(other_id)
        inherited = tuple(found)
        for label in inherited:
            labeler = self._source.labeler(label)
            outcome = (
                await labeler.ensure(conversation_id, source="segment_inheritance")
                if labeler is not None
                else "sin_macro"
            )
            logger.info(
                "segment_label_inherited conversation=%s contact=%s segment=%s label=%s "
                "from=%s outcome=%s",
                conversation_id,
                contact_id,
                segments[label].key,
                label,
                ",".join(str(source) for source in dict.fromkeys(sources)),
                outcome,
            )
        if not inherited:
            logger.info(
                "segment_label_inheritance outcome=nada conversation=%s contact=%s others=%s",
                conversation_id,
                contact_id,
                sum(1 for other in others if getattr(other, "conversation_id", None) != conversation_id),
            )
        self._remember(conversation_id, inherited, frozenset(segments))
        return with_labels(payload, inherited)

    def _remember(
        self, conversation_id: int, labels: tuple[str, ...], with_segments: frozenset[str] = frozenset()
    ) -> None:
        self._seen[conversation_id] = labels
        self._seen_with[conversation_id] = with_segments
        self._seen.move_to_end(conversation_id)
        while len(self._seen) > self._remembered:
            dropped, _ = self._seen.popitem(last=False)
            self._seen_with.pop(dropped, None)


def _active_since(conversation: object, start: datetime | None) -> bool:
    """Si la conversacion tuvo actividad desde ``start`` (``last_activity_at`` en epoch)."""
    if start is None:
        return False
    activity = getattr(conversation, "last_activity_at", None)
    if type(activity) is not int:
        return False
    return activity >= int(start.timestamp())


BeforeSend = Callable[[int], Awaitable[object]]


# ---------------------------------------------------------------------------
# Lo que ve el Automation Expert (automation-expert-read-v1 1.5.0).
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _labels_in(steps: object) -> list[str]:
    found: list[str] = []
    if not isinstance(steps, list):
        return found
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        label = step.get("etiquetar")
        if isinstance(label, str):
            found.append(label)
        for branch in ("entonces", "si_no"):
            found.extend(_labels_in(step.get(branch)))
    return found


def automation_expert_segments(
    segments: Iterable[SegmentLabel], flows: Iterable[object], *, now: datetime
) -> dict[str, object]:
    """``segments`` y ``allowed_labels`` del sobre de ``/flows`` (E-316).

    Cada segmento con su etiqueta, si tiene macro (sin macro el motor no la puede
    poner), su vigencia y su estado ahora, y los flujos cuya version activa la
    pone. ``allowed_labels``: lo que acepta ``etiquetar`` en esta instancia.
    """
    by_label: dict[str, list[str]] = {}
    for flow in flows:
        if not isinstance(flow, Mapping):
            continue
        flow_key = flow.get("flow_key")
        definition = flow.get("definition")
        steps = definition.get("pasos") if isinstance(definition, Mapping) else None
        for label in _labels_in(steps):
            if isinstance(flow_key, str):
                keys = by_label.setdefault(label, [])
                if flow_key not in keys:
                    keys.append(flow_key)
    items: list[dict[str, object]] = [
        {
            "key": segment.key,
            "label": segment.label,
            "labelable": segment.macro_id is not None,
            "state": segment.state_at(now),
            "valid_from": _iso(segment.valid_from),
            "valid_until": _iso(segment.valid_until),
            "labeled_by": sorted(by_label.get(segment.label, [])),
        }
        for segment in segments
    ]
    allowed = [CUSTOMER_LABEL, *(str(item["label"]) for item in items if item["labelable"])]
    return {"segments": items, "allowed_labels": allowed}


# ---------------------------------------------------------------------------
# El armado desde la configuracion del bridge.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentLabelsSetup:
    """Lo que arma el bridge al arrancar con los flags de E-316."""

    segments: tuple[SegmentLabel, ...] = ()
    native: NativeSegmentLabels | None = None
    inheritance: SegmentLabelInheritance | None = None
    # E-411: con AGENT_CAMPAIGN_LABEL_PATHS_ENABLED, la fuente viva (el manifiesto
    # mas las campanas del AE). None: los segmentos fijos de arriba, lo de hoy.
    source: SegmentLabelSource | None = None

    @property
    def validator_enabled(self) -> bool:
        """El validador y el AE ven los segmentos solo con el flag del motor."""
        return self.native is not None

    def current_segments(self) -> tuple[SegmentLabel, ...]:
        """Los segmentos de ahora: los de la fuente viva, o los fijos."""
        return self.source.segments() if self.source is not None else self.segments

    def validator_labels(self, now: datetime) -> list[dict[str, str]]:
        return validator_segment_labels(self.current_segments(), now=now) if self.validator_enabled else []


def segment_labels_setup(settings: Any, control_client: Any, *, live: Any = None) -> SegmentLabelsSetup:
    """Los etiquetadores de los segmentos, o nada sin los flags. Falla cerrado.

    ``FLOW_ENGINE_SEGMENT_LABELS_ENABLED`` pide el motor de flujos, el cliente de
    control de Chatwoot y al menos un segmento con ``macro_etiqueta``.
    ``SEGMENT_LABEL_INHERITANCE_ENABLED`` pide ademas los segmentos del agente
    cargados (``AGENT_SEGMENTS_ENABLED`` de PR-A) y el inbox: heredar una etiqueta
    que el agente no usa no sirve. El macro de un segmento no puede ser el de la
    pausa, la reanudacion, la baja ni el de cliente.

    ``live`` (E-411, con ``AGENT_CAMPAIGN_LABEL_PATHS_ENABLED``): el catalogo vivo
    de las campanas del AE. Con el, el arranque no exige ``[[agente.segmentos]]``
    ni un segmento con macro (como ``campaign_inbound_entry_setup`` con
    ``campaigns_live``), y la herencia y el plan nativo leen la fuente viva: los
    segmentos del manifiesto mas las campanas servidas. Sin ``live``, lo de hoy.
    """
    from .product_label import ProductLabeler

    motor = bool(getattr(settings, "flow_engine_segment_labels_enabled", False))
    inherit = bool(getattr(settings, "segment_label_inheritance_enabled", False))
    if not motor and not inherit:
        return SegmentLabelsSetup()
    manifest = getattr(settings, "instance_manifest", None)
    catalog = getattr(settings, "agent_segment_catalog", None)
    flag = "FLOW_ENGINE_SEGMENT_LABELS_ENABLED" if motor else "SEGMENT_LABEL_INHERITANCE_ENABLED"
    if manifest is None:
        raise ValueError(f"{flag} requires an instance manifest")
    if control_client is None:
        raise ValueError(f"{flag} requires the Chatwoot control client")
    segments = declared_segment_labels(manifest, catalog)
    if not segments and live is None:
        raise ValueError(f"{flag} requires [[agente.segmentos]] in the manifest")
    reserved = {
        getattr(settings, name, None)
        for name in (
            "chatwoot_pause_macro_id",
            "chatwoot_resume_macro_id",
            "chatwoot_opt_out_macro_id",
            "chatwoot_client_label_macro_id",
        )
    } - {None}
    labelers: dict[str, ProductLabeler] = {}
    for segment in labelable(segments):
        assert segment.macro_id is not None
        if segment.macro_id in reserved:
            raise ValueError(
                "a segment label macro cannot be a pause, resume, opt-out or client macro"
            )
        labelers[segment.label] = ProductLabeler(
            client=control_client, label=segment.label, macro_id=segment.macro_id
        )
    source: SegmentLabelSource | None = None
    if live is not None:
        live.bind_labels(
            segments=segments,
            labelers=labelers,
            make_labeler=lambda label, macro_id: ProductLabeler(
                client=control_client, label=label, macro_id=macro_id
            ),
        )
        source = live.label_source()
    native = None
    if motor:
        if not getattr(settings, "flow_engine_enabled", False):
            raise ValueError("FLOW_ENGINE_SEGMENT_LABELS_ENABLED requires FLOW_ENGINE_ENABLED")
        if source is not None:
            native = NativeSegmentLabels(source=source)
        else:
            if not labelers:
                raise ValueError(
                    "FLOW_ENGINE_SEGMENT_LABELS_ENABLED requires a segment with macro_etiqueta"
                )
            native = NativeSegmentLabels(segments=segments, labelers=labelers)
    inheritance = None
    if inherit:
        if catalog is None or not getattr(settings, "agent_segments_enabled", False):
            raise ValueError("SEGMENT_LABEL_INHERITANCE_ENABLED requires AGENT_SEGMENTS_ENABLED")
        inbox_id = getattr(settings, "chatwoot_inbox_id", None)
        if type(inbox_id) is not int:
            raise ValueError("SEGMENT_LABEL_INHERITANCE_ENABLED requires CHATWOOT_INBOX_ID")
        inheritance = (
            SegmentLabelInheritance(client=control_client, inbox_id=inbox_id, source=source)
            if source is not None
            else SegmentLabelInheritance(
                client=control_client, inbox_id=inbox_id, segments=segments, labelers=labelers
            )
        )
    return SegmentLabelsSetup(segments=segments, native=native, inheritance=inheritance, source=source)
