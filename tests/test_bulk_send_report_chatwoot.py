"""El adaptador del reporte del masivo contra un Chatwoot falso (encargo 10).

El Chatwoot falso sirve, con ``httpx.MockTransport`` y el ``ChatwootClient`` real
del bridge, lo capturado en
``chatwoot_conversation_inbox_11_conv_50_opt_out_paused_20261017.json`` (ATT1,
cuenta 2, bandeja 11, 2026-10-17, show + mensajes desde el bridge con el token
de control, anonimizada): el show tal cual en
``GET /api/v1/accounts/2/conversations/50`` y los mensajes en
``GET .../conversations/50/messages`` con la forma ``{"payload": [...]}`` de la
API (la de ``chatwoot_conversation_messages_inbox_13_conv_6_20261029.json``),
paginando hacia atras con ``before`` de a 20 como Chatwoot.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from bridge.bulk_send_report import (
    BulkSendRecipient,
    ChatwootBulkSendReader,
    build_report,
    read_conversations,
)
from bridge.chatwoot import ChatwootClient

FIXTURES = Path(__file__).parent / "fixtures"
CONV_50 = json.loads(
    (FIXTURES / "chatwoot_conversation_inbox_11_conv_50_opt_out_paused_20261017.json").read_text(
        encoding="utf-8"
    )
)["conversation"]
ACCOUNT = 2
INBOX = 11
PAGE = 20


class _FakeChatwoot:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = sorted(messages, key=lambda m: m["id"])
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        base = f"/api/v1/accounts/{ACCOUNT}/conversations/50"
        if request.url.path == base:
            return httpx.Response(200, json=CONV_50["conversation"])
        if request.url.path == base + "/messages":
            before = request.url.params.get("before")
            older = [m for m in self.messages if before is None or m["id"] < int(before)]
            return httpx.Response(200, json={"meta": {}, "payload": older[-PAGE:]})
        return httpx.Response(404, json={"error": "not found"})


def _client(fake: _FakeChatwoot) -> ChatwootClient:
    return ChatwootClient(
        base_url="https://chatwoot.invalid",
        account_id=ACCOUNT,
        access_token="token-de-prueba",
        transport=httpx.MockTransport(fake.handler),
    )


def _recipient() -> BulkSendRecipient:
    template = next(m for m in CONV_50["messages"] if m["id"] == 2820)
    return BulkSendRecipient(
        recipient_id="r-50",
        phone="+5215500000050",
        state="sent",
        conversation_id=50,
        message_id=2820,
        sent_at=template["created_at"],
    )


def test_reads_the_captured_conversation_and_builds_the_report() -> None:
    fake = _FakeChatwoot(list(CONV_50["messages"]))
    reader = ChatwootBulkSendReader(_client(fake), inbox_id=INBOX)

    reading = asyncio.run(reader.read_conversation(conversation_id=50, since_message_id=2820))

    assert reading.labels == ("automation_paused",)
    assert reading.team_present is True and reading.assignee_present is False
    assert [m["id"] for m in reading.messages] == sorted(m["id"] for m in CONV_50["messages"])
    report = build_report([_recipient()], {50: reading})
    assert (report.read, report.replies, report.opt_outs, report.handoffs) == (1, 1, 1, 1)
    assert all("/contacts" not in r.url.path for r in fake.requests)


def test_pages_back_until_the_template_when_it_is_older_than_the_window() -> None:
    # 45 mensajes del agente despues de la conversacion capturada: la plantilla
    # queda dos paginas atras y fuera de los ultimos 5.
    last = CONV_50["messages"][-1]
    filler = [
        {**last, "id": 4000 + i, "created_at": last["created_at"] + i, "status": "read",
         "content_attributes": {}, "message_type": 1}
        for i in range(45)
    ]
    fake = _FakeChatwoot([*CONV_50["messages"], *filler])
    reader = ChatwootBulkSendReader(_client(fake), inbox_id=INBOX, messages_limit=5)

    reading = asyncio.run(reader.read_conversation(conversation_id=50, since_message_id=2820))

    ids = [m["id"] for m in reading.messages]
    assert 2820 in ids and ids[-5:] == [4040, 4041, 4042, 4043, 4044]
    befores = [r.url.params.get("before") for r in fake.requests if r.url.path.endswith("/messages")]
    assert befores.count(None) == 2 and len(befores) >= 4
    report = build_report([_recipient()], {50: reading})
    assert report.read == 1 and report.unreadable == 0


def test_a_conversation_of_another_inbox_is_unreadable() -> None:
    fake = _FakeChatwoot(list(CONV_50["messages"]))
    reader = ChatwootBulkSendReader(_client(fake), inbox_id=13)

    readings = asyncio.run(read_conversations(reader, [_recipient()]))

    assert readings == {50: None}
    assert build_report([_recipient()], readings).unreadable == 1
