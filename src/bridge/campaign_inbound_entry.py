"""La entrada a una campana por el primer mensaje (E-317, ``campaign-inbound-entry-v1``).

Diseno: ``documentacion/2026-11-03-e319-diseno-campanas.md`` (E-319, §1.1 y
§1.2, D4, D5, D11 y D14) y el contrato ``segment-labels-v1`` §3 (E-316).
Contrato: ``docs/contracts/campaign-inbound-entry-v1.md``.

Una campana (hoy, un segmento de ``[[agente.segmentos]]``, PR-A) se activa con
una etiqueta de la conversacion de Chatwoot. Este modulo la pone **antes de
armar el pedido al agente** por dos caminos mas, los dos detras de
``CAMPAIGN_INBOUND_ENTRY_ENABLED`` (apagado por defecto):

* **La palabra clave del primer mensaje** (``palabras_clave``): el boton de
  WhatsApp de la landing de la campana lleva un texto prellenado («Black
  November»). Si el primer mensaje de la persona en la conversacion contiene
  una palabra clave declarada de un segmento vigente (contenida en cualquier parte), el bridge le pone la etiqueta por
  el macro del segmento y la suma al turno: la primera respuesta ya sale con el
  conocimiento de la campana (decision 1 de Dan, 03/11). Solo el primer mensaje
  (D14): quien cambio el texto entra por la pregunta.
* **La pregunta de entrada** (``pregunta_entrada``, decision de Dan opcion b):
  mientras el segmento con la pregunta esta vigente, a quien escribio por su
  cuenta (la conversacion la abrio la persona, no una plantilla nuestra) sin la
  palabra clave, el turno le suma un aviso corto y fijo como ``system``: hay una
  campana, preguntale si viene por ella. El agente devuelve en su salida
  ``campana_respuesta: si|no`` cuando la persona contesta. ``si``: el bridge le
  pone la etiqueta y desde el turno siguiente va el bloque entero. ``no``: queda
  en la base (``segment_entry_answers``) y no se vuelve a preguntar en esa
  conversacion (D4). Sin respuesta clara, nada.

Sin el flag, o sin campana vigente, el pedido a Hermes queda byte a byte igual.
Los logs no llevan telefono, mail, nombre ni el texto de la persona: la
conversacion, la clave y la etiqueta del segmento.
"""

from __future__ import annotations

import hashlib
import logging
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)

CONTRACT = "campaign-inbound-entry-v1"
# La clave que el agente suma a su salida estructurada cuando contesta la persona.
CAMPAIGN_ANSWER_KEY = "campana_respuesta"
CAMPAIGN_ANSWERS = frozenset({"si", "no"})
# En la procedencia del turno, el aviso va con este tipo para no confundirlo con
# el bloque de un segmento (cuenta como «entro por la pregunta», E-319 §1.2).
NOTICE_KIND = "aviso_de_campana"
STATE_ACTIVE = "vigente"
DEFAULT_REMEMBERED_CONVERSATIONS = 4096


def normalize_entry_text(text: str) -> str:
    """Sin acentos ni mayusculas, todo lo que no es letra o numero como un espacio."""

    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    folded = plain.casefold()
    return " ".join("".join(char if char.isalnum() else " " for char in folded).split())


def keyword_in(text: str, keyword: str) -> bool:
    """Si ``text`` contiene la palabra clave (ya normalizada) como palabras enteras, en cualquier parte."""

    return bool(keyword) and f" {keyword} " in f" {normalize_entry_text(text)} "


class CampaignEntryConfigError(ValueError):
    """La declaracion de la entrada a una campana no sirve."""


