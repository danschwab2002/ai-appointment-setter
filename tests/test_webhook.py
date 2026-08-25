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

from bridge.app import (
    Settings,
    _capture_payload,
    _requires_medication_guidance_handoff,
    build_app,
    create_app,
)
from bridge.chatwoot import ChatwootProtocolError
from bridge.chatwoot_inbox import (
    ChatwootWorker,
    DurableChatwootInbox,
    RetryableChatwootWorkError,
)
from bridge.reply_splitter import HermesReplySplitter
from bridge.supabase import (
    InboundCommercialCaseAdmissionResult,
    InboundOptOutResult,
)


class StubChatwootClient:
    def __init__(
        self,
        *,
        changed: bool = True,
        fail: bool = False,
        messages: list[dict[str, object]] | None = None,
        history_error: Exception | None = None,
        authority_error: Exception | None = None,
        label_error: Exception | None = None,
    ) -> None:
        self.changed = changed
        self.fail = fail
        self.calls: list[tuple[int, str]] = []
        self.messages = messages or []
        self.history_error = history_error
        self.authority_error = authority_error
        self.label_error = label_error
        self.history_calls: list[tuple[int, int]] = []
        self.history_required_ids: list[tuple[int, ...]] = []
        self.reply_calls: list[dict[str, object]] = []
        self.authority_calls: list[dict[str, object]] = []
        self.opt_out_macro_calls: list[int] = []
        self.events: list[str] = []

    async def validate_conversation_authority(
        self,
        *,
        conversation_id: int,
        expected_inbox_id: int,
        expected_jid: str | None = None,
    ) -> None:
        self.authority_calls.append(
            {
                "conversation_id": conversation_id,
                "expected_inbox_id": expected_inbox_id,
                "expected_jid": expected_jid,
            }
        )
        if self.authority_error is not None:
            raise self.authority_error

    async def get_conversation_messages(
        self,
        *,
        conversation_id: int,
        limit: int = 20,
        required_message_ids: tuple[int, ...] = (),
    ) -> list[dict[str, object]]:
        self.history_calls.append((conversation_id, limit))
        self.history_required_ids.append(required_message_ids)
        if self.history_error is not None:
            raise self.history_error
        if self.fail:
            request = httpx.Request("GET", "https://chatwoot.example.test")
            raise httpx.ConnectError("unavailable", request=request)
        return self.messages[-limit:]

    async def ensure_conversation_label(
        self,
        *,
        conversation_id: int,
        label: str,
        expected_inbox_id: int | None = None,
        expected_jid: str | None = None,
    ) -> bool:
        self.events.append(f"label:{label}")
        self.calls.append((conversation_id, label))
        if self.label_error is not None:
            raise self.label_error
        if self.fail:
            request = httpx.Request("GET", "https://chatwoot.example.test")
            raise httpx.ConnectError("unavailable", request=request)
        return self.changed

    async def apply_opt_out_macro(self, *, conversation_id: int) -> None:
        self.opt_out_macro_calls.append(conversation_id)
        if self.fail:
            request = httpx.Request("POST", "https://chatwoot.example.test")
            raise httpx.ConnectError("unavailable", request=request)

    async def send_agent_bot_reply(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        delivery_id: str,
        content: str,
        part_index: int = 1,
        part_count: int = 1,
        prior_parts: tuple[str, ...] = (),
        expected_jid: str | None = None,
    ) -> dict[str, object]:
        self.events.append("reply")
        call: dict[str, object] = {
            "conversation_id": conversation_id,
            "trigger_message_id": trigger_message_id,
            "delivery_id": delivery_id,
            "content": content,
        }
        if part_count > 1:
            call.update(
                {
                    "part_index": part_index,
                    "part_count": part_count,
                    "prior_parts": prior_parts,
                }
            )
        if expected_jid is not None:
            call["expected_jid"] = expected_jid
        self.reply_calls.append(call)
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


class StubInboundCommercialSupabase:
    def __init__(self, *, outcome: str = "created") -> None:
        self.outcome = outcome
        self.admission_calls: list[dict[str, object]] = []
        self.handoff_calls: list[dict[str, object]] = []

    async def admit_inbound_commercial_case(
        self, **kwargs: object
    ) -> InboundCommercialCaseAdmissionResult:
        self.admission_calls.append(kwargs)
        return InboundCommercialCaseAdmissionResult(
            outcome=self.outcome,
            commercial_case_id="case-1",
            contact_id="contact-1",
            channel_identity_id="identity-1",
            conversation_id="conversation-1",
            automation_status="draft_only",
        )

    async def has_chatwoot_opt_out_stop(self, **_: object) -> bool:
        return False

    async def request_inbound_human_handoff(self, **kwargs: object) -> object:
        self.handoff_calls.append(kwargs)
        return type(
            "HandoffResult",
            (),
            {"outcome": "requested", "handoff_request_id": "handoff-1"},
        )()


class StubOptOutSupabase:
    def __init__(self, *, stopped: bool = False) -> None:
        self.stopped = stopped
        self.stop_checks: list[dict[str, object]] = []
        self.apply_calls: list[dict[str, object]] = []
        self.reconcile_calls: list[dict[str, object]] = []
        self.projection_claim_calls: list[dict[str, object]] = []

    async def has_chatwoot_opt_out_stop(self, **kwargs: object) -> bool:
        self.stop_checks.append(kwargs)
        return self.stopped

    async def apply_chatwoot_inbound_opt_out(
        self, **kwargs: object
    ) -> InboundOptOutResult:
        self.apply_calls.append(kwargs)
        self.stopped = True
        return InboundOptOutResult(
            outcome="applied",
            opt_out_event_id="opt-out-event-1",
            contact_id="contact-1",
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
            opt_out_event_id="opt-out-event-1",
            contact_id="contact-1",
            affected_cases=0,
            affected_actions=0,
            affected_attempts=0,
        )

    async def claim_chatwoot_opt_out_projections(
        self, **kwargs: object
    ) -> tuple[()]:
        self.projection_claim_calls.append(kwargs)
        return ()


class BlockingShadowProcessor(StubShadowProcessor):
    async def run(
        self, *, delivery_id: str, context: dict[str, object]
    ) -> None:
        self.calls.append((delivery_id, context))
        await asyncio.Event().wait()


class StubReplySplitter:
    def __init__(self, parts: tuple[str, ...]) -> None:
        self.parts = parts
        self.calls: list[tuple[int, int, str]] = []

    async def split(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        reply: str,
    ) -> tuple[str, ...]:
        self.calls.append((conversation_id, trigger_message_id, reply))
        return self.parts


class FailingReplySplitter:
    async def split(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        reply: str,
    ) -> tuple[str, ...]:
        raise RuntimeError("splitter storage unavailable")


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

    def flaky_admitted_items(*, include_deferred=False):  # type: ignore[no-untyped-def]
        nonlocal scans
        scans += 1
        if scans == 1:
            raise RuntimeError("scan failed with private data")
        return admitted_items(include_deferred=include_deferred)

    monkeypatch.setattr(inbox, "admitted_items", flaky_admitted_items)
    handled: list[str] = []

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
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

    def counted_admitted_items(*, include_deferred=False):  # type: ignore[no-untyped-def]
        nonlocal scans
        scans += 1
        return admitted_items(include_deferred=include_deferred)

    monkeypatch.setattr(inbox, "admitted_items", counted_admitted_items)
    handled: list[str] = []

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        handled.append(delivery_id)

    worker = ChatwootWorker(inbox=inbox, handler=handler)

    asyncio.run(worker.run_once())

    assert scans == 1
    assert sorted(handled) == ["delivery-one", "delivery-two"]


