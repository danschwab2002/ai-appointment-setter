import asyncio
import hashlib
import hmac
import json
import os
import stat
import time
from pathlib import Path

import httpx
import pytest

from bridge.app import Settings, _capture_payload, build_app, create_app
from bridge.chatwoot import ChatwootProtocolError
from bridge.chatwoot_inbox import (
    ChatwootWorker,
    DurableChatwootInbox,
    RetryableChatwootWorkError,
)


class StubChatwootClient:
    def __init__(
        self,
        *,
        changed: bool = True,
        fail: bool = False,
        messages: list[dict[str, object]] | None = None,
        history_error: Exception | None = None,
    ) -> None:
        self.changed = changed
        self.fail = fail
        self.calls: list[tuple[int, str]] = []
        self.messages = messages or []
        self.history_error = history_error
        self.history_calls: list[tuple[int, int]] = []
        self.reply_calls: list[dict[str, object]] = []

    async def get_conversation_messages(
        self, *, conversation_id: int, limit: int = 20
    ) -> list[dict[str, object]]:
        self.history_calls.append((conversation_id, limit))
        if self.history_error is not None:
            raise self.history_error
        if self.fail:
            request = httpx.Request("GET", "https://chatwoot.example.test")
            raise httpx.ConnectError("unavailable", request=request)
        return self.messages[-limit:]

    async def ensure_conversation_label(
        self, *, conversation_id: int, label: str
    ) -> bool:
        self.calls.append((conversation_id, label))
        if self.fail:
            request = httpx.Request("GET", "https://chatwoot.example.test")
            raise httpx.ConnectError("unavailable", request=request)
        return self.changed

    async def send_agent_bot_reply(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        delivery_id: str,
        content: str,
    ) -> dict[str, object]:
        self.reply_calls.append(
            {
                "conversation_id": conversation_id,
                "trigger_message_id": trigger_message_id,
                "delivery_id": delivery_id,
                "content": content,
            }
        )
        return {"status": "sent", "message_id": 900}


