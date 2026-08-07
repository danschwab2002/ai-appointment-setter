"""Strict, durable formatting-only splitter for outbound replies."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

import httpx


_SYSTEM_PROMPT = """You split an already approved WhatsApp reply into natural message bubbles.
Return only one JSON object with exactly this shape: {\"parts\":[\"...\"]}.
Use between 1 and 4 non-empty parts according to length and natural sentence boundaries.
Do not add, remove, translate, correct, summarize, reorder, or rewrite any word or punctuation.
Only boundary whitespace may change. Short replies should remain one part.
"""


def reply_batch_hash(*, conversation_id: int, trigger_message_id: int) -> str:
    """Return the semantic identity shared by a logical reply and its parts."""
    return hashlib.sha256(
        f"{conversation_id}:{trigger_message_id}".encode("utf-8")
    ).hexdigest()


def reply_part_hash(*, batch_hash: str, part_index: int, part_count: int) -> str:
    """Return the sender-compatible identity for one immutable reply part."""
    if part_count == 1:
        return batch_hash
    return hashlib.sha256(
        f"{batch_hash}:{part_index}:{part_count}".encode("utf-8")
    ).hexdigest()


def _parse_json_object(content: object) -> dict[str, object] | None:
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    candidates = [stripped]
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1 and stripped.endswith("```"):
            opener = stripped[3:first_newline].strip().lower()
            if opener in {"", "json"}:
                candidates.append(stripped[first_newline + 1 : -3].strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _validated_parts(reply: str, proposal: object) -> tuple[str, ...] | None:
    if not isinstance(proposal, dict) or set(proposal) != {"parts"}:
        return None
    raw_parts = proposal["parts"]
    if not isinstance(raw_parts, list) or not 1 <= len(raw_parts) <= 4:
        return None
    if not all(isinstance(part, str) for part in raw_parts):
        return None
    parts = tuple(raw_parts)
    if any(
        not part
        or part != part.strip()
        or len(part) > 1000
        for part in parts
    ):
        return None

    cursor = 0
    for index, part in enumerate(parts):
        if index > 0:
            while cursor < len(reply) and reply[cursor].isspace():
                cursor += 1
        if not reply.startswith(part, cursor):
            return None
        cursor += len(part)
    if cursor != len(reply):
        return None
    return parts


def validate_reply_parts(reply: str, parts: object) -> tuple[str, ...] | None:
    """Validate an injected splitter result at the application boundary."""
    if not isinstance(parts, tuple):
        return None
    return _validated_parts(reply, {"parts": list(parts)})


class ReplySplitManifestConflictError(RuntimeError):
    """The semantic reply batch already owns a different immutable reply."""


class ReplySplitManifestStorageError(RuntimeError):
    """The immutable reply manifest cannot be safely read or persisted."""


class HermesReplySplitter:
    """Ask a small Hermes-routed model for boundaries and cache the result."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        provider: str,
        model_name: str,
        result_dir: Path,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._provider = provider
        self._model_name = model_name
        self._result_dir = result_dir
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    async def split(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        reply: str,
    ) -> tuple[str, ...]:
        try:
            return await self._split_locked(
                conversation_id=conversation_id,
                trigger_message_id=trigger_message_id,
                reply=reply,
            )
        except (ReplySplitManifestConflictError, ReplySplitManifestStorageError):
            raise
        except Exception as exc:
            raise ReplySplitManifestStorageError(
                "reply_split_manifest_storage_error"
            ) from exc

    async def load_existing(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        reply: str,
    ) -> tuple[str, ...] | None:
        """Load a semantic batch without creating or re-splitting it."""
        try:
            os.lstat(self._result_dir)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ReplySplitManifestStorageError(
                "reply_split_manifest_unreadable"
            ) from exc

        digest = reply_batch_hash(
            conversation_id=conversation_id,
            trigger_message_id=trigger_message_id,
        )
        reply_hash = hashlib.sha256(reply.encode("utf-8")).hexdigest()
        try:
            self._ensure_private_result_dir()
            lock_fd = os.open(
                self._result_dir / f".{digest}.processing.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            lock_stat = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.geteuid()
            ):
                os.close(lock_fd)
                raise ReplySplitManifestStorageError(
                    "reply_split_lock_not_private"
                )
            os.fchmod(lock_fd, 0o600)
            try:
                await self._acquire_lock(lock_fd)
                cache_exists, cached = self._read_claimed_result(
                    digest=digest,
                    reply_hash=reply_hash,
                    reply=reply,
                )
                return cached if cache_exists else None
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
        except (ReplySplitManifestConflictError, ReplySplitManifestStorageError):
            raise
        except Exception as exc:
            raise ReplySplitManifestStorageError(
                "reply_split_manifest_storage_error"
            ) from exc

    async def persist_parts(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        reply: str,
        parts: tuple[str, ...],
        failure: str | None = None,
    ) -> tuple[str, ...]:
        """Atomically materialize validated parts before any outbound effect."""
        validated = validate_reply_parts(reply, parts)
        if validated is None:
            raise ValueError("invalid_reply_parts")
        digest = reply_batch_hash(
            conversation_id=conversation_id,
            trigger_message_id=trigger_message_id,
        )
        reply_hash = hashlib.sha256(reply.encode("utf-8")).hexdigest()
        try:
            self._ensure_private_result_dir()
            lock_fd = os.open(
                self._result_dir / f".{digest}.processing.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            lock_stat = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.geteuid()
            ):
                os.close(lock_fd)
                raise ReplySplitManifestStorageError(
                    "reply_split_lock_not_private"
                )
            os.fchmod(lock_fd, 0o600)
            try:
                await self._acquire_lock(lock_fd)
                cache_exists, cached = self._read_claimed_result(
                    digest=digest,
                    reply_hash=reply_hash,
                    reply=reply,
                )
                if cache_exists:
                    return cached
                status = "completed" if len(validated) > 1 else "fallback"
                self._persist(
                    digest=digest,
                    result=self._manifest(
                        batch_hash=digest,
                        reply_hash=reply_hash,
                        status=status,
                        parts=validated,
                        reason=failure,
                    ),
                )
                return validated
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
        except (ReplySplitManifestConflictError, ReplySplitManifestStorageError):
            raise
        except Exception as exc:
            raise ReplySplitManifestStorageError(
                "reply_split_manifest_storage_error"
            ) from exc

    async def _split_locked(
        self,
        *,
        conversation_id: int,
        trigger_message_id: int,
        reply: str,
    ) -> tuple[str, ...]:
        digest = reply_batch_hash(
            conversation_id=conversation_id,
            trigger_message_id=trigger_message_id,
        )
        reply_hash = hashlib.sha256(reply.encode("utf-8")).hexdigest()
        self._ensure_private_result_dir()
        lock_path = self._result_dir / f".{digest}.processing.lock"
        lock_fd = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        lock_stat = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.geteuid()
        ):
            os.close(lock_fd)
            raise RuntimeError("reply_split_lock_not_private")
        os.fchmod(lock_fd, 0o600)
        try:
            await self._acquire_lock(lock_fd)
            cache_exists, cached = self._read_claimed_result(
                digest=digest,
                reply_hash=reply_hash,
                reply=reply,
            )
            if cache_exists:
                return cached
            return await self._request_and_persist(
                digest=digest,
                reply_hash=reply_hash,
                reply=reply,
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _ensure_private_result_dir(self) -> None:
        self._result_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory_fd = os.open(
            self._result_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            directory_stat = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_uid != os.geteuid()
            ):
                raise RuntimeError("reply_split_dir_not_private")
            os.fchmod(directory_fd, 0o700)
        finally:
            os.close(directory_fd)

    @staticmethod
    async def _acquire_lock(lock_fd: int) -> None:
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                await asyncio.sleep(0.01)

    async def _request_and_persist(
        self,
        *,
        digest: str,
        reply_hash: str,
        reply: str,
    ) -> tuple[str, ...]:
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Idempotency-Key": digest,
                    },
                    json={
                        "model": self._model_name,
                        "provider": self._provider,
                        "stream": False,
                        "messages": [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {"reply": reply, "max_parts": 4},
                                    ensure_ascii=False,
                                ),
                            },
                        ],
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError:
            self._persist(
                digest=digest,
                result=self._manifest(
                    status="fallback",
                    batch_hash=digest,
                    reply_hash=reply_hash,
                    parts=(reply,),
                    reason="splitter_unavailable",
                ),
            )
            return (reply,)

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (
            KeyError,
            IndexError,
            TypeError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            parts = None
        else:
            parts = _validated_parts(reply, _parse_json_object(content))
        if parts is None:
            self._persist(
                digest=digest,
                result=self._manifest(
                    status="fallback",
                    batch_hash=digest,
                    reply_hash=reply_hash,
                    parts=(reply,),
                    reason="invalid_splitter_output",
                ),
            )
            return (reply,)

        self._persist(
            digest=digest,
            result=self._manifest(
                status="completed",
                batch_hash=digest,
                reply_hash=reply_hash,
                parts=parts,
            ),
        )
        return parts

    @staticmethod
    def _manifest(
        *,
        status: str,
        batch_hash: str,
        reply_hash: str,
        parts: tuple[str, ...],
        reason: str | None = None,
    ) -> dict[str, object]:
        part_count = len(parts)
        manifest: dict[str, object] = {
            "manifest_version": 1,
            "status": status,
            "batch_hash": batch_hash,
            "reply_hash": reply_hash,
            "part_count": part_count,
            "parts": [
                {
                    "index": index,
                    "content": content,
                    "content_hash": hashlib.sha256(
                        content.encode("utf-8")
                    ).hexdigest(),
                    "part_hash": reply_part_hash(
                        batch_hash=batch_hash,
                        part_index=index,
                        part_count=part_count,
                    ),
                }
                for index, content in enumerate(parts, start=1)
            ],
        }
        if reason is not None:
            manifest["reason"] = reason
        return manifest

    def _read_result(
        self,
        *,
        digest: str,
        reply_hash: str,
        reply: str,
    ) -> tuple[bool, tuple[str, ...]]:
        result_path = self._result_dir / f"{digest}.json"
        try:
            result_fd = os.open(result_path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False, (reply,)
        except OSError as exc:
            raise ReplySplitManifestStorageError(
                "reply_split_manifest_unreadable"
            ) from exc
        try:
            result_stat = os.fstat(result_fd)
            if (
                not stat.S_ISREG(result_stat.st_mode)
                or result_stat.st_uid != os.geteuid()
                or result_stat.st_mode & 0o077
            ):
                raise ReplySplitManifestStorageError(
                    "reply_split_manifest_not_private"
                )
            with os.fdopen(result_fd, "r", encoding="utf-8") as handle:
                result_fd = -1
                result = json.load(handle)
        except ReplySplitManifestStorageError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReplySplitManifestStorageError(
                "reply_split_manifest_invalid"
            ) from exc
        finally:
            if result_fd >= 0:
                os.close(result_fd)
        if (
            not isinstance(result, dict)
            or result.get("manifest_version") != 1
            or result.get("batch_hash") != digest
        ):
            raise ReplySplitManifestStorageError("reply_split_manifest_invalid")
        if result.get("reply_hash") != reply_hash:
            raise ReplySplitManifestConflictError("reply_split_manifest_conflict")
        status_value = result.get("status")
        if status_value not in {"completed", "fallback"}:
            raise ReplySplitManifestStorageError("reply_split_manifest_invalid")
        raw_parts = result.get("parts")
        part_count = result.get("part_count")
        if (
            not isinstance(raw_parts, list)
            or not isinstance(part_count, int)
            or isinstance(part_count, bool)
            or part_count != len(raw_parts)
        ):
            raise ReplySplitManifestStorageError("reply_split_manifest_invalid")
        contents: list[object] = []
        for expected_index, raw_part in enumerate(raw_parts, start=1):
            if not isinstance(raw_part, dict):
                raise ReplySplitManifestStorageError("reply_split_manifest_invalid")
            content = raw_part.get("content")
            if (
                not isinstance(content, str)
                or raw_part.get("index") != expected_index
                or raw_part.get("content_hash")
                != hashlib.sha256(content.encode("utf-8")).hexdigest()
                or raw_part.get("part_hash")
                != reply_part_hash(
                    batch_hash=digest,
                    part_index=expected_index,
                    part_count=part_count,
                )
            ):
                raise ReplySplitManifestStorageError("reply_split_manifest_invalid")
            contents.append(content)
        parts = _validated_parts(reply, {"parts": contents})
        if status_value == "fallback" and (
            parts != (reply,) or not isinstance(result.get("reason"), str)
        ):
            raise ReplySplitManifestStorageError("reply_split_manifest_invalid")
        if parts is None:
            raise ReplySplitManifestStorageError("reply_split_manifest_invalid")
        return True, parts

    def _read_claimed_result(
        self,
        *,
        digest: str,
        reply_hash: str,
        reply: str,
    ) -> tuple[bool, tuple[str, ...]]:
        cache_exists, cached = self._read_result(
            digest=digest,
            reply_hash=reply_hash,
            reply=reply,
        )
        claim_exists = self._batch_claim_exists(digest)
        if cache_exists:
            if not claim_exists:
                self._ensure_batch_claim(digest)
            return True, cached
        if claim_exists:
            raise ReplySplitManifestStorageError(
                "reply_split_manifest_missing_after_claim"
            )
        return False, cached

    def _open_claim_dir(self) -> int:
        try:
            claim_dir_fd = os.open(
                self._result_dir.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise ReplySplitManifestStorageError(
                "reply_split_claim_dir_unreadable"
            ) from exc
        claim_dir_stat = os.fstat(claim_dir_fd)
        if (
            not stat.S_ISDIR(claim_dir_stat.st_mode)
            or claim_dir_stat.st_uid != os.geteuid()
        ):
            os.close(claim_dir_fd)
            raise ReplySplitManifestStorageError("reply_split_claim_dir_not_private")
        os.fchmod(claim_dir_fd, 0o700)
        return claim_dir_fd

    def _batch_claim_exists(self, digest: str) -> bool:
        claim_dir_fd = self._open_claim_dir()
        claim_name = f".{digest}.reply-batch.claim"
        try:
            try:
                claim_fd = os.open(
                    claim_name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=claim_dir_fd,
                )
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise ReplySplitManifestStorageError(
                    "reply_split_batch_claim_unreadable"
                ) from exc
            try:
                claim_stat = os.fstat(claim_fd)
                if (
                    not stat.S_ISREG(claim_stat.st_mode)
                    or claim_stat.st_uid != os.geteuid()
                    or claim_stat.st_mode & 0o077
                    or os.read(claim_fd, 9) != b"claimed\n"
                    or os.read(claim_fd, 1) != b""
                ):
                    raise ReplySplitManifestStorageError(
                        "reply_split_batch_claim_invalid"
                    )
            finally:
                os.close(claim_fd)
            return True
        finally:
            os.close(claim_dir_fd)

    def _ensure_batch_claim(self, digest: str) -> None:
        if self._batch_claim_exists(digest):
            return
        claim_dir_fd = self._open_claim_dir()
        claim_name = f".{digest}.reply-batch.claim"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            try:
                claim_fd = os.open(
                    claim_name,
                    flags,
                    0o600,
                    dir_fd=claim_dir_fd,
                )
            except FileExistsError:
                if not self._batch_claim_exists(digest):
                    raise ReplySplitManifestStorageError(
                        "reply_split_batch_claim_missing"
                    )
                return
            except OSError as exc:
                raise ReplySplitManifestStorageError(
                    "reply_split_batch_claim_unwritable"
                ) from exc
            try:
                claim_stat = os.fstat(claim_fd)
                if (
                    not stat.S_ISREG(claim_stat.st_mode)
                    or claim_stat.st_uid != os.geteuid()
                ):
                    raise ReplySplitManifestStorageError(
                        "reply_split_batch_claim_invalid"
                    )
                os.fchmod(claim_fd, 0o600)
                if os.write(claim_fd, b"claimed\n") != len(b"claimed\n"):
                    raise OSError("short_reply_split_batch_claim_write")
                os.fsync(claim_fd)
            finally:
                os.close(claim_fd)
            os.fsync(claim_dir_fd)
        finally:
            os.close(claim_dir_fd)

    def _persist(self, *, digest: str, result: dict[str, object]) -> None:
        self._ensure_batch_claim(digest)
        result_path = self._result_dir / f"{digest}.json"
        serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.",
            suffix=".tmp",
            dir=self._result_dir,
        )
        try:
            os.fchmod(temporary_fd, 0o600)
            with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, result_path)
            directory_fd = os.open(
                self._result_dir,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
