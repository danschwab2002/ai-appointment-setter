from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import httpx
import uvicorn

from bridge.app import Settings, create_app
from bridge.supabase import InboundOptOutResult, OptOutProjectionClaim


ALLOWED_JID = "12025550123@s.whatsapp.net"
WEBHOOK_SECRET = "controlled-http-e2e-secret"


class StatefulOptOutAuthority:
    """Stateful test authority that survives bridge restarts in this E2E."""

    def __init__(self) -> None:
        self.stopped = False
        self.apply_calls: list[dict[str, object]] = []
        self.reconcile_calls: list[dict[str, object]] = []
        self.finalizations: list[dict[str, object]] = []
        self._projection_claimed = False
        self._projection_finished = False

    async def has_chatwoot_opt_out_stop(self, **_: object) -> bool:
        return self.stopped

    async def apply_chatwoot_inbound_opt_out(
        self, **kwargs: object
    ) -> InboundOptOutResult:
        self.apply_calls.append(kwargs)
        self.stopped = True
        return InboundOptOutResult(
            outcome="applied",
            opt_out_event_id="controlled-opt-out-event",
            contact_id="controlled-contact",
            affected_cases=1,
            affected_actions=1,
            affected_attempts=0,
        )

    async def reconcile_chatwoot_opt_out_stop(
        self, **kwargs: object
    ) -> InboundOptOutResult:
        self.reconcile_calls.append(kwargs)
        return InboundOptOutResult(
            outcome="already_applied",
            opt_out_event_id="controlled-opt-out-event",
            contact_id="controlled-contact",
            affected_cases=0,
            affected_actions=0,
            affected_attempts=0,
        )

    async def claim_chatwoot_opt_out_projections(
        self, **_: object
    ) -> list[OptOutProjectionClaim]:
        if not self.stopped or self._projection_claimed or self._projection_finished:
            return []
        self._projection_claimed = True
        return [
            OptOutProjectionClaim(
                opt_out_event_id="controlled-opt-out-event",
                chatwoot_conversation_id=123,
                lease_generation=1,
            )
        ]

    async def finalize_chatwoot_opt_out_projection(
        self, **kwargs: object
    ) -> str:
        self.finalizations.append(kwargs)
        self._projection_claimed = False
        self._projection_finished = bool(kwargs["applied"])
        return "applied" if kwargs["applied"] else "retryable_failed"


class ControlledChatwoot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []
        self.reply_calls: list[dict[str, object]] = []
        self.macro_calls: list[int] = []

    async def validate_conversation_authority(
        self, *, conversation_id: int, expected_inbox_id: int
    ) -> None:
        assert conversation_id == 123
        assert expected_inbox_id == 7

    async def get_conversation_messages(
        self,
        *,
        conversation_id: int,
        limit: int = 20,
        required_message_ids: tuple[int, ...] = (),
    ) -> list[dict[str, object]]:
        assert conversation_id == 123
        available = {message["id"] for message in self.messages}
        assert set(required_message_ids) <= available
        return self.messages[-limit:]

    async def apply_opt_out_macro(self, *, conversation_id: int) -> None:
        self.macro_calls.append(conversation_id)

    async def send_agent_bot_reply(self, **kwargs: object) -> dict[str, object]:
        self.reply_calls.append(kwargs)
        return {"status": "sent", "message_id": 999}


