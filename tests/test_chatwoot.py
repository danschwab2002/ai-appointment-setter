import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from bridge.chatwoot import (
    ChatwootClient,
    ChatwootHistoryScanLimitError,
    ChatwootProtocolError,
)


ALLOWED_JID = "12025550123@s.whatsapp.net"


def test_authorizes_waba_e164_phone_number_for_the_configured_jid(
    tmp_path: Path,
) -> None:
    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
    )
    response = httpx.Response(
        200,
        json={
            "id": 39,
            "meta": {
                "sender": {
                    "identifier": None,
                    "phone_number": "+12025550123",
                }
            },
        },
    )

    assert client._is_authorized_conversation(response, conversation_id=39) is True


def test_rejects_a_different_waba_digit_source_id(tmp_path: Path) -> None:
    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
    )
    response = httpx.Response(
        200,
        json={
            "id": 39,
            "meta": {"sender": {}},
            "contact_inbox": {"source_id": "12025550124"},
        },
    )

    assert client._is_authorized_conversation(response, conversation_id=39) is False


def test_authorizes_conversation_against_operation_scoped_jid(tmp_path: Path) -> None:
    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
    )
    response = httpx.Response(
        200,
        json={
            "id": 39,
            "meta": {"sender": {"identifier": "12025550999"}},
        },
    )

    assert client._is_authorized_conversation(
        response,
        conversation_id=39,
        expected_jid="12025550999@s.whatsapp.net",
    ) is True
    assert client._is_authorized_conversation(
        response,
        conversation_id=39,
        expected_jid="12025550888@s.whatsapp.net",
    ) is False


class AuthorizedConversationTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        authorized_jid: str = ALLOWED_JID,
    ) -> None:
        self._inner = inner
        self._authorized_jid = authorized_jid

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/conversations/2"):
            return httpx.Response(
                200,
                json={
                    "id": 2,
                    "meta": {"sender": {"identifier": self._authorized_jid}},
                },
            )
        return await self._inner.handle_async_request(request)


def test_sends_an_idempotent_agent_bot_reply_after_authorization(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    delivery_id = "delivery-123"
    reply_hash = hashlib.sha256(b"2:10").hexdigest()
    scoped_jid = "12025550999@s.whatsapp.net"

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
        transport=AuthorizedConversationTransport(
            httpx.MockTransport(handler),
            authorized_jid=scoped_jid,
        ),
    )

    result = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=10,
            delivery_id=delivery_id,
            content="¡Hola! Soy el asistente virtual de Dan. ¿Cómo te llamás?",
            expected_jid=scoped_jid,
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


def test_sends_the_next_part_after_prior_parts_from_the_same_reply_batch(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    batch_hash = hashlib.sha256(b"2:10").hexdigest()
    first_hash = hashlib.sha256(f"{batch_hash}:1:2".encode()).hexdigest()
    second_hash = hashlib.sha256(f"{batch_hash}:2:2".encode()).hexdigest()

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
                            "conversation_id": 2,
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
                            "content": "Primera parte.",
                            "content_attributes": {
                                "appointment_setter_reply_hash": first_hash,
                                "appointment_setter_reply_batch_hash": batch_hash,
                                "appointment_setter_reply_part_index": 1,
                                "appointment_setter_reply_part_count": 2,
                            },
                            "sender": {"type": "agent_bot", "id": 1},
                        },
                    ]
                },
            )
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["content"] == "Segunda parte."
        assert body["content_attributes"] == {
            "appointment_setter_reply_hash": second_hash,
            "appointment_setter_reply_batch_hash": batch_hash,
            "appointment_setter_reply_part_index": 2,
            "appointment_setter_reply_part_count": 2,
        }
        return httpx.Response(
            200,
            json={
                "id": 12,
                "conversation_id": 2,
                "message_type": 1,
                "private": False,
                "content": "Segunda parte.",
                "content_attributes": body["content_attributes"],
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
            delivery_id="multipart-delivery",
            content="Segunda parte.",
            part_index=2,
            part_count=2,
            prior_parts=("Primera parte.",),
        )
    )

    assert result == {"status": "sent", "message_id": 12}


