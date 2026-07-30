"""Chatwoot control-plane API client."""

from __future__ import annotations

import asyncio

import httpx


class ChatwootProtocolError(RuntimeError):
    """Raised when Chatwoot returns an unexpected response shape."""


class ChatwootClient:
    """Perform deterministic control-plane operations in Chatwoot."""

    def __init__(
        self,
        *,
        base_url: str,
        account_id: int,
        access_token: str,
        pause_macro_id: int | None = None,
        confirmation_attempts: int = 10,
        confirmation_delay_seconds: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id
        self._access_token = access_token
        self._pause_macro_id = pause_macro_id
        self._confirmation_attempts = confirmation_attempts
        self._confirmation_delay_seconds = confirmation_delay_seconds
        self._transport = transport

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
