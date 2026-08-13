"""Durable fixture-backed batches for the daily owner feedback cycle."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
import fcntl
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

from fastapi import FastAPI, Header, HTTPException, Response


@dataclass(frozen=True)
class FeedbackFixture:
    fixture_id: str
    canonical_conversation_ref: str
    release_id: str
    release_version: int
    context_summary: str
    apparent_objective: str
    observed_outcome: str


@dataclass(frozen=True)
class FeedbackFixtureSet:
    fixture_set_id: str
    sanitized: bool
    fixtures: tuple[FeedbackFixture, ...]


@dataclass(frozen=True)
class ReviewItem:
    fixture_id: str
    position: int
    snapshot_id: str


@dataclass(frozen=True)
class ReviewBatch:
    batch_id: str
    status: str
    item_count: int
    revision: int
    items: tuple[ReviewItem, ...]


@dataclass(frozen=True)
class CreateBatchResult:
    status: str
    batch: ReviewBatch


@dataclass(frozen=True)
class FixtureOperatorGrant:
    token: str
    tenant_id: str
    scope_id: str
    reviewer_id: str
    reviewer_binding_id: str
    fixture_set_ids: frozenset[str]
    active: bool


class IdempotencyConflictError(RuntimeError):
    """A command ID was reused with different semantic inputs."""


class LogicalBatchConflictError(RuntimeError):
    """A logical review window was reused with different durable inputs."""


class FixtureNotSanitizedError(RuntimeError):
    """The controlled cut only accepts explicitly sanitized fixture sets."""


class InvalidBatchInputError(ValueError):
    """The batch inputs cannot describe one valid immutable review window."""


class StorageConflictError(RuntimeError):
    """An immutable path already contains different content."""


class DailyFeedbackBatchStore:
    """Materialize immutable, sanitized fixture batches on private storage."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._commands_dir = root / "commands"
        self._batches_dir = root / "batches"
        self._logical_dir = root / "logical"
        self._snapshots_dir = root / "snapshots"
        self._manifests_dir = root / "manifests"
        self._commits_dir = root / "commits"
        self._ensure_private_directory(self._root)
        self._ensure_private_directory(self._commands_dir)
        self._ensure_private_directory(self._batches_dir)
        self._ensure_private_directory(self._logical_dir)
        self._ensure_private_directory(self._snapshots_dir)
        self._ensure_private_directory(self._manifests_dir)
        self._ensure_private_directory(self._commits_dir)

    def _ensure_private_directory(self, directory: Path) -> None:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory_fd = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            directory_stat = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or directory_stat.st_uid != os.geteuid()
            ):
                raise RuntimeError("daily_feedback_store_not_private")
            os.fchmod(directory_fd, 0o700)
        finally:
            os.close(directory_fd)

    def create_review_batch(
        self,
        *,
        command_id: str,
        tenant_id: str,
        scope_id: str,
        window_start: datetime,
        window_end: datetime,
        selection_contract_version: str,
        selection_config_fingerprint: str,
        reviewer_id: str,
        reviewer_binding_id: str,
        fixture_set: FeedbackFixtureSet,
    ) -> CreateBatchResult:
        if fixture_set.sanitized is not True:
            raise FixtureNotSanitizedError("fixture_not_sanitized")
        if (
            window_start.tzinfo is None
            or window_end.tzinfo is None
            or window_start.utcoffset() is None
            or window_end.utcoffset() is None
            or window_start.utcoffset() != UTC.utcoffset(window_start)
            or window_end.utcoffset() != UTC.utcoffset(window_end)
            or window_start >= window_end
        ):
            raise InvalidBatchInputError("invalid_review_window")
        fixture_ids = [fixture.fixture_id for fixture in fixture_set.fixtures]
        if len(fixture_ids) != len(set(fixture_ids)):
            raise InvalidBatchInputError("duplicate_fixture_identity")
        conversation_refs = [
            fixture.canonical_conversation_ref for fixture in fixture_set.fixtures
        ]
        if len(conversation_refs) != len(set(conversation_refs)):
            raise InvalidBatchInputError("duplicate_canonical_conversation")
        logical_inputs = {
            "tenant_id": tenant_id,
            "scope_id": scope_id,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "selection_contract_version": selection_contract_version,
            "selection_config_fingerprint": selection_config_fingerprint,
        }
        inputs = {
            **logical_inputs,
            "reviewer_id": reviewer_id,
            "reviewer_binding_id": reviewer_binding_id,
            "fixture_set_id": fixture_set.fixture_set_id,
            "sanitized": fixture_set.sanitized,
            "fixtures": [
                {
                    "fixture_id": fixture.fixture_id,
                    "canonical_conversation_ref": fixture.canonical_conversation_ref,
                    "release_id": fixture.release_id,
                    "release_version": fixture.release_version,
                    "context_summary": fixture.context_summary,
                    "apparent_objective": fixture.apparent_objective,
                    "observed_outcome": fixture.observed_outcome,
                }
                for fixture in fixture_set.fixtures
            ],
        }
        fingerprint = _hash_json(inputs)
        command_fingerprint = _hash_json(
            {"command_type": "create_review_batch", "payload": inputs}
        )
        command_path = self._commands_dir / f"{_hash_text(command_id)}.json"
        logical_key = _hash_json(logical_inputs)
        logical_path = self._logical_dir / f"{logical_key}.json"
        lock_path = self._root / ".create-batch.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return self._create_review_batch_locked(
                command_id=command_id,
                command_path=command_path,
                command_fingerprint=command_fingerprint,
                logical_path=logical_path,
                logical_key=logical_key,
                fingerprint=fingerprint,
                fixture_set=fixture_set,
            )
        finally:
            os.close(lock_fd)

    def _create_review_batch_locked(
        self,
        *,
        command_id: str,
        command_path: Path,
        command_fingerprint: str,
        logical_path: Path,
        logical_key: str,
        fingerprint: str,
        fixture_set: FeedbackFixtureSet,
    ) -> CreateBatchResult:
        if command_path.exists():
            command = self._read_json(command_path)
            if command.get("fingerprint") != command_fingerprint:
                raise IdempotencyConflictError("idempotency_conflict")
            manifest_id = command.get("manifest_id")
            if not isinstance(manifest_id, str):
                raise StorageConflictError("daily_feedback_integrity_error")
            manifest = self._read_json(self._manifests_dir / f"{manifest_id}.json")
            if (self._commits_dir / f"{manifest_id}.json").exists():
                return CreateBatchResult(
                    status="replayed", batch=self._load_committed_batch(manifest)
                )
            if manifest.get("command_id") != command_id:
                raise StorageConflictError("daily_feedback_integrity_error")
        batch_id = f"batch_{fingerprint}"
        items = tuple(
            ReviewItem(
                fixture_id=fixture.fixture_id,
                position=position,
                snapshot_id=f"snapshot_{_hash_json({'batch_id': batch_id, 'position': position, 'fixture': fixture.fixture_id})}",
            )
            for position, fixture in enumerate(fixture_set.fixtures, start=1)
        )
        batch = ReviewBatch(
            batch_id=batch_id,
            status="ready" if items else "completed_empty",
            item_count=len(items),
            revision=1,
            items=items,
        )
        snapshots = [
            {
                "path": f"snapshots/{item.snapshot_id}.json",
                "envelope": {
                    "schema_version": 1,
                    "snapshot_id": item.snapshot_id,
                    "fixture_id": fixture.fixture_id,
                    "canonical_conversation_ref": fixture.canonical_conversation_ref,
                    "context_summary": fixture.context_summary,
                    "apparent_objective": fixture.apparent_objective,
                    "observed_outcome": fixture.observed_outcome,
                    "release": {
                        "id": fixture.release_id,
                        "version": fixture.release_version,
                    },
                    "sanitized": True,
                },
            }
            for item, fixture in zip(items, fixture_set.fixtures, strict=True)
        ]
        batch_envelope = {
                "schema_version": 1,
                "batch_id": batch.batch_id,
                "status": batch.status,
                "revision": batch.revision,
                "items": [
                    {
                        "fixture_id": item.fixture_id,
                        "position": item.position,
                        "snapshot_id": item.snapshot_id,
                    }
                    for item in items
                ],
            }
        logical_envelope = {
                "schema_version": 1,
                "fingerprint": fingerprint,
                "batch_id": batch_id,
                "manifest_id": logical_key,
            }
        command_envelope = {
            "schema_version": 1,
            "command_id": command_id,
            "fingerprint": command_fingerprint,
            "batch_id": batch_id,
            "manifest_id": logical_key,
        }
        manifest_path = self._manifests_dir / f"{logical_key}.json"
        proposed_manifest = {
            "schema_version": 1,
            "manifest_id": logical_key,
            "command_id": command_id,
            "command_fingerprint": command_fingerprint,
            "fingerprint": fingerprint,
            "batch_id": batch_id,
            "artifacts": [
                *[
                    {"path": snapshot["path"], "hash": _hash_json(snapshot["envelope"])}
                    for snapshot in snapshots
                ],
                {"path": f"batches/{batch_id}.json", "hash": _hash_json(batch_envelope)},
                {"path": f"logical/{logical_path.name}", "hash": _hash_json(logical_envelope)},
                {"path": f"commands/{command_path.name}", "hash": _hash_json(command_envelope)},
            ],
        }
        if manifest_path.exists():
            manifest = self._read_json(manifest_path)
            if manifest.get("command_id") == command_id and manifest.get(
                "command_fingerprint"
            ) != command_fingerprint:
                raise IdempotencyConflictError("idempotency_conflict")
            if manifest.get("fingerprint") != fingerprint:
                raise LogicalBatchConflictError("logical_batch_conflict")
            if manifest.get("command_id") != command_id:
                loaded = self._load_committed_batch(manifest)
                self._record_command(
                    command_path=command_path,
                    command_id=command_id,
                    fingerprint=command_fingerprint,
                    batch_id=loaded.batch_id,
                    manifest_id=logical_key,
                )
                return CreateBatchResult(status="replayed", batch=loaded)
            if manifest != proposed_manifest:
                raise StorageConflictError("daily_feedback_integrity_error")
            intent_existed = True
            commit_path = self._commits_dir / f"{logical_key}.json"
            if commit_path.exists():
                return CreateBatchResult(
                    status="replayed", batch=self._load_committed_batch(manifest)
                )
        else:
            self._write_once(manifest_path, proposed_manifest)
            manifest = proposed_manifest
            intent_existed = False

        artifact_values = [
            *[(snapshot["path"], snapshot["envelope"]) for snapshot in snapshots],
            (f"batches/{batch_id}.json", batch_envelope),
            (f"logical/{logical_path.name}", logical_envelope),
            (f"commands/{command_path.name}", command_envelope),
        ]
        for relative_path, envelope in artifact_values:
            self._write_once(self._root / str(relative_path), envelope)  # type: ignore[arg-type]
        commit_envelope = {
            "schema_version": 1,
            "manifest_id": logical_key,
            "manifest_hash": _hash_json(manifest),
        }
        self._write_once(self._commits_dir / f"{logical_key}.json", commit_envelope)
        loaded = self._load_committed_batch(manifest)
        return CreateBatchResult(
            status="replayed" if intent_existed else "applied", batch=loaded
        )

    def _record_command(
        self,
        *,
        command_path: Path,
        command_id: str,
        fingerprint: str,
        batch_id: str,
        manifest_id: str,
    ) -> None:
        self._write_once(
            command_path,
            {
                "schema_version": 1,
                "command_id": command_id,
                "fingerprint": fingerprint,
                "batch_id": batch_id,
                "manifest_id": manifest_id,
            },
        )

    def _load_batch(self, batch_id: str) -> ReviewBatch:
        envelope = self._read_json(self._batches_dir / f"{batch_id}.json")
        items_value = envelope.get("items")
        if not isinstance(items_value, list):
            raise RuntimeError("daily_feedback_batch_invalid")
        items = tuple(
            ReviewItem(
                fixture_id=str(item["fixture_id"]),
                position=int(item["position"]),
                snapshot_id=str(item["snapshot_id"]),
            )
            for item in items_value
            if isinstance(item, dict)
        )
        if len(items) != len(items_value):
            raise RuntimeError("daily_feedback_batch_invalid")
        return ReviewBatch(
            batch_id=batch_id,
            status=str(envelope["status"]),
            item_count=len(items),
            revision=int(envelope["revision"]),
            items=items,
        )

    def _load_committed_batch(self, manifest: dict[str, object]) -> ReviewBatch:
        manifest_id = manifest.get("manifest_id")
        batch_id = manifest.get("batch_id")
        artifacts = manifest.get("artifacts")
        if (
            not isinstance(manifest_id, str)
            or not isinstance(batch_id, str)
            or not isinstance(artifacts, list)
        ):
            raise StorageConflictError("daily_feedback_integrity_error")
        commit_path = self._commits_dir / f"{manifest_id}.json"
        if not commit_path.exists():
            raise StorageConflictError("daily_feedback_integrity_error")
        commit = self._read_json(commit_path)
        if commit != {
            "schema_version": 1,
            "manifest_id": manifest_id,
            "manifest_hash": _hash_json(manifest),
        }:
            raise StorageConflictError("daily_feedback_integrity_error")
        expected_paths: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise StorageConflictError("daily_feedback_integrity_error")
            relative_path = artifact.get("path")
            expected_hash = artifact.get("hash")
            if (
                not isinstance(relative_path, str)
                or not isinstance(expected_hash, str)
                or relative_path in expected_paths
                or relative_path.startswith("/")
                or ".." in Path(relative_path).parts
            ):
                raise StorageConflictError("daily_feedback_integrity_error")
            expected_paths.add(relative_path)
            path = self._root / relative_path
            if not path.exists() or _hash_json(self._read_json(path)) != expected_hash:
                raise StorageConflictError("daily_feedback_integrity_error")
        batch_path = f"batches/{batch_id}.json"
        snapshot_paths = {
            path for path in expected_paths if path.startswith("snapshots/")
        }
        if batch_path not in expected_paths:
            raise StorageConflictError("daily_feedback_integrity_error")
        batch = self._load_batch(batch_id)
        if [item.position for item in batch.items] != list(
            range(1, len(batch.items) + 1)
        ):
            raise StorageConflictError("daily_feedback_integrity_error")
        if {
            f"snapshots/{item.snapshot_id}.json" for item in batch.items
        } != snapshot_paths:
            raise StorageConflictError("daily_feedback_integrity_error")
        expected_status = "ready" if batch.items else "completed_empty"
        if batch.status != expected_status or batch.revision != 1:
            raise StorageConflictError("daily_feedback_integrity_error")
        return batch

    def _read_json(self, path: Path) -> dict[str, object]:
        file_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            file_stat = os.fstat(file_fd)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_uid != os.geteuid()
                or stat.S_IMODE(file_stat.st_mode) != 0o600
            ):
                raise RuntimeError("daily_feedback_record_not_private")
            with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
                file_fd = -1
                value = json.load(handle)
        finally:
            if file_fd >= 0:
                os.close(file_fd)
        if not isinstance(value, dict):
            raise RuntimeError("daily_feedback_record_invalid")
        return value

    def _write_once(self, path: Path, envelope: dict[str, object]) -> None:
        serialized = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        temporary_fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
        )
        try:
            os.fchmod(temporary_fd, 0o600)
            with os.fdopen(temporary_fd, "w", encoding="utf-8") as handle:
                temporary_fd = -1
                handle.write(serialized)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_name, path)
            except FileExistsError:
                existing = self._read_json(path)
                if existing != envelope:
                    raise StorageConflictError("daily_feedback_storage_conflict")
                return
            directory_fd = os.open(
                path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            os.unlink(temporary_name)


def _hash_json(value: object) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_daily_feedback_fixture_app(
    *,
    store: DailyFeedbackBatchStore,
    operator_grant: FixtureOperatorGrant,
    fixture_sets: Mapping[str, FeedbackFixtureSet],
) -> FastAPI:
    """Create the fixture-only internal HTTP boundary for Cut A verification."""
    if not operator_grant.token:
        raise ValueError("daily_feedback_operator_token_required")
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/daily-feedback/fixture-batches")
    async def create_fixture_batch(
        payload: dict[str, object],
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        expected = f"Bearer {operator_grant.token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid_operator_token")
        if not operator_grant.active:
            raise HTTPException(status_code=403, detail="operator_binding_inactive")
        required_strings = (
            "command_id",
            "tenant_id",
            "scope_id",
            "window_start",
            "window_end",
            "selection_contract_version",
            "selection_config_fingerprint",
            "reviewer_id",
            "reviewer_binding_id",
            "fixture_set_id",
        )
        if set(payload) != set(required_strings) or any(
            not isinstance(payload.get(field), str) or not payload[field]
            for field in required_strings
        ):
            raise HTTPException(status_code=422, detail="invalid_fixture_batch_payload")
        authorized_dimensions = {
            "tenant_id": operator_grant.tenant_id,
            "scope_id": operator_grant.scope_id,
            "reviewer_id": operator_grant.reviewer_id,
            "reviewer_binding_id": operator_grant.reviewer_binding_id,
        }
        if any(
            payload.get(field) != expected_value
            for field, expected_value in authorized_dimensions.items()
        ) or payload.get("fixture_set_id") not in operator_grant.fixture_set_ids:
            raise HTTPException(status_code=403, detail="operator_scope_denied")
        fixture_set = fixture_sets.get(str(payload["fixture_set_id"]))
        if fixture_set is None:
            raise HTTPException(status_code=422, detail="fixture_set_unknown")
        try:
            result = store.create_review_batch(
                command_id=str(payload["command_id"]),
                tenant_id=str(payload["tenant_id"]),
                scope_id=str(payload["scope_id"]),
                window_start=_parse_utc_timestamp(str(payload["window_start"])),
                window_end=_parse_utc_timestamp(str(payload["window_end"])),
                selection_contract_version=str(
                    payload["selection_contract_version"]
                ),
                selection_config_fingerprint=str(
                    payload["selection_config_fingerprint"]
                ),
                reviewer_id=str(payload["reviewer_id"]),
                reviewer_binding_id=str(payload["reviewer_binding_id"]),
                fixture_set=fixture_set,
            )
        except InvalidBatchInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FixtureNotSanitizedError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except LogicalBatchConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        response.status_code = 201 if result.status == "applied" else 200
        return {
            "status": result.status,
            "batch": {
                "batch_id": result.batch.batch_id,
                "status": result.batch.status,
                "item_count": result.batch.item_count,
                "revision": result.batch.revision,
                "items": [
                    {
                        "fixture_id": item.fixture_id,
                        "position": item.position,
                        "snapshot_id": item.snapshot_id,
                    }
                    for item in result.batch.items
                ],
            },
        }

    return app


def _parse_utc_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidBatchInputError("invalid_review_window") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidBatchInputError("invalid_review_window")
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise InvalidBatchInputError("invalid_review_window")
    return parsed
