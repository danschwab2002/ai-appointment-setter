from __future__ import annotations

import json
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlencode

import httpx
import uvicorn

from slack_correlation.app import SlackConnectorSettings, create_app
from slack_correlation.client import SlackClient
from slack_correlation.operator_client import OperatorBridgeClient
from bridge.app import Settings as BridgeSettings, create_app as create_bridge_app


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
        elif self.path == "/api/views.open":
            payload = {"ok": True, "view": {
                "id": "V12345678", "team_id": "T12345678",
                "callback_id": body["view"]["callback_id"],
            }}
        elif self.path == "/api/views.update":
            payload = {"ok": True, "view": {
                "id": body["view_id"], "team_id": "T12345678",
                "callback_id": body["view"]["callback_id"],
            }}
        elif self.path == "/api/chat.update":
            payload = {"ok": True, "channel": body["channel"], "ts": body["ts"]}
        else:
            payload = {"ok": False, "error": "unknown_method"}
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class _BridgeCorrelationStore:
    case_id = "11111111-1111-4111-8111-111111111111"
    candidate_id = "22222222-2222-4222-8222-222222222222"
    command_id = "33333333-3333-4333-8333-333333333333"

    def __init__(self) -> None:
        self.prepare_calls: list[dict] = []
        self.confirm_calls: list[dict] = []

    async def get_unresolved_purchase_intent_correlation(self, **kwargs):
        assert kwargs["webhook_event_id"] == self.case_id
        return {
            "webhook_event_id": self.case_id,
            "scope_id": "44444444-4444-4444-8444-444444444444",
            "event_type": "PURCHASE_APPROVED", "outcome": "conflict",
            "reason_code": "email_phone_conflict", "candidate_count": 1,
            "manual_handoff_required": True,
            "observed_at": "2026-09-10T00:00:00+00:00",
            "scope": {"tenant_ref": "lancemos", "funnel_ref": "psicologajohanna",
                      "product_ref": "f106691755g", "offer_ref": "bxjge6zq"},
            "identity": {"email_present": True, "phone_present": True,
                         "masked_email": "b***r@example.com", "masked_phone": "********4567"},
            "candidates": [{"purchase_intent_id": self.candidate_id,
                            "email_match": True, "phone_match": True,
                            "submitted_at": "2026-09-10T00:00:00+00:00",
                            "lifecycle_state": "waiting_for_purchase",
                            "masked_email": "b***r@example.com",
                            "masked_phone": "********4567"}],
        }

    async def prepare_operator_correlation_resolution(self, **kwargs):
        self.prepare_calls.append(kwargs)
        return {"command_id": self.command_id,
                "idempotency_key": kwargs["idempotency_key"],
                "webhook_event_id": self.case_id,
                "action": kwargs["action"],
                "selected_purchase_intent_id": kwargs["selected_purchase_intent_id"],
                "verification_basis": kwargs["verification_basis"],
                "deterministic_outcome": "ambiguous",
                "deterministic_reason_code": "multiple_candidates",
                "candidate_count": 1, "expires_at": "2026-09-10T01:00:00+00:00",
                "requires_human_approval": True, "automation_blocked": True}

    async def confirm_operator_correlation_resolution(self, **kwargs):
        self.confirm_calls.append(kwargs)
        return {"resolution_id": "55555555-5555-4555-8555-555555555555",
                "command_id": self.command_id, "webhook_event_id": self.case_id,
                "resolution_outcome": "linked_candidate",
                "effective_purchase_intent_id": self.candidate_id,
                "deterministic_outcome": "ambiguous",
                "applied_at": "2026-09-10T00:10:00+00:00", "replayed": False,
                "automation_blocked": True}