def test_chatwoot_worker_resets_the_conversation_debounce_window(
    tmp_path: Path,
) -> None:
    current_time = 1_000.0
    clock = lambda: current_time
    inbox = DurableChatwootInbox(tmp_path / ".work", clock=clock)
    handled: list[tuple[str, dict[str, object]]] = []

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        handled.append((delivery_id, payload))

    def conversation_key(payload: dict[str, object]) -> str | None:
        conversation = payload.get("conversation")
        conversation_id = (
            conversation.get("id") if isinstance(conversation, dict) else None
        )
        return str(conversation_id) if isinstance(conversation_id, int) else None

    worker = ChatwootWorker(
        inbox=inbox,
        handler=handler,
        debounce_key=conversation_key,
        debounce_seconds=30,
        clock=clock,
    )
    first_payload: dict[str, object] = {
        "id": 10,
        "conversation": {"id": 2},
    }
    second_payload: dict[str, object] = {
        "id": 11,
        "conversation": {"id": 2},
    }

    assert inbox.admit(delivery_id="delivery-one", payload=first_payload)
    asyncio.run(worker.run_once())
    assert handled == []

    current_time += 20
    assert inbox.admit(delivery_id="delivery-two", payload=second_payload)
    current_time += 29
    asyncio.run(worker.run_once())
    assert handled == []

    current_time += 1
    asyncio.run(worker.run_once())

    assert handled == [("delivery-two", second_payload)]
    envelopes = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".work").glob("*.json")
    ]
    assert len(envelopes) == 2
    assert {envelope["status"] for envelope in envelopes} == {"completed"}


def test_chatwoot_worker_uses_canonical_message_order_for_the_group_leader(
    tmp_path: Path,
) -> None:
    current_time = 1_500.0
    clock = lambda: current_time
    inbox = DurableChatwootInbox(tmp_path / ".work", clock=clock)
    handled: list[str] = []

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        handled.append(delivery_id)

    worker = ChatwootWorker(
        inbox=inbox,
        handler=handler,
        debounce_key=lambda payload: "conversation-one",
        debounce_seconds=30,
        clock=clock,
    )
    assert inbox.admit(
        delivery_id="canonical-newer",
        payload={"id": 51, "conversation": {"id": 1}},
    )
    current_time += 1
    assert inbox.admit(
        delivery_id="canonically-older-arrived-last",
        payload={"id": 50, "conversation": {"id": 1}},
    )
    current_time += 30

    asyncio.run(worker.run_once())

    assert handled == ["canonical-newer"]


def test_chatwoot_worker_preserves_debounce_across_restart_and_per_conversation(
    tmp_path: Path,
) -> None:
    current_time = 2_000.0
    clock = lambda: current_time
    inbox = DurableChatwootInbox(tmp_path / ".work", clock=clock)
    handled: list[str] = []

    def conversation_key(payload: dict[str, object]) -> str | None:
        conversation = payload.get("conversation")
        conversation_id = (
            conversation.get("id") if isinstance(conversation, dict) else None
        )
        return str(conversation_id) if isinstance(conversation_id, int) else None

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        handled.append(delivery_id)

    assert inbox.admit(
        delivery_id="conversation-one",
        payload={"id": 20, "conversation": {"id": 1}},
    )
    current_time += 10
    assert inbox.admit(
        delivery_id="conversation-two",
        payload={"id": 21, "conversation": {"id": 2}},
    )

    restarted_inbox = DurableChatwootInbox(tmp_path / ".work", clock=clock)
    restarted_worker = ChatwootWorker(
        inbox=restarted_inbox,
        handler=handler,
        debounce_key=conversation_key,
        debounce_seconds=30,
        clock=clock,
    )

    current_time += 20
    asyncio.run(restarted_worker.run_once())
    assert handled == ["conversation-one"]

    current_time += 10
    asyncio.run(restarted_worker.run_once())
    assert handled == ["conversation-one", "conversation-two"]


def test_chatwoot_worker_retries_only_the_latest_delivery_in_a_batch(
    tmp_path: Path,
) -> None:
    current_time = 3_000.0
    clock = lambda: current_time
    inbox = DurableChatwootInbox(tmp_path / ".work", clock=clock)
    calls: list[str] = []

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        calls.append(delivery_id)
        if len(calls) == 1:
            raise RetryableChatwootWorkError("temporary failure")

    worker = ChatwootWorker(
        inbox=inbox,
        handler=handler,
        debounce_key=lambda payload: "conversation-one",
        debounce_seconds=30,
        clock=clock,
    )
    assert inbox.admit(
        delivery_id="batch-old",
        payload={"id": 30, "conversation": {"id": 1}},
    )
    current_time += 1
    assert inbox.admit(
        delivery_id="batch-latest",
        payload={"id": 31, "conversation": {"id": 1}},
    )
    current_time += 30

    asyncio.run(worker.run_once())

    envelopes = {
        envelope["delivery_id"]: (path, envelope)
        for path in (tmp_path / ".work").glob("*.json")
        if (envelope := json.loads(path.read_text(encoding="utf-8")))
    }
    assert envelopes["batch-old"][1]["status"] == "admitted"
    assert envelopes["batch-latest"][1]["status"] == "admitted"
    assert envelopes["batch-latest"][1]["attempts"] == 1

    latest_path, latest_envelope = envelopes["batch-latest"]
    latest_envelope["next_attempt_at"] = current_time
    latest_path.write_text(json.dumps(latest_envelope), encoding="utf-8")
    asyncio.run(worker.run_once())

    assert calls == ["batch-latest", "batch-latest"]
    completed = [
        json.loads(path.read_text(encoding="utf-8"))["status"]
        for path in (tmp_path / ".work").glob("*.json")
    ]
    assert set(completed) == {"completed"}


def test_chatwoot_worker_fails_the_whole_batch_when_the_leader_dead_letters(
    tmp_path: Path,
) -> None:
    current_time = 3_500.0
    clock = lambda: current_time
    inbox = DurableChatwootInbox(tmp_path / ".work", clock=clock)

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        raise ValueError("invalid grouped work")

    worker = ChatwootWorker(
        inbox=inbox,
        handler=handler,
        debounce_key=lambda payload: "conversation-one",
        debounce_seconds=30,
        clock=clock,
    )
    assert inbox.admit(
        delivery_id="dead-letter-old",
        payload={"id": 35, "conversation": {"id": 1}},
    )
    current_time += 1
    assert inbox.admit(
        delivery_id="dead-letter-leader",
        payload={"id": 36, "conversation": {"id": 1}},
    )
    leader_path = next(
        path
        for path in (tmp_path / ".work").glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["delivery_id"]
        == "dead-letter-leader"
    )
    leader_envelope = json.loads(leader_path.read_text(encoding="utf-8"))
    leader_envelope["attempts"] = 7
    leader_path.write_text(json.dumps(leader_envelope), encoding="utf-8")
    current_time += 30

    asyncio.run(worker.run_once())

    envelopes = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".work").glob("*.json")
    ]
    assert {envelope["status"] for envelope in envelopes} == {"failed"}