@dataclass(frozen=True)
class EntrySegment:
    """Un segmento visto desde su entrada: palabras clave, pregunta, etiqueta y vigencia."""

    key: str
    label: str
    name: str
    version: int
    valid_from: datetime
    valid_until: datetime
    keywords: tuple[str, ...] = ()
    question: str | None = None

    def state_at(self, now: datetime) -> str:
        """``hasta`` es exclusivo, como en PR-A (D-E311-2)."""
        if now < self.valid_from:
            return "por_empezar"
        if now >= self.valid_until:
            return "vencido"
        return STATE_ACTIVE

    def overlaps(self, other: EntrySegment) -> bool:
        return self.valid_from < other.valid_until and other.valid_from < self.valid_until


def render_notice(segment: EntrySegment) -> str:
    """El aviso de campana: un ``system`` corto y fijo (E-319 §1.2), en tu (D-E311-3)."""

    assert segment.question is not None
    return (
        f"# Campaña vigente: {segment.name}\n"
        f"Hay una campaña vigente: {segment.name}. Esta persona escribió por su cuenta "
        "y no sabemos si viene por ella. Pregúntale una sola vez: "
        f"«{segment.question}». Que sea la única pregunta de esa respuesta. Hasta que "
        "conteste, no des precios, montos, descuentos ni plazos, ni los de siempre. "
        f'Cuando conteste, suma a tu JSON "{CAMPAIGN_ANSWER_KEY}": "si" o "no". '
        "Si no contesta claro, no la sumes."
    )


@dataclass(frozen=True)
class EntryNotice:
    """El aviso de un turno, con la forma de un ``system`` extra de Hermes."""

    segment: EntrySegment
    agent_replies_before: int
    kind: str = NOTICE_KIND

    @property
    def key(self) -> str:
        return self.segment.key

    @property
    def version(self) -> int:
        return self.segment.version

    @property
    def rendered(self) -> str:
        return render_notice(self.segment)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.rendered.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EntryTurn:
    """Lo que la entrada le suma a un turno."""

    # Etiquetas de segmento que el turno lleva sin que el webhook las traiga.
    labels: tuple[str, ...] = ()
    notice: EntryNotice | None = None
    # La clave del segmento cuya respuesta se acepta en este turno (la del aviso).
    pending: str | None = None


NO_ENTRY = EntryTurn()


class _Labeler(Protocol):
    async def ensure(self, conversation_id: int, *, source: str) -> str: ...


class EntryAnswers(Protocol):
    """La base: lo que contesto cada conversacion a la pregunta (D4)."""

    async def get_segment_entry_answers(
        self,
        *,
        chatwoot_account_id: int,
        chatwoot_inbox_id: int,
        chatwoot_conversation_id: int,
    ) -> Mapping[str, str]: ...

    async def record_segment_entry_answer(
        self,
        *,
        chatwoot_account_id: int,
        chatwoot_inbox_id: int,
        chatwoot_conversation_id: int,
        segment_key: str,
        answer: str,
    ) -> str: ...


def opened_by_the_person(
    history: Sequence[Mapping[str, Any]], *, reset_command: str, reset_confirmation: str
) -> bool:
    """Si el primer mensaje publico de la conversacion (desde el ultimo ``/nuevo``) es de la persona.

    ``history`` es lo que devuelve Chatwoot (``GET .../messages``, del mas viejo
    al mas nuevo). Una conversacion que abrio una plantilla nuestra (carrito,
    pago rechazado, primer contacto: un saliente del AgentBot o del equipo antes
    que nada de la persona) no es inbound y no lleva la pregunta. Se saltean las
    notas privadas, las actividades y la confirmacion de ``/nuevo``.
    """

    start = 0
    for index, message in enumerate(history):
        if message.get("message_type") == 0 and message.get("content") == reset_command:
            start = index + 1
    for message in history[start:]:
        if message.get("private") is not False or message.get("message_type") == 2:
            continue
        if message.get("content") == reset_confirmation and message.get("message_type") == 1:
            continue
        sender = message.get("sender")
        return (
            message.get("message_type") == 0
            and isinstance(sender, Mapping)
            and sender.get("type") == "contact"
        )
    return False


