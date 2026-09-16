from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bridge.chatwoot import ChatwootProtocolError
from bridge.chatwoot_inbox import ChatwootStalledConversationMonitor, DurableChatwootInbox


def _exercise_recovering_monitor(
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> list[str]:
    inbox = DurableChatwootInbox(tmp_path / ".work")
    scans = 0

    async def scan() -> list[object]:
        nonlocal scans
        scans += 1
        if scans == 1:
            raise failure
        return []

    monitor = ChatwootStalledConversationMonitor(
        inbox=inbox,
        scanner=scan,
        scan_interval_seconds=0.01,
    )

    async def exercise() -> None:
        await monitor.start()
        try:
            async with asyncio.timeout(1):
                while monitor.last_scan_state != "healthy":
                    await asyncio.sleep(0.01)
        finally:
            await monitor.stop()

    caplog.set_level("WARNING", logger="bridge.chatwoot_inbox")
    asyncio.run(exercise())

    assert scans >= 2
    assert monitor.has_completed_scan is True
    assert monitor.last_scan_state == "stopped"
    return caplog.messages


def test_stalled_monitor_does_not_log_arbitrary_exception_message(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    messages = _exercise_recovering_monitor(
        tmp_path=tmp_path,
        caplog=caplog,
        failure=RuntimeError("private conversation text"),
    )

    assert (
        "chatwoot_stalled_monitor_scan_failed "
        "error_type=RuntimeError"
    ) in messages
    assert "private conversation text" not in caplog.text


def test_stalled_monitor_logs_sanitized_chatwoot_protocol_reason_code(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    messages = _exercise_recovering_monitor(
        tmp_path=tmp_path,
        caplog=caplog,
        failure=ChatwootProtocolError("invalid_conversations_payload"),
    )

    assert (
        "chatwoot_stalled_monitor_scan_failed "
        "error_type=ChatwootProtocolError "
        "reason_code=invalid_conversations_payload"
    ) in messages


def test_stalled_monitor_redacts_unsafe_chatwoot_protocol_reason(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    messages = _exercise_recovering_monitor(
        tmp_path=tmp_path,
        caplog=caplog,
        failure=ChatwootProtocolError("alice_smith"),
    )

    assert (
        "chatwoot_stalled_monitor_scan_failed "
        "error_type=ChatwootProtocolError"
    ) in messages
    assert "reason_code=" not in caplog.text
    assert "alice_smith" not in caplog.text