def test_group_dead_letter_recovers_after_a_crash_between_member_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 3_600.0
    clock = lambda: current_time
    inbox = DurableChatwootInbox(tmp_path / ".work", clock=clock)
    calls: list[str] = []

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        calls.append(delivery_id)
        raise ValueError("invalid grouped work")

    worker = ChatwootWorker(
        inbox=inbox,
        handler=handler,
        debounce_key=lambda payload: "conversation-one",
        debounce_seconds=30,
        clock=clock,
    )
    for delivery_id, message_id in (("crash-old", 36), ("crash-leader", 37)):
        assert inbox.admit(
            delivery_id=delivery_id,
            payload={"id": message_id, "conversation": {"id": 1}},
        )
        current_time += 1
    leader_path = next(
        path
        for path in (tmp_path / ".work").glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["delivery_id"]
        == "crash-leader"
    )
    leader_envelope = json.loads(leader_path.read_text(encoding="utf-8"))
    leader_envelope["attempts"] = 7
    leader_path.write_text(json.dumps(leader_envelope), encoding="utf-8")
    current_time += 30
    replace_envelope = inbox._replace_envelope
    terminal_writes = 0

    def crash_after_first_terminal_write(
        path: Path, envelope: dict[str, object]
    ) -> None:
        nonlocal terminal_writes
        replace_envelope(path, envelope)
        if envelope.get("status") == "failed" and not path.name.startswith("."):
            terminal_writes += 1
            if terminal_writes == 1:
                raise RuntimeError("simulated crash")

    monkeypatch.setattr(inbox, "_replace_envelope", crash_after_first_terminal_write)

    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(worker.run_once())

    journal_path = next(
        (tmp_path / ".work").glob(".group-failure-*.json")
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert set(journal) == {
        "status",
        "leader_file",
        "member_files",
        "leader_error_type",
    }
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600
    assert "crash-old" not in journal_path.read_text(encoding="utf-8")
    assert "crash-leader" not in journal_path.read_text(encoding="utf-8")

    restarted_inbox = DurableChatwootInbox(tmp_path / ".work", clock=clock)
    restarted_worker = ChatwootWorker(
        inbox=restarted_inbox,
        handler=handler,
        debounce_key=lambda payload: "conversation-one",
        debounce_seconds=30,
        clock=clock,
    )
    asyncio.run(restarted_worker.run_once())

    envelopes = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".work").glob("[!.]*.json")
    ]
    assert {envelope["status"] for envelope in envelopes} == {"failed"}
    assert calls == ["crash-leader"]
    assert list((tmp_path / ".work").glob(".group-failure-*.json")) == []


def test_chatwoot_worker_serializes_handlers_by_conversation(
    tmp_path: Path,
) -> None:
    current_time = 3_800.0
    clock = lambda: current_time
    inbox = DurableChatwootInbox(tmp_path / ".work", clock=clock)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        calls.append(delivery_id)
        if delivery_id == "concurrent-old":
            first_started.set()
            await release_first.wait()

    def worker() -> ChatwootWorker:
        return ChatwootWorker(
            inbox=inbox,
            handler=handler,
            debounce_key=lambda payload: "conversation-one",
            debounce_seconds=30,
            clock=clock,
        )

    assert inbox.admit(
        delivery_id="concurrent-old",
        payload={"id": 38, "conversation": {"id": 1}},
    )
    current_time += 30

    async def exercise_workers() -> None:
        first_run = asyncio.create_task(worker().run_once())
        await first_started.wait()
        nonlocal current_time
        current_time += 1
        assert inbox.admit(
            delivery_id="concurrent-new",
            payload={"id": 39, "conversation": {"id": 1}},
        )
        current_time += 30
        await worker().run_once()
        assert calls == ["concurrent-old"]
        release_first.set()
        await first_run
        await worker().run_once()

    asyncio.run(exercise_workers())

    assert calls == ["concurrent-old", "concurrent-new"]


def test_new_admission_between_scan_and_conversation_lock_resets_the_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 3_900.0
    clock = lambda: current_time
    inbox = DurableChatwootInbox(tmp_path / ".work", clock=clock)
    calls: list[tuple[str, tuple[int, ...]]] = []

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        calls.append((delivery_id, batch_message_ids))

    worker = ChatwootWorker(
        inbox=inbox,
        handler=handler,
        debounce_key=lambda payload: "conversation-one",
        debounce_seconds=30,
        clock=clock,
    )
    assert inbox.admit(
        delivery_id="scan-old",
        payload={"id": 38, "conversation": {"id": 1}},
    )
    current_time += 30
    processing_lock_path = inbox.processing_lock_path
    injected = False

    def inject_before_lock(*, namespace: str, key: str) -> Path:
        nonlocal current_time, injected
        if namespace == "conversation" and not injected:
            current_time += 1
            assert inbox.admit(
                delivery_id="scan-new",
                payload={"id": 39, "conversation": {"id": 1}},
            )
            injected = True
        return processing_lock_path(namespace=namespace, key=key)

    monkeypatch.setattr(inbox, "processing_lock_path", inject_before_lock)

    asyncio.run(worker.run_once())

    assert calls == []
    assert len(inbox.admitted_items(include_deferred=True)) == 2

    current_time += 30
    asyncio.run(worker.run_once())

    assert calls == [("scan-new", (38, 39))]
    assert inbox.admitted_items(include_deferred=True) == []


def test_new_delivery_supersedes_an_older_delivery_still_in_backoff(
    tmp_path: Path,
) -> None:
    current_time = 4_000.0
    clock = lambda: current_time
    inbox = DurableChatwootInbox(tmp_path / ".work", clock=clock)
    calls: list[str] = []

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        calls.append(delivery_id)
        if delivery_id == "backoff-old" and calls.count(delivery_id) == 1:
            raise RetryableChatwootWorkError("temporary failure")

    worker = ChatwootWorker(
        inbox=inbox,
        handler=handler,
        debounce_key=lambda payload: "conversation-one",
        debounce_seconds=30,
        clock=clock,
    )
    assert inbox.admit(
        delivery_id="backoff-old",
        payload={"id": 40, "conversation": {"id": 1}},
    )
    old_path = next((tmp_path / ".work").glob("*.json"))
    old_envelope = json.loads(old_path.read_text(encoding="utf-8"))
    old_envelope["attempts"] = 6
    old_path.write_text(json.dumps(old_envelope), encoding="utf-8")
    current_time += 30
    asyncio.run(worker.run_once())

    current_time += 1
    assert inbox.admit(
        delivery_id="backoff-new",
        payload={"id": 41, "conversation": {"id": 1}},
    )
    current_time += 30
    asyncio.run(worker.run_once())
    current_time += 30
    asyncio.run(worker.run_once())

    assert calls == ["backoff-old", "backoff-new"]
    envelopes = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".work").glob("*.json")
    ]
    assert {envelope["status"] for envelope in envelopes} == {"completed"}


def test_chatwoot_worker_does_not_debounce_unkeyed_control_work(
    tmp_path: Path,
) -> None:
    inbox = DurableChatwootInbox(tmp_path / ".work")
    assert inbox.admit(
        delivery_id="human-control",
        payload={"event": "message_created", "message_type": "outgoing"},
    )
    handled: list[str] = []

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        handled.append(delivery_id)

    worker = ChatwootWorker(
        inbox=inbox,
        handler=handler,
        debounce_key=lambda payload: None,
        debounce_seconds=30,
    )

    asyncio.run(worker.run_once())

    assert handled == ["human-control"]


@pytest.mark.parametrize("debounce_seconds", [-1.0, float("nan"), float("inf")])
def test_rejects_invalid_chatwoot_debounce_values(
    tmp_path: Path,
    debounce_seconds: float,
) -> None:
    with pytest.raises(
        ValueError,
        match="CHATWOOT_INBOUND_DEBOUNCE_SECONDS must be finite and not negative",
    ):
        create_app(
            Settings(
                webhook_secret="webhook-secret",
                allowed_jid="12025550123@s.whatsapp.net",
                capture_dir=tmp_path,
                max_age_seconds=300,
                chatwoot_inbound_debounce_seconds=debounce_seconds,
            )
        )


def test_chatwoot_worker_times_out_a_blocked_handler_and_preserves_work(
    tmp_path: Path,
) -> None:
    inbox = DurableChatwootInbox(tmp_path / ".work")
    inbox.admit(delivery_id="blocked-delivery", payload={"event": "test"})

    async def blocked_handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
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
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
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
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
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
        handler=lambda delivery_id, payload, batch_message_ids: asyncio.sleep(0),
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


def test_same_sender_from_another_inbox_is_ignored_before_capture(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 789,
        "content": "No pertenece al inbox autorizado",
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 1},
        "conversation": {
            "id": 123,
            "inbox_id": 1,
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
            chatwoot_account_id=1,
            chatwoot_inbox_id=6,
        )
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="wrong-inbox"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "inbox_not_allowed",
    }
    assert list(tmp_path.iterdir()) == []


