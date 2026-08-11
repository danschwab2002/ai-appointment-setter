from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import uvicorn

from bridge.app import Settings, create_app


SECRET = "controlled-waba-ingress-secret"
ALLOWED_JID = "12025550123@s.whatsapp.net"


def _payload(*, inbox_id: int) -> bytes:
    return json.dumps(
        {
            "event": "message_created",
            "id": 789,
            "content": "Controlled inbound probe",
            "message_type": "incoming",
            "private": False,
            "account": {"id": 1},
            "inbox": {"id": inbox_id},
            "conversation": {
                "id": 123,
                "inbox_id": inbox_id,
                "contact_inbox": {"source_id": ALLOWED_JID},
            },
        },
        separators=(",", ":"),
    ).encode()


def _headers(body: bytes, *, delivery_id: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    digest = hmac.new(
        SECRET.encode(),
        timestamp.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Chatwoot-Signature": f"sha256={digest}",
        "X-Chatwoot-Timestamp": timestamp,
        "X-Chatwoot-Delivery": delivery_id,
    }


@contextmanager
def _http_server(app: object) -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    server = uvicorn.Server(
        uvicorn.Config(
            app,  # type: ignore[arg-type]
            log_level="warning",
            lifespan="on",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{int(sock.getsockname()[1])}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def test_real_http_waba_scope_rejects_legacy_then_captures_once(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            webhook_secret=SECRET,
            allowed_jid=ALLOWED_JID,
            capture_dir=tmp_path,
            max_age_seconds=300,
            chatwoot_account_id=1,
            chatwoot_inbox_id=6,
            automated_replies_enabled=False,
            hermes_shadow_enabled=False,
            pilot_boundary_enabled=False,
            dispatcher_enabled=False,
            dispatcher_outbound_enabled=False,
        )
    )
    legacy = _payload(inbox_id=1)
    official = _payload(inbox_id=6)

    with _http_server(app) as base_url:
        with httpx.Client(base_url=base_url, timeout=3) as client:
            health = client.get("/health")
            rejected = client.post(
                "/webhooks/chatwoot",
                content=legacy,
                headers=_headers(legacy, delivery_id="legacy-inbox-delivery"),
            )
            captured = client.post(
                "/webhooks/chatwoot",
                content=official,
                headers=_headers(official, delivery_id="official-inbox-delivery"),
            )
            duplicate = client.post(
                "/webhooks/chatwoot",
                content=official,
                headers=_headers(official, delivery_id="official-inbox-delivery"),
            )

    assert health.status_code == 200
    assert rejected.status_code == 200
    assert rejected.json() == {
        "status": "ignored",
        "reason": "inbox_not_allowed",
    }
    assert captured.status_code == 202
    assert captured.json()["status"] == "captured"
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    captures = list(tmp_path.glob("*.json"))
    assert len(captures) == 1
    assert json.loads(captures[0].read_text()) == json.loads(official)