def _signed_interaction(client: httpx.Client, secret: str, payload: dict) -> httpx.Response:
    timestamp = str(int(time.time()))
    raw = urlencode({"payload": json.dumps(payload, separators=(",", ":"))}).encode()
    signature = "v0=" + hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + raw, hashlib.sha256
    ).hexdigest()
    return client.post("/slack/interactions", content=raw, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": signature,
    })


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
        activation_mode="one_shot",
        activation_generation=1,
        bot_token="xoxb-synthetic",
        team_id="T12345678",
        tenant_channels={"johanna": "C0C0YEACVT2"},
        storage_path=str(tmp_path / "slack.sqlite3"),
        tenant_tokens={"johanna": token},
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
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Expected-Tenant-Ref": "johanna",
                },
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
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Expected-Tenant-Ref": "johanna",
                    },
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


def test_interaction_crosses_connector_bridge_and_slack_simulator(tmp_path: Path) -> None:
    _FakeSlackHandler.requests = []
    fake_slack = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSlackHandler)
    fake_thread = threading.Thread(target=fake_slack.serve_forever, daemon=True)
    fake_thread.start()
    slack_port = int(fake_slack.server_address[1])
    bridge_store = _BridgeCorrelationStore()
    bridge_app = create_bridge_app(
        BridgeSettings(
            webhook_secret="unused", allowed_jid="593999999999@s.whatsapp.net",
            capture_dir=tmp_path / "capture", max_age_seconds=300,
            operator_correlation_read_enabled=True,
            operator_correlation_read_token="r" * 32,
            operator_correlation_tenant_ref="lancemos",
            operator_correlation_funnel_ref="psicologajohanna",
            operator_correlation_write_enabled=True,
            operator_correlation_write_token="w" * 32,
            operator_correlation_actor_ref="fallback-operator",
            operator_correlation_actor_prefix="slack",
        ),
        supabase_client=bridge_store,  # type: ignore[arg-type]
    )
    bridge, bridge_thread, bridge_port = _start_uvicorn(bridge_app)
    secret = "synthetic-signing-secret"
    producer_token = "j" * 32
    slack = SlackClient(
        bot_token="xoxb-synthetic", base_url=f"http://127.0.0.1:{slack_port}/api"
    )
    operator = OperatorBridgeClient(
        base_url=f"http://127.0.0.1:{bridge_port}",
        read_bearer="r" * 32, write_bearer="w" * 32,
    )
    connector_app = create_app(
        SlackConnectorSettings(
            ingress_enabled=True, notifications_enabled=True, interactions_enabled=True,
            activation_mode="one_shot", activation_generation=1,
            bot_token="xoxb-synthetic", signing_secret=secret,
            team_id="T12345678", tenant_channels={"johanna": "C0C0YEACVT2"},
            tenant_tokens={"johanna": producer_token},
            tenant_operator_user_ids={"johanna": frozenset({"U12345678"})},
            operator_backends={"johanna": {
                "base_url": "https://operator.invalid",
                "read_token": "r" * 32, "write_token": "w" * 32,
            }},
            storage_path=str(tmp_path / "interactive.sqlite3"),
        ),
        slack_client=slack,
        operator_clients={"johanna": operator},
    )
    connector, connector_thread, connector_port = _start_uvicorn(connector_app)
    event_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{connector_port}", timeout=3) as client:
            admitted = client.post("/internal/v1/notifications", headers={
                "Authorization": f"Bearer {producer_token}",
                "X-Expected-Tenant-Ref": "johanna",
            }, json={
                "event_id": event_id, "event_code": "COR-001", "dedupe_key": "a" * 64,
                "occurred_at": "2026-09-10T00:00:00Z",
                "subject_ref": f"C-{bridge_store.case_id}",
            })
            assert admitted.status_code == 202
            deadline = time.monotonic() + 10
            while not any(path == "/api/chat.postMessage" for path, _ in _FakeSlackHandler.requests):
                assert time.monotonic() < deadline
                time.sleep(0.02)
            while True:
                delivered = client.get(
                    f"/internal/v1/notifications/{event_id}",
                    headers={
                        "Authorization": f"Bearer {producer_token}",
                        "X-Expected-Tenant-Ref": "johanna",
                    },
                )
                if delivered.json().get("delivery_state") == "accepted":
                    break
                assert time.monotonic() < deadline
                time.sleep(0.02)
            opened = _signed_interaction(client, secret, {
                "type": "block_actions", "team": {"id": "T12345678"},
                "user": {"id": "U12345678"}, "channel": {"id": "C0C0YEACVT2"},
                "message": {"ts": "1788800000.000001"}, "trigger_id": "123.456.valid",
                "actions": [{"type": "button", "action_id": "review_operator_correlation",
                             "value": bridge_store.case_id}],
            })
            assert opened.status_code == 200 and opened.json() == {}
            deadline = time.monotonic() + 10
            while not any(path == "/api/views.open" for path, _ in _FakeSlackHandler.requests):
                assert time.monotonic() < deadline
                time.sleep(0.02)
            opened_body = next(body for path, body in _FakeSlackHandler.requests
                               if path == "/api/views.open")
            token = json.loads(opened_body["view"]["private_metadata"])["review_token"]
            prepared = _signed_interaction(client, secret, {
                "type": "view_submission", "team": {"id": "T12345678"},
                "user": {"id": "U12345678"},
                "view": {"id": "V12345678", "hash": "1.abc",
                         "callback_id": "prepare_operator_correlation_resolution",
                         "private_metadata": json.dumps({"review_token": token}),
                         "state": {"values": {
                             "resolution": {"selected_resolution": {"selected_option": {
                                 "value": bridge_store.candidate_id}}},
                             "verification": {"verification_basis": {"selected_option": {
                                 "value": "operator_source_record"}}},
                         }}},
            })
            assert prepared.json()["view"]["callback_id"] == "operator_correlation_resolution_processing"
            deadline = time.monotonic() + 10
            while not any(
                path == "/api/views.update" and body["view"]["callback_id"] == "confirm_operator_correlation_resolution"
                for path, body in _FakeSlackHandler.requests
            ):
                assert time.monotonic() < deadline
                time.sleep(0.02)
            confirmed = _signed_interaction(client, secret, {
                "type": "view_submission", "team": {"id": "T12345678"},
                "user": {"id": "U12345678"},
                "view": {"id": "V12345678", "hash": "2.def",
                         "callback_id": "confirm_operator_correlation_resolution",
                         "private_metadata": json.dumps({"review_token": token}),
                         "state": {"values": {}}},
            })
            assert confirmed.json()["view"]["callback_id"] == "operator_correlation_resolution_processing"
            deadline = time.monotonic() + 10
            while not any(path == "/api/chat.update" for path, _ in _FakeSlackHandler.requests):
                assert time.monotonic() < deadline
                time.sleep(0.02)
    finally:
        connector.should_exit = True
        connector_thread.join(timeout=5)
        bridge.should_exit = True
        bridge_thread.join(timeout=5)
        fake_slack.shutdown()
        fake_slack.server_close()
        fake_thread.join(timeout=3)

    paths = [path for path, _ in _FakeSlackHandler.requests]
    assert paths.count("/api/chat.postMessage") == 1
    assert paths.count("/api/chat.update") == 1
    assert len(bridge_store.prepare_calls) == len(bridge_store.confirm_calls) == 1
    assert bridge_store.prepare_calls[0]["actor_ref"] == "slack.u12345678"
    assert bridge_store.confirm_calls[0]["actor_ref"] == "slack.u12345678"
    updated = next(body for path, body in _FakeSlackHandler.requests
                   if path == "/api/chat.update")
    assert updated["channel"] == "C0C0YEACVT2"
    assert updated["ts"] == "1788800000.000001"
    assert any(path == "/api/views.update" and body["view"]["callback_id"] == "operator_correlation_resolution_complete"
               for path, body in _FakeSlackHandler.requests)