class TemptingShadowProcessor:
    """Would propose a reply if the durable opt-out gate did not stop the turn."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.completed: set[str] = set()

    async def run(self, *, delivery_id: str, context: dict[str, object]) -> None:
        self.calls.append(delivery_id)
        self.completed.add(delivery_id)

    def record_failure(self, *, delivery_id: str, reason: str) -> None:
        self.completed.add(delivery_id)

    def has_result(self, *, delivery_id: str) -> bool:
        return delivery_id in self.completed

    def get_completed_proposal(
        self, *, delivery_id: str
    ) -> dict[str, object] | None:
        if delivery_id not in self.completed:
            return None
        return {"reply": "Esta respuesta nunca debe enviarse"}


def _signed_headers(raw_body: bytes, *, delivery_id: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(
        WEBHOOK_SECRET.encode(),
        timestamp.encode("ascii") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Chatwoot-Signature": signature,
        "X-Chatwoot-Timestamp": timestamp,
        "X-Chatwoot-Delivery": delivery_id,
    }


def _payload(*, message_id: int, content: str) -> bytes:
    return json.dumps(
        {
            "event": "message_created",
            "id": message_id,
            "content": content,
            "message_type": "incoming",
            "private": False,
            "account": {"id": 1},
            "inbox": {"id": 7},
            "conversation": {
                "id": 123,
                "inbox_id": 7,
                "contact_inbox": {"source_id": ALLOWED_JID},
            },
        },
        separators=(",", ":"),
    ).encode()


def _canonical_message(*, message_id: int, content: str) -> dict[str, object]:
    return {
        "id": message_id,
        "created_at": int(time.time()),
        "message_type": 0,
        "private": False,
        "content": content,
        "sender": {"type": "contact", "id": 20},
    }


@contextmanager
def _real_http_server(app: object) -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = int(sock.getsockname()[1])
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
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("controlled HTTP E2E condition did not become true")


def _app(
    *,
    capture_dir: Path,
    authority: StatefulOptOutAuthority,
    chatwoot: ControlledChatwoot,
    shadow: TemptingShadowProcessor,
) -> object:
    return create_app(
        Settings(
            webhook_secret=WEBHOOK_SECRET,
            allowed_jid=ALLOWED_JID,
            capture_dir=capture_dir,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
            chatwoot_durable_opt_out_enabled=True,
            chatwoot_opt_out_macro_id=9,
            opt_out_projection_worker_id="controlled-opt-out-projection",
            chatwoot_inbound_debounce_seconds=0,
            automated_replies_enabled=True,
        ),
        chatwoot_client=chatwoot,  # type: ignore[arg-type]
        shadow_processor=shadow,  # type: ignore[arg-type]
        supabase_client=authority,  # type: ignore[arg-type]
    )


def test_signed_opt_out_survives_restart_and_blocks_all_automated_replies(
    tmp_path: Path,
) -> None:
    authority = StatefulOptOutAuthority()
    chatwoot = ControlledChatwoot()
    shadow = TemptingShadowProcessor()
    opt_out_body = _payload(message_id=789, content="Quiero darme de baja")
    chatwoot.messages.append(
        _canonical_message(message_id=789, content="Quiero darme de baja")
    )

    with _real_http_server(
        _app(
            capture_dir=tmp_path,
            authority=authority,
            chatwoot=chatwoot,
            shadow=shadow,
        )
    ) as base_url:
        with httpx.Client(base_url=base_url, timeout=3) as client:
            health = client.get("/health")
            first = client.post(
                "/webhooks/chatwoot",
                content=opt_out_body,
                headers=_signed_headers(
                    opt_out_body,
                    delivery_id="controlled-opt-out-delivery",
                ),
            )
            duplicate = client.post(
                "/webhooks/chatwoot",
                content=opt_out_body,
                headers=_signed_headers(
                    opt_out_body,
                    delivery_id="controlled-opt-out-delivery",
                ),
            )
        assert health.status_code == 200
        assert first.status_code == 202
        assert duplicate.status_code == 200
        assert duplicate.json()["status"] == "duplicate"
        _wait_until(lambda: authority.stopped)
        semantic_replay = httpx.post(
            f"{base_url}/webhooks/chatwoot",
            content=opt_out_body,
            headers=_signed_headers(
                opt_out_body,
                delivery_id="controlled-opt-out-semantic-replay",
            ),
            timeout=3,
        )
        assert semantic_replay.status_code == 202
        _wait_until(lambda: len(authority.reconcile_calls) == 1)
        _wait_until(lambda: bool(authority.finalizations))

    assert len(authority.apply_calls) == 1
    assert authority.finalizations[0]["applied"] is True
    assert chatwoot.macro_calls == [123]
    assert shadow.calls == []
    assert chatwoot.reply_calls == []

    later_body = _payload(message_id=790, content="Gracias")
    chatwoot.messages.append(_canonical_message(message_id=790, content="Gracias"))
    with _real_http_server(
        _app(
            capture_dir=tmp_path,
            authority=authority,
            chatwoot=chatwoot,
            shadow=shadow,
        )
    ) as base_url:
        response = httpx.post(
            f"{base_url}/webhooks/chatwoot",
            content=later_body,
            headers=_signed_headers(
                later_body,
                delivery_id="controlled-post-restart-delivery",
            ),
            timeout=3,
        )
        assert response.status_code == 202
        _wait_until(lambda: len(authority.reconcile_calls) == 2)

    assert len(authority.apply_calls) == 1
    assert len(authority.reconcile_calls) == 2
    assert shadow.calls == []
    assert chatwoot.reply_calls == []