def test_wrong_inbox_human_outgoing_cannot_pause_or_invoke_hermes(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 1},
        "sender": {"id": 1, "type": "user"},
        "conversation": {
            "id": 123,
            "inbox_id": 1,
            "meta": {
                "sender": {"identifier": "12025550123@s.whatsapp.net"},
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    chatwoot = StubChatwootClient()
    shadow = StubShadowProcessor()
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            chatwoot_account_id=1,
            chatwoot_inbox_id=6,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="wrong-inbox-human"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "inbox_not_allowed",
    }
    assert chatwoot.calls == []
    assert chatwoot.history_calls == []
    assert chatwoot.reply_calls == []
    assert shadow.calls == []
    assert list(tmp_path.rglob("*.json")) == []


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


def test_nuevo_bypasses_debounce_and_confirms_without_invoking_hermes(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 790,
        "content": "/nuevo",
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
    chatwoot = StubChatwootClient()
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            automated_replies_enabled=True,
            chatwoot_inbound_debounce_seconds=30,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="reset-delivery"),
    )
    assert response.status_code == 202

    asyncio.run(app.state.chatwoot_worker.run_once())

    assert shadow.calls == []
    assert chatwoot.history_calls == []
    assert chatwoot.reply_calls == [
        {
            "conversation_id": 123,
            "trigger_message_id": 790,
            "delivery_id": "reset-delivery",
            "content": "Memoria eliminada.",
        }
    ]


def test_nuevo_cannot_bypass_an_existing_durable_opt_out(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 791,
        "content": "/nuevo",
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 7},
        "conversation": {
            "id": 123,
            "inbox_id": 7,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    shadow = StubShadowProcessor()
    chatwoot = StubChatwootClient()
    supabase = StubOptOutSupabase(stopped=True)
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
            automated_replies_enabled=True,
            chatwoot_inbound_debounce_seconds=30,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="stopped-reset-delivery"),
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert supabase.stop_checks == [
        {
            "chatwoot_account_id": 1,
            "chatwoot_inbox_id": 7,
            "chatwoot_conversation_id": 123,
            "external_user_id": "12025550123",
        }
    ]
    assert len(supabase.reconcile_calls) == 1
    assert shadow.calls == []
    assert chatwoot.reply_calls == []


def test_scoped_nuevo_checks_opt_out_for_the_observed_sender(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    observed_user_id = "12025550124"
    payload = {
        "event": "message_created",
        "id": 792,
        "content": "/nuevo",
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 9},
        "conversation": {
            "id": 123,
            "inbox_id": 9,
            "contact_inbox": {
                "source_id": f"{observed_user_id}@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    shadow = StubShadowProcessor()
    chatwoot = StubChatwootClient()

    class SenderScopedOptOutSupabase(StubOptOutSupabase):
        async def has_chatwoot_opt_out_stop(
            self,
            **kwargs: object,
        ) -> bool:
            self.stop_checks.append(dict(kwargs))
            return kwargs.get("external_user_id") == observed_user_id

    supabase = SenderScopedOptOutSupabase(stopped=False)
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            chatwoot_cut_b_admission_enabled=True,
            chatwoot_cut_b_scope_key="libre-de-ansiedad-inbound",
            chatwoot_cut_b_scope_version=2,
            chatwoot_cut_b_agent_enabled=True,
            chatwoot_scoped_inbound_senders_enabled=True,
            automated_replies_enabled=True,
            chatwoot_durable_opt_out_enabled=True,
            chatwoot_human_pause_enabled=True,
            chatwoot_opt_out_macro_id=2,
            opt_out_projection_worker_id="opt-out-test",
            human_handoff_admission_enabled=True,
            human_handoff_projection_enabled=True,
            handoff_projection_policy_key="lancemos-inbound-handoff",
            handoff_projection_policy_version=1,
            human_handoff_projection_worker_id="handoff-projection-test",
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="scoped-stopped-reset"),
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert supabase.stop_checks == [
        {
            "chatwoot_account_id": 1,
            "chatwoot_inbox_id": 9,
            "chatwoot_conversation_id": 123,
            "external_user_id": observed_user_id,
        }
    ]
    assert len(supabase.reconcile_calls) == 1
    assert shadow.calls == []
    assert chatwoot.reply_calls == []


def test_canonical_context_starts_after_the_latest_nuevo_command(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 904,
        "content": "¿Cuánto cuesta?",
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
                "id": 900,
                "message_type": 0,
                "private": False,
                "content": "Tengo un problema anterior",
                "sender": {"type": "contact", "id": 20},
            },
            {
                "id": 901,
                "message_type": 1,
                "private": False,
                "content": "Respuesta anterior",
                "sender": {"type": "agent_bot", "id": 1},
            },
            {
                "id": 902,
                "message_type": 0,
                "private": False,
                "content": "/nuevo",
                "sender": {"type": "contact", "id": 20},
            },
            {
                "id": 903,
                "message_type": 1,
                "private": False,
                "content": "Memoria eliminada.",
                "sender": {"type": "agent_bot", "id": 1},
            },
            {
                "id": 904,
                "message_type": 0,
                "private": False,
                "content": "¿Cuánto cuesta?",
                "sender": {"type": "contact", "id": 20},
            },
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
        _signed_headers(raw_body, secret=secret, delivery="post-reset-delivery"),
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert len(shadow.calls) == 1
    _, context = shadow.calls[0]
    assert context["messages"] == [
        {"actor": "prospect", "text": "¿Cuánto cuesta?"}
    ]


@pytest.mark.parametrize("content", ["/Nuevo", " /nuevo", "/nuevo "])
def test_only_the_exact_nuevo_command_resets_the_conversation(
    tmp_path: Path,
    content: str,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 905,
        "content": content,
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
                "id": 905,
                "message_type": 0,
                "private": False,
                "content": content,
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
        _signed_headers(raw_body, secret=secret, delivery=f"non-reset-{content!r}"),
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert len(shadow.calls) == 1
    _, context = shadow.calls[0]
    assert context["messages"] == [{"actor": "prospect", "text": content.strip()}]
    assert chatwoot.reply_calls == []


def test_applies_canonical_opt_out_before_shadow_or_reply(tmp_path: Path) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 789,
        "content": "Quiero darme de baja",
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 7},
        "conversation": {
            "id": 123,
            "inbox_id": 7,
            "contact_inbox": {"source_id": "12025550123@s.whatsapp.net"},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    shadow = StubShadowProcessor(proposal={"reply": "No debe enviarse"})
    supabase = StubOptOutSupabase()
    chatwoot = StubChatwootClient(
        messages=[
            {
                "id": 789,
                "created_at": 1786233900,
                "message_type": 0,
                "private": False,
                "content": "Quiero darme de baja",
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
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
            chatwoot_durable_opt_out_enabled=True,
            chatwoot_opt_out_macro_id=9,
            opt_out_projection_worker_id="opt-out-projection-test",
            automated_replies_enabled=True,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="opt-out-delivery"),
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert supabase.stop_checks == [
        {
            "chatwoot_account_id": 1,
            "chatwoot_inbox_id": 7,
            "chatwoot_conversation_id": 123,
            "external_user_id": "12025550123",
        }
    ]
    assert supabase.apply_calls == [
        {
            "chatwoot_account_id": 1,
            "chatwoot_inbox_id": 7,
            "chatwoot_conversation_id": 123,
            "chatwoot_message_id": 789,
            "external_user_id": "12025550123",
            "occurred_at": "2026-08-09T00:05:00+00:00",
            "rule_key": "unsubscribe",
        }
    ]
    assert shadow.calls == []
    assert chatwoot.reply_calls == []


def test_existing_durable_stop_blocks_later_inbound_before_shadow(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "id": 790,
        "content": "Gracias",
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 7},
        "conversation": {
            "id": 123,
            "inbox_id": 7,
            "contact_inbox": {"source_id": "12025550123@s.whatsapp.net"},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    shadow = StubShadowProcessor()
    supabase = StubOptOutSupabase(stopped=True)
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
            chatwoot_durable_opt_out_enabled=True,
            chatwoot_opt_out_macro_id=9,
            opt_out_projection_worker_id="opt-out-projection-test",
        ),
        chatwoot_client=StubChatwootClient(
            messages=[
                {
                    "id": 790,
                    "created_at": 1786233960,
                    "message_type": 0,
                    "private": False,
                    "content": "Gracias",
                    "sender": {"type": "contact", "id": 20},
                }
            ]
        ),
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="stopped-delivery"),
    )
    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    assert supabase.apply_calls == []
    assert supabase.reconcile_calls == [
        {
            "chatwoot_account_id": 1,
            "chatwoot_inbox_id": 7,
            "chatwoot_conversation_id": 123,
            "external_user_id": "12025550123",
        }
    ]
    assert shadow.calls == []


def test_opt_out_is_detected_in_earlier_message_of_canonical_batch(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    base_payload: dict[str, object] = {
        "event": "message_created",
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 7},
        "conversation": {
            "id": 123,
            "inbox_id": 7,
            "contact_inbox": {"source_id": "12025550123@s.whatsapp.net"},
        },
    }
    first_payload = {
        **base_payload,
        "id": 789,
        "content": "No quiero recibir más mensajes",
    }
    second_payload = {**base_payload, "id": 790, "content": "Gracias"}
    supabase = StubOptOutSupabase()
    shadow = StubShadowProcessor()
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
            chatwoot_inbound_debounce_seconds=30,
            chatwoot_durable_opt_out_enabled=True,
            chatwoot_opt_out_macro_id=9,
            opt_out_projection_worker_id="opt-out-projection-test",
        ),
        chatwoot_client=StubChatwootClient(
            messages=[
                {
                    "id": 789,
                    "created_at": 1786233900,
                    "message_type": 0,
                    "private": False,
                    "content": "No quiero recibir más mensajes",
                    "sender": {"type": "contact", "id": 20},
                },
                {
                    "id": 790,
                    "created_at": 1786233960,
                    "message_type": 0,
                    "private": False,
                    "content": "Gracias",
                    "sender": {"type": "contact", "id": 20},
                },
            ]
        ),
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    for delivery_id, payload in (
        ("opt-out-batch-one", first_payload),
        ("opt-out-batch-two", second_payload),
    ):
        raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        assert _post(
            app,
            raw_body,
            _signed_headers(raw_body, secret=secret, delivery=delivery_id),
        ).status_code == 202

    for work_path in (tmp_path / ".work").glob("*.json"):
        envelope = json.loads(work_path.read_text(encoding="utf-8"))
        envelope["admitted_at"] -= 30
        work_path.write_text(json.dumps(envelope), encoding="utf-8")
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert len(supabase.apply_calls) == 1
    assert supabase.apply_calls[0]["chatwoot_message_id"] == 789
    assert supabase.apply_calls[0]["rule_key"] == "stop_receiving_messages"
    assert shadow.calls == []


def test_durable_opt_out_is_admitted_without_hermes_shadow(tmp_path: Path) -> None:
    secret = "test-secret"
    payload: dict[str, object] = {
        "event": "message_created",
        "id": 791,
        "message_type": "incoming",
        "private": False,
        "content": "No quiero recibir más mensajes",
        "account": {"id": 1},
        "inbox": {"id": 7},
        "conversation": {
            "id": 123,
            "inbox_id": 7,
            "contact_inbox": {"source_id": "12025550123@s.whatsapp.net"},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    supabase = StubOptOutSupabase()
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
            chatwoot_durable_opt_out_enabled=True,
            chatwoot_opt_out_macro_id=9,
            opt_out_projection_worker_id="opt-out-projection-test",
        ),
        chatwoot_client=StubChatwootClient(
            messages=[{
                "id": 791,
                "created_at": 1786233960,
                "message_type": 0,
                "private": False,
                "content": "No quiero recibir más mensajes",
                "sender": {"type": "contact", "id": 20},
            }]
        ),
        shadow_processor=None,
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="opt-out-no-shadow"),
    )

    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    assert len(supabase.apply_calls) == 1


def test_existing_durable_stop_is_enforced_when_detection_is_disabled(
    tmp_path: Path,
) -> None:
    secret = "test-secret"
    payload: dict[str, object] = {
        "event": "message_created",
        "id": 792,
        "message_type": "incoming",
        "private": False,
        "content": "Gracias",
        "account": {"id": 1},
        "inbox": {"id": 7},
        "conversation": {
            "id": 123,
            "inbox_id": 7,
            "contact_inbox": {"source_id": "12025550123@s.whatsapp.net"},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    shadow = StubShadowProcessor()
    supabase = StubOptOutSupabase(stopped=True)
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
            chatwoot_durable_opt_out_enabled=False,
        ),
        chatwoot_client=StubChatwootClient(
            messages=[{
                "id": 792,
                "created_at": 1786233960,
                "message_type": 0,
                "private": False,
                "content": "Gracias",
                "sender": {"type": "contact", "id": 20},
            }]
        ),
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="opt-out-enforced"),
    )

    assert response.status_code == 202
    asyncio.run(app.state.chatwoot_worker.run_once())
    assert shadow.calls == []
    assert len(supabase.reconcile_calls) == 1