class StubShadowProcessor:
    def __init__(self, proposal: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.failures: list[tuple[str, str]] = []
        self.completed_delivery_ids: set[str] = set()
        self.proposal = proposal

    async def run(
        self, *, delivery_id: str, context: dict[str, object]
    ) -> None:
        self.calls.append((delivery_id, context))
        self.completed_delivery_ids.add(delivery_id)

    def record_failure(self, *, delivery_id: str, reason: str) -> None:
        self.failures.append((delivery_id, reason))
        self.completed_delivery_ids.add(delivery_id)

    def has_result(self, *, delivery_id: str) -> bool:
        return delivery_id in self.completed_delivery_ids

    def get_completed_proposal(
        self, *, delivery_id: str
    ) -> dict[str, object] | None:
        if delivery_id not in self.completed_delivery_ids:
            return None
        return self.proposal


class BlockingShadowProcessor(StubShadowProcessor):
    async def run(
        self, *, delivery_id: str, context: dict[str, object]
    ) -> None:
        self.calls.append((delivery_id, context))
        await asyncio.Event().wait()


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


def test_rejects_an_oversized_chatwoot_body_before_authentication(
    tmp_path: Path,
) -> None:
    raw_body = b"x" * (1024 * 1024 + 1)
    headers = _signed_headers(
        raw_body,
        secret="webhook-secret",
        delivery="oversized-delivery",
    )
    headers["X-Chatwoot-Signature"] = "sha256=invalid"
    app = create_app(
        Settings(
            webhook_secret="webhook-secret",
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
        )
    )

    response = _post(app, raw_body, headers)

    assert response.status_code == 413
    assert response.json() == {"detail": "chatwoot_webhook_body_too_large"}
    assert list(tmp_path.iterdir()) == []


def test_rejects_a_symlinked_chatwoot_work_directory(tmp_path: Path) -> None:
    capture_dir = tmp_path / "captures"
    outside_dir = tmp_path / "outside"
    capture_dir.mkdir()
    outside_dir.mkdir()
    (capture_dir / ".work").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(RuntimeError, match="chatwoot_work_dir_not_private"):
        DurableChatwootInbox(capture_dir / ".work")


def test_capture_is_fsynced_before_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsync_targets: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        fsync_targets.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    captured = _capture_payload(
        capture_dir=tmp_path,
        delivery_id="capture-delivery",
        payload={"event": "message_created"},
    )

    assert captured is True
    assert fsync_targets == ["file", "directory"]
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_chatwoot_worker_loop_survives_an_unexpected_iteration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    inbox = DurableChatwootInbox(tmp_path / ".work")
    inbox.admit(delivery_id="worker-delivery", payload={"event": "test"})
    admitted_items = inbox.admitted_items
    scans = 0

    def flaky_admitted_items():  # type: ignore[no-untyped-def]
        nonlocal scans
        scans += 1
        if scans == 1:
            raise RuntimeError("scan failed with private data")
        return admitted_items()

    monkeypatch.setattr(inbox, "admitted_items", flaky_admitted_items)
    handled: list[str] = []

    async def handler(delivery_id: str, payload: dict[str, object]) -> None:
        handled.append(delivery_id)

    worker = ChatwootWorker(
        inbox=inbox,
        handler=handler,
        poll_interval_seconds=0.01,
    )

    async def run_worker() -> None:
        await worker.start()
        try:
            async with asyncio.timeout(1):
                while not handled:
                    await asyncio.sleep(0.01)
        finally:
            await worker.stop()

    caplog.set_level("WARNING", logger="bridge.chatwoot_inbox")
    asyncio.run(run_worker())

    assert handled == ["worker-delivery"]
    assert "chatwoot_worker_iteration_failed error_type=RuntimeError" in caplog.messages
    assert "private data" not in caplog.text


def test_chatwoot_worker_scans_the_inbox_once_per_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox = DurableChatwootInbox(tmp_path / ".work")
    inbox.admit(delivery_id="delivery-one", payload={"event": "test"})
    inbox.admit(delivery_id="delivery-two", payload={"event": "test"})
    admitted_items = inbox.admitted_items
    scans = 0

    def counted_admitted_items():  # type: ignore[no-untyped-def]
        nonlocal scans
        scans += 1
        return admitted_items()

    monkeypatch.setattr(inbox, "admitted_items", counted_admitted_items)
    handled: list[str] = []

    async def handler(delivery_id: str, payload: dict[str, object]) -> None:
        handled.append(delivery_id)

    worker = ChatwootWorker(inbox=inbox, handler=handler)

    asyncio.run(worker.run_once())

    assert scans == 1
    assert sorted(handled) == ["delivery-one", "delivery-two"]


def test_chatwoot_worker_times_out_a_blocked_handler_and_preserves_work(
    tmp_path: Path,
) -> None:
    inbox = DurableChatwootInbox(tmp_path / ".work")
    inbox.admit(delivery_id="blocked-delivery", payload={"event": "test"})

    async def blocked_handler(
        delivery_id: str, payload: dict[str, object]
    ) -> None:
        await asyncio.Event().wait()

    worker = ChatwootWorker(
        inbox=inbox,
        handler=blocked_handler,
        handler_timeout_seconds=0.01,
    )

    asyncio.run(worker.run_once())

    work_path = next((tmp_path / ".work").glob("*.json"))
    envelope = json.loads(work_path.read_text(encoding="utf-8"))
    assert envelope["status"] == "admitted"
    assert envelope["attempts"] == 1
    assert envelope["last_error_type"] == "TimeoutError"


def test_chatwoot_worker_dead_letters_an_unclassified_error_after_eight_attempts(
    tmp_path: Path,
) -> None:
    inbox = DurableChatwootInbox(tmp_path / ".work")
    inbox.admit(delivery_id="invalid-delivery", payload={"event": "test"})

    async def invalid_handler(
        delivery_id: str, payload: dict[str, object]
    ) -> None:
        raise ValueError("invalid work")

    worker = ChatwootWorker(inbox=inbox, handler=invalid_handler)
    work_path = next((tmp_path / ".work").glob("*.json"))

    for _ in range(8):
        envelope = json.loads(work_path.read_text(encoding="utf-8"))
        envelope["next_attempt_at"] = 0
        work_path.write_text(json.dumps(envelope), encoding="utf-8")
        asyncio.run(worker.run_once())

    failed = json.loads(work_path.read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["attempts"] == 8
    assert failed["last_error_type"] == "ValueError"


def test_retryable_failure_backoff_stays_bounded_after_many_attempts(
    tmp_path: Path,
) -> None:
    inbox = DurableChatwootInbox(tmp_path / ".work")
    inbox.admit(delivery_id="long-outage", payload={"event": "test"})
    work_path = next((tmp_path / ".work").glob("*.json"))
    envelope = json.loads(work_path.read_text(encoding="utf-8"))
    envelope["attempts"] = 2048
    envelope["next_attempt_at"] = 0
    work_path.write_text(json.dumps(envelope), encoding="utf-8")

    async def unavailable_handler(
        delivery_id: str, payload: dict[str, object]
    ) -> None:
        raise RetryableChatwootWorkError("still unavailable")

    worker = ChatwootWorker(inbox=inbox, handler=unavailable_handler)
    before = time.time()

    asyncio.run(worker.run_once())

    retried = json.loads(work_path.read_text(encoding="utf-8"))
    assert retried["status"] == "admitted"
    assert retried["attempts"] == 2049
    assert before < retried["next_attempt_at"] <= before + 61


def test_chatwoot_worker_start_restarts_a_finished_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = ChatwootWorker(
        inbox=DurableChatwootInbox(tmp_path / ".work"),
        handler=lambda delivery_id, payload: asyncio.sleep(0),
    )
    runs = 0
    keep_second_run_alive = asyncio.Event()

    async def controlled_run() -> None:
        nonlocal runs
        runs += 1
        if runs > 1:
            await keep_second_run_alive.wait()

    monkeypatch.setattr(worker, "_run", controlled_run)

    async def restart_worker() -> None:
        await worker.start()
        await asyncio.sleep(0)
        first_task = worker._task
        assert first_task is not None and first_task.done()

        await worker.start()
        await asyncio.sleep(0)
        assert worker._task is not first_task
        assert runs == 2
        keep_second_run_alive.set()
        await worker.stop()

    asyncio.run(restart_worker())


def test_lifespan_stops_chatwoot_worker_after_application_error(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            webhook_secret="webhook-secret",
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
        ),
        chatwoot_client=StubChatwootClient(),
    )
    worker = app.state.chatwoot_worker
    assert worker is not None

    async def fail_inside_lifespan() -> None:
        with pytest.raises(RuntimeError, match="application failed"):
            async with app.router.lifespan_context(app):
                assert worker._task is not None
                raise RuntimeError("application failed")
        assert worker._task is None

    asyncio.run(fail_inside_lifespan())


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


def test_processes_a_normalized_shadow_evaluation_for_an_allowed_message(
    tmp_path: Path,
) -> None:
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
    shadow = StubShadowProcessor()
    chatwoot = StubChatwootClient(
        messages=[
            {
                "id": 789,
                "message_type": 0,
                "private": False,
                "content": "Hola",
                "sender": {"type": "contact", "id": 20},
            }
        ]
    )
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="shadow-delivery"),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "delivery_id": "shadow-delivery",
    }
    asyncio.run(app.state.chatwoot_worker.run_once())
    assert shadow.calls == [
        (
            "shadow-delivery",
            {
                "conversation_ref": "123",
                "human_handoff_confirmed": False,
                "known_fields": {
                    "person_name": None,
                    "location": None,
                    "role": None,
                    "company_name": None,
                    "company_size": None,
                    "business_model": None,
                    "company_operational": None,
                    "can_invest_in_education": None,
                },
                "messages": [
                    {
                        "actor": "prospect",
                        "text": "Hola",
                    }
                ],
            },
        )
    ]
    assert chatwoot.reply_calls == []