def test_blocks_duplicate_or_out_of_order_prior_reply_parts(tmp_path: Path) -> None:
    batch_hash = hashlib.sha256(b"2:10").hexdigest()

    def part_message(message_id: int, part_index: int) -> dict[str, object]:
        part_hash = hashlib.sha256(
            f"{batch_hash}:{part_index}:3".encode()
        ).hexdigest()
        return {
            "id": message_id,
            "conversation_id": 2,
            "message_type": 1,
            "private": False,
            "content": ("Primera parte.", "Segunda parte.")[part_index - 1],
            "content_attributes": {
                "appointment_setter_reply_hash": part_hash,
                "appointment_setter_reply_batch_hash": batch_hash,
                "appointment_setter_reply_part_index": part_index,
                "appointment_setter_reply_part_count": 3,
            },
            "sender": {"type": "agent_bot", "id": 1},
        }

    trigger = {
        "id": 10,
        "conversation_id": 2,
        "message_type": 0,
        "private": False,
        "content": "Hola",
        "sender": {"type": "contact", "id": 20},
    }
    cases = (
        (part_message(11, 1), part_message(12, 1), part_message(13, 2)),
        (part_message(11, 2), part_message(12, 1)),
    )

    for case_index, previous_parts in enumerate(cases):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET" and request.url.path.endswith("/labels"):
                return httpx.Response(200, json={"payload": []})
            if request.method == "GET" and request.url.path.endswith("/messages"):
                return httpx.Response(
                    200,
                    json={"payload": [trigger, *previous_parts]},
                )
            raise AssertionError("an invalid prior-part sequence must not POST")

        client = ChatwootClient(
            base_url="https://chatwoot.example.test",
            account_id=1,
            access_token="control-token",
            allowed_jid=ALLOWED_JID,
            agent_bot_access_token="agent-bot-token",
            agent_bot_id=1,
            reply_dir=tmp_path / str(case_index),
            transport=AuthorizedConversationTransport(httpx.MockTransport(handler)),
        )

        result = asyncio.run(
            client.send_agent_bot_reply(
                conversation_id=2,
                trigger_message_id=10,
                delivery_id=f"invalid-sequence-{case_index}",
                content="Tercera parte.",
                part_index=3,
                part_count=3,
                prior_parts=("Primera parte.", "Segunda parte."),
            )
        )

        assert result == {
            "status": "blocked",
            "reason": "reply_sequence_incomplete",
        }


def test_reconciles_a_multipart_reply_marker_beyond_the_first_history_page(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    batch_hash = hashlib.sha256(b"2:10").hexdigest()
    first_hash = hashlib.sha256(f"{batch_hash}:1:2".encode()).hexdigest()
    second_hash = hashlib.sha256(f"{batch_hash}:2:2".encode()).hexdigest()
    later_private_messages = [
        {
            "id": message_id,
            "conversation_id": 2,
            "message_type": 1,
            "private": True,
            "content": "private",
            "sender": {"type": "user", "id": 99},
        }
        for message_id in range(100, 120)
    ]
    older_page = [
        {
            "id": 10,
            "conversation_id": 2,
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
            "content": "Primera parte.",
            "content_attributes": {
                "appointment_setter_reply_hash": first_hash,
                "appointment_setter_reply_batch_hash": batch_hash,
                "appointment_setter_reply_part_index": 1,
                "appointment_setter_reply_part_count": 2,
            },
            "sender": {"type": "agent_bot", "id": 1},
        },
        {
            "id": 12,
            "conversation_id": 2,
            "message_type": 1,
            "private": False,
            "content": "Segunda parte.",
            "content_attributes": {
                "appointment_setter_reply_hash": second_hash,
                "appointment_setter_reply_batch_hash": batch_hash,
                "appointment_setter_reply_part_index": 2,
                "appointment_setter_reply_part_count": 2,
            },
            "sender": {"type": "agent_bot", "id": 1},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"payload": []})
        if request.method == "GET" and request.url.path.endswith("/messages"):
            before = request.url.params.get("before")
            return httpx.Response(
                200,
                json={
                    "payload": older_page if before == "100" else later_private_messages
                },
            )
        raise AssertionError("an accepted marker must reconcile without POST")

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
            delivery_id="multipart-replay",
            content="Segunda parte.",
            part_index=2,
            part_count=2,
            prior_parts=("Primera parte.",),
        )
    )

    assert result == {"status": "duplicate", "message_id": 12}
    message_gets = [
        request for request in requests if request.url.path.endswith("/messages")
    ]
    assert len(message_gets) == 2
    assert message_gets[1].url.params.get("before") == "100"


