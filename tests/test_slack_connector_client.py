from __future__ import annotations

import asyncio
import json

import httpx

from slack_correlation.client import SlackClient


def test_slack_message_delivery_always_disables_link_and_media_unfurls() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "channel": "C123", "ts": "1.2"})

    client = SlackClient(
        bot_token="xoxb-not-a-secret",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        client.post_message(
            channel_id="C123",
            message={"text": "<https://reviews.example.test/review|Abrir>"},
        )
    )

    assert captured["unfurl_links"] is False
    assert captured["unfurl_media"] is False
