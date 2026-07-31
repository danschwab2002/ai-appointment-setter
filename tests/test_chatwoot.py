import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from bridge.chatwoot import ChatwootClient, ChatwootProtocolError


ALLOWED_JID = "12025550123@s.whatsapp.net"


class AuthorizedConversationTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/conversations/2"):
            return httpx.Response(
                200,
                json={
                    "id": 2,
                    "meta": {"sender": {"identifier": ALLOWED_JID}},
                },
            )
        return await self._inner.handle_async_request(request)


def test_sends_an_idempotent_agent_bot_reply_after_authorization(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    delivery_id = "delivery-123"
    reply_hash = hashlib.sha256(b"2:10").hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"payload": []})
        if request.method == "GET" and request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "payload": [
                        {
                            "id": 10,
                            "message_type": 0,
                            "private": False,
                            "content": "Hola",
                            "sender": {"type": "contact", "id": 20},
                        }
                    ]
                },
            )
        assert request.method == "POST"
        assert request.headers["api_access_token"] == "agent-bot-token"
        assert json.loads(request.content) == {
            "content": "¡Hola! Soy el asistente virtual de Dan. ¿Cómo te llamás?",
            "message_type": "outgoing",
            "private": False,
            "content_type": "text",
            "content_attributes": {
                "appointment_setter_reply_hash": reply_hash,
            },
        }
        return httpx.Response(
            200,
            json={
                "id": 11,
                "conversation_id": 2,
                "message_type": 1,
                "private": False,
                "content": "¡Hola! Soy el asistente virtual de Dan. ¿Cómo te llamás?",
                "content_attributes": {
                    "appointment_setter_reply_hash": reply_hash,
                },
                "sender": {"type": "agent_bot", "id": 1},
            },
        )

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=AuthorizedConversationTransport(httpx.MockTransport(handler)),
    )

    result = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=10,
            delivery_id=delivery_id,
            content="¡Hola! Soy el asistente virtual de Dan. ¿Cómo te llamás?",
        )
    )

    assert result == {"status": "sent", "message_id": 11}
    assert [request.method for request in requests] == [
        "GET",
        "GET",
        "GET",
        "GET",
        "POST",
    ]


def test_does_not_send_an_agent_bot_reply_when_automation_is_paused(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path.endswith("/labels")
        return httpx.Response(200, json={"payload": ["automation_paused"]})

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=AuthorizedConversationTransport(httpx.MockTransport(handler)),
    )

    result = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=10,
            delivery_id="paused-delivery",
            content="No debe enviarse",
        )
    )

    assert result == {"status": "blocked", "reason": "automation_paused"}
    assert len(requests) == 1


def test_does_not_send_when_the_canonical_conversation_has_another_jid(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path.endswith("/conversations/2")
        return httpx.Response(
            200,
            json={
                "id": 2,
                "meta": {
                    "sender": {"identifier": "12025550999@s.whatsapp.net"}
                },
            },
        )

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid="12025550123@s.whatsapp.net",
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=10,
            delivery_id="forged-conversation",
            content="No debe enviarse",
        )
    )

    assert result == {"status": "blocked", "reason": "jid_not_authorized"}
    assert len(requests) == 1


def test_does_not_send_when_the_canonical_trigger_is_not_an_incoming_contact_message(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/conversations/2"):
            return httpx.Response(
                200,
                json={
                    "id": 2,
                    "meta": {
                        "sender": {"identifier": "12025550123@s.whatsapp.net"}
                    },
                },
            )
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"payload": []})
        return httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 10,
                        "message_type": 1,
                        "private": False,
                        "content": "No es un mensaje entrante",
                        "sender": {"type": "agent_bot", "id": 1},
                    }
                ]
            },
        )

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid="12025550123@s.whatsapp.net",
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=10,
            delivery_id="wrong-trigger-kind",
            content="No debe enviarse",
        )
    )

    assert result == {"status": "blocked", "reason": "invalid_trigger_message"}
    assert [request.method for request in requests] == ["GET", "GET", "GET"]


