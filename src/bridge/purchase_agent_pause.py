"""E-100: al comprar, el agente deja de atender y la persona pasa al equipo.

Es la replica de «Ventas maribel», el workflow de GHL que se dispara con la
compra: le quita el usuario asignado, le quita la etiqueta del carrito y apaga el
bot (relevamiento de E-093). En el producto, la compra aprobada ya cierra los
casos de recuperacion y corta los toques (``apply_hotmart_purchase_approved``);
lo que faltaba es que el agente no le siga contestando como a un prospecto.

Con ``PURCHASE_AGENT_PAUSE_ENABLED`` (apagado por defecto), despues de aplicar
una compra aprobada de un producto de la instancia:

1. la base busca las conversaciones de esa persona con la instancia
   (``read_purchase_agent_pause_conversations_v1``, por los ultimos 10 digitos
   del WhatsApp o por el mail; si son de dos numeros distintos, ninguna);
2. en cada una, el bridge pone la etiqueta ``automation_paused`` (el macro de
   pausa: bloquea todo envio del agente), la asigna al team de derivacion del
   manifiesto si nadie la tiene, y deja una nota privada corta, una sola vez por
   compra (el marcador lleva el id del evento de la compra).

La conversacion vuelve al agente como hoy: cuando una persona del equipo saca la
etiqueta a mano. La reanudacion automatica por silencio del equipo
(``CONVERSATION_RESUME_ENABLED``) no la levanta: la nota de la compra la frena
(``purchase_pause_note_present``).

Nunca frena la compra: todo error va al log y la compra queda aplicada. Los logs
no llevan telefono, mail ni nombre.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from bridge.chatwoot import ChatwootAssignmentConflictError, ChatwootProtocolError
from bridge.hotmart import HotmartPurchaseData

logger = logging.getLogger(__name__)

PURCHASE_PAUSE_NOTE_MARKER_PREFIX = "[supportmagician-compra-pausa:"
# E-190 (E-196, I-1): la nota que queda cuando la pausa despues de la bienvenida no se pudo poner
# ni con los reintentos. Otro prefijo a proposito: no es una pausa puesta, y la reanudacion no la
# toma por una.
PURCHASE_PAUSE_PENDING_NOTE_MARKER_PREFIX = "[supportmagician-compra-pausa-pendiente:"
PURCHASE_PAUSE_PENDING_NOTE_BODY = (
    "Compró, y no pude pausar al agente en esta conversación. Para que la atienda el "
    "equipo, poné la etiqueta automation_paused."
)
# Los reintentos cortos de la pausa despues de la bienvenida: entre intento e intento.
PURCHASE_PAUSE_RETRY_DELAYS_SECONDS = (2.0, 5.0)
_EVENT_ID_CHARS = frozenset("0123456789abcdefABCDEF-")


def purchase_pause_note_marker(webhook_event_id: str) -> str:
    """El marcador de idempotencia: uno por compra."""
    if (
        not isinstance(webhook_event_id, str)
        or not 8 <= len(webhook_event_id) <= 64
        or not set(webhook_event_id) <= _EVENT_ID_CHARS
    ):
        raise ValueError("webhook_event_id must be a uuid")
    return f"{PURCHASE_PAUSE_NOTE_MARKER_PREFIX}{webhook_event_id.lower()}]"


def purchase_pause_note_body(approved_at: datetime, *, time_zone: str) -> str:
    """El texto de la nota: la fecha de la compra, en la zona de la instancia."""
    if approved_at.tzinfo is None:
        raise ValueError("approved_at must be aware")
    day = approved_at.astimezone(ZoneInfo(time_zone)).strftime("%d/%m/%Y")
    return (
        f"Compró el {day}: la atiende el equipo. "
        "El agente quedó pausado en esta conversación; para que vuelva a "
        "contestar, sacá la etiqueta automation_paused."
    )


def purchase_pause_note_present(messages: Iterable[Mapping[str, object]]) -> bool:
    """Si la conversacion tiene la nota privada de una compra (E-100)."""
    for message in messages:
        content = message.get("content")
        if (
            message.get("private") is True
            and isinstance(content, str)
            and PURCHASE_PAUSE_NOTE_MARKER_PREFIX in content
        ):
            return True
    return False


class _Lookup(Protocol):
    outcome: str
    conversation_ids: tuple[int, ...]


class _Supabase(Protocol):
    async def read_purchase_agent_pause_conversations(
        self,
        *,
        chatwoot_account_id: int,
        chatwoot_inbox_id: int,
        inbound_scope_key: str,
        buyer_email: str | None,
        buyer_phone: str | None,
    ) -> _Lookup: ...


class _Chatwoot(Protocol):
    async def pause_purchased_conversation(
        self, *, conversation_id: int, expected_inbox_id: int, team_id: int | None
    ) -> str: ...

    async def ensure_marked_private_note(
        self,
        *,
        conversation_id: int,
        expected_inbox_id: int,
        note_body: str,
        idempotency_marker: str,
    ) -> bool: ...


@dataclass(frozen=True)
class PurchaseAgentPause:
    """Lo que hace falta para pausar al agente en la conversacion del comprador."""

    supabase: _Supabase
    chatwoot: _Chatwoot
    chatwoot_account_id: int
    chatwoot_inbox_id: int
    inbound_scope_key: str
    team_id: int | None
    time_zone: str
    # E-190 (E-196, I-1): solo los usa la pausa despues de la bienvenida.
    retry_delays_seconds: tuple[float, ...] = field(
        default=PURCHASE_PAUSE_RETRY_DELAYS_SECONDS, compare=False
    )
    sleep: Callable[[float], Awaitable[None]] = field(
        default=asyncio.sleep, compare=False, repr=False
    )

    async def pause_buyer(
        self, *, webhook_event_id: str, purchase: HotmartPurchaseData
    ) -> str:
        """Pausa las conversaciones del comprador. Devuelve el resultado, para el log.

        ``paused`` (al menos una), ``not_found``, ``ambiguous``, ``no_contact``
        (la compra no trae mail ni telefono) o ``failed``. Nunca levanta.
        """
        if purchase.buyer_email is None and purchase.buyer_phone is None:
            logger.info(
                "purchase_agent_pause_skipped event_id=%s reason=no_contact",
                webhook_event_id,
            )
            return "no_contact"
        try:
            marker = purchase_pause_note_marker(webhook_event_id)
            body = purchase_pause_note_body(
                datetime.fromtimestamp(purchase.approved_date_ms / 1000, tz=UTC),
                time_zone=self.time_zone,
            )
            lookup = await self.supabase.read_purchase_agent_pause_conversations(
                chatwoot_account_id=self.chatwoot_account_id,
                chatwoot_inbox_id=self.chatwoot_inbox_id,
                inbound_scope_key=self.inbound_scope_key,
                buyer_email=purchase.buyer_email,
                buyer_phone=purchase.buyer_phone,
            )
        except Exception as exc:  # noqa: BLE001 - la compra ya quedo aplicada
            logger.warning(
                "purchase_agent_pause_failed event_id=%s step=read error_type=%s",
                webhook_event_id,
                type(exc).__name__,
            )
            return "failed"
        if lookup.outcome != "found" or not lookup.conversation_ids:
            logger.info(
                "purchase_agent_pause_skipped event_id=%s reason=%s",
                webhook_event_id,
                lookup.outcome,
            )
            return lookup.outcome
        paused = 0
        for conversation_id in lookup.conversation_ids:
            try:
                assignment = await self.chatwoot.pause_purchased_conversation(
                    conversation_id=conversation_id,
                    expected_inbox_id=self.chatwoot_inbox_id,
                    team_id=self.team_id,
                )
            except Exception as exc:  # noqa: BLE001 - las demas siguen
                logger.warning(
                    "purchase_agent_pause_failed event_id=%s conversation=%s "
                    "step=pause error_type=%s",
                    webhook_event_id,
                    conversation_id,
                    type(exc).__name__,
                )
                continue
            paused += 1
            try:
                await self.chatwoot.ensure_marked_private_note(
                    conversation_id=conversation_id,
                    expected_inbox_id=self.chatwoot_inbox_id,
                    note_body=body,
                    idempotency_marker=marker,
                )
            except Exception as exc:  # noqa: BLE001 - la pausa ya quedo puesta
                logger.warning(
                    "purchase_agent_pause_note_not_sent event_id=%s conversation=%s "
                    "error_type=%s",
                    webhook_event_id,
                    conversation_id,
                    type(exc).__name__,
                )
            logger.info(
                "purchase_agent_paused event_id=%s conversation=%s assignment=%s",
                webhook_event_id,
                conversation_id,
                assignment,
            )
        return "paused" if paused else "failed"

    async def pause_after_native_welcome(
        self,
        *,
        action_id: str,
        purchased_at: str | None,
        conversation_id: int | None = None,
        buyer_email: str | None = None,
        buyer_phone: str | None = None,
    ) -> str:
        """E-190: la pausa al comprar, al cerrar el paso de un flujo nativo de la compra.

        Con ``PURCHASE_AGENT_PAUSE_AFTER_NATIVE_WELCOME_ENABLED``, el despachador
        la llama cuando el paso de un flujo nativo con ``permiso: compra`` (la
        bienvenida) se cierra para siempre. Desde E-165 el worker no pausa al
        comprador que tomo el flujo, porque una conversacion pausada frena el
        envio; sin esto, con el flujo prendido para todos, ningun comprador se
        pausaba.

        - Con ``conversation_id`` (la bienvenida salio a esa conversacion) se
          pausa esa, sin leer la base (D2).
        - Sin ella (el paso cerro sin envio confirmado, E-196 I-2) se buscan las
          del comprador como al comprar (``read_purchase_agent_pause_conversations_v1``).

        Cada conversacion lleva la etiqueta, el team y la nota de la pausa al
        comprar; el marcador lleva el id de la accion y el prefijo de siempre, asi
        que la reanudacion por silencio la reconoce. Un error de red, un 5xx o una
        asignacion sin confirmar se reintentan con ``retry_delays_seconds``; si
        sigue fallando, queda una nota privada que le pide al equipo pausarla a
        mano (``PURCHASE_PAUSE_PENDING_NOTE_MARKER_PREFIX``). Otro team ya asignado
        deja la etiqueta puesta: se escribe la nota igual (E-196, M-1).

        Devuelve ``paused`` (todas), ``partial``, ``failed``, ``not_found``,
        ``ambiguous`` o ``no_contact``. Nunca levanta.
        """
        try:
            marker = purchase_pause_note_marker(action_id)
            pending_marker = (
                f"{PURCHASE_PAUSE_PENDING_NOTE_MARKER_PREFIX}{action_id.lower()}]"
            )
            approved_at = _aware_or_now(purchased_at)
            body = purchase_pause_note_body(approved_at, time_zone=self.time_zone)
        except Exception as exc:  # noqa: BLE001 - el paso ya cerro
            logger.warning(
                "purchase_agent_pause_after_welcome_failed action_id=%s step=prepare "
                "error_type=%s",
                action_id,
                type(exc).__name__,
            )
            return "failed"
        if conversation_id is not None:
            conversation_ids: tuple[int, ...] = (conversation_id,)
        else:
            if buyer_email is None and buyer_phone is None:
                logger.info(
                    "purchase_agent_pause_after_welcome_skipped action_id=%s reason=no_contact",
                    action_id,
                )
                return "no_contact"
            try:
                lookup = await self.supabase.read_purchase_agent_pause_conversations(
                    chatwoot_account_id=self.chatwoot_account_id,
                    chatwoot_inbox_id=self.chatwoot_inbox_id,
                    inbound_scope_key=self.inbound_scope_key,
                    buyer_email=buyer_email,
                    buyer_phone=buyer_phone,
                )
            except Exception as exc:  # noqa: BLE001 - el paso ya cerro
                logger.warning(
                    "purchase_agent_pause_after_welcome_failed action_id=%s step=read "
                    "error_type=%s",
                    action_id,
                    type(exc).__name__,
                )
                return "failed"
            if lookup.outcome != "found" or not lookup.conversation_ids:
                logger.info(
                    "purchase_agent_pause_after_welcome_skipped action_id=%s reason=%s",
                    action_id,
                    lookup.outcome,
                )
                return lookup.outcome
            conversation_ids = tuple(lookup.conversation_ids)
        paused = 0
        for target in conversation_ids:
            assignment = await self._pause_with_retries(action_id, target)
            if assignment is None:
                await self._leave_pending_note(action_id, target, pending_marker)
                continue
            paused += 1
            try:
                await self.chatwoot.ensure_marked_private_note(
                    conversation_id=target,
                    expected_inbox_id=self.chatwoot_inbox_id,
                    note_body=body,
                    idempotency_marker=marker,
                )
            except Exception as exc:  # noqa: BLE001 - la pausa ya quedo puesta
                logger.warning(
                    "purchase_agent_pause_note_not_sent action_id=%s conversation=%s "
                    "error_type=%s",
                    action_id,
                    target,
                    type(exc).__name__,
                )
            logger.info(
                "purchase_agent_paused_after_welcome action_id=%s conversation=%s "
                "assignment=%s",
                action_id,
                target,
                assignment,
            )
        if paused == len(conversation_ids):
            return "paused"
        return "partial" if paused else "failed"

    async def _pause_with_retries(self, action_id: str, conversation_id: int) -> str | None:
        """The assignment outcome, or None when the pause could not be put."""
        delays = (*self.retry_delays_seconds, None)
        for attempt, delay in enumerate(delays, start=1):
            try:
                return await self.chatwoot.pause_purchased_conversation(
                    conversation_id=conversation_id,
                    expected_inbox_id=self.chatwoot_inbox_id,
                    team_id=self.team_id,
                )
            except ChatwootAssignmentConflictError:
                # La etiqueta ya esta: otro team tiene la conversacion. La pausa vale.
                return "other_team"
            except Exception as exc:  # noqa: BLE001 - nunca frena el despachador
                retryable = _pause_error_is_transient(exc)
                logger.warning(
                    "purchase_agent_pause_after_welcome_failed action_id=%s "
                    "conversation=%s step=pause attempt=%s retryable=%s error_type=%s",
                    action_id,
                    conversation_id,
                    attempt,
                    retryable,
                    type(exc).__name__,
                )
                if not retryable or delay is None:
                    return None
                await self.sleep(delay)
        return None

    async def _leave_pending_note(
        self, action_id: str, conversation_id: int, marker: str
    ) -> None:
        """The durable trace of a pause that failed: a private note for the team."""
        try:
            await self.chatwoot.ensure_marked_private_note(
                conversation_id=conversation_id,
                expected_inbox_id=self.chatwoot_inbox_id,
                note_body=PURCHASE_PAUSE_PENDING_NOTE_BODY,
                idempotency_marker=marker,
            )
            noted = True
        except Exception as exc:  # noqa: BLE001 - queda el log
            noted = False
            logger.warning(
                "purchase_agent_pause_pending_note_not_sent action_id=%s conversation=%s "
                "error_type=%s",
                action_id,
                conversation_id,
                type(exc).__name__,
            )
        logger.warning(
            "purchase_agent_pause_after_welcome_pending action_id=%s conversation=%s "
            "note=%s",
            action_id,
            conversation_id,
            noted,
        )


def _aware_or_now(value: str | None) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed
    return datetime.now(UTC)


def _pause_error_is_transient(exc: Exception) -> bool:
    """A network error, a 5xx or an assignment Chatwoot did not confirm yet."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, ChatwootProtocolError) and (
        str(exc) == "purchase_pause_assignment_not_confirmed"
    )
