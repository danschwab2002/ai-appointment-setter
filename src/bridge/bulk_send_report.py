"""El reporte de un envio masivo (encargo 10 del masivo manejado por el AE).

Pieza 12 del mapa del 2026-11-25 («El masivo manejado por el Automation
Expert»). Por envio: mandados, entregados, leidos, fallidos por codigo de Meta
con su explicacion en castellano, respuestas, bajas, derivaciones y compras de
quien recibio. Dos vistas: la del AE, solo con numeros, y la de la pagina, con
el CSV de los no entregados (telefono enmascarado salvo para quien puede verlo,
que decide la pagina).

Los estados se leen de Chatwoot (``messages.status`` y
``content_attributes.external_error``), no de logs: los logs de Chatwoot y de
Traefik rotan, y el 22/11 los conteos minuto a minuto bajaron entre una corrida
y la siguiente (entregados 2.694 -> 2.441) porque se contaban sobre logs que ya
no estaban (``demora-de-meta-22-11.md``, «Lo que se perdio», en el vault). Es
el mismo criterio de la verificacion del prototipo (``masivo.py``,
``cmd_verificar`` y ``estados_de``), que leia la base de Chatwoot; aca se lee
por la API, como ``meta_delivery`` y el AE.

Tres piezas, separadas a proposito:

* ``build_report`` es el criterio, puro y sin red: toma el registro por persona
  del envio (conversacion y mensaje de Chatwoot de cada una), lo leido de cada
  conversacion y las compras, y arma el reporte.
* ``BulkSendChatwootReader`` es el puerto de lectura, y
  ``ChatwootBulkSendReader`` su adaptador sobre ``ChatwootClient``: el show de
  la conversacion (etiquetas, asignacion, bandeja) y sus mensajes paginando
  hacia atras con ``before``. Lee por el id de la conversacion; nunca busca
  contactos (``/contacts/search`` tiene su propio cupo de 100 por minuto y lo
  usa el bridge).
* ``read_conversations`` lee las conversaciones de un envio con poca
  concurrencia; una conversacion que no se pudo leer queda como «no se pudo
  leer» y no tumba el reporte.

Nada de esto escribe. No lo usa ningun codigo todavia: lo van a usar la ruta
del AE y la seccion de la pagina del masivo.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from bridge.automation_expert_read import META_ERROR_EXPLANATIONS
from bridge.chatwoot import ChatwootClient, ChatwootProtocolError
from bridge.meta_error import meta_error_code_of
from bridge.opt_out import detect_explicit_opt_out

# El registro por persona del envio (lo escribe el motor, encargo 9). Solo
# «sent» tiene un mensaje en Chatwoot que leer.
RECIPIENT_SENT = "sent"
RECIPIENT_SEND_FAILED = "send_failed"  # Chatwoot no acepto el envio
RECIPIENT_VETOED = "vetoed"  # salio de la lista al revalidar los vetos
RECIPIENT_DUBIOUS = "dubious"  # reservado sin cierre: no se sabe si salio, no se reintenta
RECIPIENT_PENDING = "pending"  # no llego a salir (freno, corte u hora de corte)
RECIPIENT_STATES = frozenset({
    RECIPIENT_SENT, RECIPIENT_SEND_FAILED, RECIPIENT_VETOED, RECIPIENT_DUBIOUS, RECIPIENT_PENDING,
})

# Lo que vale para cada persona mandada, leido de Chatwoot.
DELIVERY_SENT = "sent"  # Chatwoot la mando y Meta todavia no confirmo
DELIVERY_DELIVERED = "delivered"
DELIVERY_READ = "read"
DELIVERY_FAILED = "failed"
DELIVERY_UNREADABLE = "unreadable"  # la conversacion o el mensaje no se pudieron leer
RECEIVED = frozenset({DELIVERY_DELIVERED, DELIVERY_READ})
_CHATWOOT_STATUSES = frozenset({DELIVERY_SENT, DELIVERY_DELIVERED, DELIVERY_READ, DELIVERY_FAILED})

OPTED_OUT_LABEL = "automation_opted_out"
PAUSED_LABEL = "automation_paused"

# Los codigos que frenan un masivo (los de ``FRENO_CODIGOS`` del prototipo,
# masivo.py:121-124, y del encargo 8) y el 135000 del portfolio pacing. La
# frase es nuestra; de Chatwoot sale solo el codigo en digitos, nunca el texto
# de Meta, que puede traer el telefono.
BULK_SEND_CUT_CODE_EXPLANATIONS = {
    "368": "Meta bloqueó los envíos de la cuenta por un tiempo, por incumplir sus políticas.",
    "130429": "Meta frenó el envío porque se superó la cantidad de mensajes por segundo del número.",
    "131031": "Meta bloqueó la cuenta de WhatsApp: no se puede mandar nada hasta resolverlo con Meta.",
    "131048": "Meta frenó el envío por spam: demasiadas personas bloquearon o reportaron los mensajes.",
    "131056": "Meta no la entregó porque se mandaron demasiados mensajes seguidos a ese mismo número.",
    "132005": "Meta la rechazó porque el texto con las variables queda más largo de lo permitido.",
    "132007": "Meta la rechazó porque el texto con las variables no cumple sus reglas de formato.",
    "132012": "Meta la rechazó porque las variables no tienen el formato que pide la plantilla.",
    "132015": "Meta no la mandó porque la plantilla está pausada por baja calidad.",
    "132016": "Meta no la mandó porque la plantilla está desactivada.",
    "135000": "Meta retuvo el mensaje y después lo descartó (revisión de calidad de un envío grande).",
}
META_CODE_EXPLANATIONS = {**META_ERROR_EXPLANATIONS, **BULK_SEND_CUT_CODE_EXPLANATIONS}
NO_CODE_EXPLANATION = "Meta no la entregó y Chatwoot no guardó el código."
_UNKNOWN_CODE_EXPLANATION = "Meta no la entregó (código {code}); no tenemos la explicación de ese código."

# El estado de cada fila del CSV de no entregados, como lo lee una persona.
_ROW_STATE_LABELS = {
    DELIVERY_FAILED: "fallido",
    DELIVERY_SENT: "sin confirmar",
    DELIVERY_UNREADABLE: "no se pudo leer",
    RECIPIENT_SEND_FAILED: "no salió",
    RECIPIENT_VETOED: "vetado",
    RECIPIENT_DUBIOUS: "dudoso",
    RECIPIENT_PENDING: "no llegó a salir",
}
_ROW_STATE_EXPLANATIONS = {
    DELIVERY_SENT: "Chatwoot la mandó y Meta todavía no confirmó la entrega.",
    DELIVERY_UNREADABLE: "No se pudo leer la conversación en Chatwoot.",
    RECIPIENT_SEND_FAILED: "Chatwoot no aceptó el envío.",
    RECIPIENT_VETOED: "Se sacó de la lista al revisar los vetos antes de mandar.",
    RECIPIENT_DUBIOUS: "No se sabe si salió: quedó reservada sin cierre y no se reintenta.",
    RECIPIENT_PENDING: "El envío frenó o terminó antes de llegar a esta persona.",
}
UNDELIVERED_CSV_COLUMNS = ("telefono", "conversacion", "estado", "codigo_meta", "explicacion")


def meta_code_explanation(code: str | None) -> str:
    """La frase en castellano de un codigo de Meta, o la generica."""
    if code is None:
        return NO_CODE_EXPLANATION
    return META_CODE_EXPLANATIONS.get(code, _UNKNOWN_CODE_EXPLANATION.format(code=code))


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@dataclass(frozen=True)
class BulkSendRecipient:
    """Una persona del envio, como la dejo el registro del motor.

    ``conversation_id`` es el id de la conversacion en la API de Chatwoot (el
    ``display_id``), y ``message_id`` el del mensaje de la plantilla. Solo una
    persona ``sent`` los tiene obligatorios.
    """

    recipient_id: str
    phone: str
    state: str
    conversation_id: int | None = None
    message_id: int | None = None
    sent_at: int | None = None

    def __post_init__(self) -> None:
        if not self.recipient_id:
            raise ValueError("recipient_id is required")
        if self.state not in RECIPIENT_STATES:
            raise ValueError(f"unknown recipient state: {self.state}")
        if self.state == RECIPIENT_SENT and not (
            _positive_int(self.conversation_id)
            and _positive_int(self.message_id)
            and _positive_int(self.sent_at)
        ):
            raise ValueError("a sent recipient needs conversation_id, message_id and sent_at")


@dataclass(frozen=True)
class BulkSendPurchase:
    """Una compra aprobada atada a una persona del envio (la ata quien llama)."""

    recipient_id: str
    purchased_at: int


@dataclass(frozen=True)
class ConversationReading:
    """Lo leido de una conversacion de Chatwoot: el show y sus mensajes."""

    conversation_id: int
    messages: tuple[Mapping[str, object], ...]
    labels: tuple[str, ...] = ()
    assignee_present: bool = False
    team_present: bool = False


@dataclass(frozen=True)
class RecipientOutcome:
    recipient_id: str
    phone: str
    recipient_state: str
    conversation_id: int | None
    delivery: str | None  # None si no salio
    meta_code: str | None
    replied: bool
    opted_out: bool
    handed_off: bool
    purchased: bool

    @property
    def received(self) -> bool:
        return self.delivery in RECEIVED

    @property
    def row_state(self) -> str:
        """Que se pone en el CSV: lo de Chatwoot si salio, lo del registro si no."""
        return self.delivery if self.recipient_state == RECIPIENT_SENT else self.recipient_state


@dataclass(frozen=True)
class FailureCount:
    code: str | None
    count: int
    explanation: str


@dataclass(frozen=True)
class BulkSendReport:
    """Los numeros de un envio. ``delivered`` cuenta tambien los leidos."""

    sent: int
    delivered: int
    read: int
    failed: int
    awaiting_delivery: int
    unreadable: int
    failed_by_code: tuple[FailureCount, ...]
    replies: int
    opt_outs: int
    handoffs: int
    purchases: int
    not_sent: Mapping[str, int]
    outcomes: tuple[RecipientOutcome, ...] = field(repr=False)


def _attributes(message: Mapping[str, object]) -> Mapping[str, object]:
    raw = message.get("content_attributes")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, Mapping) else {}


def _public_inbound_after(messages: Sequence[Mapping[str, object]], message_id: int) -> list[Mapping[str, object]]:
    """Los mensajes publicos de la persona posteriores a la plantilla (los ids de Chatwoot crecen)."""
    return [
        m
        for m in messages
        if m.get("message_type") == 0
        and m.get("private") is False
        and _positive_int(m.get("id"))
        and int(m["id"]) > message_id  # type: ignore[call-overload]
    ]


def _outcome(
    recipient: BulkSendRecipient,
    reading: ConversationReading | None,
    purchased_at: Sequence[int],
) -> RecipientOutcome:
    if recipient.state != RECIPIENT_SENT:
        return RecipientOutcome(
            recipient.recipient_id, recipient.phone, recipient.state, recipient.conversation_id,
            None, None, False, False, False, False,
        )
    assert recipient.message_id is not None and recipient.sent_at is not None
    message = None
    if reading is not None and reading.conversation_id == recipient.conversation_id:
        message = next((m for m in reading.messages if m.get("id") == recipient.message_id), None)
    if reading is None or message is None or message.get("status") not in _CHATWOOT_STATUSES:
        return RecipientOutcome(
            recipient.recipient_id, recipient.phone, recipient.state, recipient.conversation_id,
            DELIVERY_UNREADABLE, None, False, False, False, False,
        )
    delivery = str(message.get("status"))
    code = meta_error_code_of(_attributes(message).get("external_error")) if delivery == DELIVERY_FAILED else None
    inbound = _public_inbound_after(reading.messages, recipient.message_id)
    texts = [m.get("content") for m in inbound]
    opted_out = OPTED_OUT_LABEL in reading.labels or detect_explicit_opt_out(texts) is not None
    handed_off = PAUSED_LABEL in reading.labels or reading.assignee_present or reading.team_present
    received = delivery in RECEIVED
    purchased = received and any(at >= recipient.sent_at for at in purchased_at)
    return RecipientOutcome(
        recipient.recipient_id, recipient.phone, recipient.state, recipient.conversation_id,
        delivery, code, bool(inbound), opted_out, handed_off, purchased,
    )


def build_report(
    recipients: Iterable[BulkSendRecipient],
    readings: Mapping[int, ConversationReading | None],
    purchases: Iterable[BulkSendPurchase] = (),
) -> BulkSendReport:
    """El reporte de un envio. Puro: no lee nada.

    ``readings`` va por id de conversacion; una conversacion que falta o que es
    ``None`` cuenta como «no se pudo leer». Las respuestas, las bajas, las
    derivaciones y las compras se cuentan solo entre las personas mandadas; las
    compras, solo de quien recibio (entregado o leido) y desde el envio.
    """
    by_recipient: dict[str, list[int]] = {}
    for purchase in purchases:
        by_recipient.setdefault(purchase.recipient_id, []).append(purchase.purchased_at)
    seen: set[str] = set()
    outcomes: list[RecipientOutcome] = []
    for recipient in recipients:
        if recipient.recipient_id in seen:
            raise ValueError(f"duplicated recipient: {recipient.recipient_id}")
        seen.add(recipient.recipient_id)
        reading = readings.get(recipient.conversation_id) if recipient.conversation_id else None
        outcomes.append(_outcome(recipient, reading, by_recipient.get(recipient.recipient_id, ())))

    sent = [o for o in outcomes if o.recipient_state == RECIPIENT_SENT]
    deliveries = Counter(o.delivery for o in sent)
    codes = Counter(o.meta_code for o in sent if o.delivery == DELIVERY_FAILED)
    failed_by_code = tuple(
        FailureCount(code, count, meta_code_explanation(code))
        for code, count in sorted(codes.items(), key=lambda item: (-item[1], item[0] or "~"))
    )
    not_sent = Counter(o.recipient_state for o in outcomes if o.recipient_state != RECIPIENT_SENT)
    return BulkSendReport(
        sent=len(sent),
        delivered=deliveries[DELIVERY_DELIVERED] + deliveries[DELIVERY_READ],
        read=deliveries[DELIVERY_READ],
        failed=deliveries[DELIVERY_FAILED],
        awaiting_delivery=deliveries[DELIVERY_SENT],
        unreadable=deliveries[DELIVERY_UNREADABLE],
        failed_by_code=failed_by_code,
        replies=sum(o.replied for o in sent),
        opt_outs=sum(o.opted_out for o in sent),
        handoffs=sum(o.handed_off for o in sent),
        purchases=sum(o.purchased for o in sent),
        not_sent=dict(sorted(not_sent.items())),
        outcomes=tuple(outcomes),
    )


def ae_view(report: BulkSendReport) -> dict[str, object]:
    """Lo que ve el AE: solo numeros y las explicaciones, sin telefonos ni conversaciones."""
    return {
        "sent": report.sent,
        "delivered": report.delivered,
        "read": report.read,
        "failed": report.failed,
        "awaiting_delivery": report.awaiting_delivery,
        "unreadable": report.unreadable,
        "failed_by_code": [
            {"code": f.code, "count": f.count, "explanation": f.explanation} for f in report.failed_by_code
        ],
        "replies": report.replies,
        "opt_outs": report.opt_outs,
        "handoffs": report.handoffs,
        "purchases": report.purchases,
        "not_sent": dict(report.not_sent),
    }


def mask_phone(phone: str) -> str:
    """Los digitos con solo los dos primeros y los cuatro ultimos a la vista."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 8:
        return "*" * len(digits)
    return digits[:2] + "*" * (len(digits) - 6) + digits[-4:]