def test_does_not_duplicate_an_agent_bot_reply_already_in_chatwoot(
    tmp_path: Path,
) -> None:
    delivery_id = "existing-delivery"
    reply_hash = hashlib.sha256(b"2:10").hexdigest()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"payload": []})
        return httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 10,
                        "message_type": 0,
                        "private": False,
                        "content": "Hola",
                        "sender": {"type": "contact", "id": 20},
                    },
                    {
                        "id": 11,
                        "conversation_id": 2,
                        "message_type": 1,
                        "private": False,
                        "content": "No debe duplicarse",
                        "sender": {"type": "agent_bot", "id": 1},
                        "content_attributes": {
                            "appointment_setter_reply_hash": reply_hash,
                        },
                    },
                ]
            },
        )

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=AuthorizedConversationTransport(httpx.MockTransport(handler)),
    )

    result = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=10,
            delivery_id=delivery_id,
            content="No debe duplicarse",
        )
    )

    assert result == {"status": "duplicate", "message_id": 11}
    assert [request.method for request in requests] == ["GET", "GET"]


def test_does_not_send_a_stale_reply_after_the_conversation_advanced(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"payload": []})
        return httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 10,
                        "message_type": 0,
                        "private": False,
                        "content": "Primero",
                        "sender": {"type": "contact", "id": 20},
                    },
                    {
                        "id": 11,
                        "message_type": 0,
                        "private": False,
                        "sender": {"type": "contact", "id": 20},
                    },
                ]
            },
        )

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=AuthorizedConversationTransport(httpx.MockTransport(handler)),
    )

    result = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=10,
            delivery_id="stale-delivery",
            content="No debe enviarse",
        )
    )

    assert result == {"status": "blocked", "reason": "conversation_advanced"}
    assert [request.method for request in requests] == ["GET", "GET"]


def test_does_not_send_after_a_public_human_reply(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"payload": []})
        return httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 10,
                        "message_type": 0,
                        "private": False,
                        "content": "Hola",
                        "sender": {"type": "contact", "id": 20},
                    },
                    {
                        "id": 11,
                        "message_type": 1,
                        "private": False,
                        "sender": {"type": "user", "id": 30},
                    },
                ]
            },
        )

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=AuthorizedConversationTransport(httpx.MockTransport(handler)),
    )

    result = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=10,
            delivery_id="human-takeover",
            content="No debe enviarse",
        )
    )

    assert result == {"status": "blocked", "reason": "human_intervention"}


def test_reauthorizes_immediately_before_post_when_takeover_happens(
    tmp_path: Path,
) -> None:
    paused = False
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal paused, post_calls
        if request.method == "GET" and request.url.path.endswith("/conversations/2"):
            return httpx.Response(
                200,
                json={
                    "id": 2,
                    "meta": {
                        "sender": {"identifier": "12025550123@s.whatsapp.net"}
                    },
                },
            )
        if request.method == "GET" and request.url.path.endswith("/labels"):
            return httpx.Response(
                200,
                json={"payload": ["automation_paused"] if paused else []},
            )
        if request.method == "GET" and request.url.path.endswith("/messages"):
            response = httpx.Response(
                200,
                json={
                    "payload": [
                        {
                            "id": 10,
                            "message_type": 0,
                            "private": False,
                            "content": "Hola",
                            "sender": {"type": "contact", "id": 20},
                        }
                    ]
                },
            )
            paused = True
            return response
        post_calls += 1
        return httpx.Response(
            200,
            json={
                "id": 11,
                "message_type": 1,
                "private": False,
                "sender": {"type": "agent_bot", "id": 1},
            },
        )

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid="12025550123@s.whatsapp.net",
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=10,
            delivery_id="takeover-race",
            content="No debe enviarse",
        )
    )

    assert result == {"status": "blocked", "reason": "automation_paused"}
    assert post_calls == 0


