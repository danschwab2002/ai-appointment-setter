"""Tests for the messaging abstraction layer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

import httpx
import pytest

from bridge.chatwoot import ChatwootClient, ChatwootProtocolError
from bridge.messaging import (
    FIRST_TOUCH_RECIPIENT_MISMATCH,
    FIRST_TOUCH_TEMPLATE_NOT_CONFIGURED,
    ChatwootMessageSender,
    FirstTouchRecipient,
    WhatsAppTemplateConfig,
    _to_e164,
)


def test_payment_failure_first_touch_uses_dedicated_approved_template() -> None:
    template = WhatsAppTemplateConfig(
        first_touch_name="att1_carrito_abandonado_01",
        payment_failure_name="att1_compra_fallida_01",
        followup_name="att1_seguimiento_v1",
        language="es_MX",
        category="MARKETING",
        first_touch_parameter="buyer_name_and_product",
    )

    params = template.params(
        content="copy controlled by Meta",
        followup=False,
        buyer_name="Ana",
        product_name="ATT1",
        trigger_kind="payment_failure",
    )

    assert params["name"] == "att1_compra_fallida_01"
    assert params["processed_params"] == {
        "body": {"1": "Ana", "2": "ATT1"}
    }


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


def _chatwoot(
    transport: MockTransport,
    *,
    agent_bot_id: int = 99,
) -> ChatwootClient:
    return ChatwootClient(
        base_url="https://chatwoot.test",
        account_id=1,
        access_token="test-token",
        agent_bot_access_token="bot-token",
        agent_bot_id=agent_bot_id,
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
                        "contact_inboxes": [{"source_id": "source-55", "inbox": {"id": 1}}],
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


def test_find_contact_inbox_by_phone_returns_waba_source_id() -> None:
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
                        "contact_inboxes": [
                            {"source_id": "source-41", "inbox": {"id": 1}}
                        ],
                    },
                ],
            },
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )

    binding = _run(_chatwoot(transport).find_contact_inbox_by_phone(
        inbox_id=1,
        phone_number="+553****9999",
    ))

    assert binding == (41, "source-41")


def test_find_contact_inbox_by_phone_rejects_conflicting_source_ids() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 42,
                        "phone_number": "+5531999999999",
                        "blocked": False,
                        "contact_inboxes": [
                            {"source_id": "source-a", "inbox": {"id": 1}},
                            {"source_id": "source-b", "inbox": {"id": 1}},
                        ],
                    }
                ]
            },
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )

    with pytest.raises(ChatwootProtocolError, match="ambiguous_contact_inbox_source"):
        _run(
            _chatwoot(transport).find_contact_inbox_by_phone(
                inbox_id=1,
                phone_number="+5531999999999",
            )
        )


@pytest.mark.parametrize("malformed_source_id", [None, "", 42])
def test_find_contact_inbox_by_phone_rejects_malformed_duplicate_source_id(
    malformed_source_id: object,
) -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 42,
                        "phone_number": "+553****9999",
                        "blocked": False,
                        "contact_inboxes": [
                            {"source_id": "source-a", "inbox": {"id": 1}},
                            {"source_id": malformed_source_id, "inbox": {"id": 1}},
                        ],
                    }
                ]
            },
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )

    with pytest.raises(ChatwootProtocolError, match="invalid_contact_inbox_source"):
        _run(
            _chatwoot(transport).find_contact_inbox_by_phone(
                inbox_id=1,
                phone_number="+553****9999",
            )
        )


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
                        "contact_inboxes": [{"source_id": "source-55", "inbox": {"id": 1}}],
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
                        "contact_inboxes": [{"source_id": "source-55", "inbox": {"id": 1}}],
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
                        "contact_inboxes": [{"source_id": "source-55", "inbox": {"id": 1}}],
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


def test_create_contact_handles_current_official_nested_contact_response() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts",
        httpx.Response(
            200,
            json={
                "payload": {
                    "contact": {"id": 91, "name": "Test"},
                    "contact_inbox": {"source_id": "source-91"},
                }
            },
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )

    contact_id = _run(_chatwoot(transport).create_contact(
        inbox_id=1,
        name="Test",
        phone_number="+553****9999",
    ))

    assert contact_id == 91


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
        source_id="source-42",
    ))
    assert conv_id == 123
    method, path, body = transport.requests[0]
    assert method == "POST"
    req_body = json.loads(body)
    assert req_body["inbox_id"] == 1
    assert req_body["contact_id"] == 42
    assert req_body["source_id"] == "source-42"


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


def test_send_followup_message_uses_attempt_correlation() -> None:
    transport = MockTransport()
    expected_hash = hashlib.sha256(b"followup:attempt-002").hexdigest()
    transport.set(
        "/api/v1/accounts/1/conversations/200/messages",
        httpx.Response(
            200,
            json={
                "id": 889,
                "conversation_id": 200,
                "message_type": 1,
                "private": False,
                "content": "Seguimiento",
                "content_attributes": {"recovery_followup_hash": expected_hash},
                "sender": {"type": "agent_bot", "id": 99},
            },
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )

    result = _run(_chatwoot(transport).send_followup_message(
        conversation_id=200,
        content="Seguimiento",
        delivery_id="attempt-002",
    ))

    assert result == {"status": "sent", "message_id": 889}
    body = json.loads(transport.requests[0][2])
    assert body["content_attributes"] == {
        "recovery_followup_hash": expected_hash,
    }


@pytest.mark.parametrize(
    ("sender", "agent_bot_id"),
    [
        ({"type": "user", "id": 99}, 99),
        ({"type": "agent_bot", "id": True}, 1),
    ],
)
def test_send_followup_message_rejects_wrong_sender(
    sender: dict[str, object],
    agent_bot_id: int,
) -> None:
    transport = MockTransport()
    expected_hash = hashlib.sha256(b"followup:attempt-002").hexdigest()
    transport.set(
        "/api/v1/accounts/1/conversations/200/messages",
        httpx.Response(
            200,
            json={
                "id": 889,
                "conversation_id": 200,
                "message_type": 1,
                "private": False,
                "content": "Seguimiento",
                "content_attributes": {"recovery_followup_hash": expected_hash},
                "sender": sender,
            },
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )

    with pytest.raises(ChatwootProtocolError, match="invalid_sent_message"):
        _run(_chatwoot(
            transport,
            agent_bot_id=agent_bot_id,
        ).send_followup_message(
            conversation_id=200,
            content="Seguimiento",
            delivery_id="attempt-002",
        ))


# ── ChatwootMessageSender end-to-end ────────────────────────────────


def test_chatwoot_sender_requires_exactly_one_recipient_authority() -> None:
    with pytest.raises(ValueError, match="recipient authority"):
        ChatwootMessageSender(
            chatwoot=object(),  # type: ignore[arg-type]
            inbox_id=1,
            allowed_jid=None,
        )
    with pytest.raises(ValueError, match="recipient authority"):
        ChatwootMessageSender(
            chatwoot=object(),  # type: ignore[arg-type]
            inbox_id=1,
            allowed_jid="15555550100@s.whatsapp.net",
            dynamic_recipient_enabled=True,
        )


def test_chatwoot_sender_allows_valid_dynamic_recipient_explicitly() -> None:
    calls: list[dict[str, object]] = []

    class ChatwootStub:
        async def send_followup_message(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"message_id": 1}

    sender = ChatwootMessageSender(
        chatwoot=ChatwootStub(),  # type: ignore[arg-type]
        inbox_id=24,
        allowed_jid=None,
        dynamic_recipient_enabled=True,
    )

    result = _run(sender.send_followup(
        conversation_id=42,
        phone="5215550100999",
        content="Seguimiento",
        delivery_id="attempt-portable",
    ))

    assert result.status == "sent"
    assert calls == [{
        "conversation_id": 42,
        "content": "Seguimiento",
        "delivery_id": "attempt-portable",
        "template_params": None,
    }]


def test_evolution_sender_blocks_phone_outside_allowed_jid() -> None:
    transport = MockTransport()
    sender = ChatwootMessageSender(
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
    sender = ChatwootMessageSender(
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


def _waba_first_touch_transport() -> MockTransport:
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
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={
                "payload": [
                    {
                        "id": 55,
                        "phone_number": "+5531999999999",
                        "blocked": False,
                        "contact_inboxes": [
                            {"source_id": "source-55", "inbox": {"id": 1}}
                        ],
                    }
                ]
            },
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
    return transport


def test_chatwoot_sender_sends_waba_first_touch_template() -> None:
    transport = _waba_first_touch_transport()
    client = _chatwoot(transport)
    sender = ChatwootMessageSender(
        chatwoot=client,
        inbox_id=1,
        allowed_jid="5531999999999@s.whatsapp.net",
        template=WhatsAppTemplateConfig(
            first_touch_name="cart_recovery_first",
            followup_name=None,
            language="es_AR",
            category="MARKETING",
            first_touch_parameter="buyer_name_and_product",
        ),
    )
    result = _run(sender.send_first_touch(
        phone="5531999999999",
        buyer_name="Test Buyer",
        buyer_email="buyer@test.com",
        product_name="Libre de Ansiedad",
        content="¡Hola! Soy el asistente virtual de Dan.",
        delivery_id="evt-001",
    ))
    assert result.status == "sent"
    assert result.conversation_id == 200
    assert result.message_id == 888
    conversation_body = json.loads(transport.requests[-2][2])
    assert conversation_body["source_id"] == "source-55"
    body = json.loads(transport.requests[-1][2])
    assert body["template_params"] == {
        "name": "cart_recovery_first",
        "category": "MARKETING",
        "language": "es_AR",
        "processed_params": {
            "body": {"1": "Test Buyer", "2": "Libre de Ansiedad"}
        },
    }


def test_chatwoot_sender_greets_by_greeting_name_and_keeps_the_full_contact_name() -> None:
    transport = _waba_first_touch_transport()
    sender = ChatwootMessageSender(
        chatwoot=_chatwoot(transport),
        inbox_id=1,
        allowed_jid="5531999999999@s.whatsapp.net",
        template=WhatsAppTemplateConfig(
            first_touch_name="johanna_interes_precheckout_01",
            followup_name=None,
            language="es_EC",
            category="MARKETING",
            first_touch_parameter="buyer_name_and_product",
        ),
    )

    result = _run(sender.send_first_touch(
        phone="5531999999999",
        buyer_name="Andres Felipe Pérez García",
        buyer_email="buyer@test.com",
        product_name="Libre de Ansiedad",
        content="¡Hola! Soy el asistente virtual de Dan.",
        delivery_id="evt-001",
        greeting_name="Andres Felipe",
    ))

    assert result.status == "sent"
    [contact_body] = [
        json.loads(body)
        for method, path, body in transport.requests
        if method == "POST" and path == "/api/v1/accounts/1/contacts"
    ]
    assert contact_body["name"] == "Andres Felipe Pérez García"
    template = json.loads(transport.requests[-1][2])["template_params"]
    assert template["processed_params"]["body"] == {
        "1": "Andres Felipe",
        "2": "Libre de Ansiedad",
    }


@pytest.mark.parametrize(
    ("buyer_name", "product_name"),
    [(None, "Libre de Ansiedad"), ("Test Buyer", None)],
)
def test_chatwoot_sender_blocks_missing_two_variable_template_data(
    buyer_name: str | None,
    product_name: str | None,
) -> None:
    transport = MockTransport()
    sender = ChatwootMessageSender(
        chatwoot=_chatwoot(transport),
        inbox_id=1,
        allowed_jid="5531999999999@s.whatsapp.net",
        template=WhatsAppTemplateConfig(
            first_touch_name="cart_recovery_first",
            followup_name=None,
            language="es_AR",
            category="MARKETING",
            first_touch_parameter="buyer_name_and_product",
        ),
    )

    result = _run(sender.send_first_touch(
        phone="5531999999999",
        buyer_name=buyer_name,
        buyer_email="buyer@test.com",
        product_name=product_name,
        content="contenido no usado por el template",
        delivery_id="evt-missing-template-data",
    ))

    assert result.status == "blocked"
    assert result.reason == "template_parameters_missing"
    assert transport.requests == []


def test_chatwoot_sender_retry_requires_existing_contact_without_create() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/contacts/search",
        httpx.Response(
            200,
            json={"payload": []},
            request=httpx.Request("GET", "https://chatwoot.test"),
        ),
    )
    sender = ChatwootMessageSender(
        chatwoot=_chatwoot(transport),
        inbox_id=1,
        allowed_jid="5531999999999@s.whatsapp.net",
        template=WhatsAppTemplateConfig(
            first_touch_name="cart_recovery_first",
            followup_name=None,
            language="es_AR",
            category="MARKETING",
            first_touch_parameter="buyer_name_and_product",
        ),
    )

    result = _run(sender.send_first_touch(
        phone="5531999999999",
        buyer_name="Test Buyer",
        buyer_email="buyer@test.com",
        product_name="Libre de Ansiedad",
        content="contenido no usado por el template",
        delivery_id="evt-contact-retry",
        require_existing_contact=True,
    ))

    assert result.status == "failed"
    assert result.reason == "existing_contact_required"
    assert [method for method, _, _ in transport.requests] == ["GET"]


def test_chatwoot_sender_sends_waba_followup_template() -> None:
    transport = MockTransport()
    transport.set(
        "/api/v1/accounts/1/conversations/200/messages",
        httpx.Response(
            200,
            json={
                "id": 889,
                "conversation_id": 200,
                "message_type": 1,
                "private": False,
                "content": "¿Te quedó alguna duda?",
                "content_attributes": {
                    "recovery_followup_hash": hashlib.sha256(
                        b"followup:attempt-002"
                    ).hexdigest()
                },
                "sender": {"type": "agent_bot", "id": 99},
            },
            request=httpx.Request("POST", "https://chatwoot.test"),
        ),
    )
    sender = ChatwootMessageSender(
        chatwoot=_chatwoot(transport),
        inbox_id=1,
        allowed_jid="5531999999999@s.whatsapp.net",
        template=WhatsAppTemplateConfig(
            first_touch_name="cart_recovery_first",
            followup_name="cart_recovery_followup",
            language="es_AR",
            category="MARKETING",
        ),
    )

    result = _run(sender.send_followup(
        conversation_id=200,
        phone="5531999999999",
        content="¿Te quedó alguna duda?",
        delivery_id="attempt-002",
    ))

    assert result.status == "sent"
    assert result.conversation_id == 200
    assert result.message_id == 889
    assert [path for _, path, _ in transport.requests] == [
        "/api/v1/accounts/1/conversations/200/messages"
    ]
    body = json.loads(transport.requests[-1][2])
    assert body["template_params"]["name"] == "cart_recovery_followup"
    assert body["template_params"]["processed_params"] == {
        "body": {"1": "¿Te quedó alguna duda?"}
    }


def test_chatwoot_sender_blocks_followup_when_template_is_disabled() -> None:
    transport = MockTransport()
    sender = ChatwootMessageSender(
        chatwoot=_chatwoot(transport),
        inbox_id=1,
        allowed_jid="5531999999999@s.whatsapp.net",
        template=WhatsAppTemplateConfig(
            first_touch_name="cart_recovery_first",
            followup_name=None,
            language="es_AR",
            category="MARKETING",
            first_touch_parameter="buyer_name_and_product",
        ),
    )

    result = _run(sender.send_followup(
        conversation_id=200,
        phone="5531999999999",
        content="¿Te quedó alguna duda?",
        delivery_id="attempt-disabled",
    ))

    assert result.status == "blocked"
    assert result.reason == "followup_template_disabled"
    assert transport.requests == []


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
                        "contact_inboxes": [{"source_id": "source-55", "inbox": {"id": 1}}],
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
    sender = ChatwootMessageSender(
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
    conversation_body = json.loads(transport.requests[1][2])
    assert conversation_body["source_id"] == "source-55"
    body = json.loads(transport.requests[-1][2])
    assert "template_params" not in body


def test_evolution_sender_blocks_on_invalid_phone() -> None:
    client = _chatwoot(MockTransport())
    sender = ChatwootMessageSender(
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
    sender = ChatwootMessageSender(
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
    sender = ChatwootMessageSender(
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


# ── Body variables declared by the instance (manifest ``parametros``) ──

_FIXTURES = Path(__file__).parent / "fixtures"
_CAPTURED_INBOX_9 = json.loads(
    (_FIXTURES / "chatwoot_inbox_9_message_templates_20260923.json").read_text(
        encoding="utf-8"
    )
)
_CAPTURED_LEAD_NAMES = json.loads(
    (_FIXTURES / "lead_names_inbox9_template_params_20260928.json").read_text(
        encoding="utf-8"
    )
)


def _captured_placeholders(template_name: str) -> list[str]:
    [template] = [
        t for t in _CAPTURED_INBOX_9["message_templates"] if t["name"] == template_name
    ]
    [body] = [c for c in template["components"] if c["type"] == "BODY"]
    return sorted(set(re.findall(r"\{\{(\d+)\}\}", body["text"])))


@pytest.mark.parametrize(
    ("first_touch_parameter", "followup", "expected"),
    [
        ("content", False, {"1": "copy"}),
        ("buyer_name", False, {"1": "Ana"}),
        ("buyer_name_and_product", False, {"1": "Ana", "2": "ATT1"}),
        ("content", True, {"1": "copy"}),
        ("buyer_name_and_product", True, {"1": "copy"}),
    ],
)
def test_template_without_declared_variables_keeps_the_body_of_today(
    first_touch_parameter: str,
    followup: bool,
    expected: dict[str, str],
) -> None:
    # Las cuatro construcciones de Johanna (app.py) no declaran variables.
    template = WhatsAppTemplateConfig(
        first_touch_name="johanna_carrito_abandonado_01",
        followup_name="johanna_seguimiento_01",
        language="es_EC",
        category="MARKETING",
        first_touch_parameter=first_touch_parameter,
        payment_failure_name="johanna_compra_fallida_01",
    )

    for trigger_kind in (None, "cart_abandonment", "payment_failure"):
        params = template.params(
            content="copy",
            followup=followup,
            buyer_name="Ana",
            product_name="ATT1",
            trigger_kind=trigger_kind,
        )
        assert params["processed_params"] == {"body": expected}
        assert template.body_values(
            trigger_kind=trigger_kind, buyer_name="Ana", product_name="ATT1"
        ) is None


def test_declared_variables_fill_exactly_the_placeholders_of_the_approved_body() -> None:
    # Cuerpos aprobados capturados: johanna_reactivacion_01 tiene solo {{1}} y
    # johanna_interes_precheckout_01 tiene {{1}} y {{2}}.
    assert _captured_placeholders("johanna_reactivacion_01") == ["1"]
    assert _captured_placeholders("johanna_interes_precheckout_01") == ["1", "2"]
    case = _CAPTURED_LEAD_NAMES["cases"][0]

    one = WhatsAppTemplateConfig(
        first_touch_name="johanna_reactivacion_01",
        followup_name=None,
        language="es_EC",
        category="MARKETING",
        first_touch_parameter="buyer_name_and_product",
        first_touch_body_parameters=("nombre",),
    )
    two = WhatsAppTemplateConfig(
        first_touch_name="johanna_interes_precheckout_01",
        followup_name=None,
        language="es_EC",
        category="MARKETING",
        first_touch_parameter="buyer_name_and_product",
        first_touch_body_parameters=("nombre", "producto"),
    )

    one_body = one.params(
        content="copy",
        followup=False,
        buyer_name=case["deterministic"],
        product_name="Libre de Ansiedad",
    )["processed_params"]["body"]
    two_body = two.params(
        content="copy",
        followup=False,
        buyer_name=case["deterministic"],
        product_name="Libre de Ansiedad",
    )["processed_params"]["body"]

    assert one_body == {"1": case["deterministic"]}
    assert sorted(one_body) == _captured_placeholders("johanna_reactivacion_01")
    assert two_body == {"1": case["deterministic"], "2": "Libre de Ansiedad"}
    assert sorted(two_body) == _captured_placeholders("johanna_interes_precheckout_01")


def test_cart_and_payment_failure_use_each_their_declared_variables() -> None:
    template = WhatsAppTemplateConfig(
        first_touch_name="att1_carrito_abandonado_01",
        payment_failure_name="att1_compra_fallida_01",
        followup_name=None,
        language="es_MX",
        category="MARKETING",
        first_touch_parameter="buyer_name_and_product",
        first_touch_body_parameters=("nombre",),
        payment_failure_body_parameters=("producto", "nombre"),
    )

    cart = template.params(
        content="copy",
        followup=False,
        buyer_name="Ana",
        product_name="Alimenta Tu Tiroides",
        trigger_kind="cart_abandonment",
    )
    failure = template.params(
        content="copy",
        followup=False,
        buyer_name="Ana",
        product_name="Alimenta Tu Tiroides",
        trigger_kind="payment_failure",
    )

    assert cart["name"] == "att1_carrito_abandonado_01"
    assert cart["processed_params"] == {"body": {"1": "Ana"}}
    assert failure["name"] == "att1_compra_fallida_01"
    assert failure["processed_params"] == {
        "body": {"1": "Alimenta Tu Tiroides", "2": "Ana"}
    }


def test_payment_failure_without_its_own_template_uses_the_cart_variables() -> None:
    template = WhatsAppTemplateConfig(
        first_touch_name="att1_carrito_abandonado_01",
        followup_name=None,
        language="es_MX",
        category="MARKETING",
        first_touch_parameter="buyer_name_and_product",
        first_touch_body_parameters=("nombre",),
    )

    params = template.params(
        content="copy",
        followup=False,
        buyer_name="Ana",
        product_name="ATT1",
        trigger_kind="payment_failure",
    )

    assert params["name"] == "att1_carrito_abandonado_01"
    assert params["processed_params"] == {"body": {"1": "Ana"}}


def test_a_followup_ignores_the_declared_first_contact_variables() -> None:
    template = WhatsAppTemplateConfig(
        first_touch_name="att1_carrito_abandonado_01",
        followup_name="att1_seguimiento_01",
        language="es_MX",
        category="MARKETING",
        first_touch_parameter="buyer_name_and_product",
        first_touch_body_parameters=("nombre",),
    )

    params = template.params(content="copy", followup=True)

    assert params["name"] == "att1_seguimiento_01"
    assert params["processed_params"] == {"body": {"1": "copy"}}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"first_touch_body_parameters": ()},
        {"first_touch_body_parameters": ("nombre", "producto", "nombre")},
        {"first_touch_body_parameters": ("nombre", "nombre")},
        {"first_touch_body_parameters": ("apellido",)},
        {"first_touch_body_parameters": ["nombre"]},
        {
            "payment_failure_name": "att1_compra_fallida_01",
            "payment_failure_body_parameters": ("cupon",),
        },
    ],
)
def test_invalid_declared_variables_are_refused(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="invalid_template_body_parameters"):
        WhatsAppTemplateConfig(
            first_touch_name="att1_carrito_abandonado_01",
            followup_name=None,
            language="es_MX",
            category="MARKETING",
            **kwargs,  # type: ignore[arg-type]
        )


def test_payment_failure_variables_need_the_payment_failure_template() -> None:
    with pytest.raises(ValueError, match="without_template"):
        WhatsAppTemplateConfig(
            first_touch_name="att1_carrito_abandonado_01",
            followup_name=None,
            language="es_MX",
            category="MARKETING",
            payment_failure_body_parameters=("nombre",),
        )


def test_declared_values_collapse_the_whitespace_meta_refuses() -> None:
    # Un nombre de Hotmart puede traer saltos de linea, tabulaciones o varios
    # espacios; Meta rechaza el parametro (132018). Con variables declaradas
    # (manifiesto) el valor sale colapsado en processed_params y en
    # body_values, que es de donde el modo directo arma el texto que hashea.
    template = WhatsAppTemplateConfig(
        first_touch_name="att1_carrito_abandonado_01",
        followup_name=None,
        language="es_MX",
        category="MARKETING",
        first_touch_parameter="buyer_name_and_product",
        first_touch_body_parameters=("nombre", "producto"),
    )
    raw_name = " Edith\nGarcía \t      Pérez "

    params = template.params(
        content="copy", followup=False, buyer_name=raw_name,
        product_name="Alimenta  Tu\tTiroides", trigger_kind="cart_abandonment",
    )
    values = template.body_values(
        trigger_kind="cart_abandonment", buyer_name=raw_name,
        product_name="Alimenta  Tu\tTiroides",
    )

    assert params["processed_params"] == {
        "body": {"1": "Edith García Pérez", "2": "Alimenta Tu Tiroides"}
    }
    assert values == params["processed_params"]["body"]


def test_without_declared_variables_the_values_go_as_today() -> None:
    # Johanna no declara variables: su camino no normaliza nada.
    template = WhatsAppTemplateConfig(
        first_touch_name="johanna_carrito_abandonado_01",
        followup_name=None,
        language="es_EC",
        category="MARKETING",
        first_touch_parameter="buyer_name_and_product",
    )

    params = template.params(
        content="copy", followup=False, buyer_name="Edith  García",
        product_name="Curso", trigger_kind="cart_abandonment",
    )

    assert params["processed_params"] == {"body": {"1": "Edith  García", "2": "Curso"}}


def test_the_payment_failure_template_carries_its_own_category() -> None:
    # El carrito aprobado como MARKETING y el pago fallido como UTILITY: cada
    # plantilla sale con su categoria. El seguimiento y el carrito siguen con
    # la categoria unica.
    template = WhatsAppTemplateConfig(
        first_touch_name="att1_carrito_abandonado_01",
        payment_failure_name="att1_compra_fallida_01",
        followup_name="att1_seguimiento_01",
        language="es_MX",
        category="MARKETING",
        first_touch_parameter="buyer_name_and_product",
        payment_failure_category="UTILITY",
    )

    failure = template.params(
        content="copy", followup=False, buyer_name="Ana",
        product_name="ATT1", trigger_kind="payment_failure",
    )
    cart = template.params(
        content="copy", followup=False, buyer_name="Ana",
        product_name="ATT1", trigger_kind="cart_abandonment",
    )
    followup = template.params(content="copy", followup=True)

    assert failure["category"] == "UTILITY"
    assert cart["category"] == "MARKETING"
    assert followup["category"] == "MARKETING"
    assert template.category_for(trigger_kind="payment_failure") == "UTILITY"
    assert template.category_for(trigger_kind="cart_abandonment") == "MARKETING"


def test_without_its_own_category_every_template_keeps_the_single_one() -> None:
    template = WhatsAppTemplateConfig(
        first_touch_name="att1_carrito_abandonado_01",
        payment_failure_name="att1_compra_fallida_01",
        followup_name=None,
        language="es_MX",
        category="MARKETING",
        first_touch_parameter="buyer_name_and_product",
    )

    failure = template.params(
        content="copy", followup=False, buyer_name="Ana",
        product_name="ATT1", trigger_kind="payment_failure",
    )

    assert failure["category"] == "MARKETING"
    assert template.category_for(trigger_kind="payment_failure") == "MARKETING"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"payment_failure_category": "UTILITY"}, "payment_failure_category_without_template"),
        (
            {"payment_failure_name": "att1_compra_fallida_01", "payment_failure_category": "utility"},
            "invalid_payment_failure_category",
        ),
        (
            {"payment_failure_name": "att1_compra_fallida_01", "payment_failure_category": "AUTHENTICATION"},
            "invalid_payment_failure_category",
        ),
    ],
)
def test_an_invalid_payment_failure_category_is_refused(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        WhatsAppTemplateConfig(
            first_touch_name="att1_carrito_abandonado_01",
            followup_name=None,
            language="es_MX",
            category="MARKETING",
            **kwargs,  # type: ignore[arg-type]
        )


def test_chatwoot_sender_sends_a_one_variable_template_without_product() -> None:
    case = _CAPTURED_LEAD_NAMES["cases"][0]
    transport = _waba_first_touch_transport()
    sender = ChatwootMessageSender(
        chatwoot=_chatwoot(transport),
        inbox_id=1,
        allowed_jid="5531999999999@s.whatsapp.net",
        template=WhatsAppTemplateConfig(
            first_touch_name="att1_carrito_abandonado_01",
            followup_name=None,
            language="es_MX",
            category="MARKETING",
            first_touch_parameter="buyer_name_and_product",
            first_touch_body_parameters=("nombre",),
        ),
    )

    result = _run(sender.send_first_touch(
        phone="5531999999999",
        buyer_name=case["full_name"],
        buyer_email="buyer@test.com",
        product_name=None,
        content="¡Hola! Soy el asistente virtual de Dan.",
        delivery_id="evt-001",
        greeting_name=case["deterministic"],
    ))

    # Sin producto no bloquea: la plantilla declarada no lo pide.
    assert result.status == "sent"
    body = json.loads(transport.requests[-1][2])
    assert body["template_params"] == {
        "name": "att1_carrito_abandonado_01",
        "category": "MARKETING",
        "language": "es_MX",
        "processed_params": {"body": {"1": case["deterministic"]}},
    }


@pytest.mark.parametrize(
    ("declared", "buyer_name", "product_name"),
    [
        (("nombre",), None, "Alimenta Tu Tiroides"),
        (("nombre",), "   ", "Alimenta Tu Tiroides"),
        (("producto",), "Ana", None),
        (("nombre", "producto"), "Ana", ""),
    ],
)
def test_chatwoot_sender_blocks_a_missing_declared_variable(
    declared: tuple[str, ...],
    buyer_name: str | None,
    product_name: str | None,
) -> None:
    transport = MockTransport()
    sender = ChatwootMessageSender(
        chatwoot=_chatwoot(transport),
        inbox_id=1,
        allowed_jid="5531999999999@s.whatsapp.net",
        template=WhatsAppTemplateConfig(
            first_touch_name="att1_carrito_abandonado_01",
            followup_name=None,
            language="es_MX",
            category="MARKETING",
            first_touch_parameter="buyer_name_and_product",
            first_touch_body_parameters=declared,
        ),
    )

    result = _run(sender.send_first_touch(
        phone="5531999999999",
        buyer_name=buyer_name,
        buyer_email="buyer@test.com",
        product_name=product_name,
        content="contenido no usado por el template",
        delivery_id="evt-missing-declared",
    ))

    assert result.status == "blocked"
    assert result.reason == "template_parameters_missing"
    assert transport.requests == []


# ── Equivalencia de telefonos de WhatsApp (solo el runtime portable) ─
#
# El mismo movil puede estar en Chatwoot con cualquiera de sus dos formas: 52 +
# 10 digitos (lo creo un envio anterior) o 521 + 10 (lo creo la persona al
# escribir, que es la forma del wa_id). En Argentina, 54 y 549. Medido el
# 2026-10-01 sobre el Chatwoot de produccion (decisiones D13 y D14).
#
# No hay captura de /contacts/search, /contacts, /conversations ni /messages:
# sigue el precedente inline de este archivo (deuda de A0). Lo que si esta
# medido es que en un inbox de WhatsApp Cloud el source_id de un contacto es
# su wa_id, los digitos de su telefono (170 de 170 contactos de los inboxes 9
# y 11), y asi lo emula _WhatsAppCloudInbox. Los telefonos son de prueba.

MX_FORM = "525512345678"
MX_WHATSAPP = "5215512345678"
AR_FORM = "541112345678"
AR_WHATSAPP = "5491112345678"


class _WhatsAppCloudInbox:
    """Chatwoot de un inbox de WhatsApp Cloud, con memoria de sus contactos."""

    def __init__(
        self,
        contacts: dict[str, int] | None = None,
        *,
        created_source_id: str | None = None,
        blocked: set[int] | None = None,
    ) -> None:
        # telefono E.164 -> id del contacto
        self.contacts = dict(contacts or {})
        self.created_source_id = created_source_id
        self.blocked = blocked or set()
        self.source_ids: dict[int, str] = {
            contact_id: phone.lstrip("+") for phone, contact_id in self.contacts.items()
        }
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []
        self.searches: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append((request.method, path, body))
        prefix = "/api/v1/accounts/1"
        if request.method == "GET" and path == f"{prefix}/contacts/search":
            query = request.url.params["q"]
            self.searches.append(query)
            contact_id = self.contacts.get(query)
            if contact_id is None:
                return httpx.Response(200, json={"payload": []})
            return httpx.Response(200, json={"payload": [{
                "id": contact_id,
                "phone_number": query,
                "blocked": contact_id in self.blocked,
                "contact_inboxes": [{
                    "source_id": self.source_ids[contact_id],
                    "inbox": {"id": 1},
                }],
            }]})
        if request.method == "POST" and path == f"{prefix}/contacts":
            assert body is not None
            phone = str(body["phone_number"])
            contact_id = 500 + len(self.contacts)
            self.contacts[phone] = contact_id
            self.source_ids[contact_id] = self.created_source_id or phone.lstrip("+")
            return httpx.Response(200, json={"payload": {"id": contact_id}})
        if request.method == "POST" and path == f"{prefix}/conversations":
            return httpx.Response(200, json={"id": 200})
        if request.method == "POST" and path == f"{prefix}/conversations/200/messages":
            assert body is not None
            return httpx.Response(200, json={
                "id": 888,
                "conversation_id": 200,
                "message_type": 1,
                "private": False,
                "content": body["content"],
                "content_attributes": body["content_attributes"],
            })
        return httpx.Response(404, json={"error": "not_emulated"})

    def posts(self, suffix: str) -> list[dict[str, object]]:
        return [
            body
            for method, path, body in self.requests
            if method == "POST" and path.endswith(suffix) and body is not None
        ]

    def sender(self, *, equivalence: bool = True) -> ChatwootMessageSender:
        return ChatwootMessageSender(
            chatwoot=ChatwootClient(
                base_url="https://chatwoot.test",
                account_id=1,
                access_token="test-token",
                agent_bot_access_token="bot-token",
                agent_bot_id=99,
                transport=httpx.MockTransport(self.handler),
            ),
            inbox_id=1,
            allowed_jid=None,
            dynamic_recipient_enabled=True,
            whatsapp_equivalence_enabled=equivalence,
        )


def _first_touch(sender: ChatwootMessageSender, phone: str, **kwargs: object):
    return _run(sender.send_first_touch(
        phone=phone,
        buyer_name="Lead de prueba",
        buyer_email=None,
        content="Texto de la plantilla aprobada",
        delivery_id="evt-equivalence",
        **kwargs,  # type: ignore[arg-type]
    ))


def test_equivalence_reuses_the_contact_that_exists_under_the_other_form() -> None:
    # La base tiene 52 + 10 (el formulario) y la persona ya escribio: su
    # contacto de Chatwoot es 521 + 10. Antes se creaba un segundo contacto
    # 52… y la respuesta caia en otra conversacion.
    inbox = _WhatsAppCloudInbox({f"+{MX_WHATSAPP}": 41})

    result = _first_touch(inbox.sender(), MX_FORM)

    assert result.status == "sent"
    assert inbox.searches == [f"+{MX_FORM}", f"+{MX_WHATSAPP}"]
    assert inbox.posts("/contacts") == []
    assert inbox.posts("/conversations") == [
        {"inbox_id": 1, "contact_id": 41, "source_id": MX_WHATSAPP}
    ]


@pytest.mark.parametrize(
    ("phone", "created_phone"),
    [
        # Mexico: el contacto nace con el 1, que es donde cae la respuesta.
        (MX_FORM, f"+{MX_WHATSAPP}"),
        (MX_WHATSAPP, f"+{MX_WHATSAPP}"),
        # Argentina: sin el 9, la forma que Chatwoot normaliza.
        (AR_FORM, f"+{AR_FORM}"),
        (AR_WHATSAPP, f"+{AR_FORM}"),
        # Cualquier otro pais: tal cual.
        ("573001234567", "+573001234567"),
    ],
)
def test_equivalence_creates_the_contact_in_the_delivery_form(
    phone: str, created_phone: str
) -> None:
    inbox = _WhatsAppCloudInbox()

    result = _first_touch(inbox.sender(), phone)

    assert result.status == "sent"
    [contact] = inbox.posts("/contacts")
    assert contact["phone_number"] == created_phone
    [conversation] = inbox.posts("/conversations")
    assert conversation["source_id"] == created_phone.lstrip("+")


def test_equivalence_prefers_the_whatsapp_form_when_both_contacts_exist(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # La misma persona dos veces en Chatwoot (medido: 14 pares en el inbox de
    # Johanna). Se usa el contacto de forma WhatsApp, donde cae la respuesta.
    inbox = _WhatsAppCloudInbox({f"+{AR_FORM}": 40, f"+{AR_WHATSAPP}": 41})

    with caplog.at_level("WARNING"):
        recipient = _run(inbox.sender().resolve_first_touch_recipient(phone=AR_FORM))

    assert recipient == FirstTouchRecipient(
        wa_id=AR_WHATSAPP, contact_id=41, source_id=AR_WHATSAPP
    )
    assert "first_touch_recipient_duplicated_in_chatwoot" in caplog.text
    assert "region=AR" in caplog.text
    assert "contact_ids=40,41" in caplog.text
    # El aviso lleva ids de Chatwoot y region, nunca el numero.
    assert AR_FORM not in caplog.text and AR_WHATSAPP not in caplog.text
    assert AR_FORM[2:] not in caplog.text


def test_a_resolved_recipient_is_not_searched_again() -> None:
    inbox = _WhatsAppCloudInbox({f"+{MX_WHATSAPP}": 41})
    sender = inbox.sender()
    recipient = _run(sender.resolve_first_touch_recipient(phone=MX_FORM))
    assert recipient == FirstTouchRecipient(
        wa_id=MX_WHATSAPP, contact_id=41, source_id=MX_WHATSAPP
    )
    inbox.searches.clear()

    result = _first_touch(sender, MX_FORM, recipient=recipient)

    assert result.status == "sent"
    assert inbox.searches == []
    assert inbox.posts("/conversations") == [
        {"inbox_id": 1, "contact_id": 41, "source_id": MX_WHATSAPP}
    ]


def test_a_recipient_resolved_without_a_contact_is_created_in_its_form() -> None:
    inbox = _WhatsAppCloudInbox()
    sender = inbox.sender()
    recipient = _run(sender.resolve_first_touch_recipient(phone=MX_FORM))
    assert recipient == FirstTouchRecipient(wa_id=MX_WHATSAPP)
    inbox.searches.clear()

    result = _first_touch(sender, MX_FORM, recipient=recipient)

    assert result.status == "sent"
    # Solo la lectura que confirma el contacto recien creado.
    assert inbox.searches == [f"+{MX_WHATSAPP}"]
    assert [c["phone_number"] for c in inbox.posts("/contacts")] == [f"+{MX_WHATSAPP}"]


def test_resolving_the_recipient_only_reads() -> None:
    inbox = _WhatsAppCloudInbox()

    _run(inbox.sender().resolve_first_touch_recipient(phone=MX_FORM))

    assert {method for method, _, _ in inbox.requests} == {"GET"}


def test_a_recipient_of_another_phone_is_blocked_before_any_request() -> None:
    inbox = _WhatsAppCloudInbox()
    other = FirstTouchRecipient(wa_id="5215512345679", contact_id=77, source_id="5215512345679")

    result = _first_touch(inbox.sender(), MX_FORM, recipient=other)

    assert result.status == "blocked"
    assert result.reason == FIRST_TOUCH_RECIPIENT_MISMATCH
    assert inbox.requests == []


def test_an_existing_contact_with_a_foreign_source_is_blocked() -> None:
    # El contacto se encuentra por su telefono, pero Chatwoot entregaria a su
    # source_id: si no es el mismo movil, no sale nada.
    inbox = _WhatsAppCloudInbox({f"+{MX_WHATSAPP}": 41})
    inbox.source_ids[41] = "5215599999999"

    result = _first_touch(inbox.sender(), MX_FORM)

    assert result.status == "blocked"
    assert result.reason == FIRST_TOUCH_RECIPIENT_MISMATCH
    assert inbox.posts("/conversations") == []
    assert inbox.posts("/messages") == []


def test_a_new_contact_that_chatwoot_binds_to_another_source_is_blocked() -> None:
    inbox = _WhatsAppCloudInbox(created_source_id="5215599999999")

    result = _first_touch(inbox.sender(), MX_FORM)

    assert result.status == "blocked"
    assert result.reason == "contact_inbox_source_mismatch"
    assert inbox.posts("/conversations") == []
    assert inbox.posts("/messages") == []


def test_a_blocked_contact_under_the_other_form_stops_the_send() -> None:
    inbox = _WhatsAppCloudInbox({f"+{MX_WHATSAPP}": 41}, blocked={41})

    result = _first_touch(inbox.sender(), MX_FORM)

    assert result.status == "failed"
    assert result.reason == "contact_blocked"
    assert inbox.posts("/contacts") == []


def test_without_the_equivalence_the_search_stays_exact() -> None:
    # Un sender sin el flag (todo runtime sin manifiesto) busca y crea con el
    # telefono exacto, como siempre, aunque exista el contacto de la otra forma.
    inbox = _WhatsAppCloudInbox({f"+{MX_WHATSAPP}": 41})
    sender = inbox.sender(equivalence=False)

    result = _first_touch(sender, MX_FORM)

    assert result.status == "sent"
    assert set(inbox.searches) == {f"+{MX_FORM}"}
    assert [c["phone_number"] for c in inbox.posts("/contacts")] == [f"+{MX_FORM}"]
    assert sender.whatsapp_equivalence_enabled is False


def test_without_the_equivalence_a_resolved_recipient_is_refused() -> None:
    inbox = _WhatsAppCloudInbox()
    sender = inbox.sender(equivalence=False)

    with pytest.raises(ValueError, match="whatsapp_equivalence_disabled"):
        _run(sender.resolve_first_touch_recipient(phone=MX_FORM))
    result = _first_touch(
        sender, MX_FORM, recipient=FirstTouchRecipient(wa_id=MX_WHATSAPP)
    )

    assert result.status == "blocked"
    assert result.reason == "recipient_not_supported"
    assert inbox.requests == []


def test_the_equivalence_needs_the_dynamic_recipient() -> None:
    with pytest.raises(ValueError, match="requires the dynamic recipient"):
        ChatwootMessageSender(
            chatwoot=_chatwoot(MockTransport()),
            inbox_id=1,
            allowed_jid="5215512345678@s.whatsapp.net",
            whatsapp_equivalence_enabled=True,
        )
    with pytest.raises(ValueError, match="invalid whatsapp equivalence flag"):
        ChatwootMessageSender(
            chatwoot=_chatwoot(MockTransport()),
            inbox_id=1,
            allowed_jid=None,
            dynamic_recipient_enabled=True,
            whatsapp_equivalence_enabled="true",  # type: ignore[arg-type]
        )


def test_a_recipient_is_a_contact_with_its_source_or_neither() -> None:
    with pytest.raises(ValueError, match="invalid_first_touch_recipient"):
        FirstTouchRecipient(wa_id=MX_WHATSAPP, contact_id=41)
    with pytest.raises(ValueError, match="invalid_first_touch_recipient"):
        FirstTouchRecipient(wa_id=MX_WHATSAPP, source_id=MX_WHATSAPP)


# ── La plantilla del primer contacto tras el formulario ──────────────
#
# El ancla precheckout_intent tiene su propia plantilla aprobada
# (plantillas.precheckout del manifiesto). A diferencia del pago fallido, que
# sin plantilla propia sale con la del carrito, este disparador no tiene
# prestamo: sin su plantilla no se manda nada. Los nombres son los del
# manifiesto de ATT1 (tests/fixtures/instances/att1/instancia.toml); Chatwoot
# es el emulador de arriba (deuda: no hay captura de sus respuestas al envio).

PRECHECKOUT_TRIGGER = "precheckout_intent"


def _att1_templates(**overrides: object) -> WhatsAppTemplateConfig:
    values: dict[str, object] = {
        "first_touch_name": "att1_carrito_abandonado_01",
        "payment_failure_name": "att1_compra_fallida_01",
        "followup_name": None,
        "language": "es_MX",
        "category": "MARKETING",
        "first_touch_parameter": "buyer_name_and_product",
        "first_touch_body_parameters": ("nombre", "producto"),
        "payment_failure_body_parameters": ("nombre", "producto"),
    }
    values.update(overrides)
    return WhatsAppTemplateConfig(**values)  # type: ignore[arg-type]


def _templated_sender(
    inbox: _WhatsAppCloudInbox, template: WhatsAppTemplateConfig
) -> ChatwootMessageSender:
    return ChatwootMessageSender(
        chatwoot=ChatwootClient(
            base_url="https://chatwoot.test",
            account_id=1,
            access_token="test-token",
            agent_bot_access_token="bot-token",
            agent_bot_id=99,
            transport=httpx.MockTransport(inbox.handler),
        ),
        inbox_id=1,
        allowed_jid=None,
        dynamic_recipient_enabled=True,
        template=template,
        whatsapp_equivalence_enabled=True,
    )


def test_the_precheckout_trigger_names_its_own_template() -> None:
    template = _att1_templates(
        precheckout_name="att1_interes_precheckout_01",
        precheckout_body_parameters=("nombre",),
    )

    assert template.first_touch_name_for(trigger_kind=PRECHECKOUT_TRIGGER) == (
        "att1_interes_precheckout_01"
    )
    assert template.declared_body_parameters(trigger_kind=PRECHECKOUT_TRIGGER) == (
        "nombre",
    )
    assert template.category_for(trigger_kind=PRECHECKOUT_TRIGGER) == "MARKETING"
    assert template.params(
        content="copy",
        followup=False,
        buyer_name="Ana",
        product_name="Alimenta tu Tiroides",
        trigger_kind=PRECHECKOUT_TRIGGER,
    ) == {
        "name": "att1_interes_precheckout_01",
        "category": "MARKETING",
        "language": "es_MX",
        "processed_params": {"body": {"1": "Ana"}},
    }
    # Los otros disparadores no cambian por declarar la plantilla del formulario.
    plain = _att1_templates()
    for trigger_kind in (None, "cart_abandonment", "payment_failure"):
        assert template.first_touch_name_for(
            trigger_kind=trigger_kind
        ) == plain.first_touch_name_for(trigger_kind=trigger_kind)
        assert template.params(
            content="copy", followup=False, buyer_name="Ana",
            product_name="ATT1", trigger_kind=trigger_kind,
        ) == plain.params(
            content="copy", followup=False, buyer_name="Ana",
            product_name="ATT1", trigger_kind=trigger_kind,
        )


def test_the_precheckout_trigger_without_its_template_has_no_template() -> None:
    # Nunca la del carrito, ni la del pago fallido, ni sus variables.
    template = _att1_templates()

    assert template.first_touch_name_for(trigger_kind=PRECHECKOUT_TRIGGER) is None
    assert template.declared_body_parameters(trigger_kind=PRECHECKOUT_TRIGGER) is None
    with pytest.raises(ValueError, match="template_disabled"):
        template.params(
            content="copy",
            followup=False,
            buyer_name="Ana",
            product_name="Alimenta tu Tiroides",
            trigger_kind=PRECHECKOUT_TRIGGER,
        )
    # El prestamo del pago fallido sigue como estaba.
    assert _att1_templates(
        payment_failure_name=None, payment_failure_body_parameters=None
    ).first_touch_name_for(trigger_kind="payment_failure") == "att1_carrito_abandonado_01"


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        (
            {"precheckout_body_parameters": ("nombre",)},
            "precheckout_body_parameters_without_template",
        ),
        (
            {
                "precheckout_name": "att1_interes_precheckout_01",
                "precheckout_body_parameters": ("cupon",),
            },
            "invalid_template_body_parameters",
        ),
        ({"precheckout_name": "  "}, "invalid_precheckout_template_name"),
    ],
)
def test_an_invalid_precheckout_template_is_refused(
    kwargs: dict[str, object], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        _att1_templates(**kwargs)


def test_the_sender_sends_the_first_contact_of_the_form_with_its_template() -> None:
    inbox = _WhatsAppCloudInbox()
    sender = _templated_sender(
        inbox,
        _att1_templates(
            precheckout_name="att1_interes_precheckout_01",
            precheckout_body_parameters=("nombre", "producto"),
        ),
    )

    result = _first_touch(
        sender, MX_FORM, product_name="Alimenta tu Tiroides",
        trigger_kind=PRECHECKOUT_TRIGGER,
    )

    assert result.status == "sent"
    [message] = inbox.posts("/conversations/200/messages")
    assert message["template_params"] == {
        "name": "att1_interes_precheckout_01",
        "category": "MARKETING",
        "language": "es_MX",
        "processed_params": {
            "body": {"1": "Lead de prueba", "2": "Alimenta tu Tiroides"}
        },
    }


def test_the_sender_blocks_the_first_contact_of_the_form_without_its_template() -> None:
    # Falla cerrado antes de tocar Chatwoot: ni busca ni crea el contacto, no
    # abre la conversacion y no manda la plantilla del carrito.
    inbox = _WhatsAppCloudInbox()
    sender = _templated_sender(inbox, _att1_templates())

    result = _first_touch(
        sender, MX_FORM, product_name="Alimenta tu Tiroides",
        trigger_kind=PRECHECKOUT_TRIGGER,
    )

    assert result.status == "blocked"
    assert result.reason == FIRST_TOUCH_TEMPLATE_NOT_CONFIGURED
    assert result.reason == "first_touch_template_not_configured"
    assert inbox.requests == []
    # El mismo sender sigue mandando el carrito.
    cart = _first_touch(
        sender, MX_FORM, product_name="Alimenta tu Tiroides",
        trigger_kind="cart_abandonment",
    )
    assert cart.status == "sent"
    [message] = inbox.posts("/conversations/200/messages")
    assert message["template_params"]["name"] == "att1_carrito_abandonado_01"  # type: ignore[index]


# ── Las tres plantillas de ATT1 contra el catalogo capturado del inbox 11 ──
#
# chatwoot_inbox_11_message_templates_20261001.json: el catalogo del inbox 11
# leido por psql de channel_whatsapp.message_templates el 2026-10-01. Lo que
# el sender pone en template_params (nombre, idioma, categoria y las claves de
# processed_params.body) tiene que ser lo que Meta aprobo para cada plantilla:
# si no, Meta rechaza el envio despues de empezado el pedido.

_CAPTURED_INBOX_11 = json.loads(
    (_FIXTURES / "chatwoot_inbox_11_message_templates_20261001.json").read_text(
        encoding="utf-8"
    )
)
_ATT1_TEMPLATE_BY_TRIGGER = {
    "cart_abandonment": "att1_carrito_abandonado_01",
    "payment_failure": "att1_compra_fallida_01",
    PRECHECKOUT_TRIGGER: "att1_interes_precheckout_01",
}


def _captured_att1(template_name: str) -> dict[str, object]:
    [template] = [
        t for t in _CAPTURED_INBOX_11["templates"] if t["name"] == template_name
    ]
    return template


def _captured_att1_placeholders(template_name: str) -> list[str]:
    [body] = [
        c for c in _captured_att1(template_name)["components"]  # type: ignore[union-attr]
        if c["type"] == "BODY"
    ]
    return sorted(set(re.findall(r"\{\{(\d+)\}\}", body["text"])))


def _att1_templates_with_the_form() -> WhatsAppTemplateConfig:
    return _att1_templates(
        precheckout_name="att1_interes_precheckout_01",
        precheckout_body_parameters=("nombre", "producto"),
    )


@pytest.mark.parametrize("trigger_kind", sorted(_ATT1_TEMPLATE_BY_TRIGGER))
def test_each_att1_trigger_fills_exactly_what_the_captured_template_declares(
    trigger_kind: str,
) -> None:
    name = _ATT1_TEMPLATE_BY_TRIGGER[trigger_kind]
    captured = _captured_att1(name)

    params = _att1_templates_with_the_form().params(
        content="copy",
        followup=False,
        buyer_name="Ana",
        product_name="Alimenta tu Tiroides",
        trigger_kind=trigger_kind,
    )

    assert _captured_att1_placeholders(name) == ["1", "2"]
    assert params == {
        "name": captured["name"],
        "category": captured["category"],
        "language": captured["language"],
        "processed_params": {"body": {"1": "Ana", "2": "Alimenta tu Tiroides"}},
    }
    assert sorted(params["processed_params"]["body"]) == (  # type: ignore[index]
        _captured_att1_placeholders(name)
    )
    assert captured["status"] == "APPROVED"


@pytest.mark.parametrize("trigger_kind", sorted(_ATT1_TEMPLATE_BY_TRIGGER))
def test_the_sender_sends_each_att1_trigger_as_the_captured_template(
    trigger_kind: str,
) -> None:
    name = _ATT1_TEMPLATE_BY_TRIGGER[trigger_kind]
    captured = _captured_att1(name)
    inbox = _WhatsAppCloudInbox()
    sender = _templated_sender(inbox, _att1_templates_with_the_form())

    result = _first_touch(
        sender, MX_FORM, product_name="Alimenta tu Tiroides", trigger_kind=trigger_kind,
    )

    assert result.status == "sent"
    [message] = inbox.posts("/conversations/200/messages")
    template_params = message["template_params"]
    assert (
        template_params["name"],  # type: ignore[index]
        template_params["language"],  # type: ignore[index]
        template_params["category"],  # type: ignore[index]
    ) == (captured["name"], captured["language"], captured["category"])
    assert sorted(template_params["processed_params"]["body"]) == (  # type: ignore[index]
        _captured_att1_placeholders(name)
    )


def test_one_declared_variable_does_not_fill_the_captured_att1_form_template() -> None:
    # att1_interes_precheckout_01 tiene {{1}} y {{2}}: una instancia que declare
    # solo "nombre" mandaria un cuerpo que Meta no aprobo. El sender no lee el
    # catalogo; lo frena el modo directo (parse_approved_template).
    params = _att1_templates(
        precheckout_name="att1_interes_precheckout_01",
        precheckout_body_parameters=("nombre",),
    ).params(
        content="copy",
        followup=False,
        buyer_name="Ana",
        product_name="Alimenta tu Tiroides",
        trigger_kind=PRECHECKOUT_TRIGGER,
    )

    assert sorted(params["processed_params"]["body"]) != (  # type: ignore[index]
        _captured_att1_placeholders("att1_interes_precheckout_01")
    )
