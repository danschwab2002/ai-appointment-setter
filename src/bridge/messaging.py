"""Messaging abstraction layer for recovery first-touch messages.

Abstracts the channel (Evolution, WABA) behind a single interface so the
recovery workflow doesn't change when migrating from Evolution to the
official Meta WhatsApp Business API. See ADR-0004.
"""

from __future__ import annotations

import re
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


@dataclass(frozen=True)
class WhatsAppTemplateConfig:
    """Approved Chatwoot WABA templates and their body placeholders."""

    first_touch_name: str
    followup_name: str | None
    language: str
    category: str
    first_touch_parameter: str = "content"

    def params(
        self,
        *,
        content: str,
        followup: bool,
        buyer_name: str | None = None,
        product_name: str | None = None,
    ) -> dict[str, object]:
        name = self.followup_name if followup else self.first_touch_name
        if name is None:
            raise ValueError("template_disabled")
        body = {"1": content}
        if not followup and self.first_touch_parameter == "buyer_name":
            body = {"1": buyer_name or ""}
        elif (
            not followup
            and self.first_touch_parameter == "buyer_name_and_product"
        ):
            body = {"1": buyer_name or "", "2": product_name or ""}
        return {
            "name": name,
            "category": self.category,
            "language": self.language,
            "processed_params": {"body": body},
        }


class MessageSender(Protocol):
    """Interface for sending recovery first-touch messages."""

    async def send_first_touch(
        self,
        *,
        phone: str,
        buyer_name: str | None,
        buyer_email: str | None,
        product_name: str | None,
        content: str,
        delivery_id: str,
    ) -> FirstTouchResult: ...

    async def send_first_touch_to_conversation(
        self,
        *,
        conversation_id: int,
        phone: str,
        buyer_name: str,
        content: str,
        delivery_id: str,
    ) -> FirstTouchResult: ...

    async def send_followup(
        self,
        *,
        conversation_id: int,
        phone: str,
        content: str,
        delivery_id: str,
    ) -> FirstTouchResult: ...


_INDIVIDUAL_JID_RE = re.compile(r"([1-9]\d{6,14})@s\.whatsapp\.net")
_PHONE_INPUT_RE = re.compile(r"\+?[0-9 ()-]+")


def allowed_phone_from_jid(allowed_jid: str | None) -> str | None:
    """Extract digits only from one canonical individual WhatsApp JID."""
    if not isinstance(allowed_jid, str):
        return None
    match = _INDIVIDUAL_JID_RE.fullmatch(allowed_jid)
    return match.group(1) if match is not None else None


def is_allowed_whatsapp_target(
    phone: str | None,
    allowed_jid: str | None,
) -> bool:
    """Validate and compare one outbound phone against the configured JID."""
    if not isinstance(phone, str) or _PHONE_INPUT_RE.fullmatch(phone) is None:
        return False
    normalized = normalize_phone(phone)
    allowed_phone = allowed_phone_from_jid(allowed_jid)
    return normalized is not None and normalized == allowed_phone


def _to_e164(digits: str) -> str:
    """Convert bare digits (from Hotmart) to E.164 format for Chatwoot.

    Hotmart sends phone as DDI + number without '+': '5531999999999'.
    Chatwoot expects '+5531999999999'.
    """
    return f"+{digits}" if not digits.startswith("+") else digits


