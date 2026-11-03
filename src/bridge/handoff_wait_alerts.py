"""Avisar por Slack cuando una persona derivada al equipo sigue esperando.

E-309 (P-20 del plan del 2026-11-02; diagnostico E-308). La tarjeta HND-001 sale
en el momento de derivar y despues nadie vuelve a avisar. Medido el 2026-11-02
sobre produccion, solo lectura: en Maribel 3 de las 4 derivaciones seguian sin
una sola respuesta (66 a 126 h, la ventana de 24 h ya cerrada); en ATT1 la
mediana es de 6,1 h, el fin de semana de 59 h, y una sola persona contesta.

Tres avisos, al canal de Slack de la aliada por su conector (el mismo productor
que HND-001, MSG-002 y MOT-002):

* ``unattended`` (HND-006): una derivacion lleva N horas corridas sin
  respuesta del equipo (2 por defecto). Sin corte por horario (Dan, 03/11: «no
  es necesario un corte por horario», E-309b): a las 2 h de una derivacion del
  sabado a la noche sale el aviso, aunque el equipo no atienda a esa hora.
* ``window_closing`` (HND-005): faltan 2 h para que se cierre la ventana de 24 h
  de WhatsApp de una persona derivada que nadie contesto. Despues de eso el
  equipo solo puede escribirle con una plantilla.
* ``paused_reply`` (HND-012): una conversacion pausada (sin derivacion
  pendiente) donde el equipo fue el ultimo en escribir y la persona volvio a
  escribir. El agente no contesta por la pausa y nadie lo ve (E-308, punto 8:
  12 de las 14 conversaciones esperando en ATT1 no eran derivaciones).

Ninguno lleva datos de la persona: el numero de conversacion, el motivo, las
horas de espera y, en el de la ventana, la hora en que se cierra. El link a
Chatwoot lo arma el conector con su configuracion por tenant.

Un aviso por conversacion y ciclo. El ciclo es la derivacion (``unattended``),
el ultimo mensaje de la persona (``window_closing``) o el primer mensaje que
nadie contesto (``paused_reply``). El ``event_id`` sale del ciclo, asi que
volver a admitirlo despues de un reinicio da ``duplicate`` (o 409 si cambio
algun numero) en el conector, que no lo vuelve a publicar. En memoria se
recuerda lo admitido para no pedirlo en cada barrido.

Piezas, separadas a proposito:

* ``evaluate_wait_alerts`` es el criterio, puro y sin
  red, sobre el detalle de la conversacion (GET show), su historial y la
  derivacion sin atender que devuelve la base. Se testean contra capturas.
* ``HandoffWaitAlertSweeper`` es el worker recurrente: pide a la base las
  derivaciones sin atender, lista las conversaciones abiertas de la bandeja,
  lee solo las que pueden avisar y admite los avisos en el conector.

Contrato: ``docs/contracts/handoff-wait-alerts-v1.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from bridge.chatwoot import ChatwootProtocolError
from bridge.slack_notifications import NotificationProducer
from slack_correlation.catalog import NotificationCommand
from slack_correlation.producer import ConnectorRejected, ConnectorSemanticConflict

logger = logging.getLogger(__name__)

UNATTENDED_EVENT_CODE = "HND-006"
WINDOW_CLOSING_EVENT_CODE = "HND-005"
PAUSED_REPLY_EVENT_CODE = "HND-012"
ALERT_KINDS = ("unattended", "window_closing", "paused_reply")
_EVENT_CODES = {
    "unattended": UNATTENDED_EVENT_CODE,
    "window_closing": WINDOW_CLOSING_EVENT_CODE,
    "paused_reply": PAUSED_REPLY_EVENT_CODE,
}
# La ventana de servicio de Meta: 24 h desde el ultimo mensaje de la persona.
SERVICE_WINDOW_SECONDS = 86_400
# Dan (03/11): avisar cuando faltan 2 h, para que el equipo conteste con texto
# y no con plantilla.
WINDOW_WARNING_SECONDS = 7_200
DEFAULT_AFTER_HOURS = 2
DEFAULT_MAX_AGE_HOURS = 168
DEFAULT_SCAN_INTERVAL_SECONDS = 600.0
DEFAULT_MAX_PAGES = 10
DEFAULT_MAX_CONVERSATIONS_PER_SCAN = 60
PAUSED_REPLY_REASON = "automation_paused"
_NAMESPACE = uuid.UUID("5d0f3a52-6c1e-4f6b-9b51-0e3f8f7c2a09")
_MACHINE_REASON = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,79}$")


@dataclass(frozen=True)
class UnattendedHandoff:
    """La derivacion mas nueva sin atender de una conversacion (list_unattended_human_handoffs_v1)."""

    handoff_request_id: str
    external_conversation_id: int
    primary_reason_code: str
    detail_reason_code: str | None
    created_at: datetime

    @property
    def reason_code(self) -> str:
        detail = self.detail_reason_code
        if isinstance(detail, str) and _MACHINE_REASON.fullmatch(detail):
            return detail
        return self.primary_reason_code


@dataclass(frozen=True)
class WaitAlert:
    """Un aviso que toca, con todo lo que va a la tarjeta y nada de la persona."""

    kind: str
    conversation_id: int
    cycle_key: str
    reason_code: str
    waiting_since_epoch: int
    waited_hours: int
    deadline_epoch: int | None = None

    def event_id(self, *, account_id: int, inbox_id: int) -> str:
        return str(uuid.uuid5(_NAMESPACE, self._material(account_id, inbox_id)))

    def _material(self, account_id: int, inbox_id: int) -> str:
        return f"handoff-wait-v1:{account_id}:{inbox_id}:{self.cycle_key}"

    def command(self, *, account_id: int, inbox_id: int) -> NotificationCommand:
        material = self._material(account_id, inbox_id)
        return NotificationCommand(
            event_id=self.event_id(account_id=account_id, inbox_id=inbox_id),
            event_code=_EVENT_CODES[self.kind],
            dedupe_key=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            occurred_at=datetime.fromtimestamp(self.waiting_since_epoch, UTC),
            reason_code=self.reason_code,
            component=f"chatwoot.conversation.{self.conversation_id}",
            state="pending",
            count=self.waited_hours,
            deadline_at=(
                datetime.fromtimestamp(self.deadline_epoch, UTC)
                if self.deadline_epoch is not None
                else None
            ),
        )


@dataclass(frozen=True)
class _Message:
    message_id: int
    message_type: int
    created_at: int
    sender_type: str | None


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _conversational_messages(messages: Sequence[object]) -> list[_Message]:
    """Los mensajes publicos conversacionales, ordenados por id de Chatwoot.

    Mismo criterio que el monitor de estancadas y la reactivacion: las notas
    privadas y las actividades (``message_type`` 2) no participan, y un tipo
    publico desconocido o un id o una fecha ilegibles invalidan el historial.
    """
    parsed: list[_Message] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ChatwootProtocolError("invalid_messages_payload")
        if message.get("private") is not False:
            continue
        message_type = message.get("message_type")
        if not isinstance(message_type, int) or isinstance(message_type, bool):
            raise ChatwootProtocolError("invalid_message_type")
        if message_type == 2:
            continue
        if message_type not in (0, 1, 3):
            raise ChatwootProtocolError("invalid_message_type")
        message_id = _positive_int(message.get("id"))
        created_at = _positive_int(message.get("created_at"))
        if message_id is None:
            raise ChatwootProtocolError("invalid_message_id")
        if created_at is None:
            raise ChatwootProtocolError("invalid_message_timestamp")
        sender = message.get("sender")
        sender_type = sender.get("type") if isinstance(sender, dict) else None
        parsed.append(
            _Message(
                message_id=message_id,
                message_type=message_type,
                created_at=created_at,
                sender_type=sender_type if isinstance(sender_type, str) else None,
            )
        )
    parsed.sort(key=lambda item: item.message_id)
    return parsed


def _is_team(message: _Message) -> bool:
    # Un saliente publico de un usuario de Chatwoot: el mismo criterio con el
    # que el bridge marca attended_at (filtering.py) y con el que E-308 midio.
    # El agente sale como agent_bot y no cuenta.
    return message.message_type in (1, 3) and message.sender_type == "user"


def _is_contact(message: _Message) -> bool:
    return message.message_type == 0 and message.sender_type == "contact"


def _labels(details: Mapping[str, object]) -> tuple[str, ...]:
    labels = details.get("labels")
    if labels is None:
        return ()
    if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
        raise ChatwootProtocolError("invalid_conversation_labels")
    return tuple(labels)


def evaluate_wait_alerts(
    details: object,
    messages: Sequence[object],
    *,
    handoff: UnattendedHandoff | None,
    expected_inbox_id: int,
    now_epoch: int,
    after_seconds: int,
    max_age_seconds: int,
) -> tuple[tuple[WaitAlert, ...], str | None]:
    """Decidir que avisos tocan en una conversacion, o por que ninguno.

    Devuelve los avisos que ya vencieron (puede haber dos: la derivacion sin
    respuesta y la ventana por cerrarse) o, sin avisos, el motivo. Ningun camino
    mira el texto de un mensaje.
    """
    if (
        isinstance(now_epoch, bool)
        or not isinstance(now_epoch, int)
        or now_epoch <= 0
        or isinstance(after_seconds, bool)
        or not isinstance(after_seconds, int)
        or after_seconds <= 0
        or isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, int)
        or max_age_seconds <= 0
    ):
        raise ValueError("invalid handoff wait evaluation configuration")
    if not isinstance(details, dict):
        raise ChatwootProtocolError("invalid_conversation_payload")
    conversation_id = _positive_int(details.get("id"))
    if conversation_id is None or details.get("inbox_id") != expected_inbox_id:
        raise ChatwootProtocolError("invalid_conversation_scope")
    if handoff is not None and handoff.external_conversation_id != conversation_id:
        raise ValueError("handoff does not belong to the conversation")
    if details.get("status") != "open":
        return (), "conversation_not_open"
    labels = _labels(details)
    if "automation_opted_out" in labels:
        # E-308: las bajas «No mas mensajes» son 11 de las 14 que esperaban en
        # ATT1. La persona pidio que no le escriban: no hay a quien contestar.
        return (), "contact_opted_out"
    history = _conversational_messages(messages)

    if handoff is not None:
        handoff_epoch = int(handoff.created_at.timestamp())
        # attended_at se escribe con el webhook del mensaje del equipo; si el
        # historial ya lo muestra, la derivacion esta atendida aunque la marca
        # todavia no haya llegado.
        if any(_is_team(item) and item.created_at >= handoff_epoch for item in history):
            return (), "team_replied"
        waited_hours = max(0, (now_epoch - handoff_epoch) // 3600)
        alerts: list[WaitAlert] = []
        if now_epoch >= handoff_epoch + after_seconds:
            alerts.append(
                WaitAlert(
                    kind="unattended",
                    conversation_id=conversation_id,
                    cycle_key=f"unattended:{handoff.handoff_request_id}",
                    reason_code=handoff.reason_code,
                    waiting_since_epoch=handoff_epoch,
                    waited_hours=waited_hours,
                )
            )
        inbound = [item for item in history if _is_contact(item)]
        if inbound:
            last_inbound = max(inbound, key=lambda item: item.message_id)
            closes = last_inbound.created_at + SERVICE_WINDOW_SECONDS
            if closes - WINDOW_WARNING_SECONDS <= now_epoch < closes:
                alerts.append(
                    WaitAlert(
                        kind="window_closing",
                        conversation_id=conversation_id,
                        cycle_key=f"window:{conversation_id}:{last_inbound.message_id}",
                        reason_code=handoff.reason_code,
                        waiting_since_epoch=handoff_epoch,
                        waited_hours=waited_hours,
                        deadline_epoch=closes,
                    )
                )
        if alerts:
            return tuple(alerts), None
        return (), "handoff_not_due"

    if "automation_paused" not in labels:
        return (), "not_paused"
    if not history:
        return (), "no_conversational_history"
    last = history[-1]
    if not _is_contact(last):
        return (), "last_message_not_from_contact"
    outgoing = [item for item in history if item.message_type in (1, 3)]
    if not outgoing:
        return (), "no_outgoing_message"
    last_outgoing = outgoing[-1]
    if not _is_team(last_outgoing):
        # Pausada despues de que el agente escribio (la pausa al comprar, una
        # confirmacion de baja): no es la conversacion que el equipo tomo y
        # dejo. Queda afuera a proposito (D5 de E-309).
        return (), "last_outgoing_not_team"
    unanswered = [
        item
        for item in history
        if _is_contact(item) and item.message_id > last_outgoing.message_id
    ]
    first = unanswered[0]
    if now_epoch - first.created_at > max_age_seconds:
        return (), "too_old"
    if now_epoch < first.created_at + after_seconds:
        return (), "paused_reply_not_due"
    return (
        (
            WaitAlert(
                kind="paused_reply",
                conversation_id=conversation_id,
                cycle_key=f"paused:{conversation_id}:{first.message_id}",
                reason_code=PAUSED_REPLY_REASON,
                waiting_since_epoch=first.created_at,
                waited_hours=max(0, (now_epoch - first.created_at) // 3600),
            ),
        ),
        None,
    )


class HandoffWaitAlertSweeper:
    """Barre la bandeja y avisa por Slack a quien sigue esperando al equipo."""

    def __init__(
        self,
        *,
        chatwoot: object,
        supabase: object,
        producer: NotificationProducer,
        account_id: int,
        inbox_id: int,
        after_hours: int = DEFAULT_AFTER_HOURS,
        max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
        scan_interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_conversations_per_scan: int = DEFAULT_MAX_CONVERSATIONS_PER_SCAN,
        clock: Callable[[], float] = time.time,
    ) -> None:
        for value in (account_id, inbox_id, after_hours, max_age_hours):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("invalid handoff wait alert configuration")
        if (
            producer is None
            or not 1 <= after_hours <= 72
            or not 24 <= max_age_hours <= 720
            or isinstance(scan_interval_seconds, bool)
            or not isinstance(scan_interval_seconds, (int, float))
            or not math.isfinite(scan_interval_seconds)
            or not 60 <= scan_interval_seconds <= 3600
            or not 1 <= max_pages <= 20
            or not 1 <= max_conversations_per_scan <= 200
        ):
            raise ValueError("invalid handoff wait alert configuration")
        self._chatwoot = chatwoot
        self._supabase = supabase
        self._producer = producer
        self._account_id = account_id
        self._inbox_id = inbox_id
        self._after_seconds = after_hours * 3600
        self._after_hours = after_hours
        self._max_age_seconds = max_age_hours * 3600
        self._scan_interval_seconds = float(scan_interval_seconds)
        self._max_pages = max_pages
        self._max_conversations_per_scan = max_conversations_per_scan
        self._clock = clock
        self._admitted: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_scan_state = "never"
        self._last_scan_summary = "never"

    @property
    def last_scan_state(self) -> str:
        return self._last_scan_state

    @property
    def last_scan_summary(self) -> str:
        return self._last_scan_summary

    @property
    def after_hours(self) -> int:
        return self._after_hours

    async def run_once(self) -> dict[str, int]:
        """Un barrido. Devuelve los conteos (nunca un numero de persona)."""
        counts: Counter[str] = Counter()
        now_epoch = int(self._clock())
        since_epoch = now_epoch - self._max_age_seconds
        handoffs = await self._supabase.list_unattended_human_handoffs(  # type: ignore[attr-defined]
            chatwoot_account_id=self._account_id,
            chatwoot_inbox_id=self._inbox_id,
            since=datetime.fromtimestamp(since_epoch, UTC).isoformat(),
            limit=200,
        )
        by_conversation = {item.external_conversation_id: item for item in handoffs}
        counts["unattended_handoffs"] = len(by_conversation)
        listed = await self._chatwoot.list_recent_conversations_with_messages(  # type: ignore[attr-defined]
            expected_inbox_id=self._inbox_id,
            statuses=("open",),
            active_since_epoch=since_epoch,
            max_pages=self._max_pages,
            include_messages=False,
        )
        counts["open"] = len(listed)
        candidates: list[int] = []
        open_ids: set[int] = set()
        for entry in listed:
            summary = entry.get("conversation") if isinstance(entry, dict) else None
            if not isinstance(summary, dict):
                raise ChatwootProtocolError("invalid_conversation_payload")
            conversation_id = _positive_int(summary.get("id"))
            if conversation_id is None:
                raise ChatwootProtocolError("invalid_conversation_scope")
            open_ids.add(conversation_id)
            labels = _labels(summary)
            if "automation_opted_out" in labels:
                counts["contact_opted_out"] += 1
                continue
            if conversation_id in by_conversation or "automation_paused" in labels:
                candidates.append(conversation_id)
        not_open = set(by_conversation) - open_ids
        if not_open:
            counts["handoff_not_open"] = len(not_open)
        if len(candidates) > self._max_conversations_per_scan:
            counts["over_scan_cap"] = len(candidates) - self._max_conversations_per_scan
            # Primero las derivaciones (lo que mas urge), despues las pausadas.
            candidates.sort(key=lambda item: (item not in by_conversation, item))
            candidates = candidates[: self._max_conversations_per_scan]
        failed = False
        for conversation_id in candidates:
            if self._stopping.is_set():
                break
            entry = await self._chatwoot.get_conversation_with_messages(  # type: ignore[attr-defined]
                conversation_id=conversation_id,
                expected_inbox_id=self._inbox_id,
            )
            details = entry.get("conversation") if isinstance(entry, dict) else None
            messages = entry.get("messages") if isinstance(entry, dict) else None
            if not isinstance(messages, list):
                raise ChatwootProtocolError("invalid_messages_payload")
            alerts, skip = evaluate_wait_alerts(
                details,
                messages,
                handoff=by_conversation.get(conversation_id),
                expected_inbox_id=self._inbox_id,
                now_epoch=now_epoch,
                after_seconds=self._after_seconds,
                max_age_seconds=self._max_age_seconds,
            )
            if not alerts:
                counts[skip or "nothing_due"] += 1
                continue
            for alert in alerts:
                if not await self._admit(alert, counts):
                    failed = True
        self._last_scan_state = "error" if failed else "healthy"
        self._last_scan_summary = (
            " ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "nothing"
        )
        logger.info("handoff_wait_scan %s", self._last_scan_summary)
        return dict(counts)

    async def _admit(self, alert: WaitAlert, counts: Counter[str]) -> bool:
        """Admitir un aviso. Devuelve False solo si hay que reintentarlo."""
        command = alert.command(account_id=self._account_id, inbox_id=self._inbox_id)
        if command.event_id in self._admitted:
            counts["already_admitted"] += 1
            return True
        try:
            receipt = await self._producer.admit(command)
        except ConnectorSemanticConflict:
            # El mismo ciclo ya se admitio con otro numero (las horas de espera
            # de otra corrida): el conector no lo vuelve a publicar.
            self._admitted.add(command.event_id)
            counts["already_admitted"] += 1
            return True
        except ConnectorRejected:
            # Rechazo terminal del conector: reintentarlo cada 10 min no lo
            # arregla. Queda en el log y en el resumen.
            self._admitted.add(command.event_id)
            counts["rejected"] += 1
            logger.warning(
                "handoff_wait_alert_rejected kind=%s conversation=%s",
                alert.kind,
                alert.conversation_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - el proximo barrido reintenta
            counts["admit_failed"] += 1
            logger.warning(
                "handoff_wait_alert_admit_failed kind=%s conversation=%s error_type=%s",
                alert.kind,
                alert.conversation_id,
                type(exc).__name__,
            )
            return False
        self._admitted.add(command.event_id)
        if getattr(receipt, "status", None) == "duplicate":
            counts["already_admitted"] += 1
            return True
        counts[f"admitted_{alert.kind}"] += 1
        logger.info(
            "handoff_wait_alert_admitted kind=%s conversation=%s reason=%s waited_hours=%s",
            alert.kind,
            alert.conversation_id,
            alert.reason_code,
            alert.waited_hours,
        )
        return True

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="handoff-wait-alerts")

    async def stop(self) -> None:
        if self._task is None:
            self._last_scan_state = "stopped"
            return
        self._stopping.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._last_scan_state = "stopped"

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                self._last_scan_state = "error"
                logger.warning(
                    "handoff_wait_scan_failed error_type=%s", type(exc).__name__
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._scan_interval_seconds
                )
            except TimeoutError:
                pass


__all__: Sequence[str] = (
    "ALERT_KINDS",
    "HandoffWaitAlertSweeper",
    "UnattendedHandoff",
    "WaitAlert",
    "evaluate_wait_alerts",
)