def test_concurrent_reply_attempts_create_only_one_chatwoot_message(
    tmp_path: Path,
) -> None:
    reply_hash = hashlib.sha256(b"2:10").hexdigest()

    class StatefulTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.post_calls = 0
            self.messages: list[dict[str, object]] = [
                {
                    "id": 10,
                    "message_type": 0,
                    "private": False,
                    "content": "Hola",
                    "sender": {"type": "contact", "id": 20},
                }
            ]

        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            if request.method == "GET" and request.url.path.endswith("/labels"):
                return httpx.Response(200, json={"payload": []})
            if request.method == "GET":
                return httpx.Response(200, json={"payload": list(self.messages)})
            self.post_calls += 1
            await asyncio.sleep(0.02)
            message = {
                "id": 11,
                "conversation_id": 2,
                "message_type": 1,
                "private": False,
                "content": "Una sola respuesta",
                "sender": {"type": "agent_bot", "id": 1},
                "content_attributes": {
                    "appointment_setter_reply_hash": reply_hash,
                },
            }
            self.messages.append(message)
            return httpx.Response(200, json=message)

    transport = StatefulTransport()
    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=AuthorizedConversationTransport(transport),
    )

    async def send_twice() -> tuple[dict[str, object], dict[str, object]]:
        return await asyncio.gather(
            client.send_agent_bot_reply(
                conversation_id=2,
                trigger_message_id=10,
                delivery_id="delivery-A",
                content="Una sola respuesta",
            ),
            client.send_agent_bot_reply(
                conversation_id=2,
                trigger_message_id=10,
                delivery_id="delivery-B",
                content="Una sola respuesta",
            ),
        )

    results = asyncio.run(send_twice())

    assert {result["status"] for result in results} == {"sent", "duplicate"}
    assert transport.post_calls == 1


def test_retry_after_a_lost_response_does_not_duplicate_the_message(
    tmp_path: Path,
) -> None:
    delivery_id = "lost-response"
    reply_hash = hashlib.sha256(b"2:10").hexdigest()

    class LostResponseTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.post_calls = 0
            self.messages: list[dict[str, object]] = [
                {
                    "id": 10,
                    "message_type": 0,
                    "private": False,
                    "content": "Hola",
                    "sender": {"type": "contact", "id": 20},
                }
            ]

        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            if request.method == "GET" and request.url.path.endswith("/labels"):
                return httpx.Response(200, json={"payload": []})
            if request.method == "GET":
                return httpx.Response(200, json={"payload": list(self.messages)})
            self.post_calls += 1
            self.messages.append(
                {
                    "id": 11,
                    "conversation_id": 2,
                    "message_type": 1,
                    "private": False,
                    "content": "Una sola respuesta",
                    "sender": {"type": "agent_bot", "id": 1},
                    "content_attributes": {
                        "appointment_setter_reply_hash": reply_hash,
                    },
                }
            )
            raise httpx.ReadTimeout("response lost", request=request)

    transport = LostResponseTransport()
    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=AuthorizedConversationTransport(transport),
    )

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(
            client.send_agent_bot_reply(
                conversation_id=2,
                trigger_message_id=10,
                delivery_id=delivery_id,
                content="Una sola respuesta",
            )
        )

    retry_result = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=10,
            delivery_id=delivery_id,
            content="Una sola respuesta",
        )
    )

    assert retry_result == {"status": "duplicate", "message_id": 11}
    assert transport.post_calls == 1


def test_rejects_a_created_message_that_does_not_match_the_requested_reply(
    tmp_path: Path,
) -> None:
    reply_hash = hashlib.sha256(b"2:10").hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/conversations/2"):
            return httpx.Response(
                200,
                json={
                    "id": 2,
                    "meta": {
                        "sender": {"identifier": "12025550123@s.whatsapp.net"}
                    },
                },
            )
        if request.method == "GET" and request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"payload": []})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "payload": [
                        {
                            "id": 10,
                            "message_type": 0,
                            "private": False,
                            "content": "Hola",
                            "sender": {"type": "contact", "id": 20},
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "id": 11,
                "conversation_id": 2,
                "message_type": 1,
                "private": True,
                "content": "Respuesta esperada",
                "content_attributes": {
                    "appointment_setter_reply_hash": reply_hash,
                },
                "sender": {"type": "agent_bot", "id": 1},
            },
        )

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid="12025550123@s.whatsapp.net",
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ChatwootProtocolError, match="invalid_agent_bot_message"):
        asyncio.run(
            client.send_agent_bot_reply(
                conversation_id=2,
                trigger_message_id=10,
                delivery_id="invalid-response",
                content="Respuesta esperada",
            )
        )


