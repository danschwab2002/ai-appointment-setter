from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import uvicorn

from bridge.app import Settings, create_app
from bridge.chatwoot import ChatwootClient
from bridge.supabase import HumanHandoffProjectionClaim, HumanHandoffProjectionStatus


class HandoffReadinessAuthority:
    def __init__(self) -> None:
        self.status_calls = 0
        self.claim_calls = 0

    async def get_human_handoff_projection_status(
        self,
    ) -> HumanHandoffProjectionStatus:
        self.status_calls += 1
        return HumanHandoffProjectionStatus(
            pending_count=2,
            retryable_count=1,
            delivery_unknown_count=1,
            conflict_count=0,
            dead_letter_count=0,
        )

    async def claim_human_handoff_projection_effects(
        self, **_: object
    ) -> list[HumanHandoffProjectionClaim]:
        self.claim_calls += 1
        return []


@contextmanager
def _real_http_server(app: object) -> Iterator[str]:
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


def test_handoff_projection_readiness_over_real_http(tmp_path: Path) -> None:
    authority = HandoffReadinessAuthority()
    chatwoot_requests: list[httpx.Request] = []

    def chatwoot_handler(request: httpx.Request) -> httpx.Response:
        chatwoot_requests.append(request)
        return httpx.Response(500)

    chatwoot = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid="12025550123@s.whatsapp.net",
        transport=httpx.MockTransport(chatwoot_handler),
    )
    app = create_app(
        Settings(
            webhook_secret="controlled-secret",
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
            human_handoff_projection_enabled=True,
            human_handoff_projection_worker_id="controlled-handoff-worker",
            human_handoff_projection_poll_interval_seconds=0.05,
        ),
        supabase_client=authority,  # type: ignore[arg-type]
        chatwoot_client=chatwoot,
    )

    with _real_http_server(app) as base_url:
        response = httpx.get(f"{base_url}/ready", timeout=5)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "pilot_boundary": "disabled",
        "automation_state": "default_off",
        "reason_code": "pilot_boundary_disabled",
        "precheckout_delayed_first_touch": "disabled",
        "human_handoff_projection": "configured",
        "human_handoff_pending": "2",
        "human_handoff_retryable": "1",
        "human_handoff_delivery_unknown": "1",
        "human_handoff_conflicts": "0",
        "human_handoff_dead_letters": "0",
    }
    assert authority.status_calls == 1
    assert authority.claim_calls >= 1
    assert chatwoot_requests == []
