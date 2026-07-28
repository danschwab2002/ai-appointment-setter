import asyncio
import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx

from bridge.app import Settings, create_app


class StubChatwootClient:
    def __init__(self, *, changed: bool = True, fail: bool = False) -> None:
        self.changed = changed
        self.fail = fail
        self.calls: list[tuple[int, str]] = []

    async def ensure_conversation_label(
        self, *, conversation_id: int, label: str
    ) -> bool:
        self.calls.append((conversation_id, label))
        if self.fail:
            request = httpx.Request("GET", "https://chatwoot.example.test")
            raise httpx.ConnectError("unavailable", request=request)
        return self.changed


def _signed_headers(
    raw_body: bytes,
    *,
    secret: str,
    delivery: str,
    timestamp: int | None = None,
) -> dict[str, str]:
    timestamp_text = str(timestamp if timestamp is not None else int(time.time()))
    signed = timestamp_text.encode("ascii") + b"." + raw_body
    signature = "sha256=" + hmac.new(
        secret.encode("utf-8"), signed, hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Chatwoot-Signature": signature,
        "X-Chatwoot-Timestamp": timestamp_text,
        "X-Chatwoot-Delivery": delivery,
    }


def _post(app: object, raw_body: bytes, headers: dict[str, str]) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/webhooks/chatwoot", content=raw_body, headers=headers
            )

    return asyncio.run(send())


def test_captures_a_signed_allowed_incoming_message(tmp_path: Path) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 789,
        "content": "Hola",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
        )
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="delivery-1"),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "captured",
        "delivery_id": "delivery-1",
    }
    captures = list(tmp_path.glob("*.json"))
    assert len(captures) == 1
    assert json.loads(captures[0].read_text()) == payload


def test_recognizes_configured_agent_bot_without_capturing_it(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "private": False,
        "sender": {
            "id": 1,
            "type": "agent_bot",
        },
        "conversation": {
            "meta": {
                "sender": {
                    "identifier": "12025550123@s.whatsapp.net",
                }
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        )
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="agentbot-delivery"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "automation_outgoing",
    }
    assert list(tmp_path.iterdir()) == []


def test_pauses_automation_when_a_human_sends_a_public_message(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "private": False,
        "sender": {
            "id": 1,
            "type": "user",
        },
        "conversation": {
            "id": 2,
            "meta": {
                "sender": {
                    "identifier": "12025550123@s.whatsapp.net",
                }
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    chatwoot = StubChatwootClient()
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="human-delivery"),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "automation_paused",
        "reason": "human_outgoing",
        "label_status": "added",
    }
    assert chatwoot.calls == [(2, "automation_paused")]
    assert list(tmp_path.iterdir()) == []


def test_fails_closed_when_chatwoot_cannot_apply_the_pause_label(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "private": False,
        "sender": {
            "id": 1,
            "type": "user",
        },
        "conversation": {
            "id": 2,
            "meta": {
                "sender": {
                    "identifier": "12025550123@s.whatsapp.net",
                }
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    chatwoot = StubChatwootClient(fail=True)
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="failed-human-delivery"),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "chatwoot_control_unavailable"}
    assert chatwoot.calls == [(2, "automation_paused")]
    assert list(tmp_path.iterdir()) == []


def test_rejects_a_stale_webhook(tmp_path: Path) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 789,
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            }
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
        )
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(
            raw_body,
            secret=secret,
            delivery="stale-delivery",
            timestamp=int(time.time()) - 301,
        ),
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "stale_webhook"}
    assert list(tmp_path.glob("*.json")) == []


def test_treats_a_repeated_delivery_as_an_idempotent_duplicate(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 789,
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            }
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = _signed_headers(
        raw_body,
        secret=secret,
        delivery="same-delivery",
    )
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
        )
    )

    first = _post(app, raw_body, headers)
    duplicate = _post(app, raw_body, headers)

    assert first.status_code == 202
    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "status": "duplicate",
        "delivery_id": "same-delivery",
    }
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_ignores_a_message_from_any_other_whatsapp_jid(tmp_path: Path) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 790,
        "content": "Este mensaje no debe activar el flujo",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": {
                "source_id": "12025550124@s.whatsapp.net",
            }
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
        )
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="other-sender"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "sender_not_allowed",
    }
    assert list(tmp_path.glob("*.json")) == []