def test_cached_reply_cannot_bypass_stop_when_detector_is_disabled(
    tmp_path: Path,
) -> None:
    secret = "test-secret"
    payload: dict[str, object] = {
        "event": "message_created",
        "id": 793,
        "message_type": "incoming",
        "private": False,
        "content": "Gracias",
        "account": {"id": 1},
        "inbox": {"id": 7},
        "conversation": {
            "id": 123,
            "inbox_id": 7,
            "contact_inbox": {"source_id": "12025550123@s.whatsapp.net"},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    shadow = StubShadowProcessor({"reply": "No debe enviarse"})
    shadow.completed_delivery_ids.add("cached-opt-out-enforced")
    supabase = StubOptOutSupabase(stopped=True)
    chatwoot = StubChatwootClient(messages=[{
        "id": 793,
        "created_at": 1786233960,
        "message_type": 0,
        "private": False,
        "content": "Gracias",
        "sender": {"type": "contact", "id": 20},
    }])
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
            chatwoot_durable_opt_out_enabled=False,
            automated_replies_enabled=True,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(
            raw_body,
            secret=secret,
            delivery="cached-opt-out-enforced",
        ),
    )
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert response.status_code == 202
    assert len(supabase.stop_checks) == 1
    assert len(supabase.reconcile_calls) == 1
    assert chatwoot.reply_calls == []


def test_opt_out_projection_worker_survives_detector_disablement(
    tmp_path: Path,
) -> None:
    supabase = StubOptOutSupabase()
    app = create_app(
        Settings(
            webhook_secret="secret",
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_account_id=1,
            chatwoot_inbox_id=7,
            chatwoot_durable_opt_out_enabled=False,
            chatwoot_opt_out_macro_id=9,
            opt_out_projection_worker_id="opt-out-projection-test",
        ),
        chatwoot_client=StubChatwootClient(messages=[]),
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    async def exercise_lifespan() -> None:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)

    asyncio.run(exercise_lifespan())

    assert len(supabase.projection_claim_calls) == 1


def test_batches_two_incoming_messages_into_one_shadow_evaluation(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    base_payload: dict[str, object] = {
        "event": "message_created",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            },
        },
    }
    first_payload = {**base_payload, "id": 789, "content": "Hola"}
    second_payload = {**base_payload, "id": 790, "content": "Tengo una duda"}
    shadow = StubShadowProcessor()
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_inbound_debounce_seconds=30,
        ),
        chatwoot_client=StubChatwootClient(
            messages=[
                {
                    "id": 789,
                    "message_type": 0,
                    "private": False,
                    "content": "Hola",
                    "sender": {"type": "contact", "id": 20},
                },
                {
                    "id": 790,
                    "message_type": 0,
                    "private": False,
                    "content": "Tengo una duda",
                    "sender": {"type": "contact", "id": 20},
                },
            ]
        ),
        shadow_processor=shadow,
    )

    for delivery_id, payload in (
        ("batch-delivery-one", first_payload),
        ("batch-delivery-two", second_payload),
    ):
        raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = _post(
            app,
            raw_body,
            _signed_headers(raw_body, secret=secret, delivery=delivery_id),
        )
        assert response.status_code == 202

    asyncio.run(app.state.chatwoot_worker.run_once())
    assert shadow.calls == []

    for work_path in (tmp_path / ".work").glob("*.json"):
        envelope = json.loads(work_path.read_text(encoding="utf-8"))
        envelope["admitted_at"] -= 30
        work_path.write_text(json.dumps(envelope), encoding="utf-8")
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert shadow.calls == [
        (
            "batch-delivery-two",
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
                    {"actor": "prospect", "text": "Hola"},
                    {"actor": "prospect", "text": "Tengo una duda"},
                ],
            },
        )
    ]