def test_retry_does_not_accept_an_inconsistent_message_as_a_duplicate(
    tmp_path: Path,
) -> None:
    reply_hash = hashlib.sha256(b"2:10").hexdigest()

    class InconsistentMessageTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.post_calls = 0
            self.messages: list[dict[str, object]] = [
                {
                    "id": 10,
                    "message_type": 0,
                    "private": False,
                    "content": "Hola",
                    "sender": {"type": "contact", "id": 20},
                }
            ]

        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            if request.method == "GET" and request.url.path.endswith("/labels"):
                return httpx.Response(200, json={"payload": []})
            if request.method == "GET":
                return httpx.Response(200, json={"payload": list(self.messages)})
            self.post_calls += 1
            message = {
                "id": 11,
                "conversation_id": 2,
                "message_type": 1,
                "private": False,
                "content": "CONTENIDO DISTINTO",
                "content_attributes": {
                    "appointment_setter_reply_hash": reply_hash,
                },
                "sender": {"type": "agent_bot", "id": 1},
            }
            self.messages.append(message)
            return httpx.Response(200, json=message)

    transport = InconsistentMessageTransport()
    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=AuthorizedConversationTransport(transport),
    )

    with pytest.raises(ChatwootProtocolError, match="invalid_agent_bot_message"):
        asyncio.run(
            client.send_agent_bot_reply(
                conversation_id=2,
                trigger_message_id=10,
                delivery_id="invalid-first",
                content="Respuesta esperada",
            )
        )

    with pytest.raises(ChatwootProtocolError, match="invalid_agent_bot_message"):
        asyncio.run(
            client.send_agent_bot_reply(
                conversation_id=2,
                trigger_message_id=10,
                delivery_id="invalid-retry",
                content="Respuesta esperada",
            )
        )

    assert transport.post_calls == 1


def test_adds_pause_label_without_removing_existing_labels() -> None:
    requests: list[httpx.Request] = []
    label_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal label_reads
        requests.append(request)
        if request.method == "GET":
            label_reads += 1
            labels = (
                ["hot_lead"]
                if label_reads == 1
                else ["hot_lead", "automation_paused"]
            )
            return httpx.Response(200, json={"payload": labels})
        assert request.url.path.endswith("/macros/1/execute")
        assert json.loads(request.content) == {"conversation_ids": [2]}
        return httpx.Response(200)

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        pause_macro_id=1,
        confirmation_attempts=2,
        confirmation_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    changed = asyncio.run(
        client.ensure_conversation_label(
            conversation_id=2,
            label="automation_paused",
        )
    )

    assert changed is True
    assert [request.method for request in requests] == ["GET", "POST", "GET"]
    assert not any(
        request.method == "POST" and request.url.path.endswith("/labels")
        for request in requests
    )
    assert requests[1].headers["api_access_token"] == "control-token"


def test_does_not_update_chatwoot_when_pause_label_already_exists() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"payload": ["hot_lead", "automation_paused"]},
        )

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        transport=httpx.MockTransport(handler),
    )

    changed = asyncio.run(
        client.ensure_conversation_label(
            conversation_id=2,
            label="automation_paused",
        )
    )

    assert changed is False
    assert [request.method for request in requests] == ["GET"]


def test_reads_a_bounded_conversation_history_from_chatwoot() -> None:
    messages = [
        {
            "id": 10,
            "message_type": 0,
            "private": False,
            "content": "Hola",
            "sender": {"type": "contact", "id": 20},
        },
        {
            "id": 11,
            "message_type": 1,
            "private": False,
            "content": "¿Cómo te llamás?",
            "sender": {"type": "agent_bot", "id": 1},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/accounts/1/conversations/2/messages"
        assert request.headers["api_access_token"] == "control-token"
        return httpx.Response(200, json={"meta": {}, "payload": messages})

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        client.get_conversation_messages(conversation_id=2, limit=20)
    )

    assert result == messages
