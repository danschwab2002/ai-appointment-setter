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

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from bridge.hotmart import HotmartPurchaseData

logger = logging.getLogger(__name__)

PURCHASE_PAUSE_NOTE_MARKER_PREFIX = "[supportmagician-compra-pausa:"
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