def test_batch_larger_than_twenty_keeps_every_message_in_the_shadow_turn(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    history = [
        {
            "id": message_id,
            "message_type": 0,
            "private": False,
            "content": f"Parte {message_id}",
            "sender": {"type": "contact", "id": 20},
        }
        for message_id in range(1_000, 1_025)
    ]
    chatwoot = StubChatwootClient(messages=history)
    shadow = StubShadowProcessor()
    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_inbound_debounce_seconds=30,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
    )
    inbox = app.state.chatwoot_inbox
    assert inbox is not None
    for message in history:
        assert inbox.admit(
            delivery_id=f"batch-{message['id']}",
            payload={
                "event": "message_created",
                "id": message["id"],
                "content": message["content"],
                "message_type": "incoming",
                "private": False,
                "conversation": {
                    "id": 123,
                    "contact_inbox": {
                        "source_id": "12025550123@s.whatsapp.net",
                    },
                },
            },
        )
    for work_path in (tmp_path / ".work").glob("*.json"):
        envelope = json.loads(work_path.read_text(encoding="utf-8"))
        envelope["admitted_at"] = 0
        work_path.write_text(json.dumps(envelope), encoding="utf-8")

    asyncio.run(app.state.chatwoot_worker.run_once())

    assert chatwoot.history_calls == [(123, 200)]
    assert chatwoot.history_required_ids == [tuple(range(1_000, 1_025))]
    assert len(shadow.calls) == 1
    delivery_id, context = shadow.calls[0]
    assert delivery_id == "batch-1024"
    assert context["messages"] == [
        {"actor": "prospect", "text": f"Parte {message_id}"}
        for message_id in range(1_000, 1_025)
    ]


def test_missing_canonical_batch_member_keeps_the_whole_turn_admitted(
    tmp_path: Path,
) -> None:
    app = create_app(
        Settings(
            webhook_secret="webhook-secret",
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_inbound_debounce_seconds=30,
        ),
        chatwoot_client=StubChatwootClient(
            messages=[
                {
                    "id": 2_001,
                    "message_type": 0,
                    "private": False,
                    "content": "Segunda parte",
                    "sender": {"type": "contact", "id": 20},
                }
            ]
        ),
        shadow_processor=(shadow := StubShadowProcessor()),
    )
    inbox = app.state.chatwoot_inbox
    assert inbox is not None
    for message_id, content in ((2_000, "Primera parte"), (2_001, "Segunda parte")):
        assert inbox.admit(
            delivery_id=f"missing-{message_id}",
            payload={
                "event": "message_created",
                "id": message_id,
                "content": content,
                "message_type": "incoming",
                "private": False,
                "conversation": {
                    "id": 123,
                    "contact_inbox": {
                        "source_id": "12025550123@s.whatsapp.net",
                    },
                },
            },
        )
    for work_path in (tmp_path / ".work").glob("*.json"):
        envelope = json.loads(work_path.read_text(encoding="utf-8"))
        envelope["admitted_at"] = 0
        work_path.write_text(json.dumps(envelope), encoding="utf-8")

    asyncio.run(app.state.chatwoot_worker.run_once())

    assert shadow.calls == []
    envelopes = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / ".work").glob("*.json")
    ]
    assert {envelope["status"] for envelope in envelopes} == {"admitted"}


def test_legacy_admitted_incoming_without_message_id_does_not_complete(
    tmp_path: Path,
) -> None:
    shadow = StubShadowProcessor()
    app = create_app(
        Settings(
            webhook_secret="webhook-secret",
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        ),
        chatwoot_client=StubChatwootClient(messages=[]),
        shadow_processor=shadow,
    )
    inbox = app.state.chatwoot_inbox
    assert inbox is not None
    assert inbox.admit(
        delivery_id="legacy-missing-message-id",
        payload={
            "event": "message_created",
            "content": "Mensaje legacy",
            "message_type": "incoming",
            "private": False,
            "conversation": {
                "id": 123,
                "contact_inbox": {
                    "source_id": "12025550123@s.whatsapp.net",
                },
            },
        },
    )

    asyncio.run(app.state.chatwoot_worker.run_once())

    envelope = json.loads(next((tmp_path / ".work").glob("*.json")).read_text())
    assert envelope["status"] == "admitted"
    assert envelope["attempts"] == 1
    assert shadow.calls == []


