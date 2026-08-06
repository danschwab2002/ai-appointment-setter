"""Chatwoot control-plane API client."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import httpx


class ChatwootProtocolError(RuntimeError):
    """Raised when Chatwoot returns an unexpected response shape."""


@dataclass(frozen=True)
class CanonicalConversationSnapshot:
    """Bounded, validated facts read directly from Chatwoot."""

    conversation_id: int
    inbox_id: int
    status: str
    can_reply: bool
    labels: tuple[str, ...]
    anchor_found: bool
    inbound_after_anchor: bool
    human_activity_after_anchor: bool
    checkpoint_message_id: int | None
    checkpoint_created_at: int | None
    human_assignee_present: bool = False

    @property
    def automation_paused(self) -> bool:
        return "automation_paused" in self.labels or self.human_assignee_present


class ChatwootClient:
    """Perform deterministic control-plane operations in Chatwoot."""

    def __init__(
        self,
        *,
        base_url: str,
        account_id: int,
        access_token: str,
        allowed_jid: str | None = None,
        agent_bot_access_token: str | None = None,
        agent_bot_id: int | None = None,
        reply_dir: Path | None = None,
        pause_macro_id: int | None = None,
        confirmation_attempts: int = 10,
        confirmation_delay_seconds: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id
        self._access_token = access_token
        self._allowed_jid = allowed_jid
        self._agent_bot_access_token = agent_bot_access_token
        self._agent_bot_id = agent_bot_id
        self._reply_dir = reply_dir
        self._pause_macro_id = pause_macro_id
        self._confirmation_attempts = confirmation_attempts
        self._confirmation_delay_seconds = confirmation_delay_seconds
        self._transport = transport

    async def get_canonical_conversation_snapshot(
        self,
        *,
        conversation_id: int,
        expected_inbox_id: int,
        anchor_message_id: int | None,
        anchor_observed_at_epoch: int | None = None,
        message_limit: int = 100,
    ) -> CanonicalConversationSnapshot:
        """Read and validate current conversation facts from Chatwoot.

        Absence of a requested anchor is represented explicitly; callers must
        fail closed rather than interpreting a bounded history as complete.
        """
        if conversation_id <= 0 or expected_inbox_id <= 0:
            raise ValueError("conversation and inbox IDs must be positive")
        if message_limit < 1 or message_limit > 100:
            raise ValueError("message_limit must be between 1 and 100")

        path = (
            f"/api/v1/accounts/{self._account_id}"
            f"/conversations/{conversation_id}"
        )
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"api_access_token": self._access_token},
            transport=self._transport,
            timeout=15,
        ) as client:
            response = await client.get(path)
            response.raise_for_status()

        try:
            details = response.json()
        except ValueError as exc:
            raise ChatwootProtocolError("invalid_json") from exc
        if not isinstance(details, dict) or details.get("id") != conversation_id:
            raise ChatwootProtocolError("invalid_conversation_payload")
        inbox_id = details.get("inbox_id")
        status = details.get("status")
        can_reply = details.get("can_reply")
        labels = details.get("labels")
        if (
            not isinstance(inbox_id, int)
            or isinstance(inbox_id, bool)
            or inbox_id != expected_inbox_id
            or status not in {"open", "pending", "snoozed", "resolved"}
            or not isinstance(can_reply, bool)
            or not isinstance(labels, list)
            or not all(isinstance(label, str) and label for label in labels)
        ):
            raise ChatwootProtocolError("invalid_conversation_authority")
        if not self._is_authorized_conversation(
            response, conversation_id=conversation_id
        ):
            raise ChatwootProtocolError("conversation_identity_mismatch")
        meta = details.get("meta")
        if not isinstance(meta, dict):
            raise ChatwootProtocolError("invalid_conversation_authority")
        sender = meta.get("sender")
        if not isinstance(sender, dict) or sender.get("blocked") is True:
            raise ChatwootProtocolError("conversation_contact_blocked")
        assignee = meta.get("assignee")
        if assignee is not None and not isinstance(assignee, dict):
            raise ChatwootProtocolError("invalid_conversation_authority")
        human_assignee_present = isinstance(assignee, dict)

        messages, history_boundary_reached = await self._get_canonical_messages(
            conversation_id=conversation_id,
            anchor_message_id=anchor_message_id,
            anchor_observed_at_epoch=anchor_observed_at_epoch,
            limit=message_limit,
        )
        anchor_index: int | None = None
        normalized: list[tuple[int, int, int, bool, object]] = []
        for index, message in enumerate(messages):
            message_id = message.get("id")
            created_at = message.get("created_at")
            message_type = message.get("message_type")
            private = message.get("private")
            if (
                not isinstance(message_id, int)
                or isinstance(message_id, bool)
                or not isinstance(created_at, int)
                or isinstance(created_at, bool)
                or message_type not in (0, 1, 2)
                or not isinstance(private, bool)
            ):
                raise ChatwootProtocolError("invalid_canonical_message")
            normalized.append(
                (message_id, created_at, message_type, private, message.get("sender"))
            )
            if anchor_message_id is not None and message_id == anchor_message_id:
                anchor_index = index

        after_anchor = (
            [
                item for item in normalized
                if anchor_observed_at_epoch is None or item[1] > anchor_observed_at_epoch
            ]
            if anchor_message_id is None
            else (
                normalized[anchor_index + 1 :]
                if anchor_index is not None
                else []
            )
        )
        inbound = any(
            message_type == 0 and not private
            for _, _, message_type, private, _ in after_anchor
        )
        human_activity = any(
            message_type == 1
            and not private
            and isinstance(sender, dict)
            and sender.get("type") != "agent_bot"
            for _, _, message_type, private, sender in after_anchor
        )
        checkpoint = messages[-1] if messages else None
        checkpoint_id = checkpoint.get("id") if checkpoint else None
        checkpoint_at = checkpoint.get("created_at") if checkpoint else None
        if checkpoint_id is not None and not isinstance(checkpoint_id, int):
            raise ChatwootProtocolError("invalid_canonical_message")
        if checkpoint_at is not None and not isinstance(checkpoint_at, int):
            raise ChatwootProtocolError("invalid_canonical_message")
        return CanonicalConversationSnapshot(
            conversation_id=conversation_id,
            inbox_id=inbox_id,
            status=status,
            can_reply=can_reply,
            labels=tuple(sorted(set(labels))),
            anchor_found=(
                anchor_index is not None
                if anchor_message_id is not None
                else history_boundary_reached
            ),
            inbound_after_anchor=inbound,
            human_activity_after_anchor=human_activity,
            checkpoint_message_id=checkpoint_id,
            checkpoint_created_at=checkpoint_at,
            human_assignee_present=human_assignee_present,
        )

    async def _get_canonical_messages(
        self,
        *,
        conversation_id: int,
        anchor_message_id: int | None,
        anchor_observed_at_epoch: int | None,
        limit: int,
    ) -> tuple[list[dict[str, object]], bool]:
        """Page backwards until the anchor boundary is proven or limit is hit."""
        path = (
            f"/api/v1/accounts/{self._account_id}"
            f"/conversations/{conversation_id}/messages"
        )
        messages_by_id: dict[int, dict[str, object]] = {}
        before: int | None = None
        boundary_reached = False

        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"api_access_token": self._access_token},
            transport=self._transport,
            timeout=15,
        ) as client:
            while len(messages_by_id) < limit:
                response = await client.get(
                    path,
                    params={"before": str(before)} if before is not None else None,
                )
                response.raise_for_status()
                page = self._parse_messages(response)
                if not page:
                    boundary_reached = True
                    break

                page_ids: list[int] = []
                page_times: list[int] = []
                added = 0
                for message in page:
                    message_id = message.get("id")
                    created_at = message.get("created_at")
                    if (
                        not isinstance(message_id, int)
                        or isinstance(message_id, bool)
                        or not isinstance(created_at, int)
                        or isinstance(created_at, bool)
                    ):
                        raise ChatwootProtocolError("invalid_canonical_message")
                    page_ids.append(message_id)
                    page_times.append(created_at)
                    if message_id not in messages_by_id:
                        messages_by_id[message_id] = message
                        added += 1

                if anchor_message_id is not None and anchor_message_id in page_ids:
                    boundary_reached = True
                    break
                if (
                    anchor_message_id is None
                    and anchor_observed_at_epoch is not None
                    and min(page_times) <= anchor_observed_at_epoch
                ):
                    boundary_reached = True
                    break
                if len(page) < 20:
                    boundary_reached = True
                    break
                if added == 0:
                    raise ChatwootProtocolError("canonical_history_did_not_advance")
                before = min(page_ids)

        def sort_key(item: tuple[int, dict[str, object]]) -> tuple[int, int]:
            message_id, message = item
            created_at = message.get("created_at")
            if not isinstance(created_at, int) or isinstance(created_at, bool):
                raise ChatwootProtocolError("invalid_canonical_message")
            return created_at, message_id

        ordered = [
            message
            for _, message in sorted(messages_by_id.items(), key=sort_key)
        ]
        return ordered[-limit:], boundary_reached

    async def send_agent_bot_reply(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        delivery_id: str,
        content: str,
    ) -> dict[str, object]:
        """Authorize and send one public AgentBot reply for an incoming event."""
        if (
            self._agent_bot_access_token is None
            or self._agent_bot_id is None
            or self._reply_dir is None
            or self._allowed_jid is None
        ):
            raise ChatwootProtocolError("agent_bot_reply_not_configured")
        reply_hash = hashlib.sha256(
            f"{conversation_id}:{trigger_message_id}".encode("utf-8")
        ).hexdigest()
        self._reply_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._reply_dir.chmod(0o700)
        lock_path = self._reply_dir / f".{reply_hash}.processing.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(lock_fd, 0o600)
        try:
            await self._acquire_lock(lock_fd)
            return await self._authorize_and_send(
                conversation_id=conversation_id,
                trigger_message_id=trigger_message_id,
                reply_hash=reply_hash,
                content=content,
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    @staticmethod
    async def _acquire_lock(lock_fd: int) -> None:
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                await asyncio.sleep(0.01)

    async def _authorize_and_send(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        reply_hash: str,
        content: str,
    ) -> dict[str, object]:
        agent_bot_access_token = self._agent_bot_access_token
        if agent_bot_access_token is None:
            raise ChatwootProtocolError("agent_bot_reply_not_configured")
        conversation_path = (
            f"/api/v1/accounts/{self._account_id}"
            f"/conversations/{conversation_id}"
        )
        labels_path = (
            f"/api/v1/accounts/{self._account_id}"
            f"/conversations/{conversation_id}/labels"
        )
        messages_path = (
            f"/api/v1/accounts/{self._account_id}"
            f"/conversations/{conversation_id}/messages"
        )
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"api_access_token": self._access_token},
            transport=self._transport,
            timeout=15,
        ) as control_client:
            authorization_result = await self._current_authorization_result(
                control_client=control_client,
                conversation_path=conversation_path,
                labels_path=labels_path,
                messages_path=messages_path,
                conversation_id=conversation_id,
                trigger_message_id=trigger_message_id,
                reply_hash=reply_hash,
                content=content,
            )
        if authorization_result is not None:
            return authorization_result

        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"api_access_token": self._access_token},
            transport=self._transport,
            timeout=15,
        ) as final_client:
            authorization_result = await self._current_authorization_result(
                control_client=final_client,
                conversation_path=conversation_path,
                labels_path=labels_path,
                messages_path=messages_path,
                conversation_id=conversation_id,
                trigger_message_id=trigger_message_id,
                reply_hash=reply_hash,
                content=content,
            )
            if authorization_result is not None:
                return authorization_result
            response = await final_client.post(
                messages_path,
                headers={"api_access_token": agent_bot_access_token},
                json={
                    "content": content,
                    "message_type": "outgoing",
                    "private": False,
                    "content_type": "text",
                    "content_attributes": {
                        "appointment_setter_reply_hash": reply_hash,
                    },
                },
            )
            response.raise_for_status()
        try:
            message = response.json()
        except ValueError as exc:
            raise ChatwootProtocolError("invalid_json") from exc
        sender = message.get("sender") if isinstance(message, dict) else None
        message_id = message.get("id") if isinstance(message, dict) else None
        attributes = (
            message.get("content_attributes") if isinstance(message, dict) else None
        )
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message.get("conversation_id") != conversation_id
            or message.get("message_type") != 1
            or message.get("private") is not False
            or message.get("content") != content
            or not isinstance(attributes, dict)
            or attributes.get("appointment_setter_reply_hash") != reply_hash
            or not isinstance(sender, dict)
            or sender.get("type") != "agent_bot"
            or sender.get("id") != self._agent_bot_id
        ):
            raise ChatwootProtocolError("invalid_agent_bot_message")
        return {"status": "sent", "message_id": message_id}

    async def _current_authorization_result(
        self,
        *,
        control_client: httpx.AsyncClient,
        conversation_path: str,
        labels_path: str,
        messages_path: str,
        conversation_id: int,
        trigger_message_id: int,
        reply_hash: str,
        content: str,
    ) -> dict[str, object] | None:
        conversation_response = await control_client.get(conversation_path)
        conversation_response.raise_for_status()
        if not self._is_authorized_conversation(
            conversation_response,
            conversation_id=conversation_id,
        ):
            return {"status": "blocked", "reason": "jid_not_authorized"}

        labels_response = await control_client.get(labels_path)
        labels_response.raise_for_status()
        if "automation_paused" in self._parse_labels(labels_response):
            return {"status": "blocked", "reason": "automation_paused"}

        messages_response = await control_client.get(messages_path)
        messages_response.raise_for_status()
        messages = self._parse_messages(messages_response)
        trigger_index = next(
            (
                index
                for index, message in enumerate(messages)
                if message.get("id") == trigger_message_id
            ),
            None,
        )
        if trigger_index is None:
            raise ChatwootProtocolError("trigger_message_not_found")
        trigger_message = messages[trigger_index]
        trigger_sender = trigger_message.get("sender")
        trigger_content = trigger_message.get("content")
        if not (
            trigger_message.get("private") is False
            and trigger_message.get("message_type") == 0
            and isinstance(trigger_sender, dict)
            and trigger_sender.get("type") == "contact"
            and isinstance(trigger_content, str)
            and bool(trigger_content.strip())
        ):
            return {"status": "blocked", "reason": "invalid_trigger_message"}

        for message in messages:
            attributes = message.get("content_attributes")
            sender = message.get("sender")
            if (
                isinstance(attributes, dict)
                and attributes.get("appointment_setter_reply_hash") == reply_hash
                and isinstance(sender, dict)
                and sender.get("type") == "agent_bot"
                and sender.get("id") == self._agent_bot_id
            ):
                message_id = message.get("id")
                if not (
                    isinstance(message_id, int)
                    and not isinstance(message_id, bool)
                    and message.get("conversation_id") == conversation_id
                    and message.get("message_type") == 1
                    and message.get("private") is False
                    and message.get("content") == content
                ):
                    raise ChatwootProtocolError("invalid_agent_bot_message")
                return {"status": "duplicate", "message_id": message_id}

        for message in messages[trigger_index + 1 :]:
            if message.get("private") is not False:
                continue
            sender = message.get("sender")
            if (
                message.get("message_type") == 1
                and isinstance(sender, dict)
                and sender.get("type") != "agent_bot"
            ):
                return {"status": "blocked", "reason": "human_intervention"}
            return {"status": "blocked", "reason": "conversation_advanced"}
        return None

    def _is_authorized_conversation(
        self,
        response: httpx.Response,
        *,
        conversation_id: int,
    ) -> bool:
        try:
            conversation = response.json()
        except ValueError as exc:
            raise ChatwootProtocolError("invalid_json") from exc
        if not isinstance(conversation, dict) or conversation.get("id") != conversation_id:
            raise ChatwootProtocolError("invalid_conversation_payload")
        meta = conversation.get("meta")
        sender = meta.get("sender") if isinstance(meta, dict) else None
        identifier = sender.get("identifier") if isinstance(sender, dict) else None
        if not isinstance(identifier, str):
            contact_inbox = conversation.get("contact_inbox")
            identifier = (
                contact_inbox.get("source_id")
                if isinstance(contact_inbox, dict)
                else None
            )
        return isinstance(identifier, str) and identifier == self._allowed_jid

    async def get_conversation_messages(
        self, *, conversation_id: int, limit: int = 20
    ) -> list[dict[str, object]]:
        """Read a bounded canonical conversation history from Chatwoot."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        path = (
            f"/api/v1/accounts/{self._account_id}"
            f"/conversations/{conversation_id}/messages"
        )
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"api_access_token": self._access_token},
            transport=self._transport,
            timeout=15,
        ) as client:
            response = await client.get(path)
            response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ChatwootProtocolError("invalid_json") from exc
        messages = payload.get("payload") if isinstance(payload, dict) else None
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) for message in messages
        ):
            raise ChatwootProtocolError("invalid_messages_payload")
        return messages[-limit:]

    async def ensure_conversation_label(
        self, *, conversation_id: int, label: str
    ) -> bool:
        """Ensure a label exists, returning whether Chatwoot was changed."""
        path = (
            f"/api/v1/accounts/{self._account_id}"
            f"/conversations/{conversation_id}/labels"
        )
        headers = {"api_access_token": self._access_token}
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            transport=self._transport,
            timeout=15,
        ) as client:
            response = await client.get(path)
            response.raise_for_status()
            labels = self._parse_labels(response)
            if label in labels:
                return False

            if self._pause_macro_id is None:
                raise ChatwootProtocolError("pause_macro_not_configured")
            macro_path = (
                f"/api/v1/accounts/{self._account_id}"
                f"/macros/{self._pause_macro_id}/execute"
            )
            response = await client.post(
                macro_path,
                json={"conversation_ids": [conversation_id]},
            )
            response.raise_for_status()

            for _ in range(self._confirmation_attempts):
                if self._confirmation_delay_seconds > 0:
                    await asyncio.sleep(self._confirmation_delay_seconds)
                response = await client.get(path)
                response.raise_for_status()
                if label in self._parse_labels(response):
                    return True

            raise ChatwootProtocolError("macro_label_not_confirmed")

    @staticmethod
    def _parse_labels(response: httpx.Response) -> list[str]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ChatwootProtocolError("invalid_json") from exc
        labels = payload.get("payload") if isinstance(payload, dict) else None
        if not isinstance(labels, list) or not all(
            isinstance(label, str) for label in labels
        ):
            raise ChatwootProtocolError("invalid_labels_payload")
        return labels

    @staticmethod
    def _parse_messages(response: httpx.Response) -> list[dict[str, object]]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ChatwootProtocolError("invalid_json") from exc
        messages = payload.get("payload") if isinstance(payload, dict) else None
        if not isinstance(messages, list) or not all(
            isinstance(message, dict) for message in messages
        ):
            raise ChatwootProtocolError("invalid_messages_payload")
        return messages

    # ── Contact and conversation creation (recovery first-touch) ───

    async def find_contact_by_phone(
        self,
        *,
        inbox_id: int,
        phone_number: str,
    ) -> int | None:
        """Return one exact, unblocked contact linked to the target inbox."""
        if (
            not isinstance(inbox_id, int)
            or isinstance(inbox_id, bool)
            or inbox_id <= 0
        ):
            raise ChatwootProtocolError("invalid_inbox_id")
        path = f"/api/v1/accounts/{self._account_id}/contacts/search"
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"api_access_token": self._access_token},
            transport=self._transport,
            timeout=15,
        ) as client:
            response = await client.get(path, params={"q": phone_number})
            response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise ChatwootProtocolError("invalid_json") from exc
        if not isinstance(body, dict) or not isinstance(body.get("payload"), list):
            raise ChatwootProtocolError("invalid_contact_search_payload")

        exact_matches: dict[int, tuple[bool, bool]] = {}
        for contact in body["payload"]:
            if not isinstance(contact, dict):
                raise ChatwootProtocolError("invalid_contact_search_payload")
            contact_id = contact.get("id")
            candidate_phone = contact.get("phone_number")
            blocked = contact.get("blocked")
            contact_inboxes = contact.get("contact_inboxes")
            if (
                not isinstance(contact_id, int)
                or isinstance(contact_id, bool)
                or contact_id <= 0
                or not isinstance(candidate_phone, str)
                or not candidate_phone
                or not isinstance(blocked, bool)
                or not isinstance(contact_inboxes, list)
            ):
                raise ChatwootProtocolError("invalid_contact_search_result")
            linked_inbox_ids: set[int] = set()
            for contact_inbox in contact_inboxes:
                inbox = (
                    contact_inbox.get("inbox")
                    if isinstance(contact_inbox, dict)
                    else None
                )
                linked_id = inbox.get("id") if isinstance(inbox, dict) else None
                if (
                    not isinstance(linked_id, int)
                    or isinstance(linked_id, bool)
                    or linked_id <= 0
                ):
                    raise ChatwootProtocolError("invalid_contact_search_result")
                linked_inbox_ids.add(linked_id)
            if candidate_phone != phone_number:
                continue
            previous_blocked, previous_linked = exact_matches.get(
                contact_id, (False, False)
            )
            exact_matches[contact_id] = (
                previous_blocked or blocked,
                previous_linked or inbox_id in linked_inbox_ids,
            )

        if len(exact_matches) > 1:
            raise ChatwootProtocolError("ambiguous_contact_match")
        if not exact_matches:
            return None
        contact_id, (blocked, linked_to_inbox) = next(iter(exact_matches.items()))
        if blocked:
            raise ChatwootProtocolError("contact_blocked")
        if not linked_to_inbox:
            raise ChatwootProtocolError("contact_not_linked_to_inbox")
        return contact_id

    async def create_contact(
        self,
        *,
        inbox_id: int,
        name: str | None,
        phone_number: str,
        email: str | None = None,
    ) -> int:
        """Create a contact in Chatwoot and return its numeric ID.

        ``phone_number`` must be in E.164 format (``+`` + DDI + number).
        """
        path = f"/api/v1/accounts/{self._account_id}/contacts"
        body_dict: dict[str, object] = {
            "inbox_id": inbox_id,
            "phone_number": phone_number,
            "blocked": False,
        }
        if name is not None:
            body_dict["name"] = name
        if email is not None:
            body_dict["email"] = email
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"api_access_token": self._access_token},
            transport=self._transport,
            timeout=15,
        ) as client:
            response = await client.post(path, json=body_dict)
            response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ChatwootProtocolError("invalid_json") from exc
        if not isinstance(payload, dict):
            raise ChatwootProtocolError("invalid_contact_payload")
        contact_id = payload.get("id")
        embedded = payload.get("payload")
        if not isinstance(contact_id, int) or isinstance(contact_id, bool):
            if isinstance(embedded, dict):
                contact_id = embedded.get("id")
            elif (
                isinstance(embedded, list)
                and embedded
                and isinstance(embedded[0], dict)
            ):
                contact_id = embedded[0].get("id")
        if not isinstance(contact_id, int) or isinstance(contact_id, bool):
            raise ChatwootProtocolError("invalid_contact_id")
        return contact_id

    async def create_conversation(
        self,
        *,
        inbox_id: int,
        contact_id: int,
    ) -> int:
        """Create a conversation for a contact in an inbox.

        Returns the numeric conversation ID.
        """
        path = f"/api/v1/accounts/{self._account_id}/conversations"
        body: dict[str, object] = {
            "inbox_id": inbox_id,
            "contact_id": contact_id,
        }
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"api_access_token": self._access_token},
            transport=self._transport,
            timeout=15,
        ) as client:
            response = await client.post(path, json=body)
            response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ChatwootProtocolError("invalid_json") from exc
        if not isinstance(payload, dict):
            raise ChatwootProtocolError("invalid_conversation_payload")
        conversation = payload
        conversation_id = conversation.get("id")
        if not isinstance(conversation_id, int) or isinstance(
            conversation_id, bool
        ):
            raise ChatwootProtocolError("invalid_conversation_id")
        return conversation_id

    async def send_first_message(
        self,
        *,
        conversation_id: int,
        content: str,
        delivery_id: str,
    ) -> dict[str, object]:
        """Send the first outbound message to a conversation via AgentBot.

        Unlike ``send_agent_bot_reply``, this does not require a trigger
        message — it initiates the conversation.
        """
        if (
            self._agent_bot_access_token is None
            or self._agent_bot_id is None
        ):
            raise ChatwootProtocolError("agent_bot_not_configured")

        reply_hash = hashlib.sha256(
            f"first:{conversation_id}:{delivery_id}".encode("utf-8")
        ).hexdigest()
        messages_path = (
            f"/api/v1/accounts/{self._account_id}"
            f"/conversations/{conversation_id}/messages"
        )
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=15,
        ) as client:
            response = await client.post(
                messages_path,
                headers={"api_access_token": self._agent_bot_access_token},
                json={
                    "content": content,
                    "message_type": "outgoing",
                    "private": False,
                    "content_type": "text",
                    "content_attributes": {
                        "recovery_first_touch_hash": reply_hash,
                    },
                },
            )
            response.raise_for_status()
        try:
            message = response.json()
        except ValueError as exc:
            raise ChatwootProtocolError("invalid_json") from exc
        if not isinstance(message, dict):
            raise ChatwootProtocolError("invalid_message_payload")
        message_id = message.get("id")
        attributes = message.get("content_attributes")
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
            or message.get("conversation_id") != conversation_id
            or message.get("message_type") != 1
            or message.get("private") is not False
            or message.get("content") != content
            or not isinstance(attributes, dict)
            or attributes.get("recovery_first_touch_hash") != reply_hash
        ):
            raise ChatwootProtocolError("invalid_sent_message")
        return {"status": "sent", "message_id": message_id}

    async def send_followup_message(
        self,
        *,
        conversation_id: int,
        content: str,
        delivery_id: str,
    ) -> dict[str, object]:
        """Send one durable follow-up into an existing conversation."""
        if self._agent_bot_access_token is None or self._agent_bot_id is None:
            raise ChatwootProtocolError("agent_bot_not_configured")
        if (
            not isinstance(conversation_id, int)
            or isinstance(conversation_id, bool)
            or conversation_id <= 0
        ):
            raise ChatwootProtocolError("invalid_conversation_id")

        followup_hash = hashlib.sha256(
            f"followup:{delivery_id}".encode("utf-8")
        ).hexdigest()
        messages_path = (
            f"/api/v1/accounts/{self._account_id}"
            f"/conversations/{conversation_id}/messages"
        )
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=15,
        ) as client:
            response = await client.post(
                messages_path,
                headers={"api_access_token": self._agent_bot_access_token},
                json={
                    "content": content,
                    "message_type": "outgoing",
                    "private": False,
                    "content_type": "text",
                    "content_attributes": {
                        "recovery_followup_hash": followup_hash,
                    },
                },
            )
            response.raise_for_status()
        try:
            message = response.json()
        except ValueError as exc:
            raise ChatwootProtocolError("invalid_json") from exc
        if not isinstance(message, dict):
            raise ChatwootProtocolError("invalid_message_payload")
        message_id = message.get("id")
        attributes = message.get("content_attributes")
        sender = message.get("sender")
        sender_id = sender.get("id") if isinstance(sender, dict) else None
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
            or message.get("conversation_id") != conversation_id
            or message.get("message_type") != 1
            or message.get("private") is not False
            or message.get("content") != content
            or not isinstance(attributes, dict)
            or attributes.get("recovery_followup_hash") != followup_hash
            or not isinstance(sender, dict)
            or sender.get("type") != "agent_bot"
            or not isinstance(sender_id, int)
            or isinstance(sender_id, bool)
            or sender_id != self._agent_bot_id
        ):
            raise ChatwootProtocolError("invalid_sent_message")
        return {"status": "sent", "message_id": message_id}
