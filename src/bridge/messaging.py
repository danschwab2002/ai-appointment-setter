"""Messaging abstraction layer for recovery first-touch messages.

Abstracts the channel (Evolution, WABA) behind a single interface so the
recovery workflow doesn't change when migrating from Evolution to the
official Meta WhatsApp Business API. See ADR-0004.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
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
    payment_failure_name: str | None = None

    def params(
        self,
        *,
        content: str,
        followup: bool,
        buyer_name: str | None = None,
        product_name: str | None = None,
        trigger_kind: str | None = None,
    ) -> dict[str, object]:
        name = self.followup_name if followup else self.first_touch_name
        if (
            not followup
            and trigger_kind == "payment_failure"
            and self.payment_failure_name is not None
        ):
            name = self.payment_failure_name
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


@dataclass(frozen=True)
class FinalMetaEffect:
    """Complete final Meta effect constructed before the provider boundary."""

    delivery_id: str
    action_kind: str
    mode: str
    target_phone: str
    content: str
    template_name: str
    template_language: str

    def validate(self) -> None:
        if (
            not self.delivery_id.strip()
            or self.action_kind not in {"first_touch", "followup"}
            or self.mode != "approved_template"
            or re.fullmatch(r"\+[1-9]\d{6,14}", self.target_phone) is None
            or not self.content.strip()
            or not self.template_name.strip()
            or not self.template_language.strip()
        ):
            raise ValueError("invalid_final_meta_effect")

    def sanitized_evidence(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": 1,
            "status": "final_meta_gate_closed",
            "action_kind": self.action_kind,
            "mode": self.mode,
            "delivery_id_sha256": hashlib.sha256(
                self.delivery_id.encode("utf-8")
            ).hexdigest(),
            "target_sha256": hashlib.sha256(
                self.target_phone.encode("utf-8")
            ).hexdigest(),
            "content_sha256": hashlib.sha256(
                self.content.encode("utf-8")
            ).hexdigest(),
            "template_name": self.template_name,
            "template_language": self.template_language,
        }


class FinalMetaEffectGate:
    """Allow the final Meta call or durably record a sanitized blocked effect."""

    def __init__(self, *, enabled: bool, evidence_dir: Path) -> None:
        if type(enabled) is not bool:
            raise ValueError("invalid_final_meta_gate")
        self._enabled = enabled
        self._evidence_dir = evidence_dir

    def authorize(self, effect: FinalMetaEffect) -> bool:
        evidence = effect.sanitized_evidence()
        if self._enabled:
            return True

        directory = self._evidence_dir
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        digest = evidence["delivery_id_sha256"]
        assert isinstance(digest, str)
        path = directory / f"{digest}.json"
        temporary = directory / f".{digest}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        encoded = (json.dumps(evidence, sort_keys=True) + "\n").encode("utf-8")
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                try:
                    existing = path.read_bytes()
                except OSError as exc:
                    raise ValueError("final_meta_effect_conflict") from exc
                if existing != encoded:
                    raise ValueError("final_meta_effect_conflict")
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return False


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
        require_existing_contact: bool = False,
        trigger_kind: str | None = None,
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
        allowed_jid: str | None,
        dynamic_recipient_enabled: bool = False,
        template: WhatsAppTemplateConfig | None = None,
    ) -> None:
        if (allowed_jid is None and not dynamic_recipient_enabled) or (
            allowed_jid is not None and dynamic_recipient_enabled
        ):
            raise ValueError("exactly one recipient authority is required")
        self._chatwoot = chatwoot
        self._inbox_id = inbox_id
        self._allowed_jid = allowed_jid
        self._dynamic_recipient_enabled = dynamic_recipient_enabled
        self._template = template

    def _is_target_allowed(self, phone: str | None) -> bool:
        if self._dynamic_recipient_enabled:
            return (
                isinstance(phone, str)
                and _PHONE_INPUT_RE.fullmatch(phone) is not None
                and normalize_phone(phone) is not None
            )
        return is_allowed_whatsapp_target(phone, self._allowed_jid)

    async def send_first_touch(
        self,
        *,
        phone: str,
        buyer_name: str | None,
        buyer_email: str | None,
        product_name: str | None = None,
        content: str,
        delivery_id: str,
        require_existing_contact: bool = False,
        trigger_kind: str | None = None,
    ) -> FirstTouchResult:
        normalized = normalize_phone(phone)
        if normalized is None:
            return FirstTouchResult(
                status="blocked",
                conversation_id=None,
                message_id=None,
                reason="invalid_phone",
            )
        if not self._is_target_allowed(phone):
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
            contact_binding = await self._chatwoot.find_contact_inbox_by_phone(
                inbox_id=self._inbox_id,
                phone_number=e164,
            )
            if contact_binding is None:
                if require_existing_contact:
                    raise ChatwootProtocolError("existing_contact_required")
                created_contact_id = await self._chatwoot.create_contact(
                    inbox_id=self._inbox_id,
                    name=buyer_name,
                    phone_number=e164,
                    email=buyer_email,
                )
                contact_binding = await self._chatwoot.find_contact_inbox_by_phone(
                    inbox_id=self._inbox_id,
                    phone_number=e164,
                )
                if (
                    contact_binding is None
                    or contact_binding[0] != created_contact_id
                ):
                    raise ChatwootProtocolError("contact_inbox_binding_missing")
            contact_id, source_id = contact_binding
            conversation_id = await self._chatwoot.create_conversation(
                inbox_id=self._inbox_id,
                contact_id=contact_id,
                source_id=source_id,
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
                        trigger_kind=trigger_kind,
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
        if not self._is_target_allowed(phone):
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
        if not self._is_target_allowed(phone):
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