def test_blocks_remaining_parts_when_the_contact_replies_between_them(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    batch_hash = hashlib.sha256(b"2:10").hexdigest()
    first_hash = hashlib.sha256(f"{batch_hash}:1:2".encode()).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"payload": []})
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={
                    "payload": [
                        {
                            "id": 10,
                            "conversation_id": 2,
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
                            "content": "Primera parte.",
                            "content_attributes": {
                                "appointment_setter_reply_hash": first_hash,
                                "appointment_setter_reply_batch_hash": batch_hash,
                                "appointment_setter_reply_part_index": 1,
                                "appointment_setter_reply_part_count": 2,
                            },
                            "sender": {"type": "agent_bot", "id": 1},
                        },
                        {
                            "id": 12,
                            "conversation_id": 2,
                            "message_type": 0,
                            "private": False,
                            "content": "Una duda",
                            "sender": {"type": "contact", "id": 20},
                        },
                    ]
                },
            )
        raise AssertionError("must not POST after the contact advances the conversation")

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
            delivery_id="multipart-delivery",
            content="Segunda parte.",
            part_index=2,
            part_count=2,
            prior_parts=("Primera parte.",),
        )
    )

    assert result == {"status": "blocked", "reason": "conversation_advanced"}
    assert not any(request.method == "POST" for request in requests)


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


def test_retry_does_not_repost_while_an_accepted_marker_is_still_invisible(
    tmp_path: Path,
) -> None:
    class DelayedMarkerTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.post_calls = 0

        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
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
            self.post_calls += 1
            raise httpx.ReadTimeout("response lost", request=request)

    transport = DelayedMarkerTransport()
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
                delivery_id="delayed-marker",
                content="Una sola respuesta",
            )
        )

    restarted_client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=AuthorizedConversationTransport(transport),
    )
    with pytest.raises(ChatwootProtocolError, match="reply_delivery_unknown"):
        asyncio.run(
            restarted_client.send_agent_bot_reply(
                conversation_id=2,
                trigger_message_id=10,
                delivery_id="delayed-marker",
                content="Una sola respuesta",
            )
        )

    assert transport.post_calls == 1


def test_legacy_one_part_journal_blocks_new_multipart_geometry(
    tmp_path: Path,
) -> None:
    batch_hash = hashlib.sha256(b"2:10").hexdigest()
    journal_path = tmp_path / f".{batch_hash}.posting"
    journal_path.write_text("posting\n", encoding="utf-8")
    journal_path.chmod(0o600)

    class InvisibleLegacyMarkerTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.post_calls = 0

        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
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
            self.post_calls += 1
            part_hash = hashlib.sha256(f"{batch_hash}:1:2".encode()).hexdigest()
            return httpx.Response(
                200,
                json={
                    "id": 11,
                    "conversation_id": 2,
                    "message_type": 1,
                    "private": False,
                    "content": "Primera parte.",
                    "content_attributes": {
                        "appointment_setter_reply_hash": part_hash,
                        "appointment_setter_reply_batch_hash": batch_hash,
                        "appointment_setter_reply_part_index": 1,
                        "appointment_setter_reply_part_count": 2,
                    },
                    "sender": {"type": "agent_bot", "id": 1},
                },
            )

    transport = InvisibleLegacyMarkerTransport()
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

    with pytest.raises(ChatwootProtocolError, match="reply_delivery_unknown"):
        asyncio.run(
            client.send_agent_bot_reply(
                conversation_id=2,
                trigger_message_id=10,
                delivery_id="multipart-after-legacy",
                content="Primera parte.",
                part_index=1,
                part_count=2,
                prior_parts=(),
            )
        )

    assert transport.post_calls == 0


def test_history_scan_limit_fails_when_overlapping_pages_never_complete() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        before = request.url.params.get("before")
        newest = int(before) + 18 if before is not None else 500
        return httpx.Response(
            200,
            json={
                "payload": [
                    {"id": message_id}
                    for message_id in range(newest - 19, newest + 1)
                ]
            },
        )

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(
        ChatwootHistoryScanLimitError,
        match="required_messages_beyond_scan_limit",
    ):
        asyncio.run(
            client.get_conversation_messages(
                conversation_id=2,
                limit=2000,
                required_message_ids=(500,),
            )
        )

    assert calls == 100


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


def test_pause_label_revalidates_expected_jid_before_macro_mutation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/conversations/2"):
            return httpx.Response(
                200,
                json={
                    "id": 2,
                    "inbox_id": 9,
                    "meta": {
                        "sender": {
                            "identifier": "12025550123@s.whatsapp.net",
                        }
                    },
                },
            )
        raise AssertionError("label mutation reached after identity mismatch")

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid="12025550123@s.whatsapp.net",
        pause_macro_id=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ChatwootProtocolError, match="conversation_identity_mismatch"):
        asyncio.run(
            client.ensure_conversation_label(
                conversation_id=2,
                label="automation_paused",
                expected_inbox_id=9,
                expected_jid="12025550124@s.whatsapp.net",
            )
        )

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


