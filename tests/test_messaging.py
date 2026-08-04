"""Tests for the messaging abstraction layer."""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest

from bridge.chatwoot import ChatwootClient, ChatwootProtocolError
from bridge.messaging import EvolutionMessageSender, _to_e164


# ── E.164 helper ─────────────────────────────────────────────────────


def test_to_e164_adds_plus() -> None:
    assert _to_e164("5531999999999") == "+5531999999999"


def test_to_e164_preserves_existing_plus() -> None:
    assert _to_e164("+5531999999999") == "+5531999999999"


# ── Mock transport for Chatwoot ─────────────────────────────────────


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.routes: dict[str, list[httpx.Response]] = {}
        self.requests: list[tuple[str, str, bytes]] = []
        self.query_params: list[dict[str, str]] = []

    def set(self, path_prefix: str, response: httpx.Response) -> None:
        self.routes[path_prefix] = self.routes.get(path_prefix, []) + [response]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = request.content
        self.requests.append((request.method, path, body))
        self.query_params.append(dict(request.url.params))
        for prefix, responses in self.routes.items():
            if path.startswith(prefix) and responses:
                return responses.pop(0)
        return httpx.Response(404, request=request)


def _chatwoot(transport: MockTransport) -> ChatwootClient:
    return ChatwootClient(
        base_url="https://chatwoot.test",
        account_id=1,
        access_token="test-token",
        agent_bot_access_token="bot-token",
        agent_bot_id=99,
        transport=transport,
    )


def _run(coro):
    return asyncio.run(coro)


# ── ChatwootClient.create_contact ────────────────────────────────────


def test_find_contact_by_phone_returns_exact_inbox_match() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 41,
                        "phone_number": "+553****9999",
                        "blocked": False,
                        "contact_inboxes": [{"inbox": {"id": 1}}],
                    },
                ],
            },
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )

    contact_id = _run(_chatwoot(transport).find_contact_by_phone(
        inbox_id=1,
        phone_number="+553****9999",
    ))

    assert contact_id == 41
    assert transport.query_params[0] == {"q": "+553****9999"}


def test_find_contact_by_phone_returns_none_for_empty_result() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={"payload": []},
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )

    contact_id = _run(_chatwoot(transport).find_contact_by_phone(
        inbox_id=1,
        phone_number="+553****9999",
    ))

    assert contact_id is None


def test_find_contact_by_phone_rejects_blocked_match() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 41,
                        "phone_number": "+553****9999",
                        "blocked": True,
                        "contact_inboxes": [{"inbox": {"id": 1}}],
                    },
                ],
            },
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )

    with pytest.raises(ChatwootProtocolError, match="contact_blocked"):
        _run(_chatwoot(transport).find_contact_by_phone(
            inbox_id=1,
            phone_number="+553****9999",
        ))


def test_find_contact_by_phone_rejects_non_positive_id() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 0,
                        "phone_number": "+553****9999",
                        "blocked": False,
                        "contact_inboxes": [{"inbox": {"id": 1}}],
                    },
                ],
            },
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )

    with pytest.raises(ChatwootProtocolError, match="invalid_contact_search_result"):
        _run(_chatwoot(transport).find_contact_by_phone(
            inbox_id=1,
            phone_number="+553****9999",
        ))


def test_find_contact_by_phone_rejects_malformed_nonmatching_row() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={"payload": [{"id": 41, "blocked": False, "contact_inboxes": []}]},
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )

    with pytest.raises(ChatwootProtocolError, match="invalid_contact_search_result"):
        _run(_chatwoot(transport).find_contact_by_phone(
            inbox_id=1,
            phone_number="+553****9999",
        ))


def test_find_contact_by_phone_rejects_distinct_exact_matches() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 41,
                        "phone_number": "+553****9999",
                        "blocked": False,
                        "contact_inboxes": [{"inbox": {"id": 1}}],
                    },
                    {
                        "id": 42,
                        "phone_number": "+553****9999",
                        "blocked": False,
                        "contact_inboxes": [{"inbox": {"id": 2}}],
                    },
                ],
            },
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )

    with pytest.raises(ChatwootProtocolError, match="ambiguous_contact_match"):
        _run(_chatwoot(transport).find_contact_by_phone(
            inbox_id=1,
            phone_number="+553****9999",
        ))


def test_create_contact_returns_contact_id() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts",
        httpx.Response(
            200,
            json={"payload": {"id": 42, "name": "Test"}},
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )
    client = _chatwoot(transport)
    contact_id = _run(client.create_contact(
        inbox_id=1,
        name="Test Buyer",
        phone_number="+5531999999999",
        email="buyer@test.com",
    ))
    assert contact_id == 42
    # Verify request body
    method, path, body = transport.requests[0]
    assert method == "POST"
    assert path == "/api/v1/accounts/1/contacts"
    req_body = json.loads(body)
    assert req_body["phone_number"] == "+5531999999999"
    assert req_body["inbox_id"] == 1


