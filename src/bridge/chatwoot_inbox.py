"""Durable file-backed admission for Chatwoot webhook work."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import math
import os
import random
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from bridge.chatwoot import ChatwootProtocolError


logger = logging.getLogger(__name__)

_STALLED_MONITOR_PROTOCOL_REASON_CODES = frozenset(
    {
        "conversation_count_changed",
        "conversation_scan_incomplete",
        "invalid_conversation_scope",
        "invalid_conversations_payload",
        "invalid_json",
        "invalid_message_id",
        "invalid_messages_payload",
        "messages_cursor_did_not_advance",
    }
)


def _stalled_monitor_reason_code(exc: Exception) -> str | None:
    if isinstance(exc, ChatwootProtocolError):
        reason_code = str(exc)
        if reason_code in _STALLED_MONITOR_PROTOCOL_REASON_CODES:
            return reason_code
    return None


class RetryableChatwootWorkError(RuntimeError):
    """A transient dependency failure that must remain admitted."""


class ChatwootStalledConversationMonitor:
    """Recurrently admit scanner candidates into the existing durable inbox."""

    def __init__(
        self,
        *,
        inbox: DurableChatwootInbox,
        scanner: Callable[[], Awaitable[list[object]]],
        scan_interval_seconds: float = 60.0,
        recovery_cooldown_seconds: float = 300.0,
        recovery_max_admissions: int = 3,
    ) -> None:
        if not math.isfinite(scan_interval_seconds) or scan_interval_seconds <= 0:
            raise ValueError("scan_interval_seconds must be finite and positive")
        if (
            not math.isfinite(recovery_cooldown_seconds)
            or recovery_cooldown_seconds <= 0
            or not isinstance(recovery_max_admissions, int)
            or isinstance(recovery_max_admissions, bool)
            or recovery_max_admissions < 1
        ):
            raise ValueError("invalid stalled recovery policy")
        self._inbox = inbox
        self._scanner = scanner
        self._scan_interval_seconds = scan_interval_seconds
        self._recovery_cooldown_seconds = recovery_cooldown_seconds
        self._recovery_max_admissions = recovery_max_admissions
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_scan_state = "never"
        self._has_completed_scan = False

    @property
    def last_scan_state(self) -> str:
        return self._last_scan_state

    @property
    def has_completed_scan(self) -> bool:
        return self._has_completed_scan

    @property
    def ready(self) -> bool:
        return (
            self._task is not None
            and not self._task.done()
            and self._has_completed_scan
            and self._last_scan_state == "healthy"
        )

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._last_scan_state = "never"
            self._has_completed_scan = False
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            self._last_scan_state = "stopped"
            return
        self._stopping.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._last_scan_state = "stopped"

    async def run_once(self) -> None:
        candidates = await self._scanner()
        candidate_failed = False
        for candidate in candidates:
            try:
                delivery_id = getattr(candidate, "delivery_id")
                payload = getattr(candidate, "payload")
                if not isinstance(delivery_id, str) or not isinstance(payload, dict):
                    raise TypeError("invalid stalled monitor candidate")
                self._inbox.admit_recovery(
                    delivery_id=delivery_id,
                    payload=payload,
                    cooldown_seconds=self._recovery_cooldown_seconds,
                    max_admissions=self._recovery_max_admissions,
                )
            except Exception as exc:
                candidate_failed = True
                logger.warning(
                    "chatwoot_stalled_monitor_candidate_failed error_type=%s",
                    type(exc).__name__,
                )
        self._has_completed_scan = True
        self._last_scan_state = "error" if candidate_failed else "healthy"

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                self._last_scan_state = "error"
                reason_code = _stalled_monitor_reason_code(exc)
                if reason_code is None:
                    logger.warning(
                        "chatwoot_stalled_monitor_scan_failed error_type=%s",
                        type(exc).__name__,
                    )
                else:
                    logger.warning(
                        "chatwoot_stalled_monitor_scan_failed "
                        "error_type=%s reason_code=%s",
                        type(exc).__name__,
                        reason_code,
                    )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._scan_interval_seconds,
                )
            except TimeoutError:
                pass


@dataclass(frozen=True)
class ChatwootWorkItem:
    delivery_id: str
    payload: dict[str, object]
    path: Path
    attempts: int = 0
    next_attempt_at: float = 0.0
    admitted_at: float = 0.0
    recovery_count: int = 0


def _canonical_message_id(item: ChatwootWorkItem) -> int:
    message_id = item.payload.get("id")
    if isinstance(message_id, int) and not isinstance(message_id, bool):
        return message_id
    return -1


def _select_group_leader(
    items: list[ChatwootWorkItem],
) -> tuple[ChatwootWorkItem, ChatwootWorkItem]:
    latest_arrival = max(
        items,
        key=lambda candidate: (candidate.admitted_at, candidate.path.name),
    )
    canonical_items = [
        candidate for candidate in items if _canonical_message_id(candidate) >= 0
    ]
    leader = (
        max(
            canonical_items,
            key=lambda candidate: (
                _canonical_message_id(candidate),
                candidate.admitted_at,
                candidate.path.name,
            ),
        )
        if canonical_items
        else latest_arrival
    )
    return latest_arrival, leader


class DurableChatwootInbox:
    """Persist accepted webhook work before acknowledging its delivery."""

    def __init__(
        self,
        work_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._work_dir = work_dir
        self._clock = clock
        self._ensure_private_work_dir()

    def _ensure_private_work_dir(self) -> None:
        self._work_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            directory_fd = os.open(
                self._work_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise RuntimeError("chatwoot_work_dir_not_private") from exc
        try:
            directory_stat = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_uid != os.geteuid()
            ):
                raise RuntimeError("chatwoot_work_dir_not_private")
            os.fchmod(directory_fd, 0o700)
        finally:
            os.close(directory_fd)

    def admit(self, *, delivery_id: str, payload: dict[str, object]) -> bool:
        self._ensure_private_work_dir()
        digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        work_path = self._work_dir / f"{digest}.json"
        marker = payload.get("_stalled_monitor")
        recovery_count = 1 if marker == {"version": 1} else 0
        envelope: dict[str, object] = {
            "status": "admitted",
            "delivery_id": delivery_id,
            "payload": payload,
            "admitted_at": self._clock(),
        }
        if recovery_count:
            envelope["recovery_count"] = recovery_count
        serialized = (
            json.dumps(
                envelope,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )

        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.", suffix=".tmp", dir=self._work_dir
        )
        try:
            os.fchmod(temporary_fd, 0o600)
            handle = os.fdopen(temporary_fd, "w", encoding="utf-8")
            temporary_fd = -1
            with handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_name, work_path)
            except FileExistsError:
                return False
            directory_fd = os.open(
                self._work_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return True
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            os.unlink(temporary_name)

    def admit_recovery(
        self,
        *,
        delivery_id: str,
        payload: dict[str, object],
        cooldown_seconds: float,
        max_admissions: int,
    ) -> bool:
        """Admit or boundedly re-arm one deterministic stalled-message identity."""
        if (
            payload.get("_stalled_monitor") != {"version": 1}
            or not math.isfinite(cooldown_seconds)
            or cooldown_seconds <= 0
            or not isinstance(max_admissions, int)
            or isinstance(max_admissions, bool)
            or max_admissions < 1
        ):
            raise ValueError("invalid stalled recovery admission")
        digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        work_path = self._work_dir / f"{digest}.json"
        envelope = self._read_private_envelope(work_path)
        if envelope is None:
            return self.admit(delivery_id=delivery_id, payload=payload)
        if envelope.get("delivery_id") != delivery_id or envelope.get("payload") != payload:
            raise RuntimeError("stalled_recovery_identity_conflict")
        recovery_count = envelope.get("recovery_count", 0)
        terminal_at = envelope.get("terminal_at")
        if (
            envelope.get("status") not in {"completed", "failed"}
            or not isinstance(recovery_count, int)
            or isinstance(recovery_count, bool)
            or recovery_count >= max_admissions
            or not isinstance(terminal_at, (int, float))
            or isinstance(terminal_at, bool)
            or self._clock() - float(terminal_at) < cooldown_seconds
        ):
            return False
        self._replace_envelope(
            work_path,
            {
                "status": "admitted",
                "delivery_id": delivery_id,
                "payload": payload,
                "admitted_at": self._clock(),
                "attempts": 0,
                "next_attempt_at": 0.0,
                "recovery_count": recovery_count + 1,
            },
        )
        return True

    def admitted_items(
        self,
        *,
        include_deferred: bool = False,
    ) -> list[ChatwootWorkItem]:
        if not self._work_dir.exists():
            return []
        self._reconcile_group_failures()
        return [
            item
            for path in sorted(self._work_dir.glob("*.json"))
            if (
                item := self.admitted_item(
                    path,
                    include_deferred=include_deferred,
                )
            )
            is not None
        ]

    def admitted_item(
        self,
        path: Path,
        *,
        include_deferred: bool = False,
    ) -> ChatwootWorkItem | None:
        if path.parent != self._work_dir:
            return None
        try:
            file_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            file_stat = os.fstat(file_fd)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.geteuid()
            ):
                return None
            with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
                file_fd = -1
                envelope = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        finally:
            if file_fd >= 0:
                os.close(file_fd)
        if not isinstance(envelope, dict) or envelope.get("status") != "admitted":
            return None
        delivery_id = envelope.get("delivery_id")
        payload = envelope.get("payload")
        attempts = envelope.get("attempts", 0)
        next_attempt_at = envelope.get("next_attempt_at", 0.0)
        admitted_at = envelope.get("admitted_at", file_stat.st_mtime)
        recovery_count = envelope.get("recovery_count", 0)
        if (
            not isinstance(delivery_id, str)
            or not delivery_id
            or not isinstance(payload, dict)
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts < 0
            or not isinstance(next_attempt_at, (int, float))
            or isinstance(next_attempt_at, bool)
            or not math.isfinite(float(next_attempt_at))
            or not isinstance(admitted_at, (int, float))
            or isinstance(admitted_at, bool)
            or not math.isfinite(float(admitted_at))
            or not isinstance(recovery_count, int)
            or isinstance(recovery_count, bool)
            or recovery_count < 0
            or path.stem
            != hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        ):
            return None
        if not include_deferred and float(next_attempt_at) > self._clock():
            return None
        return ChatwootWorkItem(
            delivery_id=delivery_id,
            payload=payload,
            path=path,
            attempts=attempts,
            next_attempt_at=float(next_attempt_at),
            admitted_at=float(admitted_at),
            recovery_count=recovery_count,
        )

    def complete(self, item: ChatwootWorkItem) -> None:
        self._replace_envelope(
            item.path,
            {
                "status": "completed",
                "delivery_id": item.delivery_id,
                "payload": item.payload if item.recovery_count > 0 else {},
                "attempts": item.attempts,
                "recovery_count": item.recovery_count,
                "terminal_at": self._clock(),
            },
        )

    def record_failure(
        self,
        item: ChatwootWorkItem,
        *,
        error_type: str,
        max_attempts: int | None = 8,
    ) -> str:
        attempts = item.attempts + 1
        if max_attempts is not None and attempts >= max_attempts:
            status = "failed"
            next_attempt_at = 0.0
        else:
            status = "admitted"
            delay_seconds = min(
                2 ** min(attempts, 7) * random.uniform(0.8, 1.2),
                60,
            )
            next_attempt_at = self._clock() + delay_seconds
        self._replace_envelope(
            item.path,
            {
                "status": status,
                "delivery_id": item.delivery_id,
                "payload": item.payload,
                "attempts": attempts,
                "next_attempt_at": next_attempt_at,
                "admitted_at": item.admitted_at,
                "last_error_type": error_type,
                "recovery_count": item.recovery_count,
                **({"terminal_at": self._clock()} if status == "failed" else {}),
            },
        )
        return status

    def fail_group(
        self,
        items: list[ChatwootWorkItem],
        *,
        leader: ChatwootWorkItem,
        error_type: str,
    ) -> None:
        member_files = sorted({item.path.name for item in items})
        if leader.path.name not in member_files:
            raise ValueError("group leader must be a group member")
        journal_path = self._work_dir / (
            f".group-failure-{leader.path.stem}.json"
        )
        self._replace_envelope(
            journal_path,
            {
                "status": "group_failure",
                "leader_file": leader.path.name,
                "member_files": member_files,
                "leader_error_type": error_type,
            },
        )
        self._reconcile_group_failure(journal_path)

    def _reconcile_group_failures(self) -> None:
        for path in sorted(self._work_dir.glob(".group-failure-*.json")):
            self._reconcile_group_failure(path)

    def _reconcile_group_failure(self, journal_path: Path) -> None:
        journal = self._read_private_envelope(journal_path)
        if journal is None:
            if journal_path.exists():
                raise RuntimeError("invalid_group_failure_journal")
            return
        member_files = journal.get("member_files")
        leader_file = journal.get("leader_file")
        leader_error_type = journal.get("leader_error_type")
        if (
            journal.get("status") != "group_failure"
            or not isinstance(member_files, list)
            or not member_files
            or not all(self._is_work_filename(name) for name in member_files)
            or not isinstance(leader_file, str)
            or leader_file not in member_files
            or not isinstance(leader_error_type, str)
            or not leader_error_type
        ):
            raise RuntimeError("invalid_group_failure_journal")
        for member_file in member_files:
            member_path = self._work_dir / member_file
            envelope = self._read_private_envelope(member_path)
            if envelope is None:
                raise RuntimeError("missing_group_failure_member")
            if envelope.get("status") == "failed":
                continue
            item = self.admitted_item(member_path, include_deferred=True)
            if item is None:
                raise RuntimeError("invalid_group_failure_member")
            is_leader = member_file == leader_file
            self._replace_envelope(
                member_path,
                {
                    "status": "failed",
                    "delivery_id": item.delivery_id,
                    "payload": item.payload,
                    "attempts": item.attempts + (1 if is_leader else 0),
                    "next_attempt_at": 0.0,
                    "admitted_at": item.admitted_at,
                    "last_error_type": (
                        leader_error_type if is_leader else "GroupedLeaderFailed"
                    ),
                    "recovery_count": item.recovery_count,
                    "terminal_at": self._clock(),
                },
            )
        try:
            journal_path.unlink()
        except FileNotFoundError:
            return
        self._fsync_work_dir()

    @staticmethod
    def _is_work_filename(value: object) -> bool:
        if not isinstance(value, str) or not value.endswith(".json"):
            return False
        stem = value[:-5]
        return len(stem) == 64 and all(
            character in "0123456789abcdef" for character in stem
        )

    def _read_private_envelope(self, path: Path) -> dict[str, object] | None:
        if path.parent != self._work_dir:
            return None
        try:
            file_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            file_stat = os.fstat(file_fd)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.geteuid()
            ):
                return None
            with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
                file_fd = -1
                envelope = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        finally:
            if file_fd >= 0:
                os.close(file_fd)
        return envelope if isinstance(envelope, dict) else None

    def _fsync_work_dir(self) -> None:
        directory_fd = os.open(
            self._work_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def processing_lock_path(self, *, namespace: str, key: str) -> Path:
        digest = hashlib.sha256(f"{namespace}:{key}".encode("utf-8")).hexdigest()
        return self._work_dir / f".{namespace}-{digest}.processing.lock"

    def _replace_envelope(self, path: Path, envelope: dict[str, object]) -> None:
        serialized = json.dumps(envelope, ensure_ascii=False, indent=2) + "\n"
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=self._work_dir
        )
        try:
            os.fchmod(temporary_fd, 0o600)
            handle = os.fdopen(temporary_fd, "w", encoding="utf-8")
            temporary_fd = -1
            with handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            self._fsync_work_dir()
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


class ChatwootWorker:
    """Replay durably admitted Chatwoot work outside the HTTP request."""

    def __init__(
        self,
        *,
        inbox: DurableChatwootInbox,
        handler: Callable[
            [str, dict[str, object], tuple[int, ...]], Awaitable[None]
        ],
        poll_interval_seconds: float = 1.0,
        handler_timeout_seconds: float = 120.0,
        debounce_key: Callable[[dict[str, object]], str | None] | None = None,
        debounce_seconds: float = 0.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if handler_timeout_seconds <= 0:
            raise ValueError("handler_timeout_seconds must be positive")
        if not math.isfinite(debounce_seconds) or debounce_seconds < 0:
            raise ValueError("debounce_seconds must be finite and not negative")
        self._inbox = inbox
        self._handler = handler
        self._poll_interval_seconds = poll_interval_seconds
        self._handler_timeout_seconds = handler_timeout_seconds
        self._debounce_key = debounce_key
        self._debounce_seconds = debounce_seconds
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def run_once(self) -> None:
        grouped: dict[tuple[str, str], list[ChatwootWorkItem]] = {}
        for item in self._inbox.admitted_items(include_deferred=True):
            debounce_key = (
                self._debounce_key(item.payload)
                if self._debounce_key is not None
                and self._debounce_seconds > 0
                else None
            )
            group_key = (
                ("conversation", debounce_key)
                if debounce_key is not None
                else ("delivery", item.delivery_id)
            )
            grouped.setdefault(group_key, []).append(item)

        for group_key, items in grouped.items():
            latest_arrival, item = _select_group_leader(items)
            if item.next_attempt_at > self._clock():
                continue
            if (
                group_key[0] == "conversation"
                and self._clock()
                < latest_arrival.admitted_at + self._debounce_seconds
            ):
                continue
            lock_path = self._inbox.processing_lock_path(
                namespace=group_key[0],
                key=group_key[1],
            )
            lock_fd = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            try:
                lock_stat = os.fstat(lock_fd)
                if (
                    not stat.S_ISREG(lock_stat.st_mode)
                    or lock_stat.st_uid != os.geteuid()
                ):
                    raise RuntimeError("chatwoot_work_lock_not_private")
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if group_key[0] == "conversation":
                    items = [
                        candidate
                        for candidate in self._inbox.admitted_items(
                            include_deferred=True
                        )
                        if self._debounce_key is not None
                        and self._debounce_key(candidate.payload) == group_key[1]
                    ]
                    if not items:
                        continue
                    latest_arrival, current = _select_group_leader(items)
                    if current.next_attempt_at > self._clock():
                        continue
                    if (
                        self._clock()
                        < latest_arrival.admitted_at + self._debounce_seconds
                    ):
                        continue
                else:
                    current = self._inbox.admitted_item(item.path)
                    if current is None:
                        continue
                    items = [current]
                try:
                    batch_message_ids = tuple(
                        sorted(
                            message_id
                            for member in items
                            if (message_id := _canonical_message_id(member)) >= 0
                        )
                    )
                    async with asyncio.timeout(self._handler_timeout_seconds):
                        await self._handler(
                            current.delivery_id,
                            current.payload,
                            batch_message_ids,
                        )
                except Exception as exc:
                    max_attempts = (
                        1
                        if current.recovery_count > 0
                        else None
                        if isinstance(exc, RetryableChatwootWorkError)
                        else 8
                    )
                    terminal_group_failure = (
                        len(items) > 1
                        and max_attempts is not None
                        and current.attempts + 1 >= max_attempts
                    )
                    if terminal_group_failure:
                        self._inbox.fail_group(
                            items,
                            leader=current,
                            error_type=type(exc).__name__,
                        )
                        failure_status = "failed"
                    else:
                        failure_status = self._inbox.record_failure(
                            current,
                            error_type=type(exc).__name__,
                            max_attempts=max_attempts,
                        )
                    logger.warning(
                        "chatwoot_work_failed error_type=%s status=%s",
                        type(exc).__name__,
                        failure_status,
                    )
                    continue
                for superseded in items:
                    if superseded.path != current.path:
                        self._inbox.complete(superseded)
                self._inbox.complete(current)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                logger.warning(
                    "chatwoot_worker_iteration_failed error_type=%s",
                    type(exc).__name__,
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                pass