class CampaignEntry:
    """La palabra clave del primer mensaje y la pregunta de entrada, por turno."""

    def __init__(
        self,
        *,
        segments: Iterable[EntrySegment],
        labelers: Mapping[str, _Labeler],
        answers: EntryAnswers | None,
        account_id: int,
        inbox_id: int,
        reset_command: str,
        reset_confirmation: str,
        clock: Any = lambda: datetime.now(UTC),
        remembered: int = DEFAULT_REMEMBERED_CONVERSATIONS,
        allow_empty: bool = False,
    ) -> None:
        segments = tuple(segments)
        if (not segments and not allow_empty) or remembered < 1:
            raise CampaignEntryConfigError("campaign_entry_without_segments")
        self._answers = answers
        self.segments, self._labelers = self._checked(segments, labelers)
        self._account_id = account_id
        self._inbox_id = inbox_id
        self._reset_command = reset_command
        self._reset_confirmation = reset_confirmation
        self._clock = clock
        self._remembered = remembered
        # conversacion -> etiquetas que la entrada le puso (el macro corre en un
        # job: el webhook siguiente puede no traerla todavia).
        self._entered: OrderedDict[int, tuple[str, ...]] = OrderedDict()
        # (conversacion, segmento) -> si | no, lo que ya se sabe de la base.
        self._answered: OrderedDict[tuple[int, str], str] = OrderedDict()

    def _checked(
        self, segments: tuple[EntrySegment, ...], labelers: Mapping[str, _Labeler]
    ) -> tuple[tuple[EntrySegment, ...], dict[str, _Labeler]]:
        if any(segment.label not in labelers for segment in segments):
            raise CampaignEntryConfigError("campaign_entry_segment_without_macro")
        questions = [segment for segment in segments if segment.question is not None]
        if questions and self._answers is None:
            raise CampaignEntryConfigError("campaign_entry_question_requires_the_database")
        for index, first in enumerate(questions):
            for second in questions[index + 1 :]:
                if first.overlaps(second):
                    # D5: una sola campana con la pregunta prendida a la vez.
                    raise CampaignEntryConfigError("campaign_entry_two_questions_overlap")
        seen = [keyword for segment in segments for keyword in segment.keywords]
        if len(set(seen)) != len(seen):
            raise CampaignEntryConfigError("campaign_entry_keyword_repeated")
        return segments, dict(labelers)

    def reload(self, segments: Iterable[EntrySegment], labelers: Mapping[str, _Labeler]) -> None:
        """Las campanas de la base cambiaron (agent-campaigns-v1). Si no cierran, lanza y queda lo de antes."""

        self.segments, self._labelers = self._checked(tuple(segments), labelers)

    @property
    def labelers(self) -> Mapping[str, _Labeler]:
        return self._labelers

    @property
    def has_question(self) -> bool:
        return any(segment.question is not None for segment in self.segments)

    async def turn(
        self,
        *,
        conversation_id: int,
        history: Sequence[Mapping[str, Any]],
        history_complete: bool,
        messages: Sequence[Mapping[str, str]],
        batch_refs: Iterable[str],
        labels: Iterable[str],
        agent_replies_before: int,
    ) -> EntryTurn:
        """Lo que la entrada le suma a este turno. Nunca lanza.

        ``messages`` es la historia normalizada hasta el mensaje actual, desde el
        ultimo ``/nuevo``; ``batch_refs``, los mensajes del turno. Con la historia
        incompleta (mas mensajes que los que se leyeron) no hay primer mensaje ni
        inbound que valga: no se hace nada.
        """

        try:
            return await self._turn(
                conversation_id=conversation_id,
                history=history,
                history_complete=history_complete,
                messages=messages,
                batch_refs=set(batch_refs),
                labels=set(labels),
                agent_replies_before=agent_replies_before,
            )
        except Exception as exc:  # noqa: BLE001 - falla blanda: el turno sigue como hoy
            logger.warning(
                "segment_entry_failed conversation=%s error=%s",
                conversation_id,
                type(exc).__name__,
            )
            return NO_ENTRY

    async def _turn(
        self,
        *,
        conversation_id: int,
        history: Sequence[Mapping[str, Any]],
        history_complete: bool,
        messages: Sequence[Mapping[str, str]],
        batch_refs: set[str],
        labels: set[str],
        agent_replies_before: int,
    ) -> EntryTurn:
        if not history_complete:
            return NO_ENTRY
        now = self._clock()
        active = [segment for segment in self.segments if segment.state_at(now) == STATE_ACTIVE]
        if not active:
            return NO_ENTRY
        remembered = self._entered.get(conversation_id, ())
        if remembered:
            self._entered.move_to_end(conversation_id)
        present = labels | set(remembered)
        added: list[str] = [label for label in remembered if label not in labels]

        first_in_batch = next(
            (
                message
                for message in messages
                if message.get("_message_ref") in batch_refs and message.get("actor") == "prospect"
            ),
            None,
        )
        first_prospect = next(
            (message for message in messages if message.get("actor") == "prospect"), None
        )
        is_first_message = first_in_batch is not None and first_in_batch is first_prospect

        # 1. La palabra clave, solo en el primer mensaje de la conversacion (D14).
        if is_first_message:
            assert first_in_batch is not None
            text = first_in_batch.get("text") or ""
            for segment in active:
                if segment.label in present or not segment.keywords:
                    continue
                if not any(keyword_in(text, keyword) for keyword in segment.keywords):
                    continue
                outcome = await self._labelers[segment.label].ensure(
                    conversation_id, source="segment_entry_keyword"
                )
                logger.info(
                    "segment_label_by_keyword conversation=%s segment=%s label=%s outcome=%s",
                    conversation_id,
                    segment.key,
                    segment.label,
                    outcome,
                )
                present.add(segment.label)
                added.append(segment.label)
                self._enter(conversation_id, segment.label)

        # 2. La pregunta de entrada, a quien escribio por su cuenta.
        question = next(
            (segment for segment in active if segment.question is not None), None
        )
        if question is None or question.label in present or first_prospect is None:
            return EntryTurn(labels=tuple(added))
        if not opened_by_the_person(
            history,
            reset_command=self._reset_command,
            reset_confirmation=self._reset_confirmation,
        ):
            return EntryTurn(labels=tuple(added))
        started = first_prospect.get("_created_at")
        if (
            not isinstance(started, str)
            or not started.isdigit()
            or datetime.fromtimestamp(int(started), tz=UTC) < question.valid_from
        ):
            # La conversacion empezo antes de la campana: no se le pregunta en el medio.
            return EntryTurn(labels=tuple(added))
        answer = await self._known_answer(conversation_id, question.key)
        if answer is None:
            return EntryTurn(
                labels=tuple(added),
                notice=EntryNotice(segment=question, agent_replies_before=agent_replies_before),
                pending=question.key,
            )
        if answer == "si":
            # Ya dijo que si y la etiqueta todavia no llego en el webhook.
            await self._labelers[question.label].ensure(
                conversation_id, source="segment_entry_question"
            )
            self._enter(conversation_id, question.label)
            added.append(question.label)
        return EntryTurn(labels=tuple(added))

    async def _known_answer(self, conversation_id: int, segment_key: str) -> str | None:
        """Lo que la conversacion contesto, de la memoria o de la base. ``"?"``: no se pudo leer."""

        cached = self._answered.get((conversation_id, segment_key))
        if cached is not None:
            return cached
        assert self._answers is not None
        try:
            stored = await self._answers.get_segment_entry_answers(
                chatwoot_account_id=self._account_id,
                chatwoot_inbox_id=self._inbox_id,
                chatwoot_conversation_id=conversation_id,
            )
        except Exception as exc:  # noqa: BLE001 - sin la base no se pregunta de mas
            logger.warning(
                "segment_entry_answers_unavailable conversation=%s error=%s",
                conversation_id,
                type(exc).__name__,
            )
            return "?"
        answer = stored.get(segment_key)
        if answer in CAMPAIGN_ANSWERS:
            self._remember_answer(conversation_id, segment_key, answer)
            return answer
        return None

    async def settle(
        self, *, conversation_id: int, turn: EntryTurn, answer: object
    ) -> str | None:
        """Lo que contesto la persona a la pregunta de este turno. Nunca lanza.

        Solo cuenta si el turno llevaba el aviso (``turn.pending``). ``si``: la
        etiqueta por el macro y a la base; ``no``: a la base. Otro valor o nada:
        no se registra nada. Devuelve lo que quedo registrado.
        """

        if turn.pending is None or answer not in CAMPAIGN_ANSWERS:
            return None
        assert isinstance(answer, str) and self._answers is not None
        segment = next((segment for segment in self.segments if segment.key == turn.pending), None)
        if segment is None:
            # La campana se pauso o termino mientras el modelo pensaba (el catalogo
            # vivo recarga la entrada): la respuesta ya no abre nada y el turno sigue.
            logger.info(
                "segment_entry_answer_ignored conversation=%s segment=%s reason=segment_gone",
                conversation_id,
                turn.pending,
            )
            return None
        # Antes de esperar a la base: una recarga en el medio puede sacar la etiqueta.
        labeler = self._labelers[segment.label]
        try:
            outcome = await self._answers.record_segment_entry_answer(
                chatwoot_account_id=self._account_id,
                chatwoot_inbox_id=self._inbox_id,
                chatwoot_conversation_id=conversation_id,
                segment_key=segment.key,
                answer=answer,
            )
        except Exception as exc:  # noqa: BLE001 - falla blanda, el log lleva el tipo
            outcome = f"record_failed:{type(exc).__name__}"
        else:
            self._remember_answer(conversation_id, segment.key, answer)
        label_outcome = "none"
        if answer == "si":
            label_outcome = await labeler.ensure(
                conversation_id, source="segment_entry_question"
            )
            self._enter(conversation_id, segment.label)
        logger.info(
            "segment_entry_answer conversation=%s segment=%s answer=%s outcome=%s label=%s",
            conversation_id,
            segment.key,
            answer,
            outcome,
            label_outcome,
        )
        return answer

    def _enter(self, conversation_id: int, label: str) -> None:
        current = self._entered.get(conversation_id, ())
        if label not in current:
            self._entered[conversation_id] = (*current, label)
        self._entered.move_to_end(conversation_id)
        while len(self._entered) > self._remembered:
            self._entered.popitem(last=False)

    def _remember_answer(self, conversation_id: int, segment_key: str, answer: str) -> None:
        self._answered[(conversation_id, segment_key)] = answer
        self._answered.move_to_end((conversation_id, segment_key))
        while len(self._answered) > self._remembered:
            self._answered.popitem(last=False)


