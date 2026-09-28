"""Collect, minimize, and render private daily conversation review packages."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping, Sequence

import httpx

from .daily_feedback import (
    CreateBatchResult,
    DailyFeedbackBatchStore,
    MinimizedReviewConversation,
    RealConversationBatchGrant,
    ReviewTranscriptMessage,
    minimize_review_text_v2,
    sanitize_review_text,
)


# v1: paquete minimizado (sin identidad, solo prospecto y bot). Sigue vivo para
# la superficie local de cuarentena y sus tests.
PACKAGE_SCHEMA_V1 = "daily-feedback-review-package-v1"
SANITIZER_VERSION_V1 = "deterministic-redaction-v1"
SELECTION_VERSION_V1 = "chatwoot-daily-agent-dialogues-v1"
# v2: paquete identificado (ADR-0018): nombre y telefono del lead, link a
# Chatwoot, mensajes del equipo, notas, plantillas, eventos y decision del agente.
PACKAGE_SCHEMA_V2 = "daily-feedback-review-package-v2"
SANITIZER_VERSION_V2 = "identity-preserving-redaction-v2"
SELECTION_VERSION_V2 = "chatwoot-daily-conversation-context-v2"

_MESSAGE_KIND_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MESSAGE_STATUS_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_PAYMENT_LINK_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*hotmart\.com/\S+", re.IGNORECASE
)
_LABEL_ACTIVITY_RE = re.compile(r"^(?P<who>.+?) (?P<verb>added|removed) (?P<label>\S+)$")
_ASSIGNED_ACTIVITY_RE = re.compile(r"^Assigned to (?P<assignee>.+?) by (?P<who>.+)$")
_UNASSIGNED_ACTIVITY_RE = re.compile(r"^Unassigned by (?P<who>.+)$")
_STATUS_ACTIVITY_RE = re.compile(
    r"^Conversation was (?:marked )?(?P<state>resolved|reopened|open|pending|snoozed)"
    r"(?: by (?P<who>.+))?$"
)
_EMPTY_MESSAGE_TEXT = "[sin texto: adjunto, audio o sticker]"
_MAX_SUMMARY_TEXT = 300


class ConversationCollectionError(RuntimeError):
    """The source could not prove a complete, correctly scoped daily collection."""


@dataclass(frozen=True)
class RealConversationSecurityPolicy:
    real_collection_enabled: bool
    storage_encryption_verified: bool
    retention_hours: int
    deletion_owner: str


@dataclass(frozen=True)
class ReviewMessage:
    message_ref: str
    actor: str
    text: str
    occurred_at: datetime
    # v2: que es el mensaje (respuesta del agente, plantilla de reactivacion,
    # nota de derivacion, evento de etiqueta...), su estado de entrega y los
    # metadatos que la revision muestra al lado (decision, autor, atribucion).
    kind: str = "message"
    status: str = "sent"
    meta: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewConversation:
    conversation_ref: str
    display_label: str
    messages: tuple[ReviewMessage, ...]
    apparent_objective: str
    observed_outcome: str
    release_id: str
    release_version: int
    # v2: identidad del lead, link a Chatwoot, estado, eventos internos, links
    # de pago y revisiones previas. Vacio en v1.
    context: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class DailyReviewPackage:
    schema_version: str
    tenant_ref: str
    scope_ref: str
    window_start: datetime
    window_end: datetime
    conversations: tuple[ReviewConversation, ...]
    sanitizer_version: str = SANITIZER_VERSION_V1
    selection_version: str = SELECTION_VERSION_V1
    retention_hours: int | None = None
    deletion_owner: str | None = None
    storage_encryption_verified: bool = False


@dataclass(frozen=True)
class ReviewBundleResult:
    html_path: Path
    manifest_path: Path


def materialize_daily_review_package(
    *,
    store: DailyFeedbackBatchStore,
    command_id: str,
    package: DailyReviewPackage,
    authority: RealConversationBatchGrant,
) -> CreateBatchResult:
    """Commit one minimized package to the authoritative durable review workflow."""
    if (
        package.schema_version != "daily-feedback-review-package-v1"
        or package.retention_hours is None
        or package.deletion_owner is None
        or package.storage_encryption_verified is not True
        or authority.active is not True
        or authority.tenant_id != package.tenant_ref
        or authority.scope_id != package.scope_ref
        or authority.package_schema_version != package.schema_version
        or authority.sanitizer_version != package.sanitizer_version
        or authority.selection_version != package.selection_version
        or authority.retention_hours != package.retention_hours
        or authority.deletion_owner != package.deletion_owner
        or not authority.storage_encryption_evidence_ref.strip()
    ):
        raise ConversationCollectionError("review_package_not_materializable")
    if any(
        sanitize_message_text(message.text, names=()) != message.text
        for conversation in package.conversations
        for message in conversation.messages
    ):
        raise ConversationCollectionError("review_package_sanitization_mismatch")
    if any(
        sanitize_message_text(value, names=()) != value
        for conversation in package.conversations
        for value in (
            conversation.display_label,
            conversation.apparent_objective,
            conversation.observed_outcome,
        )
    ):
        raise ConversationCollectionError("review_package_sanitization_mismatch")
    # Antes esto EXIGIA el literal 'release_lineage_unavailable' con version 0,
    # o sea que el campo del linaje no podia llevar nada real. Desde
    # 20260928000100 la procedencia se registra por turno, asi que ahora se
    # admiten las dos formas y nada mas: el marcador de "no se sabe", o un digest
    # de release de verdad con su version. Cualquier otra cosa es un linaje
    # inventado, y eso es peor que no tenerlo.
    if any(
        not _release_lineage_valid(conversation)
        for conversation in package.conversations
    ):
        raise ConversationCollectionError("review_package_release_lineage_invalid")
    return store.create_minimized_review_batch(
        command_id=command_id,
        tenant_id=package.tenant_ref,
        scope_id=package.scope_ref,
        window_start=package.window_start,
        window_end=package.window_end,
        sanitizer_version=package.sanitizer_version,
        selection_version=package.selection_version,
        authority=authority,
        conversations=tuple(
            MinimizedReviewConversation(
                conversation_ref=conversation.conversation_ref,
                context_summary=conversation.display_label,
                apparent_objective=conversation.apparent_objective,
                observed_outcome=conversation.observed_outcome,
                release_id=conversation.release_id,
                release_version=conversation.release_version,
                messages=tuple(
                    ReviewTranscriptMessage(
                        message_ref=message.message_ref,
                        actor=message.actor,
                        text=message.text,
                        occurred_at=message.occurred_at,
                    )
                    for message in conversation.messages
                ),
            )
            for conversation in package.conversations
        ),
    )


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}_must_be_timezone_aware")
    return value.astimezone(UTC)


def _hmac_ref(key: bytes, namespace: str, value: object) -> str:
    digest = hmac.new(
        key,
        f"{namespace}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:20]
    return f"{namespace}_{digest}"


sanitize_message_text = sanitize_review_text


class ChatwootDailyCollector:
    """Read one configured Chatwoot inbox into a local minimized package."""

    def __init__(
        self,
        *,
        base_url: str,
        account_id: int,
        inbox_id: int,
        agent_bot_id: int,
        access_token: str,
        pseudonymization_key: bytes,
        security_policy: RealConversationSecurityPolicy,
        transport: httpx.BaseTransport | None = None,
        max_conversation_pages: int = 20,
        max_message_pages: int = 20,
        package_version: int = 1,
    ) -> None:
        if package_version not in {1, 2}:
            raise ValueError("unsupported_daily_feedback_package_version")
        self._package_version = package_version
        origin = httpx.URL(base_url)
        if (
            origin.scheme != "https"
            or not origin.host
            or bool(origin.username)
            or bool(origin.password)
            or origin.query
            or origin.fragment
            or origin.path not in {"", "/"}
        ):
            raise ConversationCollectionError("chatwoot_https_origin_required")
        if any(type(value) is not int or value <= 0 for value in (account_id, inbox_id, agent_bot_id)):
            raise ValueError("chatwoot_ids_must_be_positive_integers")
        if not access_token:
            raise ValueError("chatwoot_access_token_required")
        if len(pseudonymization_key) < 32:
            raise ValueError("pseudonymization_key_too_short")
        if (
            type(security_policy.real_collection_enabled) is not bool
            or type(security_policy.storage_encryption_verified) is not bool
            or security_policy.real_collection_enabled is not True
            or security_policy.storage_encryption_verified is not True
            or type(security_policy.retention_hours) is not int
            or not 1 <= security_policy.retention_hours <= 168
            or not security_policy.deletion_owner.strip()
        ):
            raise ConversationCollectionError(
                "real_conversation_collection_not_authorized"
            )
        if max_conversation_pages < 1 or max_message_pages < 1:
            raise ValueError("page_limits_must_be_positive")
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id
        self._inbox_id = inbox_id
        self._agent_bot_id = agent_bot_id
        self._access_token = access_token
        self._key = pseudonymization_key
        self._security_policy = security_policy
        self._transport = transport
        self._max_conversation_pages = max_conversation_pages
        self._max_message_pages = max_message_pages

    def verify_access(self) -> None:
        """Verify the configured inbox and bot without reading conversations."""
        with httpx.Client(
            base_url=self._base_url,
            headers={"api_access_token": self._access_token},
            transport=self._transport,
            timeout=20,
        ) as client:
            try:
                inbox_response = client.get(
                    f"/api/v1/accounts/{self._account_id}/inboxes/{self._inbox_id}"
                )
                inbox_response.raise_for_status()
                inbox = inbox_response.json()
                bot_response = client.get(
                    f"/api/v1/accounts/{self._account_id}/inboxes/"
                    f"{self._inbox_id}/agent_bot"
                )
                bot_response.raise_for_status()
                bot_payload = bot_response.json()
                bot = (
                    bot_payload.get("agent_bot")
                    if isinstance(bot_payload, dict)
                    else None
                )
            except (httpx.HTTPError, ValueError) as exc:
                raise ConversationCollectionError(
                    "chatwoot_scope_verification_failed"
                ) from exc
        if (
            not isinstance(inbox, dict)
            or type(inbox.get("id")) is not int
            or inbox["id"] != self._inbox_id
            or (
                "account_id" in inbox
                and (
                    type(inbox.get("account_id")) is not int
                    or inbox["account_id"] != self._account_id
                )
            )
            or not isinstance(bot, dict)
            or type(bot.get("id")) is not int
            or bot["id"] != self._agent_bot_id
        ):
            raise ConversationCollectionError("chatwoot_scope_verification_failed")

    def collect(
        self,
        *,
        tenant_ref: str,
        scope_ref: str,
        window_start: datetime,
        window_end: datetime,
    ) -> DailyReviewPackage:
        start = _utc(window_start, "window_start")
        end = _utc(window_end, "window_end")
        if start >= end:
            raise ValueError("invalid_review_window")
        if not tenant_ref or not scope_ref:
            raise ValueError("invalid_review_package_identity")

        with httpx.Client(
            base_url=self._base_url,
            headers={"api_access_token": self._access_token},
            transport=self._transport,
            timeout=20,
        ) as client:
            raw_conversations = self._list_conversations(client)
            collected: list[
                tuple[datetime, int, tuple[ReviewMessage, ...], dict[str, object]]
            ] = []
            for conversation in raw_conversations:
                conversation_id = conversation.get("id")
                inbox_id = conversation.get("inbox_id")
                updated_at = conversation.get("updated_at")
                if (
                    type(conversation_id) is not int
                    or conversation_id <= 0
                    or type(inbox_id) is not int
                    or inbox_id != self._inbox_id
                    or isinstance(updated_at, bool)
                    or not isinstance(updated_at, (int, float))
                    or not math.isfinite(updated_at)
                    or updated_at <= 0
                ):
                    raise ConversationCollectionError("chatwoot_conversation_scope_mismatch")
                if datetime.fromtimestamp(updated_at, tz=UTC) < start:
                    continue
                names = self._conversation_names(conversation)
                context: dict[str, object] = {}
                if self._package_version == 2:
                    messages = self._collect_messages_v2(
                        client,
                        conversation_id=conversation_id,
                        window_start=start,
                        window_end=end,
                    )
                    # v2 entra con que el lead haya escrito: una conversacion
                    # sin respuesta del agente es justo lo que hay que ver.
                    if not any(message.actor == "prospect" for message in messages):
                        continue
                    context = self._conversation_context(
                        conversation, conversation_id=conversation_id
                    )
                else:
                    messages = self._collect_messages(
                        client,
                        conversation_id=conversation_id,
                        window_start=start,
                        window_end=end,
                        names=names,
                    )
                    actors = {message.actor for message in messages}
                    if not messages or not {"prospect", "agent"}.issubset(actors):
                        continue
                collected.append((messages[0].occurred_at, conversation_id, messages, context))

        collected.sort(key=lambda item: (item[0], item[1]))
        conversations: list[ReviewConversation] = []
        for position, (_, conversation_id, messages, context) in enumerate(collected, start=1):
            if self._package_version == 2:
                apparent_objective = derive_apparent_objective(messages, context)
                observed_outcome = derive_observed_outcome(messages, context)
            else:
                apparent_objective = "Revisar la respuesta del agente"
                observed_outcome = (
                    "Esperando respuesta del prospecto"
                    if messages[-1].actor == "agent"
                    else "El prospecto continuó la conversación"
                )
            conversations.append(
                ReviewConversation(
                    conversation_ref=_hmac_ref(
                        self._key,
                        "conv",
                        f"{tenant_ref}:{scope_ref}:{conversation_id}",
                    ),
                    display_label=f"Conversación {position:02d}",
                    messages=messages,
                    apparent_objective=apparent_objective,
                    observed_outcome=observed_outcome,
                    release_id="release_lineage_unavailable",
                    release_version=0,
                    context=context,
                )
            )
        if self._package_version == 2:
            versions = (PACKAGE_SCHEMA_V2, SANITIZER_VERSION_V2, SELECTION_VERSION_V2)
        else:
            versions = (PACKAGE_SCHEMA_V1, SANITIZER_VERSION_V1, SELECTION_VERSION_V1)
        return DailyReviewPackage(
            schema_version=versions[0],
            tenant_ref=tenant_ref,
            scope_ref=scope_ref,
            window_start=start,
            window_end=end,
            conversations=tuple(conversations),
            sanitizer_version=versions[1],
            selection_version=versions[2],
            retention_hours=self._security_policy.retention_hours,
            deletion_owner=self._security_policy.deletion_owner,
            storage_encryption_verified=(
                self._security_policy.storage_encryption_verified
            ),
        )

    def _list_conversations(self, client: httpx.Client) -> list[dict[str, object]]:
        path = f"/api/v1/accounts/{self._account_id}/conversations"
        result: list[dict[str, object]] = []
        seen_conversation_ids: set[int] = set()
        expected_count: int | None = None
        for page_number in range(1, self._max_conversation_pages + 1):
            response = client.get(
                path,
                params={"inbox_id": self._inbox_id, "status": "all", "page": page_number},
            )
            response.raise_for_status()
            body = _json_object(response, "invalid_chatwoot_conversation_list")
            data = body.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("payload"), list):
                raise ConversationCollectionError("invalid_chatwoot_conversation_list")
            payload = data["payload"]
            if not all(isinstance(item, dict) for item in payload):
                raise ConversationCollectionError("invalid_chatwoot_conversation_list")
            for item in payload:
                conversation_id = item.get("id")
                if type(conversation_id) is not int or conversation_id <= 0:
                    raise ConversationCollectionError(
                        "invalid_chatwoot_conversation_list"
                    )
                if conversation_id in seen_conversation_ids:
                    raise ConversationCollectionError(
                        "chatwoot_conversation_pagination_repeated"
                    )
                seen_conversation_ids.add(conversation_id)
            result.extend(payload)
            meta = data.get("meta")
            if not isinstance(meta, dict):
                raise ConversationCollectionError("invalid_chatwoot_conversation_list")
            current = meta.get("current_page")
            all_count = meta.get("all_count")
            if (
                (
                    "current_page" in meta
                    and (type(current) is not int or current != page_number)
                )
                or type(all_count) is not int
                or all_count < 0
                or len(result) > all_count
            ):
                raise ConversationCollectionError("invalid_chatwoot_conversation_list")
            if expected_count is None:
                expected_count = all_count
            elif all_count != expected_count:
                raise ConversationCollectionError(
                    "chatwoot_conversation_count_changed"
                )
            if len(result) == expected_count:
                return result
            if not payload:
                raise ConversationCollectionError(
                    "chatwoot_conversation_list_incomplete"
                )
        raise ConversationCollectionError("chatwoot_conversation_page_limit_reached")

    def _collect_messages(
        self,
        client: httpx.Client,
        *,
        conversation_id: int,
        window_start: datetime,
        window_end: datetime,
        names: tuple[str, ...],
    ) -> tuple[ReviewMessage, ...]:
        by_id = self._fetch_messages_by_id(
            client, conversation_id=conversation_id, window_start=window_start
        )

        selected: list[ReviewMessage] = []
        for message_id, message in sorted(
            by_id.items(), key=lambda item: (int(item[1]["created_at"]), item[0])
        ):
            occurred_at = datetime.fromtimestamp(int(message["created_at"]), tz=UTC)
            if not (window_start <= occurred_at < window_end):
                continue
            actor = self._message_actor(message)
            if actor is None:
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            selected.append(
                ReviewMessage(
                    message_ref=_hmac_ref(
                        self._key,
                        "msg",
                        f"{conversation_id}:{message_id}",
                    ),
                    actor=actor,
                    text=sanitize_message_text(content, names=names),
                    occurred_at=occurred_at,
                )
            )
        return tuple(selected)

    def _message_actor(self, message: dict[str, object]) -> str | None:
        if message.get("private") is not False:
            return None
        sender = message.get("sender")
        if not isinstance(sender, dict):
            return None
        message_type = message.get("message_type")
        if message_type == 0 and sender.get("type") == "contact":
            return "prospect"
        if (
            message_type == 1
            and sender.get("type") == "agent_bot"
            and sender.get("id") == self._agent_bot_id
            and message.get("status") in {"sent", "delivered", "read"}
        ):
            return "agent"
        return None

    @staticmethod
    def _conversation_names(conversation: dict[str, object]) -> tuple[str, ...]:
        meta = conversation.get("meta")
        if not isinstance(meta, dict):
            return ()
        sender = meta.get("sender")
        if not isinstance(sender, dict):
            return ()
        name = sender.get("name")
        return (name,) if isinstance(name, str) and name.strip() else ()

    def _fetch_messages_by_id(
        self,
        client: httpx.Client,
        *,
        conversation_id: int,
        window_start: datetime,
    ) -> dict[int, dict[str, object]]:
        """Pagina hacia atras hasta probar que se cruzo el inicio de la ventana."""
        path = f"/api/v1/accounts/{self._account_id}/conversations/{conversation_id}/messages"
        before: int | None = None
        by_id: dict[int, dict[str, object]] = {}
        boundary_proven = False
        for _ in range(self._max_message_pages):
            response = client.get(path, params={"before": before} if before is not None else None)
            response.raise_for_status()
            body = _json_object(response, "invalid_chatwoot_message_list")
            payload = body.get("payload")
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                raise ConversationCollectionError("invalid_chatwoot_message_list")
            if not payload:
                boundary_proven = True
                break
            page_ids: list[int] = []
            page_times: list[int] = []
            for message in payload:
                message_id = message.get("id")
                created_at = message.get("created_at")
                if type(message_id) is not int or message_id <= 0 or type(created_at) is not int or created_at <= 0:
                    raise ConversationCollectionError("invalid_chatwoot_message")
                page_ids.append(message_id)
                page_times.append(created_at)
                by_id[message_id] = message
            if min(page_times) <= int(window_start.timestamp()):
                boundary_proven = True
                break
            next_before = min(page_ids)
            if before == next_before:
                raise ConversationCollectionError("chatwoot_message_history_did_not_advance")
            before = next_before
        if not boundary_proven:
            raise ConversationCollectionError("chatwoot_message_page_limit_reached")
        return by_id

    def _collect_messages_v2(
        self,
        client: httpx.Client,
        *,
        conversation_id: int,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[ReviewMessage, ...]:
        """Todo lo que paso en la conversacion dentro de la ventana, clasificado.

        A diferencia de v1 entran los mensajes del equipo, las notas privadas,
        las actividades (etiquetas, asignacion, resolucion) y los envios
        fallidos: el revisor tiene que ver lo mismo que veria en Chatwoot.
        """
        by_id = self._fetch_messages_by_id(
            client, conversation_id=conversation_id, window_start=window_start
        )
        selected: list[ReviewMessage] = []
        for message_id, message in sorted(
            by_id.items(), key=lambda item: (int(item[1]["created_at"]), item[0])
        ):
            occurred_at = datetime.fromtimestamp(int(message["created_at"]), tz=UTC)
            if not (window_start <= occurred_at < window_end):
                continue
            classified = classify_chatwoot_message(
                message, agent_bot_id=self._agent_bot_id
            )
            if classified is None:
                continue
            content = message.get("content")
            text = minimize_review_text_v2(content) if isinstance(content, str) else ""
            meta = dict(classified.meta)
            if not text:
                text = _EMPTY_MESSAGE_TEXT
                meta["empty"] = True
            selected.append(
                ReviewMessage(
                    message_ref=_hmac_ref(
                        self._key,
                        "msg",
                        f"{conversation_id}:{message_id}",
                    ),
                    actor=classified.actor,
                    text=text[:4000],
                    occurred_at=occurred_at,
                    kind=classified.kind,
                    status=classified.status,
                    meta=meta,
                )
            )
        return tuple(selected)

    def _conversation_context(
        self, conversation: dict[str, object], *, conversation_id: int
    ) -> dict[str, object]:
        """Identidad del lead y estado de la conversacion, tal como los da Chatwoot."""
        meta = conversation.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        sender = meta.get("sender")
        sender = sender if isinstance(sender, dict) else {}
        assignee = meta.get("assignee")
        assignee = assignee if isinstance(assignee, dict) else {}
        sender_id = sender.get("id")
        labels_raw = conversation.get("labels")
        labels = (
            [label for label in labels_raw if isinstance(label, str) and label.strip()][:20]
            if isinstance(labels_raw, list)
            else []
        )
        can_reply = conversation.get("can_reply")
        unread = conversation.get("unread_count")
        return {
            "chatwoot_conversation_id": conversation_id,
            "conversation_url": (
                f"{self._base_url}/app/accounts/{self._account_id}"
                f"/conversations/{conversation_id}"
            ),
            "contact": {
                "id": sender_id if type(sender_id) is int else None,
                "name": _clean_text(sender.get("name")),
                "phone": _clean_text(sender.get("phone_number")),
                "email": _clean_text(sender.get("email")),
            },
            "conversation": {
                "status": _clean_text(conversation.get("status")),
                "labels": [_clean_text(label) or "" for label in labels],
                "assignee": _clean_text(assignee.get("name")),
                "created_at": _epoch_to_iso(conversation.get("created_at")),
                "first_reply_at": _epoch_to_iso(conversation.get("first_reply_created_at")),
                "last_activity_at": _epoch_to_iso(conversation.get("last_activity_at")),
                "can_reply": can_reply if isinstance(can_reply, bool) else None,
                "unread_count": unread if type(unread) is int else None,
            },
            "origin": "inbound",
        }


@dataclass(frozen=True)
class ClassifiedMessage:
    actor: str
    kind: str
    status: str
    meta: dict[str, object]


def classify_chatwoot_message(
    message: Mapping[str, object], *, agent_bot_id: int
) -> ClassifiedMessage | None:
    """Que es un mensaje de Chatwoot para la revision diaria (v2).

    Capturado el 26/09/2026 en el inbox 9 de Johanna: el bot publica con
    ``appointment_setter_reply_hash`` (y ``appointment_setter_decision`` /
    ``appointment_setter_reason_code`` desde este cambio), la reactivacion con
    ``reactivation_command_key``, el primer toque con ``recovery_first_touch_hash``
    y el seguimiento con ``recovery_followup_hash``. Las derivaciones dejan una
    nota privada de "Bridge Service" y las etiquetas aparecen como actividades
    (``message_type`` 2) sin remitente.
    """
    message_type = message.get("message_type")
    private = message.get("private") is True
    sender_raw = message.get("sender")
    sender = sender_raw if isinstance(sender_raw, dict) else {}
    sender_type = sender.get("type")
    sender_name = _clean_text(sender.get("name"))
    attributes_raw = message.get("content_attributes")
    attributes = attributes_raw if isinstance(attributes_raw, dict) else {}
    status_raw = message.get("status")
    status = (
        status_raw
        if isinstance(status_raw, str) and _MESSAGE_STATUS_RE.fullmatch(status_raw)
        else "unknown"
    )
    content_raw = message.get("content")
    content = content_raw if isinstance(content_raw, str) else ""
    meta: dict[str, object] = {}

    if message_type == 2:
        kind, activity_meta = _classify_activity(content)
        return ClassifiedMessage(actor="system", kind=kind, status=status, meta=activity_meta)
    if message_type == 0 and sender_type == "contact":
        return ClassifiedMessage(actor="prospect", kind="prospect_message", status=status, meta=meta)
    if message_type == 1 and private:
        if sender_name:
            meta["author"] = sender_name
        lowered = content.lstrip().lower()
        kind = (
            "handoff_note"
            if lowered.startswith("derivación") or lowered.startswith("derivacion")
            else "private_note"
        )
        return ClassifiedMessage(actor="team", kind=kind, status=status, meta=meta)
    if message_type == 1 and sender_type == "agent_bot":
        if sender.get("id") != agent_bot_id:
            return None
        message_id = message.get("id")
        if type(message_id) is int and message_id > 0:
            meta["chatwoot_message_id"] = message_id
        for attribute, key in (
            ("appointment_setter_decision", "decision"),
            ("appointment_setter_reason_code", "reason_code"),
        ):
            value = attributes.get(attribute)
            if isinstance(value, str) and _MESSAGE_KIND_RE.fullmatch(value):
                meta[key] = value
        part_index = attributes.get("appointment_setter_reply_part_index")
        part_count = attributes.get("appointment_setter_reply_part_count")
        if type(part_index) is int and type(part_count) is int and part_count > 1:
            meta["part"] = f"{part_index}/{part_count}"
        if "reactivation_command_key" in attributes:
            kind = "reactivation_template"
        elif "recovery_first_touch_hash" in attributes:
            kind = "first_touch_template"
        elif "recovery_followup_hash" in attributes:
            kind = "followup_template"
        elif _PAYMENT_LINK_RE.search(content):
            kind = "payment_link"
        elif "appointment_setter_reply_hash" in attributes:
            kind = "agent_reply"
        else:
            kind = "agent_message"
        return ClassifiedMessage(actor="agent", kind=kind, status=status, meta=meta)
    if message_type == 1 and sender_type == "user":
        if sender_name:
            meta["author"] = sender_name
        return ClassifiedMessage(actor="team", kind="team_message", status=status, meta=meta)
    if message_type == 3:
        if sender_name:
            meta["author"] = sender_name
        actor = "agent" if sender_type == "agent_bot" else "team"
        return ClassifiedMessage(actor=actor, kind="template_message", status=status, meta=meta)
    return None


def _classify_activity(content: str) -> tuple[str, dict[str, object]]:
    text = " ".join(content.split())
    label_match = _LABEL_ACTIVITY_RE.match(text)
    if label_match is not None:
        label = label_match.group("label")
        meta: dict[str, object] = {
            "actor_name": label_match.group("who"),
            "label": label,
        }
        added = label_match.group("verb") == "added"
        if label == "automation_paused":
            return ("automation_paused" if added else "automation_resumed", meta)
        return ("label_added" if added else "label_removed", meta)
    assigned = _ASSIGNED_ACTIVITY_RE.match(text)
    if assigned is not None:
        return (
            "assigned",
            {"assignee": assigned.group("assignee"), "actor_name": assigned.group("who")},
        )
    unassigned = _UNASSIGNED_ACTIVITY_RE.match(text)
    if unassigned is not None:
        return ("unassigned", {"actor_name": unassigned.group("who")})
    status = _STATUS_ACTIVITY_RE.match(text)
    if status is not None:
        meta = {"actor_name": status.group("who")} if status.group("who") else {}
        return (f"conversation_{status.group('state')}", meta)
    return ("activity", {})


def _clean_text(value: object, *, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = minimize_review_text_v2(value)
    return cleaned[:limit] if cleaned else None


def _epoch_to_iso(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return _iso(datetime.fromtimestamp(value, tz=UTC))


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _iso_or_none(value: object) -> str | None:
    parsed = _parse_iso(value)
    return _iso(parsed) if parsed is not None else None


def _attribution(sck_value: object) -> str:
    """`full` cuando el sck trae el marcador del anuncio antes de `hermes`."""
    if not isinstance(sck_value, str) or not sck_value:
        return "unknown"
    head = re.split(r"[|~.]", sck_value, maxsplit=1)[0]
    return "marker_only" if head == "hermes" else "full"


def normalize_conversation_events(extra: object) -> tuple[
    list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]
]:
    """Convierte la respuesta de `get_daily_feedback_conversation_context_v1` en
    eventos, links de pago y revisiones previas acotados y ordenados."""
    if not isinstance(extra, dict):
        return [], [], []
    events: list[dict[str, object]] = []

    def rows(key: str) -> list[dict[str, object]]:
        value = extra.get(key)
        if not isinstance(value, list):
            return []
        return [row for row in value if isinstance(row, dict)][:50]

    for row in rows("handoffs"):
        events.append(
            {
                "kind": "handoff",
                "occurred_at": _iso_or_none(row.get("occurred_at")),
                "primary_reason_code": _clean_text(row.get("primary_reason_code"), limit=100),
                "detail_reason_code": _clean_text(row.get("detail_reason_code"), limit=100),
                "requested_by": _clean_text(row.get("requested_by"), limit=40),
                "status": _clean_text(row.get("status"), limit=40),
            }
        )
    for row in rows("reactivations"):
        events.append(
            {
                "kind": "reactivation",
                "occurred_at": _iso_or_none(row.get("occurred_at")),
                "status": _clean_text(row.get("status"), limit=40),
                "template_name": _clean_text(row.get("template_name"), limit=120),
                "reason_code": _clean_text(row.get("reason_code"), limit=100),
                "provider_message_id": (
                    row.get("provider_message_id")
                    if type(row.get("provider_message_id")) is int
                    else None
                ),
                "quiet_seconds": (
                    row.get("quiet_seconds") if type(row.get("quiet_seconds")) is int else None
                ),
                "failure_reason": _clean_text(row.get("failure_reason"), limit=200),
            }
        )
    for row in rows("resumes"):
        events.append(
            {
                "kind": "resume",
                "occurred_at": _iso_or_none(row.get("occurred_at")),
                "reason_code": _clean_text(row.get("reason_code"), limit=100),
                "quiet_seconds": (
                    row.get("quiet_seconds") if type(row.get("quiet_seconds")) is int else None
                ),
            }
        )
    for row in rows("opt_outs"):
        events.append(
            {
                "kind": "opt_out",
                "occurred_at": _iso_or_none(row.get("occurred_at")),
                "chatwoot_message_id": (
                    row.get("chatwoot_message_id")
                    if type(row.get("chatwoot_message_id")) is int
                    else None
                ),
            }
        )
    events.sort(key=lambda event: (event.get("occurred_at") or "", event["kind"]))

    payment_links: list[dict[str, object]] = []
    for row in rows("payment_links"):
        payment_links.append(
            {
                "occurred_at": _iso_or_none(row.get("occurred_at")),
                "status": _clean_text(row.get("status"), limit=40),
                "source_kind": _clean_text(row.get("source_kind"), limit=40),
                "chatwoot_message_id": (
                    row.get("chatwoot_message_id")
                    if type(row.get("chatwoot_message_id")) is int
                    else None
                ),
                # Hasta 255 del anuncio (limite del recuperador desde el 27/09)
                # mas los 37 del marcador |hermes|v1|<ulid>: 200 cortaba la cola.
                "sck_value": _clean_text(row.get("sck_value"), limit=320),
                "attribution": _attribution(row.get("sck_value")),
                "checkout_url_final": _clean_text(row.get("checkout_url_final"), limit=400),
                "purchased_at": _iso_or_none(row.get("purchased_at")),
                "offer_code": _clean_text(row.get("offer_code"), limit=128),
                "landing_ref": _clean_text(row.get("landing_ref"), limit=100),
            }
        )
    payment_links.sort(key=lambda link: link.get("occurred_at") or "")

    prior_reviews: list[dict[str, object]] = []
    for row in rows("prior_reviews")[:10]:
        prior_reviews.append(
            {
                "local_date": _clean_text(row.get("local_date"), limit=10),
                "decision": _clean_text(row.get("decision"), limit=40),
                "verbatim_feedback": _clean_text(row.get("verbatim_feedback"), limit=4000),
                "decided_at": _iso_or_none(row.get("decided_at")),
            }
        )
    return events, payment_links, prior_reviews


def apply_conversation_context(
    package: DailyReviewPackage, contexts: Mapping[str, object]
) -> DailyReviewPackage:
    """Suma a cada conversacion v2 lo que Supabase sabe de ella y recalcula el
    objetivo aparente y el resultado observado con esa informacion."""
    if package.schema_version != PACKAGE_SCHEMA_V2:
        return package
    conversations: list[ReviewConversation] = []
    for conversation in package.conversations:
        conversation_id = conversation.context.get("chatwoot_conversation_id")
        extra = contexts.get(str(conversation_id)) if conversation_id is not None else None
        events, payment_links, prior_reviews = normalize_conversation_events(extra)
        context: dict[str, object] = dict(conversation.context)
        context["events"] = events
        context["payment_links"] = payment_links
        context["prior_reviews"] = prior_reviews
        if any(link.get("source_kind") == "precheckout_request" for link in payment_links):
            context["origin"] = "precheckout"
        messages = _annotate_payment_links(conversation.messages, payment_links)
        context["summary"] = summarize_conversation(messages, context)
        conversations.append(
            replace(
                conversation,
                messages=messages,
                context=context,
                apparent_objective=derive_apparent_objective(messages, context),
                observed_outcome=derive_observed_outcome(messages, context),
            )
        )
    return replace(package, conversations=tuple(conversations))


def _annotate_payment_links(
    messages: tuple[ReviewMessage, ...], payment_links: list[dict[str, object]]
) -> tuple[ReviewMessage, ...]:
    by_message_id = {
        link["chatwoot_message_id"]: link
        for link in payment_links
        if type(link.get("chatwoot_message_id")) is int
    }
    if not by_message_id:
        return messages
    annotated: list[ReviewMessage] = []
    for message in messages:
        link = by_message_id.get(message.meta.get("chatwoot_message_id"))
        if message.actor != "agent" or link is None:
            annotated.append(message)
            continue
        meta = dict(message.meta)
        meta["attribution"] = link.get("attribution")
        meta["purchased"] = link.get("purchased_at") is not None
        if link.get("offer_code"):
            meta["offer_code"] = link.get("offer_code")
        annotated.append(replace(message, kind="payment_link", meta=meta))
    return tuple(annotated)


def summarize_conversation(
    messages: tuple[ReviewMessage, ...], context: Mapping[str, object]
) -> dict[str, object]:
    conversation_raw = context.get("conversation")
    conversation = conversation_raw if isinstance(conversation_raw, dict) else {}
    labels_raw = conversation.get("labels")
    labels = labels_raw if isinstance(labels_raw, list) else []
    events_raw = context.get("events")
    events = [event for event in events_raw if isinstance(event, dict)] if isinstance(events_raw, list) else []
    links_raw = context.get("payment_links")
    links = [link for link in links_raw if isinstance(link, dict)] if isinstance(links_raw, list) else []
    handoffs = [event for event in events if event.get("kind") == "handoff"]
    reactivations = [event for event in events if event.get("kind") == "reactivation"]
    agent_messages = [message for message in messages if message.actor == "agent"]
    last = messages[-1] if messages else None
    return {
        "prospect_messages": sum(1 for message in messages if message.actor == "prospect"),
        "agent_messages": len(agent_messages),
        "team_messages": sum(1 for message in messages if message.actor == "team" and message.kind == "team_message"),
        "agent_replied": bool(agent_messages),
        "payment_link_sent": bool(links) or any(message.kind == "payment_link" for message in messages),
        "purchase_recorded": any(link.get("purchased_at") for link in links),
        "handoff_count": len(handoffs),
        "last_handoff_reason": (
            handoffs[-1].get("detail_reason_code") or handoffs[-1].get("primary_reason_code")
            if handoffs
            else None
        ),
        "reactivation_count": len(reactivations),
        "opt_out": any(event.get("kind") == "opt_out" for event in events),
        "automation_paused": "automation_paused" in labels,
        "status": conversation.get("status"),
        "last_actor": last.actor if last is not None else None,
        "last_message_at": _iso(last.occurred_at) if last is not None else None,
    }


_OBJECTIVE_RULES: tuple[tuple[str, str], ...] = (
    # pedir el link es una accion ("enviame el enlace"), no mencionarlo
    # ("en este enlace dice 49", que es una consulta de precio)
    (
        r"\b(env[ií]a(me)?|m[aá]nda(me)?|p[aá]sa(me)?|dame|comparte(me)?)\b[^.?!]{0,40}\b(enlace|link)\b"
        r"|\b(quiero|deseo|me gustar[ií]a) (comprar|pagar|inscribirme|adquirir)\b"
        r"|\bd[oó]nde (pago|compro|me inscribo)\b|\blink de pago\b|\bc[oó]mo (pago|compro)\b",
        "Pedido del enlace de pago",
    ),
    (r"precio|cu[aá]nto (cuesta|vale|sale)|costo|cuotas|descuento|d[oó]lares|usd", "Consulta de precio o formas de pago"),
    (r"no (puedo|he podido|pude)|acceso|ingresar|entrar|contrase|clave|correo", "Soporte post-compra (acceso al programa)"),
    (r"info|informaci|duda|m[oó]dulo|contenido|incluye|programa|curso|taller", "Consulta sobre el programa"),
)


def derive_apparent_objective(
    messages: tuple[ReviewMessage, ...], context: Mapping[str, object]
) -> str:
    """Motivo aparente del lead: primero la decision del agente, despues lo que
    escribio el lead. Es una etiqueta orientativa, no un hecho."""
    decisions = {
        str(message.meta.get("decision"))
        for message in messages
        if message.actor == "agent" and message.meta.get("decision")
    }
    if "send_payment_link" in decisions or any(message.kind == "payment_link" for message in messages):
        return "Pedido del enlace de pago"
    prospect_text = " ".join(
        message.text.lower() for message in messages if message.actor == "prospect"
    )
    for pattern, label in _OBJECTIVE_RULES:
        if re.search(pattern, prospect_text):
            return label
    if any(message.actor == "prospect" for message in messages):
        return "Conversación sin objetivo claro"
    return "Sin mensajes del prospecto en la ventana"


def derive_observed_outcome(
    messages: tuple[ReviewMessage, ...], context: Mapping[str, object]
) -> str:
    """Hechos observables de la conversacion, del mas fuerte al mas debil."""
    summary = summarize_conversation(messages, context)
    facts: list[str] = []
    if summary["purchase_recorded"]:
        facts.append("Compra registrada por el link enviado")
    elif summary["payment_link_sent"]:
        facts.append("Link de pago enviado")
    if summary["handoff_count"]:
        reason = summary["last_handoff_reason"]
        facts.append(
            f"Derivado a humano ({reason})" if reason else "Derivado a humano"
        )
    if summary["opt_out"]:
        facts.append("El lead pidió no recibir más mensajes")
    if summary["reactivation_count"]:
        facts.append(f"Reactivación enviada ×{summary['reactivation_count']}")
    if summary["team_messages"]:
        facts.append("Atendida por el equipo")
    if not summary["agent_replied"]:
        facts.append("Sin respuesta del agente")
    elif summary["last_actor"] == "agent":
        facts.append("Esperando respuesta del prospecto")
    elif summary["last_actor"] == "prospect":
        facts.append("El prospecto escribió último")
    if summary["automation_paused"]:
        facts.append("Automatización pausada")
    if summary["status"] == "resolved":
        facts.append("Conversación resuelta")
    text = " · ".join(facts) if facts else "Sin hechos observables"
    return text[:_MAX_SUMMARY_TEXT]


def _json_object(response: httpx.Response, reason: str) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ConversationCollectionError(reason) from exc
    if not isinstance(body, dict):
        raise ConversationCollectionError(reason)
    return body


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _package_fingerprint(package: DailyReviewPackage) -> str:
    payload = {
        "schema_version": package.schema_version,
        "tenant_ref": package.tenant_ref,
        "scope_ref": package.scope_ref,
        "window_start": _iso(package.window_start),
        "window_end": _iso(package.window_end),
        "sanitizer_version": package.sanitizer_version,
        "selection_version": package.selection_version,
        "retention_hours": package.retention_hours,
        "deletion_owner": package.deletion_owner,
        "storage_encryption_verified": package.storage_encryption_verified,
        "conversations": [
            {
                "conversation_ref": conversation.conversation_ref,
                "release_id": conversation.release_id,
                "release_version": conversation.release_version,
                "messages": [
                    {
                        "message_ref": message.message_ref,
                        "actor": message.actor,
                        "text": message.text,
                        "occurred_at": _iso(message.occurred_at),
                        **(
                            {
                                "kind": message.kind,
                                "status": message.status,
                                "meta": dict(message.meta),
                            }
                            if package.schema_version == PACKAGE_SCHEMA_V2
                            else {}
                        ),
                    }
                    for message in conversation.messages
                ],
                **(
                    {"context": dict(conversation.context)}
                    if package.schema_version == PACKAGE_SCHEMA_V2
                    else {}
                ),
            }
            for conversation in package.conversations
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def render_review_html(package: DailyReviewPackage) -> str:
    """Render a self-contained, network-inert local inspection surface."""
    total = len(package.conversations)
    badge = "NO DISTRIBUIR · SUPERFICIE LOCAL"
    nav = []
    panels = []
    for index, conversation in enumerate(package.conversations, start=1):
        nav.append(
            f'<button class="conversation-nav{" active" if index == 1 else ""}" data-index="{index - 1}">'
            f'<span>{html.escape(conversation.display_label)}</span><small>Pendiente</small></button>'
        )
        messages = []
        for message in conversation.messages:
            actor_label = {
                "prospect": "Prospecto",
                "agent": "Agente",
                "team": "Equipo",
                "system": "Sistema",
            }.get(message.actor, "Agente")
            messages.append(
                f'<article class="message {message.actor}"><header>{actor_label}<time>{html.escape(_iso(message.occurred_at)[11:16])} UTC</time></header>'
                f'<p>{html.escape(message.text)}</p></article>'
            )
        panels.append(
            f'<section class="conversation-panel{" active" if index == 1 else ""}" data-panel="{index - 1}" data-ref="{html.escape(conversation.conversation_ref)}">'
            f'<div class="panel-head"><div><p class="eyebrow">{index} de {total}</p><h2>{html.escape(conversation.display_label)}</h2></div>'
            f'<div class="release">Release {html.escape(conversation.release_id)} · v{conversation.release_version}</div></div>'
            f'<div class="summary"><span><b>Objetivo aparente</b>{html.escape(conversation.apparent_objective)}</span>'
            f'<span><b>Resultado observado</b>{html.escape(conversation.observed_outcome)}</span></div>'
            f'<div class="transcript">{"".join(messages)}</div>'
            '<fieldset><legend>Evaluación</legend><div class="choices">'
            f'<label><input type="radio" name="decision-{index}" value="correct"> Correcta</label>'
            f'<label><input type="radio" name="decision-{index}" value="correct_with_feedback"> Correcta con feedback</label>'
            f'<label><input type="radio" name="decision-{index}" value="skip"> Omitir</label></div>'
            f'<label class="feedback">Feedback literal<textarea data-feedback="{index - 1}" placeholder="Escribí qué debería corregirse. No se aplicará automáticamente."></textarea></label></fieldset>'
            '</section>'
        )
    empty = '<div class="empty"><h2>No hay conversaciones elegibles</h2><p>El lote se generó correctamente, sin elementos para revisar.</p></div>' if not panels else ""
    package_ref = _package_fingerprint(package)
    return f'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'">
<title>Revisión diaria de conversaciones</title><style>
:root{{--ink:#19201c;--muted:#68716b;--paper:#f4f2eb;--surface:#fffdfa;--line:#d9d8d0;--accent:#22644a;--prospect:#e8eee9;--agent:#f3ead7;--warning:#9b4d1f}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.45 ui-sans-serif,"Segoe UI",sans-serif}}button,input,textarea{{font:inherit}}.shell{{display:grid;grid-template-columns:290px minmax(0,1fr);min-height:100vh}}aside{{padding:24px 18px;border-right:1px solid var(--line);background:#eceae2;position:sticky;top:0;height:100vh;overflow:auto}}.brand{{font:700 18px/1.2 Georgia,serif;margin:0 0 6px}}.window{{color:var(--muted);font-size:12px;margin-bottom:18px}}.badge{{display:block;color:var(--warning);font-size:11px;font-weight:800;letter-spacing:.06em;margin:12px 0 20px}}.conversation-nav{{width:100%;display:flex;justify-content:space-between;align-items:center;border:0;border-top:1px solid var(--line);padding:14px 8px;background:transparent;text-align:left;cursor:pointer;color:var(--ink)}}.conversation-nav:last-child{{border-bottom:1px solid var(--line)}}.conversation-nav small{{color:var(--muted)}}.conversation-nav.active{{color:var(--accent);font-weight:750}}main{{max-width:920px;width:100%;padding:36px 48px 56px}}.conversation-panel{{display:none}}.conversation-panel.active{{display:block}}.panel-head{{display:flex;gap:24px;justify-content:space-between;align-items:end;border-bottom:2px solid var(--ink);padding-bottom:16px}}.eyebrow{{margin:0 0 3px;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.1em}}h2{{font:700 32px/1.1 Georgia,serif;margin:0}}.release{{color:var(--muted);font-size:12px;text-align:right}}.summary{{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);border:1px solid var(--line);margin:18px 0 28px}}.summary span{{background:var(--surface);padding:13px 15px}}.summary b{{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-bottom:4px}}.transcript{{display:flex;flex-direction:column;gap:10px;margin-bottom:30px}}.message{{max-width:78%;padding:13px 15px;border-radius:4px}}.message.prospect{{background:var(--prospect);align-self:flex-start}}.message.agent{{background:var(--agent);align-self:flex-end}}.message header{{display:flex;justify-content:space-between;gap:18px;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.05em}}.message time{{font-weight:500;color:var(--muted)}}.message p{{margin:6px 0 0;white-space:pre-wrap}}fieldset{{border:1px solid var(--line);padding:18px;background:var(--surface)}}legend{{font-weight:800;padding:0 7px}}.choices{{display:flex;flex-wrap:wrap;gap:16px}}.choices label{{min-height:44px;display:flex;align-items:center;gap:7px}}.feedback{{display:block;margin-top:14px;font-weight:700}}textarea{{display:block;width:100%;min-height:96px;margin-top:7px;padding:10px;border:1px solid var(--line);background:white;resize:vertical}}.actions{{display:flex;gap:10px;margin-top:18px}}.actions button{{min-height:44px;border:1px solid var(--ink);padding:0 16px;background:var(--ink);color:white;cursor:pointer}}.actions button.secondary{{background:transparent;color:var(--ink)}}.footnote{{color:var(--muted);font-size:12px;margin-top:18px}}.empty{{padding:50px 0}}@media(max-width:760px){{.shell{{display:block}}aside{{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}}main{{padding:26px 18px}}.summary{{grid-template-columns:1fr}}.message{{max-width:92%}}}}
</style></head><body><div class="shell"><aside><p class="brand">Revisión diaria</p><div class="window">{html.escape(_iso(package.window_start)[:10])} · {total} conversaciones</div><span class="badge">{html.escape(badge)}</span><nav>{''.join(nav)}</nav></aside><main>{''.join(panels)}{empty}<div class="actions"><button id="previous" class="secondary">Anterior</button><button id="next">Siguiente</button><button id="export" class="secondary">Exportar decisiones</button></div><p class="footnote">El feedback se exporta como candidato de revisión. Cambios activados: 0.</p></main></div>
<script>(()=>{{'use strict';const panels=[...document.querySelectorAll('.conversation-panel')],nav=[...document.querySelectorAll('.conversation-nav')];let current=0;function show(i){{if(!panels.length)return;current=Math.max(0,Math.min(i,panels.length-1));panels.forEach((p,n)=>p.classList.toggle('active',n===current));nav.forEach((b,n)=>b.classList.toggle('active',n===current));}}nav.forEach((b,i)=>b.addEventListener('click',()=>show(i)));document.getElementById('previous').addEventListener('click',()=>show(current-1));document.getElementById('next').addEventListener('click',()=>show(current+1));document.getElementById('export').addEventListener('click',()=>{{const decisions=panels.map((p,i)=>({{conversation_ref:p.dataset.ref,decision:(document.querySelector(`input[name="decision-${{i+1}}"]:checked`)||{{}}).value||null,verbatim_feedback:(p.querySelector('textarea').value||'').trim()||null}}));const payload={{schema_version:'daily-feedback-owner-decisions-v1',package_ref:'{package_ref}',activated_changes:0,decisions}};const blob=new Blob([JSON.stringify(payload,null,2)],{{type:'application/json'}}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='decisiones-feedback.json';a.click();URL.revokeObjectURL(a.href);}});show(0);}})();</script></body></html>'''


def write_private_review_bundle(
    *,
    output_dir: Path,
    package: DailyReviewPackage,
    now: datetime | None = None,
    _after_html_write: Callable[[], None] | None = None,
) -> ReviewBundleResult:
    """Atomically publish a new private directory with HTML and manifest."""
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(output_dir):
        target_stat = output_dir.lstat()
        if stat.S_ISDIR(target_stat.st_mode) and any(output_dir.iterdir()):
            raise ConversationCollectionError("review_output_directory_not_empty")
        raise ConversationCollectionError("review_output_already_exists")

    current = _utc(now or datetime.now(UTC), "now")
    policy_ready = (
        package.storage_encryption_verified is True
        and type(package.retention_hours) is int
        and 1 <= package.retention_hours <= 168
        and isinstance(package.deletion_owner, str)
        and bool(package.deletion_owner.strip())
    )
    distribution_ready = False
    review_html = render_review_html(package)
    html_bytes = review_html.encode("utf-8")
    manifest = {
        "schema_version": package.schema_version,
        "package_ref": _package_fingerprint(package),
        "tenant_ref": package.tenant_ref,
        "scope_ref": package.scope_ref,
        "window_start": _iso(package.window_start),
        "window_end": _iso(package.window_end),
        "created_at": _iso(current),
        "expires_at": (
            _iso(current + timedelta(hours=package.retention_hours))
            if policy_ready and package.retention_hours is not None
            else None
        ),
        "conversation_count": len(package.conversations),
        "sanitizer_version": package.sanitizer_version,
        "selection_version": package.selection_version,
        "retention_hours": package.retention_hours,
        "storage_encryption_verified": package.storage_encryption_verified,
        "deletion_owner_present": bool(package.deletion_owner),
        "human_reviewed": False,
        "distribution_ready": distribution_ready,
        "activated_changes": 0,
        "html_sha256": "sha256:" + hashlib.sha256(html_bytes).hexdigest(),
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent)
    )
    os.chmod(staging_dir, 0o700)
    try:
        staging_html = staging_dir / "review.html"
        staging_manifest = staging_dir / "manifest.json"
        _atomic_private_write(staging_html, html_bytes)
        if _after_html_write is not None:
            _after_html_write()
        _atomic_private_write(staging_manifest, manifest_bytes)
        staging_fd = os.open(
            staging_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
        os.rename(staging_dir, output_dir)
        parent_fd = os.open(
            output_dir.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except BaseException:
        if staging_dir.exists():
            for child in staging_dir.iterdir():
                child_stat = child.lstat()
                if stat.S_ISREG(child_stat.st_mode) and child.name in {
                    "review.html",
                    "manifest.json",
                }:
                    child.unlink()
            staging_dir.rmdir()
        raise
    return ReviewBundleResult(
        html_path=output_dir / "review.html",
        manifest_path=output_dir / "manifest.json",
    )


def approve_private_review_bundle(
    *,
    bundle_dir: Path,
    expected_html_sha256: str,
    reviewer_binding_ref: str,
    now: datetime,
) -> dict[str, object]:
    """Approve the exact existing HTML bytes without recollecting or rerendering."""
    current = _utc(now, "now")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_html_sha256) is None:
        raise ConversationCollectionError("invalid_expected_html_hash")
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", reviewer_binding_ref) is None:
        raise ConversationCollectionError("invalid_reviewer_binding_ref")

    directory_fd = os.open(
        bundle_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        directory_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.geteuid()
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise ConversationCollectionError("review_bundle_not_private")
        if sorted(os.listdir(directory_fd)) != ["manifest.json", "review.html"]:
            raise ConversationCollectionError("review_bundle_inventory_mismatch")
        manifest_bytes = _read_private_bundle_file(directory_fd, "manifest.json")
        html_bytes = _read_private_bundle_file(directory_fd, "review.html")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConversationCollectionError("invalid_review_bundle_manifest") from exc
        if not isinstance(manifest, dict):
            raise ConversationCollectionError("invalid_review_bundle_manifest")
        actual_html_hash = "sha256:" + hashlib.sha256(html_bytes).hexdigest()
        if (
            manifest.get("html_sha256") != actual_html_hash
            or expected_html_sha256 != actual_html_hash
        ):
            raise ConversationCollectionError("review_bundle_integrity_error")
        expires_at = manifest.get("expires_at")
        if not isinstance(expires_at, str):
            raise ConversationCollectionError("invalid_review_bundle_manifest")
        try:
            expiry = _utc(
                datetime.fromisoformat(expires_at.replace("Z", "+00:00")),
                "expires_at",
            )
        except ValueError as exc:
            raise ConversationCollectionError("invalid_review_bundle_manifest") from exc
        if current >= expiry:
            raise ConversationCollectionError("review_bundle_expired")
        policy_ready = (
            manifest.get("storage_encryption_verified") is True
            and type(manifest.get("retention_hours")) is int
            and 1 <= manifest["retention_hours"] <= 168
            and manifest.get("deletion_owner_present") is True
        )
        if not policy_ready:
            raise ConversationCollectionError("review_bundle_policy_not_ready")
        if manifest.get("human_reviewed") is True:
            if (
                manifest.get("distribution_ready") is False
                and manifest.get("reviewed_html_sha256") == actual_html_hash
                and manifest.get("reviewer_binding_ref") == reviewer_binding_ref
            ):
                return manifest
            raise ConversationCollectionError("review_bundle_approval_conflict")

        approved = dict(manifest)
        approved.update(
            {
                "human_reviewed": True,
                "distribution_ready": False,
                "distribution_blocked_reason": (
                    "authorized_batch_api_not_implemented"
                ),
                "reviewed_html_sha256": actual_html_hash,
                "reviewer_binding_ref": reviewer_binding_ref,
                "reviewed_at": _iso(current),
            }
        )
        approved_bytes = (
            json.dumps(approved, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        _atomic_private_write(bundle_dir / "manifest.json", approved_bytes)
        return approved
    finally:
        os.close(directory_fd)


def purge_expired_review_bundle(*, bundle_dir: Path, now: datetime) -> bool:
    """Delete exactly one expired, intact private bundle; never follow links."""
    current = _utc(now, "now")
    directory_fd = os.open(
        bundle_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        directory_stat = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or directory_stat.st_uid != os.geteuid()
            or stat.S_IMODE(directory_stat.st_mode) != 0o700
        ):
            raise ConversationCollectionError("review_bundle_not_private")
        names = sorted(os.listdir(directory_fd))
        if names != ["manifest.json", "review.html"]:
            raise ConversationCollectionError("review_bundle_inventory_mismatch")
        manifest_bytes = _read_private_bundle_file(directory_fd, "manifest.json")
        html_bytes = _read_private_bundle_file(directory_fd, "review.html")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConversationCollectionError("invalid_review_bundle_manifest") from exc
        if not isinstance(manifest, dict):
            raise ConversationCollectionError("invalid_review_bundle_manifest")
        expires_at = manifest.get("expires_at")
        expected_html_hash = manifest.get("html_sha256")
        actual_html_hash = "sha256:" + hashlib.sha256(html_bytes).hexdigest()
        if not isinstance(expires_at, str) or expected_html_hash != actual_html_hash:
            raise ConversationCollectionError("review_bundle_integrity_error")
        try:
            expiry = _utc(
                datetime.fromisoformat(expires_at.replace("Z", "+00:00")),
                "expires_at",
            )
        except ValueError as exc:
            raise ConversationCollectionError("invalid_review_bundle_manifest") from exc
        if current < expiry:
            return False
        os.unlink("review.html", dir_fd=directory_fd)
        os.unlink("manifest.json", dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(bundle_dir)
    return True


def _read_private_bundle_file(directory_fd: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) != 0o600
            or file_stat.st_nlink != 1
        ):
            raise ConversationCollectionError("review_bundle_file_not_private")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def run_approval_cli(
    argv: Sequence[str],
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    parser = argparse.ArgumentParser(
        description="Record privacy review for the exact bytes of an existing bundle."
    )
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--expected-html-sha256", required=True)
    parser.add_argument("--reviewer-binding-ref", required=True)
    args = parser.parse_args(list(argv))
    manifest = approve_private_review_bundle(
        bundle_dir=args.bundle_dir,
        expected_html_sha256=args.expected_html_sha256,
        reviewer_binding_ref=args.reviewer_binding_ref,
        now=now or datetime.now(UTC),
    )
    return {
        "status": "privacy_reviewed",
        "distribution_ready": False,
        "html_sha256": manifest.get("html_sha256"),
        "manifest_path": str(args.bundle_dir / "manifest.json"),
    }


def run_cli(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    collector_factory: Callable[..., ChatwootDailyCollector] = ChatwootDailyCollector,
) -> dict[str, object]:
    parser = argparse.ArgumentParser(
        description="Build a private daily conversation review package from Chatwoot."
    )
    parser.add_argument("--tenant-ref", required=True)
    parser.add_argument("--scope-ref", required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(list(argv))

    required = (
        "CHATWOOT_BASE_URL",
        "CHATWOOT_ACCOUNT_ID",
        "CHATWOOT_INBOX_ID",
        "CHATWOOT_AGENT_BOT_ID",
        "CHATWOOT_API_ACCESS_TOKEN",
        "DAILY_FEEDBACK_PSEUDONYMIZATION_KEY",
        "DAILY_FEEDBACK_REAL_CONVERSATIONS_ENABLED",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED",
        "DAILY_FEEDBACK_RETENTION_HOURS",
        "DAILY_FEEDBACK_DELETION_OWNER",
    )
    missing = [name for name in required if not environ.get(name)]
    if missing:
        raise ValueError("missing_required_environment:" + ",".join(sorted(missing)))
    try:
        account_id = int(environ["CHATWOOT_ACCOUNT_ID"])
        inbox_id = int(environ["CHATWOOT_INBOX_ID"])
        agent_bot_id = int(environ["CHATWOOT_AGENT_BOT_ID"])
        pseudonymization_key = bytes.fromhex(
            environ["DAILY_FEEDBACK_PSEUDONYMIZATION_KEY"]
        )
        retention_hours = int(environ["DAILY_FEEDBACK_RETENTION_HOURS"])
        window_start = datetime.fromisoformat(args.window_start.replace("Z", "+00:00"))
        window_end = datetime.fromisoformat(args.window_end.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_daily_feedback_configuration") from exc
    if len(pseudonymization_key) != 32:
        raise ValueError("invalid_daily_feedback_pseudonymization_key")

    collector = collector_factory(
        base_url=environ["CHATWOOT_BASE_URL"],
        account_id=account_id,
        inbox_id=inbox_id,
        agent_bot_id=agent_bot_id,
        access_token=environ["CHATWOOT_API_ACCESS_TOKEN"],
        pseudonymization_key=pseudonymization_key,
        security_policy=RealConversationSecurityPolicy(
            real_collection_enabled=(
                environ["DAILY_FEEDBACK_REAL_CONVERSATIONS_ENABLED"] == "true"
            ),
            storage_encryption_verified=(
                environ["DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED"] == "true"
            ),
            retention_hours=retention_hours,
            deletion_owner=environ["DAILY_FEEDBACK_DELETION_OWNER"],
        ),
    )
    package = collector.collect(
        tenant_ref=args.tenant_ref,
        scope_ref=args.scope_ref,
        window_start=window_start,
        window_end=window_end,
    )
    bundle = write_private_review_bundle(
        output_dir=args.output_dir,
        package=package,
    )
    return {
        "status": "created",
        "conversation_count": len(package.conversations),
        "distribution_ready": False,
        "html_path": str(bundle.html_path),
        "manifest_path": str(bundle.manifest_path),
    }


def _atomic_private_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


_RELEASE_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")

_AGENT_RELEASE_CONFIDENCES = frozenset(
    {"verified", "open", "misattributed", "no_release"}
)


def _release_lineage_valid(conversation: ReviewConversation) -> bool:
    """El marcador de 'no se sabe', o un digest real con su version."""
    if (
        conversation.release_id == "release_lineage_unavailable"
        and conversation.release_version == 0
    ):
        return True
    return bool(
        _RELEASE_DIGEST_RE.match(conversation.release_id or "")
    ) and conversation.release_version >= 1


def normalize_agent_release(registro: object) -> dict[str, object] | None:
    """Acota lo que devuelve `get_agent_turn_provenance_v1` a lo publicable.

    Devuelve None cuando no hay un release atribuido: ahi la conversacion se
    queda con el marcador de 'no se sabe', que es la verdad. El texto del SOUL no
    entra --- vive en `agent_prompt_releases` y son 19 KB por release --- y
    tampoco el contexto en crudo.
    """
    if not isinstance(registro, dict):
        return None
    digest = registro.get("release_digest")
    ordinal = registro.get("release_ordinal")
    if not isinstance(digest, str) or not _RELEASE_DIGEST_RE.match(digest):
        return None
    if type(ordinal) is not int or ordinal < 1:
        return None
    confidence = registro.get("confidence")
    if confidence not in _AGENT_RELEASE_CONFIDENCES:
        confidence = "open"
    return {
        "release_digest": digest,
        "release_ordinal": ordinal,
        "confidence": confidence,
        "model_requested": _clean_text(registro.get("model_requested"), limit=128),
        "model_answered": _clean_text(registro.get("model_answered"), limit=128),
        "bridge_release": _clean_text(registro.get("bridge_release"), limit=128),
        "context_builder_version": _clean_text(
            registro.get("context_builder_version"), limit=64
        ),
        "context_digest": _clean_text(registro.get("context_digest"), limit=64),
        "turn_occurred_at": _iso_or_none(registro.get("occurred_at")),
    }


def apply_agent_provenance(
    package: DailyReviewPackage, provenance: Mapping[str, object]
) -> DailyReviewPackage:
    """Pone en cada conversacion con que prompt contesto el agente.

    Llena `release_id` y `release_version` --- que existian en el esquema desde
    20260910000100 y nunca se llenaron --- y deja el detalle en
    `context['agent_release']`, incluida la CONFIANZA de la atribucion: si dice
    'misattributed', quien lee el feedback tiene que saber en el momento que esa
    version no es de fiar.
    """
    if package.schema_version != PACKAGE_SCHEMA_V2:
        return package
    conversations: list[ReviewConversation] = []
    for conversation in package.conversations:
        conversation_id = conversation.context.get("chatwoot_conversation_id")
        acotado = normalize_agent_release(
            provenance.get(str(conversation_id)) if conversation_id is not None else None
        )
        if acotado is None:
            conversations.append(conversation)
            continue
        context: dict[str, object] = dict(conversation.context)
        context["agent_release"] = acotado
        conversations.append(
            replace(
                conversation,
                context=context,
                release_id=acotado["release_digest"],
                release_version=acotado["release_ordinal"],
            )
        )
    return replace(package, conversations=tuple(conversations))
