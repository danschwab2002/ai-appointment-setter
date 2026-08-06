"""Durable file-backed admission for Chatwoot webhook work."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import random
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)


class RetryableChatwootWorkError(RuntimeError):
    """A transient dependency failure that must remain admitted."""


@dataclass(frozen=True)
class ChatwootWorkItem:
    delivery_id: str
    payload: dict[str, object]
    path: Path
    attempts: int = 0
    next_attempt_at: float = 0.0


class DurableChatwootInbox:
    """Persist accepted webhook work before acknowledging its delivery."""

    def __init__(self, work_dir: Path) -> None:
        self._work_dir = work_dir
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
        serialized = (
            json.dumps(
                {
                    "status": "admitted",
                    "delivery_id": delivery_id,
                    "payload": payload,
                },
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

    def admitted_items(self) -> list[ChatwootWorkItem]:
        if not self._work_dir.exists():
            return []
        return [
            item
            for path in sorted(self._work_dir.glob("*.json"))
            if (item := self.admitted_item(path)) is not None
        ]

    def admitted_item(self, path: Path) -> ChatwootWorkItem | None:
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
        if (
            not isinstance(delivery_id, str)
            or not delivery_id
            or not isinstance(payload, dict)
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts < 0
            or not isinstance(next_attempt_at, (int, float))
            or isinstance(next_attempt_at, bool)
            or path.stem
            != hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
        ):
            return None
        if float(next_attempt_at) > time.time():
            return None
        return ChatwootWorkItem(
            delivery_id=delivery_id,
            payload=payload,
            path=path,
            attempts=attempts,
            next_attempt_at=float(next_attempt_at),
        )

    def complete(self, item: ChatwootWorkItem) -> None:
        self._replace_envelope(
            item.path,
            {
                "status": "completed",
                "delivery_id": item.delivery_id,
                "payload": {},
                "attempts": item.attempts,
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
            next_attempt_at = time.time() + delay_seconds
        self._replace_envelope(
            item.path,
            {
                "status": status,
                "delivery_id": item.delivery_id,
                "payload": item.payload,
                "attempts": attempts,
                "next_attempt_at": next_attempt_at,
                "last_error_type": error_type,
            },
        )
        return status

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
            directory_fd = os.open(
                self._work_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
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
        handler: Callable[[str, dict[str, object]], Awaitable[None]],
        poll_interval_seconds: float = 1.0,
        handler_timeout_seconds: float = 120.0,
    ) -> None:
        if handler_timeout_seconds <= 0:
            raise ValueError("handler_timeout_seconds must be positive")
        self._inbox = inbox
        self._handler = handler
        self._poll_interval_seconds = poll_interval_seconds
        self._handler_timeout_seconds = handler_timeout_seconds
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
        for item in self._inbox.admitted_items():
            lock_fd = os.open(
                item.path.with_suffix(".processing.lock"),
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
                current = self._inbox.admitted_item(item.path)
                if current is None:
                    continue
                try:
                    async with asyncio.timeout(self._handler_timeout_seconds):
                        await self._handler(current.delivery_id, current.payload)
                except Exception as exc:
                    failure_status = self._inbox.record_failure(
                        current,
                        error_type=type(exc).__name__,
                        max_attempts=(
                            None
                            if isinstance(exc, RetryableChatwootWorkError)
                            else 8
                        ),
                    )
                    logger.warning(
                        "chatwoot_work_failed error_type=%s status=%s",
                        type(exc).__name__,
                        failure_status,
                    )
                    continue
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
