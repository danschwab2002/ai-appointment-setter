import asyncio
import json

import httpx

from bridge.chatwoot import ChatwootClient


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