def test_create_contact_handles_direct_response() -> None:
    """Chatwoot may return the contact without a 'payload' wrapper."""
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts",
        httpx.Response(
            200,
            json={"id": 77, "name": "Test"},
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )
    client = _chatwoot(transport)
    contact_id = _run(client.create_contact(
        inbox_id=1,
        name="Test",
        phone_number="+5531999999999",
    ))
    assert contact_id == 77


def test_create_contact_handles_official_payload_list_response() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts",
        httpx.Response(
            200,
            json={"payload": [{"id": 88, "name": "Test"}]},
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )
    client = _chatwoot(transport)

    contact_id = _run(client.create_contact(
        inbox_id=1,
        name="Test",
        phone_number="+553****9999",
    ))

    assert contact_id == 88


def test_create_contact_raises_on_invalid_id() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts",
        httpx.Response(
            200,
            json={"payload": {"id": "not-an-int"}},
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )
    client = _chatwoot(transport)
    with pytest.raises(ChatwootProtocolError, match="invalid_contact_id"):
        _run(client.create_contact(
            inbox_id=1,
            name="Test",
            phone_number="+5531999999999",
        ))


# ── ChatwootClient.create_conversation ──────────────────────────────


def test_create_conversation_returns_conversation_id() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/conversations",
        httpx.Response(
            200,
            json={"id": 123, "status": "open"},
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )
    client = _chatwoot(transport)
    conv_id = _run(client.create_conversation(
        inbox_id=1,
        contact_id=42,
    ))
    assert conv_id == 123
    method, path, body = transport.requests[0]
    assert method == "POST"
    req_body = json.loads(body)
    assert req_body["inbox_id"] == 1
    assert req_body["contact_id"] == 42


# ── ChatwootClient.send_first_message ────────────────────────────────


def test_send_first_message_returns_sent_status() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/conversations/123/messages",
        httpx.Response(
            200,
            json={
                "id": 999,
                "conversation_id": 123,
                "message_type": 1,
                "private": False,
                "content": "¡Hola!",
                "content_attributes": {
                    "recovery_first_touch_hash": hashlib.sha256(
                        b"first:123:evt-001"
                    ).hexdigest()
                },
            },
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )
    client = _chatwoot(transport)
    result = _run(client.send_first_message(
        conversation_id=123,
        content="¡Hola!",
        delivery_id="evt-001",
    ))
    assert result["status"] == "sent"
    assert result["message_id"] == 999


def test_send_first_message_raises_without_agent_bot() -> None:
    transport = MockTransport()
    client = ChatwootClient(
        base_url="https://chatwoot.test",
        account_id=1,
        access_token="test-token",
        transport=transport,
    )
    with pytest.raises(ChatwootProtocolError, match="agent_bot_not_configured"):
        _run(client.send_first_message(
            conversation_id=123,
            content="¡Hola!",
            delivery_id="evt-001",
        ))


def test_send_first_message_rejects_noncanonical_success_payload() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/conversations/200/messages",
        httpx.Response(
            200,
            json={
                "id": 888,
                "conversation_id": 200,
                "message_type": 0,
                "private": False,
                "content": "Mensaje aprobado",
            },
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )

    with pytest.raises(ChatwootProtocolError, match="invalid_sent_message"):
        _run(_chatwoot(transport).send_first_message(
            conversation_id=200,
            content="Mensaje aprobado",
            delivery_id="attempt-001",
        ))


# ── EvolutionMessageSender end-to-end ────────────────────────────────


def test_evolution_sender_blocks_phone_outside_allowed_jid() -> None:
    transport = MockTransport()
    sender = EvolutionMessageSender(
        chatwoot=_chatwoot(transport),
        inbox_id=1,
        allowed_jid="15555550100@s.whatsapp.net",
    )

    result = _run(sender.send_first_touch(
        phone="12025550123",
        buyer_name="Test",
        buyer_email="test@test.com",
        content="¡Hola!",
        delivery_id="evt-not-allowed",
    ))

    assert result.status == "blocked"
    assert result.reason == "target_not_allowed"
    assert transport.requests == []


@pytest.mark.parametrize(
    "allowed_jid",
    [
        "12025550123",
        "12025550123@g.us",
        "+1 (202) 555-0123@s.whatsapp.net",
        "12025550123@s.whatsapp.net@evil",
    ],
)
def test_evolution_sender_rejects_noncanonical_allowed_jid(
    allowed_jid: str,
) -> None:
    transport = MockTransport()
    sender = EvolutionMessageSender(
        chatwoot=_chatwoot(transport),
        inbox_id=1,
        allowed_jid=allowed_jid,
    )

    result = _run(sender.send_first_touch(
        phone="12025550123",
        buyer_name="Test",
        buyer_email="test@test.com",
        content="¡Hola!",
        delivery_id="evt-invalid-jid",
    ))

    assert result.status == "blocked"
    assert result.reason == "target_not_allowed"
    assert transport.requests == []