def campaign_inbound_entry_setup(
    settings: Any,
    control_client: Any,
    supabase: Any,
    *,
    reset_command: str,
    reset_confirmation: str,
    campaigns_live: bool = False,
) -> CampaignEntry | None:
    """La entrada a las campanas, o nada sin el flag. Falla cerrado.

    ``CAMPAIGN_INBOUND_ENTRY_ENABLED`` pide el manifiesto, los segmentos del
    agente cargados (``AGENT_SEGMENTS_ENABLED``: la vigencia y el nombre salen del
    archivo de cada segmento), el cliente de control de Chatwoot, la cuenta y el
    inbox, y al menos un segmento con ``palabras_clave`` o ``pregunta_entrada``
    (los dos piden ``macro_etiqueta``, lo exige el manifiesto). La pregunta pide
    ademas la base (para el ``no``, D4) y que dos segmentos con la pregunta no se
    pisen en la vigencia (D5). El macro no puede ser el de la pausa, la
    reanudacion, la baja ni el de ``cliente``.

    Con ``campaigns_live`` (``AGENT_CAMPAIGNS_ENABLED``, agent-campaigns-v1) puede
    arrancar sin ningun segmento declarado: las campanas llegan despues, de la
    base, y entonces la pregunta pide la base desde el arranque.
    """

    from .product_label import ProductLabeler

    if not getattr(settings, "campaign_inbound_entry_enabled", False):
        return None
    flag = "CAMPAIGN_INBOUND_ENTRY_ENABLED"
    manifest = getattr(settings, "instance_manifest", None)
    catalog = getattr(settings, "agent_segment_catalog", None)
    if manifest is None:
        raise ValueError(f"{flag} requires an instance manifest")
    if catalog is None or not getattr(settings, "agent_segments_enabled", False):
        raise ValueError(f"{flag} requires AGENT_SEGMENTS_ENABLED")
    if control_client is None:
        raise ValueError(f"{flag} requires the Chatwoot control client")
    account_id = getattr(settings, "chatwoot_account_id", None)
    inbox_id = getattr(settings, "chatwoot_inbox_id", None)
    if type(account_id) is not int or type(inbox_id) is not int:
        raise ValueError(f"{flag} requires CHATWOOT_ACCOUNT_ID and CHATWOOT_INBOX_ID")
    loaded = {segment.key: segment for segment in getattr(catalog, "segments", ())}
    reserved = {
        getattr(settings, name, None)
        for name in (
            "chatwoot_pause_macro_id",
            "chatwoot_resume_macro_id",
            "chatwoot_opt_out_macro_id",
            "chatwoot_client_label_macro_id",
        )
    } - {None}
    segments: list[EntrySegment] = []
    labelers: dict[str, ProductLabeler] = {}
    for declared in getattr(manifest, "agent_segments", ()):
        keywords = tuple(getattr(declared, "entry_keywords", ()) or ())
        question = getattr(declared, "entry_question", None)
        if not keywords and question is None:
            continue
        macro_id = getattr(declared, "label_macro_id", None)
        if macro_id is None:
            raise ValueError(f"{flag}: a segment entry requires macro_etiqueta")
        if macro_id in reserved:
            raise ValueError(
                "a segment label macro cannot be a pause, resume, opt-out or client macro"
            )
        segment = loaded.get(declared.key)
        if segment is None:
            raise ValueError(f"{flag}: the segment {declared.key} is not loaded")
        knowledge = segment.knowledge
        segments.append(
            EntrySegment(
                key=declared.key,
                label=declared.label,
                name=knowledge.name,
                version=segment.version,
                valid_from=knowledge.valid_from,
                valid_until=knowledge.valid_until,
                keywords=keywords,
                question=question,
            )
        )
        labelers[declared.label] = ProductLabeler(
            client=control_client, label=declared.label, macro_id=macro_id
        )
    if not segments and not campaigns_live:
        raise ValueError(f"{flag} requires palabras_clave or pregunta_entrada in [[agente.segmentos]]")
    questions = campaigns_live or any(segment.question is not None for segment in segments)
    if questions and (
        supabase is None
        or not hasattr(supabase, "get_segment_entry_answers")
        or not hasattr(supabase, "record_segment_entry_answer")
    ):
        raise ValueError(f"{flag}: pregunta_entrada requires the Supabase client")
    try:
        return CampaignEntry(
            segments=segments,
            labelers=labelers,
            answers=supabase if questions else None,
            account_id=account_id,
            inbox_id=inbox_id,
            reset_command=reset_command,
            reset_confirmation=reset_confirmation,
            allow_empty=campaigns_live,
        )
    except CampaignEntryConfigError as exc:
        raise ValueError(f"{flag}: {exc}") from exc