class ChatwootMessageSender:
    """Send WhatsApp messages through the configured Chatwoot inbox.

    Creates a contact, creates a conversation, and sends the first message
    via AgentBot — all through the Chatwoot REST API. The inbox may be backed
    by Evolution or by the official WABA integration; that durable provider is
    bound separately by the pilot scope.
    """

    def __init__(
        self,
        *,
        chatwoot: ChatwootClient,
        inbox_id: int,
        allowed_jid: str,
        template: WhatsAppTemplateConfig | None = None,
    ) -> None:
        self._chatwoot = chatwoot
        self._inbox_id = inbox_id
        self._allowed_jid = allowed_jid
        self._template = template

    async def send_first_touch(
        self,
        *,
        phone: str,
        buyer_name: str | None,
        buyer_email: str | None,
        product_name: str | None = None,
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
        if not is_allowed_whatsapp_target(phone, self._allowed_jid):
            return FirstTouchResult(
                status="blocked",
                conversation_id=None,
                message_id=None,
                reason="target_not_allowed",
            )
        if (
            self._template is not None
            and self._template.first_touch_parameter == "buyer_name_and_product"
            and (
                not isinstance(buyer_name, str)
                or not buyer_name.strip()
                or not isinstance(product_name, str)
                or not product_name.strip()
            )
        ):
            return FirstTouchResult(
                status="blocked",
                conversation_id=None,
                message_id=None,
                reason="template_parameters_missing",
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
                template_params=(
                    self._template.params(
                        content=content,
                        followup=False,
                        buyer_name=buyer_name,
                        product_name=product_name,
                    )
                    if self._template is not None
                    else None
                ),
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

    async def send_first_touch_to_conversation(
        self,
        *,
        conversation_id: int,
        phone: str,
        buyer_name: str,
        content: str,
        delivery_id: str,
    ) -> FirstTouchResult:
        """Send a first-touch template in an existing canonical conversation."""
        if not is_allowed_whatsapp_target(phone, self._allowed_jid):
            return FirstTouchResult("blocked", None, None, "target_not_allowed")
        if (
            not isinstance(conversation_id, int)
            or isinstance(conversation_id, bool)
            or conversation_id < 1
            or not buyer_name.strip()
            or self._template is None
        ):
            return FirstTouchResult(
                "blocked", conversation_id, None, "invalid_first_touch"
            )
        try:
            result = await self._chatwoot.send_followup_message(
                conversation_id=conversation_id,
                content=content,
                delivery_id=delivery_id,
                template_params=self._template.params(
                    content=content,
                    followup=False,
                    buyer_name=buyer_name,
                ),
            )
        except ChatwootProtocolError:
            return FirstTouchResult(
                "failed", conversation_id, None, "chatwoot_protocol_error"
            )
        except httpx.HTTPError:
            return FirstTouchResult(
                "failed", conversation_id, None, "chatwoot_http_error"
            )
        message_id = result.get("message_id")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            message_id = None
        return FirstTouchResult("sent", conversation_id, message_id)

    async def send_followup(
        self,
        *,
        conversation_id: int,
        phone: str,
        content: str,
        delivery_id: str,
    ) -> FirstTouchResult:
        """Send a follow-up without creating another Chatwoot conversation."""
        if not is_allowed_whatsapp_target(phone, self._allowed_jid):
            return FirstTouchResult(
                status="blocked",
                conversation_id=None,
                message_id=None,
                reason="target_not_allowed",
            )
        if (
            not isinstance(conversation_id, int)
            or isinstance(conversation_id, bool)
            or conversation_id <= 0
        ):
            return FirstTouchResult(
                status="blocked",
                conversation_id=None,
                message_id=None,
                reason="invalid_conversation_id",
            )
        if self._template is not None and self._template.followup_name is None:
            return FirstTouchResult(
                status="blocked",
                conversation_id=conversation_id,
                message_id=None,
                reason="followup_template_disabled",
            )

        try:
            result = await self._chatwoot.send_followup_message(
                conversation_id=conversation_id,
                content=content,
                delivery_id=delivery_id,
                template_params=(
                    self._template.params(content=content, followup=True)
                    if self._template is not None
                    else None
                ),
            )
        except ChatwootProtocolError as exc:
            return FirstTouchResult(
                status="failed",
                conversation_id=conversation_id,
                message_id=None,
                reason=str(exc),
            )
        except httpx.HTTPError:
            return FirstTouchResult(
                status="failed",
                conversation_id=conversation_id,
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
