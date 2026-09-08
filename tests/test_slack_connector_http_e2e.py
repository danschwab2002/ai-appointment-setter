from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
import time

import httpx
import uvicorn

from slack_correlation.app import SlackConnectorSettings, create_app
from slack_correlation.client import SlackClient


class _FakeSlackHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict[str, object]]] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        body = json.loads(raw) if raw else {}
        type(self).requests.append((self.path, body))
        if self.path == "/api/auth.test":
            payload = {"ok": True, "team_id": "T12345678", "bot_id": "B12345678"}
        elif self.path == "/api/chat.postMessage":
            payload = {
                "ok": True,
                "channel": body["channel"],
                "ts": "1788800000.000001",
            }
        else:
            payload = {"ok": False, "error": "unknown_method"}
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _start_uvicorn(app) -> tuple[uvicorn.Server, threading.Thread, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = int(sock.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on")  # type: ignore[arg-type]
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    return server, thread, port


def test_notification_crosses_real_tcp_to_connector_and_fake_slack(
    tmp_path: Path,
) -> None:
    _FakeSlackHandler.requests = []
    fake_slack = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSlackHandler)
    fake_thread = threading.Thread(target=fake_slack.serve_forever, daemon=True)
    fake_thread.start()
    fake_port = int(fake_slack.server_address[1])
    token = "j" * 32
    settings = SlackConnectorSettings(
        ingress_enabled=True,
        notifications_enabled=True,
        bot_token="xoxb-synthetic",
        team_id="T12345678",
        channel_id="C0C0YEACVT2",
        storage_path=str(tmp_path / "slack.sqlite3"),
        tenant_tokens={"johanna": token, "att1": "a" * 32},
        poll_interval_seconds=1.0,
    )
    slack = SlackClient(
        bot_token="xoxb-synthetic",
        base_url=f"http://127.0.0.1:{fake_port}/api",
    )
    app = create_app(settings, slack_client=slack)
    connector, connector_thread, connector_port = _start_uvicorn(app)
    event_id = "11111111-1111-4111-8111-111111111111"
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{connector_port}", timeout=2
        ) as client:
            readiness = client.get("/ready")
            admitted = client.post(
                "/internal/v1/notifications",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "event_id": event_id,
                    "event_code": "HND-001",
                    "dedupe_key": "1" * 64,
                    "occurred_at": "2026-09-07T22:00:00Z",
                    "subject_ref": "C-11111111",
                    "reason_code": "explicit_human_request",
                    "state": "paused",
                },
            )
            deadline = time.monotonic() + 10
            while True:
                status = client.get(
                    f"/internal/v1/notifications/{event_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if status.json().get("delivery_state") == "accepted":
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(status.json())
                time.sleep(0.02)
    finally:
        connector.should_exit = True
        connector_thread.join(timeout=3)
        fake_slack.shutdown()
        fake_slack.server_close()
        fake_thread.join(timeout=3)

    assert readiness.status_code == 200
    assert readiness.json()["mode"] == "operational"
    assert admitted.status_code == 202
    assert status.status_code == 200
    assert status.json()["message_ts"] == "1788800000.000001"
    assert [path for path, _ in _FakeSlackHandler.requests] == [
        "/api/auth.test",
        "/api/chat.postMessage",
    ]
    posted = _FakeSlackHandler.requests[1][1]
    assert posted["channel"] == "C0C0YEACVT2"
    assert posted["text"] == "[p2] Nueva derivación · Johanna · C-11111111"