def _csv_phone(phone: str, *, reveal: bool) -> str:
    # Sin el «+»: una celda que arranca con «+» la lee la planilla como formula.
    return "".join(ch for ch in phone if ch.isdigit()) if reveal else mask_phone(phone)


def undelivered_rows(report: BulkSendReport, *, viewer_can_see_phones: bool) -> list[dict[str, str]]:
    """Las personas que no recibieron, en el orden del registro.

    ``viewer_can_see_phones`` lo decide la pagina: true solo para un aprobador
    del envio con la sesion de Slack. Para el resto el telefono va enmascarado.
    """
    rows: list[dict[str, str]] = []
    for outcome in report.outcomes:
        if outcome.received:
            continue
        state = outcome.row_state or DELIVERY_UNREADABLE
        if state == DELIVERY_FAILED:
            explanation = meta_code_explanation(outcome.meta_code)
        else:
            explanation = _ROW_STATE_EXPLANATIONS[state]
        rows.append({
            "telefono": _csv_phone(outcome.phone, reveal=viewer_can_see_phones),
            "conversacion": str(outcome.conversation_id) if outcome.conversation_id else "",
            "estado": _ROW_STATE_LABELS[state],
            "codigo_meta": outcome.meta_code or "",
            "explicacion": explanation,
        })
    return rows