def test_acknowledges_durable_work_without_waiting_for_hermes(
    tmp_path: Path,
) -> None:
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
    shadow = BlockingShadowProcessor()
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        ),
        chatwoot_client=StubChatwootClient(
            messages=[
                {
                    "id": 789,
                    "message_type": 0,
                    "private": False,
                    "content": "Hola",
                    "sender": {"type": "contact", "id": 20},
                }
            ]
        ),
        shadow_processor=shadow,
    )

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await asyncio.wait_for(
                client.post(
                    "/webhooks/chatwoot",
                    content=raw_body,
                    headers=_signed_headers(
                        raw_body,
                        secret=secret,
                        delivery="fast-ack-delivery",
                    ),
                ),
                timeout=0.1,
            )

    response = asyncio.run(send())

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "delivery_id": "fast-ack-delivery",
    }
    assert shadow.calls == []
    work_items = list((tmp_path / ".work").glob("*.json"))
    assert len(work_items) == 1
    admitted = json.loads(work_items[0].read_text(encoding="utf-8"))
    assert admitted == {
        "status": "admitted",
        "delivery_id": "fast-ack-delivery",
        "payload": payload,
    }


def test_sends_the_validated_agent_reply_for_an_allowed_message(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    proposal: dict[str, object] = {
        "decision": "ask_question",
        "qualification_status": "in_progress",
        "reason_code": "need_person_name",
        "reply": "¡Hola! Soy el asistente virtual de Dan. ¿Cómo te llamás?",
        "captured_fields": {
            "person_name": None,
            "location": None,
            "role": None,
            "company_name": None,
            "company_size": None,
            "business_model": None,
            "company_operational": None,
            "can_invest_in_education": None,
        },
        "missing_fields": ["person_name"],
    }
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
    shadow = StubShadowProcessor(proposal)
    chatwoot = StubChatwootClient(
        messages=[
            {
                "id": 789,
                "message_type": 0,
                "private": False,
                "content": "Hola",
                "sender": {"type": "contact", "id": 20},
            }
        ]
    )
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            automated_replies_enabled=True,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="reply-delivery"),
    )

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "delivery_id": "reply-delivery",
    }
    assert chatwoot.reply_calls == []

    asyncio.run(app.state.chatwoot_worker.run_once())
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert chatwoot.reply_calls == [
        {
            "conversation_id": 123,
            "trigger_message_id": 789,
            "delivery_id": "reply-delivery",
            "content": "¡Hola! Soy el asistente virtual de Dan. ¿Cómo te llamás?",
        }
    ]
    work_path = next((tmp_path / ".work").glob("*.json"))
    completed = json.loads(work_path.read_text(encoding="utf-8"))
    assert completed["status"] == "completed"
    assert completed["payload"] == {}