def test_evolution_sender_sends_first_touch() -> None:
    transport = MockTransport()
    # search_contact → no existing contact
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={"payload": []},
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )
    # create_contact
    transport.set(
        "/api/v1/accounts/1/contacts",
        httpx.Response(
            200,
            json={"payload": {"id": 55}},
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )
    # create_conversation
    transport.set(
        "/api/v1/accounts/1/conversations",
        httpx.Response(
            200,
            json={"id": 200},
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )
    # send_first_message
    transport.set(
        "/api/v1/accounts/1/conversations/200/messages",
        httpx.Response(
            200,
            json={
                "id": 888,
                "conversation_id": 200,
                "message_type": 1,
                "private": False,
                "content": "¡Hola! Soy el asistente virtual de Dan.",
                "content_attributes": {
                    "recovery_first_touch_hash": hashlib.sha256(
                        b"first:200:evt-001"
                    ).hexdigest()
                },
            },
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )
    client = _chatwoot(transport)
    sender = EvolutionMessageSender(
        chatwoot=client,
        inbox_id=1,
        allowed_jid="5531999999999@s.whatsapp.net",
    )
    result = _run(sender.send_first_touch(
        phone="5531999999999",
        buyer_name="Test Buyer",
        buyer_email="buyer@test.com",
        content="¡Hola! Soy el asistente virtual de Dan.",
        delivery_id="evt-001",
    ))
    assert result.status == "sent"
    assert result.conversation_id == 200
    assert result.message_id == 888


def test_evolution_sender_reuses_existing_contact() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 55,
                        "phone_number": "+15555550100",
                        "blocked": False,
                        "contact_inboxes": [{"inbox": {"id": 1}}],
                    },
                ],
            },
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )
    transport.set(
        "/api/v1/accounts/1/conversations",
        httpx.Response(
            200,
            json={"id": 200},
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )
    transport.set(
        "/api/v1/accounts/1/conversations/200/messages",
        httpx.Response(
            200,
            json={
                "id": 888,
                "conversation_id": 200,
                "message_type": 1,
                "private": False,
                "content": "¡Hola!",
                "content_attributes": {
                    "recovery_first_touch_hash": hashlib.sha256(
                        b"first:200:evt-existing"
                    ).hexdigest()
                },
            },
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )
    sender = EvolutionMessageSender(
        chatwoot=_chatwoot(transport),
        inbox_id=1,
        allowed_jid="15555550100@s.whatsapp.net",
    )

    result = _run(sender.send_first_touch(
        phone="15555550100",
        buyer_name="Test Buyer",
        buyer_email="buyer@test.com",
        content="¡Hola!",
        delivery_id="evt-existing",
    ))

    assert result.status == "sent", result.reason
    assert not any(
        method == "POST" and path == "/api/v1/accounts/1/contacts"
        for method, path, _ in transport.requests
    )


def test_evolution_sender_blocks_on_invalid_phone() -> None:
    client = _chatwoot(MockTransport())
    sender = EvolutionMessageSender(
        chatwoot=client,
        inbox_id=1,
        allowed_jid="5531999999999@s.whatsapp.net",
    )
    result = _run(sender.send_first_touch(
        phone="",
        buyer_name="Test",
        buyer_email="test@test.com",
        content="¡Hola!",
        delivery_id="evt-002",
    ))
    assert result.status == "blocked"
    assert result.reason == "invalid_phone"


def test_evolution_sender_fails_on_chatwoot_error() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={"payload": []},
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )
    # create_contact returns 500
    transport.set(
        "/api/v1/accounts/1/contacts",
        httpx.Response(500, request=httpx.Request("POST", "https://chatwoot.test")),
    )
    client = _chatwoot(transport)
    sender = EvolutionMessageSender(
        chatwoot=client,
        inbox_id=1,
        allowed_jid="5531999999999@s.whatsapp.net",
    )
    result = _run(sender.send_first_touch(
        phone="5531999999999",
        buyer_name="Test",
        buyer_email="test@test.com",
        content="¡Hola!",
        delivery_id="evt-003",
    ))
    assert result.status == "failed"
    assert result.reason == "chatwoot_http_error"


def test_evolution_sender_sanitizes_search_http_error() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(500, request=httpx.Request("GET", "https://chatwoot.test")),
    )
    sender = EvolutionMessageSender(
        chatwoot=_chatwoot(transport),
        inbox_id=1,
        allowed_jid="15555550100@s.whatsapp.net",
    )

    result = _run(sender.send_first_touch(
        phone="15555550100",
        buyer_name="Test",
        buyer_email="test@test.com",
        content="¡Hola!",
        delivery_id="evt-http-error",
    ))

    assert result.status == "failed"
    assert result.reason == "chatwoot_http_error"
    assert "15555550100" not in result.reason