def undelivered_csv(report: BulkSendReport, *, viewer_can_see_phones: bool) -> str:
    """El CSV de los no entregados para bajar desde la pagina."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=UNDELIVERED_CSV_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(undelivered_rows(report, viewer_can_see_phones=viewer_can_see_phones))
    return buffer.getvalue()


def page_view(report: BulkSendReport, *, viewer_can_see_phones: bool) -> dict[str, object]:
    """Lo que muestra la pagina: los numeros del AE y el CSV de los no entregados."""
    return {
        **ae_view(report),
        "undelivered_count": sum(1 for o in report.outcomes if not o.received),
        "undelivered_csv": undelivered_csv(report, viewer_can_see_phones=viewer_can_see_phones),
        "phones_visible": viewer_can_see_phones,
    }


class BulkSendChatwootReader(Protocol):
    """El puerto: leer una conversacion del envio en Chatwoot."""

    async def read_conversation(
        self, *, conversation_id: int, since_message_id: int
    ) -> ConversationReading: ...


class ChatwootBulkSendReader:
    """El adaptador por la API de Chatwoot, con el cliente del bridge.

    Lee el show (etiquetas, asignacion y que la conversacion sea de la bandeja
    del envio) y los ultimos ``messages_limit`` mensajes paginando con
    ``before``. Si la plantilla quedo mas atras, la pide de nuevo exigiendo ese
    id (``required_message_ids``): llegan la plantilla y los ultimos mensajes,
    que es lo que hace falta para el estado y para saber si la persona escribio.
    """

    def __init__(self, client: ChatwootClient, *, inbox_id: int, messages_limit: int = 100) -> None:
        if messages_limit <= 0:
            raise ValueError("messages_limit must be positive")
        self._client = client
        self._inbox_id = inbox_id
        self._messages_limit = messages_limit

    async def read_conversation(self, *, conversation_id: int, since_message_id: int) -> ConversationReading:
        data = await self._client.get_conversation_with_messages(
            conversation_id=conversation_id,
            expected_inbox_id=self._inbox_id,
            messages_limit=self._messages_limit,
        )
        messages = {m["id"]: m for m in data["messages"] if isinstance(m, dict)}
        if since_message_id not in messages:
            extra = await self._client.get_conversation_messages(
                conversation_id=conversation_id,
                limit=self._messages_limit,
                required_message_ids=(since_message_id,),
            )
            messages.update({m["id"]: m for m in extra if isinstance(m, dict)})
        details = data["conversation"]
        labels = details.get("labels")
        meta = details.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        return ConversationReading(
            conversation_id=conversation_id,
            messages=tuple(messages[key] for key in sorted(messages)),
            labels=tuple(label for label in labels if isinstance(label, str)) if isinstance(labels, list) else (),
            assignee_present=meta.get("assignee") is not None,
            team_present=meta.get("team") is not None,
        )


# Los errores de una conversacion que no tumban el reporte entero.
_READ_ERRORS = (ChatwootProtocolError, httpx.HTTPError, KeyError, TypeError, ValueError)


async def read_conversations(
    reader: BulkSendChatwootReader,
    recipients: Collection[BulkSendRecipient],
    *,
    max_concurrency: int = 4,
) -> dict[int, ConversationReading | None]:
    """Lee una vez cada conversacion de las personas mandadas.

    Poca concurrencia a proposito: el cupo de la API de Chatwoot (Rack::Attack,
    3000 por minuto por IP) lo comparte con el bridge, que esta vendiendo.
    """
    if max_concurrency <= 0:
        raise ValueError("max_concurrency must be positive")
    since: dict[int, int] = {}
    for recipient in recipients:
        if recipient.state == RECIPIENT_SENT:
            assert recipient.conversation_id is not None and recipient.message_id is not None
            current = since.get(recipient.conversation_id)
            since[recipient.conversation_id] = (
                recipient.message_id if current is None else min(current, recipient.message_id)
            )
    semaphore = asyncio.Semaphore(max_concurrency)
    readings: dict[int, ConversationReading | None] = {}

    async def one(conversation_id: int, message_id: int) -> None:
        async with semaphore:
            try:
                readings[conversation_id] = await reader.read_conversation(
                    conversation_id=conversation_id, since_message_id=message_id
                )
            except _READ_ERRORS:
                readings[conversation_id] = None

    await asyncio.gather(*(one(c, m) for c, m in sorted(since.items())))
    return readings