def test_replays_admitted_chatwoot_work_after_restart(tmp_path: Path) -> None:
    secret = "webhook-secret"
    proposal: dict[str, object] = {
        "decision": "ask_question",
        "qualification_status": "in_progress",
        "reason_code": "answer_question",
        "reply": "Sí, podés acceder desde el celular.",
        "captured_fields": {
            "person_name": None,
            "location": None,
            "role": None,
            "company_name": None,
            "company_size": None,
            "business_model": None,
            "company_operational": None,
            "can_invest_in_education": None,
        },
        "missing_fields": [],
    }
    payload = {
        "event": "message_created",
        "id": 901,
        "content": "¿Puedo acceder desde el celular?",
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
    settings = Settings(
        webhook_secret=secret,
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path,
        max_age_seconds=300,
        agent_bot_id=1,
        automated_replies_enabled=True,
    )
    first_app = create_app(
        settings,
        chatwoot_client=StubChatwootClient(),
        shadow_processor=StubShadowProcessor(proposal),
    )

    response = _post(
        first_app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="restart-delivery"),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"

    shadow = StubShadowProcessor(proposal)
    chatwoot = StubChatwootClient(
        messages=[
            {
                "id": 901,
                "message_type": 0,
                "private": False,
                "content": "¿Puedo acceder desde el celular?",
                "sender": {"type": "contact", "id": 20},
            }
        ]
    )
    restarted_app = create_app(
        settings,
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    async def run_lifespan() -> None:
        async with restarted_app.router.lifespan_context(restarted_app):
            async with asyncio.timeout(1):
                while not chatwoot.reply_calls:
                    await asyncio.sleep(0.01)

    asyncio.run(run_lifespan())

    assert len(shadow.calls) == 1
    assert len(chatwoot.reply_calls) == 1


def test_retries_shadow_processing_for_a_capture_without_a_terminal_result(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 300,
        "message_type": "incoming",
        "content": "Hola",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    headers = _signed_headers(raw_body, secret=secret, delivery="durable-shadow")
    settings = Settings(
        secret,
        "12025550123@s.whatsapp.net",
        tmp_path,
        300,
        agent_bot_id=1,
    )

    capture_response = _post(create_app(settings), raw_body, headers)

    shadow = StubShadowProcessor()
    chatwoot = StubChatwootClient(
        messages=[
            {
                "id": 300,
                "message_type": 0,
                "private": False,
                "content": "Hola",
                "sender": {"type": "contact", "id": 20},
            }
        ]
    )
    retry_app = create_app(
        settings,
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )
    retry_response = _post(
        retry_app,
        raw_body,
        headers,
    )

    assert capture_response.status_code == 202
    assert retry_response.status_code == 202
    assert retry_response.json() == {
        "status": "accepted",
        "delivery_id": "durable-shadow",
    }
    asyncio.run(retry_app.state.chatwoot_worker.run_once())
    assert shadow.calls
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_records_failure_without_canonical_chatwoot_context(tmp_path: Path) -> None:
    secret = "webhook-secret"
    shadow = StubShadowProcessor()
    payload = {
        "event": "message_created",
        "id": 301,
        "message_type": "incoming",
        "content": "Hola",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    app = create_app(
        Settings(secret, "12025550123@s.whatsapp.net", tmp_path, 300),
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="missing-canonical"),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    asyncio.run(app.state.chatwoot_worker.run_once())
    assert shadow.calls == []
    assert shadow.failures == [
        ("missing-canonical", "chatwoot_history_not_configured")
    ]


def test_build_app_wires_the_shadow_processor_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "webhook-secret"
    settings = Settings(
        webhook_secret=secret,
        allowed_jid="12025550123@s.whatsapp.net",
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        agent_bot_id=1,
        chatwoot_base_url="https://chatwoot.example.test",
        chatwoot_account_id=1,
        chatwoot_control_api_access_token="test-control-token",
        chatwoot_pause_macro_id=1,
        hermes_shadow_enabled=True,
        hermes_api_base_url="https://hermes.example.test/v1",
        hermes_api_key="test-hermes-key",
        hermes_model_name="agente-comercial",
        shadow_dir=tmp_path / "shadow",
    )
    created_with: list[dict[str, object]] = []
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeHermesShadowProcessor:
        def __init__(self, **kwargs: object) -> None:
            created_with.append(kwargs)

        async def run(
            self, *, delivery_id: str, context: dict[str, object]
        ) -> None:
            calls.append((delivery_id, context))

        def has_result(self, *, delivery_id: str) -> bool:
            return False

        def get_completed_proposal(
            self, *, delivery_id: str
        ) -> dict[str, object] | None:
            return None

        def record_failure(self, *, delivery_id: str, reason: str) -> None:
            raise AssertionError(reason)

    monkeypatch.setattr(
        Settings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(
        "bridge.app.HermesShadowProcessor",
        FakeHermesShadowProcessor,
    )
    monkeypatch.setattr(
        "bridge.app.ChatwootClient",
        lambda **kwargs: StubChatwootClient(
            messages=[
                {
                    "id": 790,
                    "message_type": 0,
                    "private": False,
                    "content": "Hola",
                    "sender": {"type": "contact", "id": 20},
                }
            ]
        ),
    )
    app = build_app()
    payload = {
        "event": "message_created",
        "id": 790,
        "content": "Hola",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": 124,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="factory-shadow"),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    asyncio.run(app.state.chatwoot_worker.run_once())
    assert created_with == [
        {
            "base_url": "https://hermes.example.test/v1",
            "api_key": "test-hermes-key",
            "model_name": "agente-comercial",
            "shadow_dir": tmp_path / "shadow",
        }
    ]
    assert calls[0][0] == "factory-shadow"


@pytest.mark.parametrize(
    ("content", "conversation_id"),
    [
        (None, 123),
        ("Hola", None),
    ],
)
def test_does_not_queue_shadow_without_normalized_business_context(
    tmp_path: Path,
    content: object,
    conversation_id: object,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 791,
        "content": content,
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": conversation_id,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    shadow = StubShadowProcessor()
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
        ),
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="invalid-context"),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "captured"
    assert shadow.calls == []


def test_uses_canonical_chatwoot_history_for_the_shadow_context(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    history = [
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
        {
            "id": 12,
            "message_type": 1,
            "private": True,
            "content": "Nota privada",
            "sender": {"type": "user", "id": 2},
        },
        {
            "id": 13,
            "message_type": 1,
            "private": False,
            "content": "Respuesta de otro bot",
            "sender": {"type": "agent_bot", "id": 99},
        },
        {
            "id": 14,
            "message_type": 0,
            "private": False,
            "content": "Juan",
            "sender": {"type": "contact", "id": 20},
        },
        {
            "id": 15,
            "message_type": 0,
            "private": False,
            "content": "Mensaje posterior",
            "sender": {"type": "contact", "id": 20},
        },
    ]
    chatwoot = StubChatwootClient(messages=history)
    shadow = StubShadowProcessor()
    payload = {
        "event": "message_created",
        "id": 14,
        "content": "Juan",
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
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="history-shadow"),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    asyncio.run(app.state.chatwoot_worker.run_once())
    assert chatwoot.history_calls == [(123, 20)]
    assert shadow.calls[0][1]["messages"] == [
        {"actor": "prospect", "text": "Hola"},
        {"actor": "assistant", "text": "¿Cómo te llamás?"},
        {"actor": "prospect", "text": "Juan"},
    ]


@pytest.mark.parametrize(
    "history_error",
    [
        httpx.ConnectError(
            "unavailable",
            request=httpx.Request("GET", "https://chatwoot.example.test"),
        ),
        ChatwootProtocolError("unexpected response"),
    ],
    ids=["httpx", "protocol"],
)
def test_retries_chatwoot_history_failure_and_recovers_from_backoff(
    tmp_path: Path,
    history_error: Exception,
) -> None:
    secret = "webhook-secret"
    chatwoot = StubChatwootClient(history_error=history_error)
    shadow = StubShadowProcessor()
    payload = {
        "event": "message_created",
        "id": 15,
        "content": "Juan",
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
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="history-failed"),
    )
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert response.status_code == 202
    assert shadow.calls == []
    assert shadow.failures == []
    work_path = next((tmp_path / ".work").glob("*.json"))
    admitted = json.loads(work_path.read_text(encoding="utf-8"))
    assert admitted["status"] == "admitted"
    assert admitted["attempts"] == 1
    assert admitted["next_attempt_at"] > time.time()

    chatwoot.history_error = None
    chatwoot.messages = [
        {
            "id": 15,
            "message_type": 0,
            "private": False,
            "content": "Juan",
            "sender": {"type": "contact", "id": 20},
        }
    ]
    admitted["next_attempt_at"] = 0
    work_path.write_text(json.dumps(admitted), encoding="utf-8")

    asyncio.run(app.state.chatwoot_worker.run_once())

    completed = json.loads(work_path.read_text(encoding="utf-8"))
    assert completed["status"] == "completed"
    assert len(shadow.calls) == 1
    assert shadow.failures == []


def test_transient_chatwoot_history_failure_does_not_exhaust(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    history_error = httpx.ConnectError(
        "unavailable",
        request=httpx.Request("GET", "https://chatwoot.example.test"),
    )
    chatwoot = StubChatwootClient(history_error=history_error)
    shadow = StubShadowProcessor()
    payload = {
        "event": "message_created",
        "id": 15,
        "content": "Juan",
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
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="history-never-terminal"),
    )
    work_path = next((tmp_path / ".work").glob("*.json"))

    for _ in range(8):
        envelope = json.loads(work_path.read_text(encoding="utf-8"))
        envelope["next_attempt_at"] = 0
        work_path.write_text(json.dumps(envelope), encoding="utf-8")
        asyncio.run(app.state.chatwoot_worker.run_once())

    exhausted = json.loads(work_path.read_text(encoding="utf-8"))
    assert response.status_code == 202
    assert exhausted["status"] == "admitted"
    assert exhausted["attempts"] == 8
    assert exhausted["next_attempt_at"] > time.time()
    assert shadow.calls == []


def test_retries_when_current_message_is_temporarily_missing_from_history(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    chatwoot = StubChatwootClient(
        messages=[
            {
                "id": 15,
                "message_type": 0,
                "private": False,
                "content": "Mensaje posterior",
                "sender": {"type": "contact", "id": 20},
            }
        ]
    )
    shadow = StubShadowProcessor()
    payload = {
        "event": "message_created",
        "id": 14,
        "content": "Juan",
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
            agent_bot_id=1,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(
            raw_body,
            secret=secret,
            delivery="current-message-missing",
        ),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    asyncio.run(app.state.chatwoot_worker.run_once())
    assert shadow.calls == []
    assert shadow.failures == []
    work_path = next((tmp_path / ".work").glob("*.json"))
    admitted = json.loads(work_path.read_text(encoding="utf-8"))
    assert admitted["status"] == "admitted"
    assert admitted["attempts"] == 1

    chatwoot.messages.insert(
        0,
        {
            "id": 14,
            "message_type": 0,
            "private": False,
            "content": "Juan",
            "sender": {"type": "contact", "id": 20},
        },
    )
    admitted["next_attempt_at"] = 0
    work_path.write_text(json.dumps(admitted), encoding="utf-8")

    asyncio.run(app.state.chatwoot_worker.run_once())

    completed = json.loads(work_path.read_text(encoding="utf-8"))
    assert completed["status"] == "completed"
    assert len(shadow.calls) == 1
    assert shadow.failures == []


def test_recognizes_configured_agent_bot_without_capturing_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="bridge.app")
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
    assert "chatwoot_webhook_ignored reason=automation_outgoing" in caplog.messages
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
        "status": "accepted",
        "delivery_id": "human-delivery",
    }
    assert chatwoot.calls == []
    asyncio.run(app.state.chatwoot_worker.run_once())
    assert chatwoot.calls == [(2, "automation_paused")]


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

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "delivery_id": "failed-human-delivery",
    }
    assert chatwoot.calls == []
    asyncio.run(app.state.chatwoot_worker.run_once())
    asyncio.run(app.state.chatwoot_worker.run_once())
    assert chatwoot.calls == [(2, "automation_paused")]
    work_items = list((tmp_path / ".work").glob("*.json"))
    assert len(work_items) == 1
    work = json.loads(work_items[0].read_text(encoding="utf-8"))
    assert work["status"] == "admitted"
    assert work["attempts"] == 1
    assert work["next_attempt_at"] > time.time()


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
