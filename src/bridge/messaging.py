"""Messaging abstraction layer for recovery first-touch messages.

Abstracts the channel (Evolution, WABA) behind a single interface so the
recovery workflow doesn't change when migrating from Evolution to the
official Meta WhatsApp Business API. See ADR-0004.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from bridge.chatwoot import ChatwootClient, ChatwootProtocolError
from bridge.hotmart import normalize_phone

import httpx


@dataclass(frozen=True)
class FirstTouchResult:
    """Result of sending a first-touch recovery message."""

    status: str  # "sent" | "blocked" | "failed"
    conversation_id: int | None
    message_id: int | None
    reason: str | None = None


class MessageSender(Protocol):
    """Interface for sending recovery first-touch messages."""

    async def send_first_touch(
        self,
        *,
        phone: str,
        buyer_name: str | None,
        buyer_email: str | None,
        content: str,
        delivery_id: str,
    ) -> FirstTouchResult: ...


def _to_e164(digits: str) -> str:
    """Convert bare digits (from Hotmart) to E.164 format for Chatwoot.

    Hotmart sends phone as DDI + number without '+': '5531999999999'.
    Chatwoot expects '+5531999999999'.
    """
    return f"+{digits}" if not digits.startswith("+") else digits


class EvolutionMessageSender:
    """Send first-touch messages via Chatwoot + Evolution API.

    Creates a contact, creates a conversation, and sends the first message
    via AgentBot — all through the Chatwoot REST API.
    """

    def __init__(
        self,
        *,
        chatwoot: ChatwootClient,
        inbox_id: int,
    ) -> None:
        self._chatwoot = chatwoot
        self._inbox_id = inbox_id

    async def send_first_touch(
        self,
        *,
        phone: str,
        buyer_name: str | None,
        buyer_email: str | None,
        content: str,
        delivery_id: str,
    ) -> FirstTouchResult:
        normalized = normalize_phone(phone)
        if normalized is None:
            return FirstTouchResult(
                status="blocked",
                conversation_id=None,
                message_id=None,
                reason="invalid_phone",
            )

        e164 = _to_e164(normalized)

        try:
            contact_id = await self._chatwoot.find_contact_by_phone(
                inbox_id=self._inbox_id,
                phone_number=e164,
            )
            if contact_id is None:
                contact_id = await self._chatwoot.create_contact(
                    inbox_id=self._inbox_id,
                    name=buyer_name,
                    phone_number=e164,
                    email=buyer_email,
                )
            conversation_id = await self._chatwoot.create_conversation(
                inbox_id=self._inbox_id,
                contact_id=contact_id,
            )
            result = await self._chatwoot.send_first_message(
                conversation_id=conversation_id,
                content=content,
                delivery_id=delivery_id,
            )
        except ChatwootProtocolError as exc:
            return FirstTouchResult(
                status="failed",
                conversation_id=None,
                message_id=None,
                reason=str(exc),
            )
        except httpx.HTTPError:
            return FirstTouchResult(
                status="failed",
                conversation_id=None,
                message_id=None,
                reason="chatwoot_http_error",
            )

        message_id_raw = result.get("message_id")
        message_id = (
            message_id_raw
            if isinstance(message_id_raw, int)
            and not isinstance(message_id_raw, bool)
            else None
        )
        return FirstTouchResult(
            status="sent",
            conversation_id=conversation_id,
            message_id=message_id,
        )