@pytest.mark.parametrize("field", ["admitted_at", "next_attempt_at"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_durable_timestamps_never_execute_work(
    tmp_path: Path,
    field: str,
    value: float,
) -> None:
    inbox = DurableChatwootInbox(tmp_path / ".work", clock=lambda: 5_000.0)
    assert inbox.admit(
        delivery_id=f"non-finite-{field}-{value}",
        payload={"id": 500, "conversation": {"id": 1}},
    )
    envelope_path = next((tmp_path / ".work").glob("*.json"))
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope[field] = value
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    calls: list[str] = []

    async def handler(
        delivery_id: str,
        payload: dict[str, object],
        batch_message_ids: tuple[int, ...],
    ) -> None:
        calls.append(delivery_id)

    worker = ChatwootWorker(
        inbox=inbox,
        handler=handler,
        debounce_key=lambda payload: "conversation-one",
        debounce_seconds=30,
        clock=lambda: 5_100.0,
    )

    asyncio.run(worker.run_once())

    assert calls == []
    assert inbox.admitted_items(include_deferred=True) == []
    assert json.loads(envelope_path.read_text(encoding="utf-8"))["status"] == "admitted"


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
    admitted_at = admitted.pop("admitted_at")
    assert isinstance(admitted_at, float)
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


def test_splits_and_sends_a_reply_in_order_with_delays_between_parts(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    reply = "Primera parte. Segunda parte. Tercera parte."
    proposal: dict[str, object] = {
        "decision": "ask_question",
        "qualification_status": "in_progress",
        "reason_code": "answer_question",
        "reply": reply,
        "captured_fields": {},
        "missing_fields": [],
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
    splitter = StubReplySplitter(
        ("Primera parte.", "Segunda parte.", "Tercera parte.")
    )
    delays: list[float] = []

    async def record_delay(seconds: float) -> None:
        delays.append(seconds)

    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            automated_replies_enabled=True,
            reply_dir=tmp_path / "replies",
            reply_splitter_enabled=True,
            reply_part_delay_seconds=2,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        reply_splitter=splitter,
        reply_part_sleep=record_delay,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="multipart-delivery"),
    )

    assert response.status_code == 202
    assert splitter.calls == []
    assert chatwoot.reply_calls == []

    asyncio.run(app.state.chatwoot_worker.run_once())
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert splitter.calls == [(123, 789, reply)]
    assert delays == [2, 2]
    assert chatwoot.reply_calls == [
        {
            "conversation_id": 123,
            "trigger_message_id": 789,
            "delivery_id": "multipart-delivery",
            "content": "Primera parte.",
            "part_index": 1,
            "part_count": 3,
            "prior_parts": (),
        },
        {
            "conversation_id": 123,
            "trigger_message_id": 789,
            "delivery_id": "multipart-delivery",
            "content": "Segunda parte.",
            "part_index": 2,
            "part_count": 3,
            "prior_parts": ("Primera parte.",),
        },
        {
            "conversation_id": 123,
            "trigger_message_id": 789,
            "delivery_id": "multipart-delivery",
            "content": "Tercera parte.",
            "part_index": 3,
            "part_count": 3,
            "prior_parts": ("Primera parte.", "Segunda parte."),
        },
    ]
    manifest_digest = hashlib.sha256(b"123:789").hexdigest()
    manifest = json.loads(
        (tmp_path / "replies" / ".splits" / f"{manifest_digest}.json").read_text(
            encoding="utf-8"
        )
    )
    assert [part["content"] for part in manifest["parts"]] == [
        "Primera parte.",
        "Segunda parte.",
        "Tercera parte.",
    ]


def test_replays_an_existing_multipart_manifest_when_feature_flag_is_off(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    reply = "Primera parte. Segunda parte."
    model_calls = 0

    def split_handler(request: httpx.Request) -> httpx.Response:
        nonlocal model_calls
        model_calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"parts":["Primera parte.","Segunda parte."]}'
                            )
                        }
                    }
                ]
            },
        )

    result_dir = tmp_path / "replies" / ".splits"
    splitter = HermesReplySplitter(
        base_url="https://hermes.example.test/v1",
        api_key="test-key",
        provider="small-provider",
        model_name="small-model",
        result_dir=result_dir,
        transport=httpx.MockTransport(split_handler),
    )
    assert asyncio.run(
        splitter.split(conversation_id=123, trigger_message_id=789, reply=reply)
    ) == ("Primera parte.", "Segunda parte.")

    proposal: dict[str, object] = {
        "decision": "ask_question",
        "qualification_status": "in_progress",
        "reason_code": "answer_question",
        "reply": reply,
        "captured_fields": {},
        "missing_fields": [],
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
    delays: list[float] = []

    async def record_delay(seconds: float) -> None:
        delays.append(seconds)

    app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path / "captures",
            max_age_seconds=300,
            agent_bot_id=1,
            automated_replies_enabled=True,
            reply_dir=tmp_path / "replies",
            reply_splitter_enabled=False,
            reply_part_delay_seconds=2,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=StubShadowProcessor(proposal),
        reply_part_sleep=record_delay,
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="flag-off-replay"),
    )
    assert response.status_code == 202

    asyncio.run(app.state.chatwoot_worker.run_once())

    assert model_calls == 1
    assert delays == [2]
    assert [call.get("part_index") for call in chatwoot.reply_calls] == [1, 2]
    assert [call.get("part_count") for call in chatwoot.reply_calls] == [2, 2]


def test_missing_manifest_after_claim_does_not_send_from_the_app(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    reply = "Primera parte. Segunda parte."

    def split_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"parts":["Primera parte.","Segunda parte."]}'
                        }
                    }
                ]
            },
        )

    result_dir = tmp_path / "replies" / ".splits"
    splitter = HermesReplySplitter(
        base_url="https://hermes.example.test/v1",
        api_key="test-key",
        provider="small-provider",
        model_name="small-model",
        result_dir=result_dir,
        transport=httpx.MockTransport(split_handler),
    )
    assert asyncio.run(
        splitter.split(conversation_id=123, trigger_message_id=789, reply=reply)
    ) == ("Primera parte.", "Segunda parte.")
    batch_hash = hashlib.sha256(b"123:789").hexdigest()
    (result_dir / f"{batch_hash}.json").unlink()

    payload = {
        "event": "message_created",
        "id": 789,
        "content": "Hola",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "id": 123,
            "contact_inbox": {"source_id": "12025550123@s.whatsapp.net"},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    proposal: dict[str, object] = {
        "decision": "ask_question",
        "qualification_status": "in_progress",
        "reason_code": "answer_question",
        "reply": reply,
        "captured_fields": {},
        "missing_fields": [],
    }
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
            capture_dir=tmp_path / "captures",
            max_age_seconds=300,
            agent_bot_id=1,
            automated_replies_enabled=True,
            reply_dir=tmp_path / "replies",
            reply_splitter_enabled=False,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=StubShadowProcessor(proposal),
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="missing-manifest"),
    )
    assert response.status_code == 202

    asyncio.run(app.state.chatwoot_worker.run_once())

    assert chatwoot.reply_calls == []
    work_path = next((tmp_path / "captures" / ".work").glob("*.json"))
    work = json.loads(work_path.read_text(encoding="utf-8"))
    assert work["status"] == "admitted"


