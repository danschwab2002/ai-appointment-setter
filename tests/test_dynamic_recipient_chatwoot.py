import asyncio
import hashlib
import json
from pathlib import Path

import httpx

from bridge.chatwoot import ChatwootClient


def test_scoped_reply_uses_expected_jid_without_fixed_allowed_jid(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    scoped_jid = "12025550999@s.whatsapp.net"
    reply_hash = hashlib.sha256(b"2:10").hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/conversations/2"):
            return httpx.Response(
                200,
                json={"id": 2, "meta": {"sender": {"identifier": scoped_jid}}},
            )
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
        assert json.loads(request.content)["content_attributes"] == {
            "appointment_setter_reply_hash": reply_hash,
        }
        return httpx.Response(
            200,
            json={
                "id": 11,
                "conversation_id": 2,
                "message_type": 1,
                "private": False,
                "content": "Respuesta",
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
        allowed_jid=None,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        client.send_agent_bot_reply(
            conversation_id=2,
            trigger_message_id=10,
            delivery_id="dynamic-recipient",
            content="Respuesta",
            expected_jid=scoped_jid,
        )
    )

    assert result == {"status": "sent", "message_id": 11}
    assert [request.method for request in requests].count("POST") == 1