def test_paginates_conversation_history_with_the_before_cursor() -> None:
    requests: list[httpx.Request] = []
    messages = [{"id": message_id} for message_id in range(1, 26)]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        before = request.url.params.get("before")
        if before is None:
            page = messages[-20:]
        else:
            assert before == "6"
            page = messages[:5]
        return httpx.Response(200, json={"payload": page})

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        client.get_conversation_messages(conversation_id=2, limit=25)
    )

    assert result == messages
    assert [request.url.params.get("before") for request in requests] == [None, "6"]


def test_paginates_past_the_recent_limit_until_required_ids_are_found() -> None:
    requests: list[httpx.Request] = []
    messages = [{"id": message_id} for message_id in range(1, 251)]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        before = int(request.url.params.get("before", "251"))
        eligible = [message for message in messages if message["id"] < before]
        return httpx.Response(200, json={"payload": eligible[-20:]})

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        client.get_conversation_messages(
            conversation_id=2,
            limit=200,
            required_message_ids=(1, 250),
        )
    )

    result_ids = [message["id"] for message in result]
    assert result_ids == [1, *range(51, 251)]
    assert len(requests) == 13


def test_fails_explicitly_when_required_ids_exceed_the_history_scan_limit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        before = int(request.url.params.get("before", "100001"))
        page = [{"id": message_id} for message_id in range(before - 20, before)]
        return httpx.Response(200, json={"payload": page})

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(
        ChatwootHistoryScanLimitError,
        match="required_messages_beyond_scan_limit",
    ):
        asyncio.run(
            client.get_conversation_messages(
                conversation_id=2,
                limit=200,
                required_message_ids=(1,),
            )
        )

    assert len(requests) == 100


def test_reads_canonical_snapshot_and_detects_inbound_after_anchor() -> None:
    requests: list[httpx.Request] = []
    scoped_jid = "12025550999@s.whatsapp.net"
    messages = [
        {"id": 40, "created_at": 100, "message_type": 1, "private": False,
         "sender": {"type": "agent_bot", "id": 1}},
        {"id": 41, "created_at": 101, "message_type": 0, "private": False,
         "sender": {"type": "contact", "id": 20}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"payload": messages})
        return httpx.Response(200, json={
            "id": 2,
            "inbox_id": 7,
            "status": "open",
            "can_reply": True,
            "labels": ["cart_recovery"],
            "meta": {"sender": {"identifier": scoped_jid}},
        })

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=ALLOWED_JID,
        transport=httpx.MockTransport(handler),
    )
    snapshot = asyncio.run(client.get_canonical_conversation_snapshot(
        conversation_id=2,
        expected_inbox_id=7,
        anchor_message_id=40,
        expected_jid=scoped_jid,
    ))

    assert snapshot.anchor_found is True
    assert snapshot.inbound_after_anchor is True
    assert snapshot.human_activity_after_anchor is False
    assert snapshot.checkpoint_message_id == 41
    assert [request.url.path for request in requests] == [
        "/api/v1/accounts/1/conversations/2",
        "/api/v1/accounts/1/conversations/2/messages",
    ]


def test_canonical_snapshot_exposes_missing_anchor_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"payload": [{
                "id": 41, "created_at": 101, "message_type": 0,
                "private": False, "sender": {"type": "contact", "id": 20},
            }]})
        return httpx.Response(200, json={
            "id": 2, "inbox_id": 7, "status": "open", "can_reply": True,
            "labels": [], "meta": {"sender": {"identifier": ALLOWED_JID}},
        })

    client = ChatwootClient(
        base_url="https://chatwoot.example.test", account_id=1,
        access_token="control-token", allowed_jid=ALLOWED_JID,
        transport=httpx.MockTransport(handler),
    )
    snapshot = asyncio.run(client.get_canonical_conversation_snapshot(
        conversation_id=2, expected_inbox_id=7, anchor_message_id=40,
    ))
    assert snapshot.anchor_found is False
    assert snapshot.inbound_after_anchor is False


def test_canonical_snapshot_pages_back_until_anchor() -> None:
    latest = [
        {"id": message_id, "created_at": message_id,
         "message_type": 0 if message_id == 41 else 2, "private": False,
         "sender": {"type": "contact", "id": 20}}
        for message_id in range(41, 61)
    ]
    older = [{
        "id": 40, "created_at": 40, "message_type": 1, "private": False,
        "sender": {"type": "agent_bot", "id": 1},
    }]
    message_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            message_requests.append(request)
            return httpx.Response(
                200,
                json={"payload": older if request.url.params.get("before") else latest},
            )
        return httpx.Response(200, json={
            "id": 2, "inbox_id": 7, "status": "open", "can_reply": True,
            "labels": [], "meta": {"sender": {"identifier": ALLOWED_JID}},
        })

    client = ChatwootClient(
        base_url="https://chatwoot.example.test", account_id=1,
        access_token="control-token", allowed_jid=ALLOWED_JID,
        transport=httpx.MockTransport(handler),
    )
    snapshot = asyncio.run(client.get_canonical_conversation_snapshot(
        conversation_id=2, expected_inbox_id=7, anchor_message_id=40,
    ))

    assert snapshot.anchor_found is True
    assert snapshot.inbound_after_anchor is True
    assert len(message_requests) == 2
    assert message_requests[1].url.params["before"] == "41"