def test_splitter_exception_falls_back_to_one_original_reply(tmp_path: Path) -> None:
    secret = "webhook-secret"
    reply = "Respuesta comercial válida."
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
    shadow = StubShadowProcessor(
        {
            "decision": "ask_question",
            "qualification_status": "in_progress",
            "reason_code": "answer_question",
            "reply": reply,
            "captured_fields": {},
            "missing_fields": [],
        }
    )
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
            reply_dir=tmp_path / "replies",
            reply_splitter_enabled=True,
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        reply_splitter=FailingReplySplitter(),
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="splitter-failure"),
    )
    assert response.status_code == 202

    asyncio.run(app.state.chatwoot_worker.run_once())

    assert chatwoot.reply_calls == [
        {
            "conversation_id": 123,
            "trigger_message_id": 789,
            "delivery_id": "splitter-failure",
            "content": reply,
        }
    ]
    manifest_digest = hashlib.sha256(b"123:789").hexdigest()
    manifest = json.loads(
        (tmp_path / "replies" / ".splits" / f"{manifest_digest}.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["status"] == "fallback"
    assert [part["content"] for part in manifest["parts"]] == [reply]


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
        chatwoot_inbox_id=7,
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
        "account": {"id": 1},
        "inbox": {"id": 7},
        "conversation": {
            "id": 124,
            "inbox_id": 7,
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
    assert chatwoot.history_calls == [(123, 200)]
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
            chatwoot_human_pause_enabled=True,
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
            chatwoot_human_pause_enabled=True,
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


def test_default_off_human_pause_discards_new_and_stale_work(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "private": False,
        "sender": {"id": 1, "type": "user"},
        "conversation": {
            "id": 2,
            "meta": {
                "sender": {"identifier": "12025550123@s.whatsapp.net"},
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    producer_client = StubChatwootClient()
    producer = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
            chatwoot_human_pause_enabled=True,
        ),
        chatwoot_client=producer_client,
    )
    admitted = _post(
        producer,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="stale-human-pause"),
    )
    assert admitted.status_code == 202

    safe_client = StubChatwootClient()
    safe_app = create_app(
        Settings(
            webhook_secret=secret,
            allowed_jid="12025550123@s.whatsapp.net",
            capture_dir=tmp_path,
            max_age_seconds=300,
            agent_bot_id=1,
        ),
        chatwoot_client=safe_client,
    )
    asyncio.run(safe_app.state.chatwoot_worker.run_once())

    assert producer_client.calls == []
    assert safe_client.calls == []
    work_items = list((tmp_path / ".work").glob("*.json"))
    assert len(work_items) == 1
    completed = json.loads(work_items[0].read_text(encoding="utf-8"))
    assert completed["status"] == "completed"

    ignored = _post(
        safe_app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="new-human-pause"),
    )
    assert ignored.status_code == 200
    assert ignored.json() == {
        "status": "ignored",
        "reason": "human_pause_disabled",
    }
    assert len(list((tmp_path / ".work").glob("*.json"))) == 1
    assert safe_client.calls == []


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


def test_ignores_an_incoming_message_without_a_canonical_id(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    payload = {
        "event": "message_created",
        "content": "Mensaje sin ID",
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
        _signed_headers(raw_body, secret=secret, delivery="missing-message-id"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ignored",
        "reason": "invalid_message_id",
    }
    assert list((tmp_path / ".work").glob("*.json")) == []


@pytest.mark.parametrize(
    ("admission_outcome", "reply_expected"),
    [
        ("created", True),
        ("already_exists", True),
        ("evidence_conflict", False),
    ],
)
def test_cut_b_agent_gate_admits_then_replies_through_canonical_chatwoot(
    tmp_path: Path,
    admission_outcome: str,
    reply_expected: bool,
) -> None:
    secret = "webhook-secret"
    payload: dict[str, object] = {
        "event": "message_created",
        "id": 901,
        "content": "Quiero saber más",
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 9},
        "conversation": {
            "id": 321,
            "inbox_id": 9,
            "contact_inbox": {
                "source_id": "12025550124@s.whatsapp.net",
            },
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    supabase = StubInboundCommercialSupabase(outcome=admission_outcome)
    shadow = StubShadowProcessor({"reply": "Claro, ¿qué te gustaría saber?"})
    chatwoot = StubChatwootClient(
        messages=[
            {
                "id": 901,
                "created_at": 1786233960,
                "message_type": 0,
                "private": False,
                "content": "Quiero saber más",
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
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            chatwoot_cut_b_admission_enabled=True,
            chatwoot_cut_b_scope_key="libre-de-ansiedad-inbound",
            chatwoot_cut_b_scope_version=2,
            chatwoot_cut_b_agent_enabled=True,
            chatwoot_scoped_inbound_senders_enabled=True,
            automated_replies_enabled=True,
            chatwoot_durable_opt_out_enabled=True,
            chatwoot_human_pause_enabled=True,
            chatwoot_opt_out_macro_id=2,
            opt_out_projection_worker_id="opt-out-test",
            human_handoff_admission_enabled=True,
            human_handoff_projection_enabled=True,
            handoff_projection_policy_key="lancemos-inbound-handoff",
            handoff_projection_policy_version=1,
            human_handoff_projection_worker_id="handoff-projection-test",
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="cut-b-agent-reply"),
    )
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert response.status_code == 202
    assert supabase.admission_calls == [
        {
            "scope_key": "libre-de-ansiedad-inbound",
            "scope_version": 2,
            "external_conversation_id": 321,
            "external_user_id": "12025550124",
        }
    ]
    if reply_expected:
        assert len(shadow.calls) == 1
        assert chatwoot.reply_calls == [
            {
                "conversation_id": 321,
                "trigger_message_id": 901,
                "delivery_id": "cut-b-agent-reply",
                "content": "Claro, ¿qué te gustaría saber?",
                "expected_jid": "12025550124@s.whatsapp.net",
            }
        ]
    else:
        assert shadow.calls == []
        assert chatwoot.reply_calls == []


@pytest.mark.parametrize(
    ("label_error", "expected_work_status"),
    [(None, "completed"), (ChatwootProtocolError("not confirmed"), "admitted")],
)
def test_cut_b_handoff_confirms_automation_pause_without_reply(
    tmp_path: Path,
    label_error: Exception | None,
    expected_work_status: str,
) -> None:
    secret = "webhook-secret"
    payload: dict[str, object] = {
        "event": "message_created",
        "id": 902,
        "content": "Me cobraron dos veces y quiero una devolución",
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 9},
        "conversation": {
            "id": 322,
            "inbox_id": 9,
            "contact_inbox": {"source_id": "12025550124@s.whatsapp.net"},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    supabase = StubInboundCommercialSupabase()
    shadow = StubShadowProcessor(
        {"decision": "handoff", "reply": "Este caso requiere revisión humana."}
    )
    chatwoot = StubChatwootClient(
        label_error=label_error,
        messages=[
            {
                "id": 902,
                "created_at": 1786233960,
                "message_type": 0,
                "private": False,
                "content": "Me cobraron dos veces y quiero una devolución",
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
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            chatwoot_cut_b_admission_enabled=True,
            chatwoot_cut_b_scope_key="libre-de-ansiedad-inbound",
            chatwoot_cut_b_scope_version=2,
            chatwoot_cut_b_agent_enabled=True,
            chatwoot_scoped_inbound_senders_enabled=True,
            automated_replies_enabled=True,
            chatwoot_durable_opt_out_enabled=True,
            chatwoot_human_pause_enabled=True,
            chatwoot_opt_out_macro_id=2,
            opt_out_projection_worker_id="opt-out-test",
            human_handoff_admission_enabled=True,
            human_handoff_projection_enabled=True,
            handoff_projection_policy_key="lancemos-inbound-handoff",
            handoff_projection_policy_version=1,
            human_handoff_projection_worker_id="handoff-projection-test",
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="cut-b-handoff"),
    )
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert response.status_code == 202
    assert len(supabase.handoff_calls) == 1
    assert supabase.handoff_calls[0]["commercial_case_id"] == "case-1"
    assert chatwoot.reply_calls == []
    assert chatwoot.authority_calls == [
        {
            "conversation_id": 322,
            "expected_inbox_id": 9,
            "expected_jid": "12025550124@s.whatsapp.net",
        }
    ]
    assert chatwoot.calls == [(322, "automation_paused")]
    assert chatwoot.events == ["label:automation_paused"]
    envelope = json.loads(next((tmp_path / ".work").glob("*.json")).read_text())
    assert envelope["status"] == expected_work_status
    if label_error is not None:
        assert envelope["last_error_type"] == "RetryableChatwootWorkError"


def test_cut_b_direct_medication_guidance_forces_durable_handoff(
    tmp_path: Path,
) -> None:
    secret = "webhook-secret"
    content = (
        "Necesito saber si debo dejar mi medicación psiquiátrica para hacer "
        "el programa y qué dosis debería tomar"
    )
    payload: dict[str, object] = {
        "event": "message_created",
        "id": 903,
        "content": content,
        "message_type": "incoming",
        "private": False,
        "account": {"id": 1},
        "inbox": {"id": 9},
        "conversation": {
            "id": 323,
            "inbox_id": 9,
            "contact_inbox": {"source_id": "12025550123@s.whatsapp.net"},
        },
    }
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    supabase = StubInboundCommercialSupabase()
    safe_reply = "No puedo orientar sobre medicación; consultá a tu profesional."
    shadow = StubShadowProcessor({"decision": "reply", "reply": safe_reply})
    chatwoot = StubChatwootClient(
        messages=[
            {
                "id": 903,
                "created_at": 1786233960,
                "message_type": 0,
                "private": False,
                "content": content,
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
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            chatwoot_cut_b_admission_enabled=True,
            chatwoot_cut_b_scope_key="libre-de-ansiedad-inbound",
            chatwoot_cut_b_scope_version=2,
            chatwoot_cut_b_agent_enabled=True,
            automated_replies_enabled=True,
            human_handoff_admission_enabled=True,
            human_handoff_projection_enabled=True,
            handoff_projection_policy_key="lancemos-inbound-handoff",
            handoff_projection_policy_version=1,
            human_handoff_projection_worker_id="handoff-projection-test",
        ),
        chatwoot_client=chatwoot,
        shadow_processor=shadow,
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(
        app,
        raw_body,
        _signed_headers(raw_body, secret=secret, delivery="clinical-handoff"),
    )
    asyncio.run(app.state.chatwoot_worker.run_once())

    assert response.status_code == 202
    assert len(supabase.handoff_calls) == 1
    assert supabase.handoff_calls[0]["commercial_case_id"] == "case-1"
    assert chatwoot.reply_calls == []
    assert chatwoot.calls == [(323, "automation_paused")]
    assert chatwoot.events == ["label:automation_paused"]


@pytest.mark.parametrize(
    "content",
    [
        "¿El programa incluye información general sobre medicación?",
        "¿Cuántas dosis semanales incluye el programa?",
        None,
    ],
)
def test_medication_handoff_guard_ignores_general_or_non_text_content(
    content: object,
) -> None:
    assert _requires_medication_guidance_handoff(content) is False