def test_canonical_snapshot_rejects_wrong_inbox_or_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": 2, "inbox_id": 99, "status": "open", "can_reply": True,
            "labels": [],
            "meta": {"sender": {"identifier": "12025550000@s.whatsapp.net"}},
        })

    client = ChatwootClient(
        base_url="https://chatwoot.example.test", account_id=1,
        access_token="control-token", allowed_jid=ALLOWED_JID,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ChatwootProtocolError, match="invalid_conversation_authority"):
        asyncio.run(client.get_canonical_conversation_snapshot(
            conversation_id=2, expected_inbox_id=7, anchor_message_id=None,
        ))


def test_apply_opt_out_macro_confirms_stop_and_pause_labels() -> None:
    requests: list[httpx.Request] = []
    posted = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posted
        requests.append(request)
        if request.url.path.endswith("/conversations/42"):
            return httpx.Response(200, json={
                "id": 42,
                "inbox_id": 9,
                "meta": {"sender": {"identifier": ALLOWED_JID}},
            })
        if request.method == "POST":
            posted = True
            assert request.url.path.endswith("/macros/9/execute")
            assert json.loads(request.content) == {"conversation_ids": [42]}
            return httpx.Response(200, json={})
        assert request.url.path.endswith("/conversations/42/labels")
        return httpx.Response(200, json={"payload": (
            ["automation_opted_out", "automation_paused"] if posted else []
        )})

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="token",
        opt_out_macro_id=9,
        confirmation_attempts=1,
        confirmation_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(client.apply_opt_out_macro(
        conversation_id=42,
        expected_account_id=1,
        expected_inbox_id=9,
        expected_jid=ALLOWED_JID,
    ))
    assert [request.method for request in requests] == [
        "GET", "GET", "POST", "GET",
    ]


def test_apply_opt_out_macro_rejects_sender_mismatch_before_post() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path.endswith("/conversations/42")
        return httpx.Response(200, json={
            "id": 42,
            "inbox_id": 9,
            "meta": {"sender": {"identifier": "12025550999@s.whatsapp.net"}},
        })

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="token",
        opt_out_macro_id=9,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ChatwootProtocolError, match="conversation_identity_mismatch"):
        asyncio.run(client.apply_opt_out_macro(
            conversation_id=42,
            expected_account_id=1,
            expected_inbox_id=9,
            expected_jid="12025550124@s.whatsapp.net",
        ))
    assert [request.method for request in requests] == ["GET"]


def test_validate_conversation_authority_rejects_wrong_inbox() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": 42,
            "inbox_id": 99,
            "meta": {"sender": {"identifier": ALLOWED_JID}},
        })

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="token",
        allowed_jid=ALLOWED_JID,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ChatwootProtocolError, match="invalid_conversation_authority"):
        asyncio.run(client.validate_conversation_authority(
            conversation_id=42, expected_inbox_id=7,
        ))


def test_validate_conversation_authority_accepts_operation_scoped_jid() -> None:
    scoped_jid = "12025550999@s.whatsapp.net"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": 42,
            "inbox_id": 7,
            "meta": {"sender": {"identifier": scoped_jid}},
        })

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="token",
        allowed_jid=ALLOWED_JID,
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(client.validate_conversation_authority(
        conversation_id=42,
        expected_inbox_id=7,
        expected_jid=scoped_jid,
    ))


def test_apply_opt_out_macro_skips_post_when_labels_already_projected() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/conversations/42"):
            return httpx.Response(200, json={
                "id": 42,
                "inbox_id": 9,
                "meta": {"sender": {"identifier": ALLOWED_JID}},
            })
        return httpx.Response(
            200,
            json={"payload": ["automation_opted_out", "automation_paused"]},
        )

    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="token",
        opt_out_macro_id=9,
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(client.apply_opt_out_macro(
        conversation_id=42,
        expected_account_id=1,
        expected_inbox_id=9,
        expected_jid=ALLOWED_JID,
    ))
    assert [request.method for request in requests] == ["GET", "GET"]
