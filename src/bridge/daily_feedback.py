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
from datetime import UTC, datetime, timedelta
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


@dataclass(frozen=True)
class FixtureReviewerGrant:
    token: str
    reviewer_id: str
    reviewer_binding_id: str
    session_owner: str
    active: bool


@dataclass(frozen=True)
class ReviewPrincipal:
    reviewer_id: str
    reviewer_binding_id: str
    session_owner: str
    active: bool


@dataclass(frozen=True)
class SessionClaimResult:
    status: str
    batch_id: str
    batch_status: str
    batch_revision: int
    session_owner: str
    session_fence: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class NextReviewItem:
    fixture_id: str
    position: int
    total: int
    status: str
    item_revision: int
    snapshot_id: str
    context_summary: str
    apparent_objective: str
    observed_outcome: str
    release_id: str
    release_version: int
    payload_hash: str


@dataclass(frozen=True)
class WorkerPrincipal:
    worker_owner: str
    worker_lease_generation: int
    active: bool


@dataclass(frozen=True)
class WorkerLeaseGrant:
    worker_owner: str
    worker_lease_generation: int
    lease_expires_at: datetime
    active: bool


@dataclass(frozen=True)
class ReviewDeliveryResult:
    status: str
    delivery_attempt_id: str
    semantic_delivery_key: str
    phase: str
    outcome: str | None
    item_status: str
    batch_status: str
    batch_revision: int


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


class SessionClaimConflictError(RuntimeError):
    """A review session cannot be claimed under the observed authority."""


class ReviewAuthorizationError(RuntimeError):
    """A session principal no longer owns the active review fence."""


class ReviewDeliveryConflictError(RuntimeError):
    """A simulated review delivery violates its durable authority or phase."""


class DailyFeedbackBatchStore:
    """Materialize immutable, sanitized fixture batches on private storage."""

    def __init__(
        self,
        root: Path,
        *,
        worker_grants: Mapping[str, WorkerLeaseGrant] | None = None,
    ) -> None:
        self._root = root
        self._worker_grants = dict(worker_grants or {})
        self._commands_dir = root / "commands"
        self._batches_dir = root / "batches"
        self._logical_dir = root / "logical"
        self._snapshots_dir = root / "snapshots"
        self._manifests_dir = root / "manifests"
        self._commits_dir = root / "commits"
        self._runtime_dir = root / "runtime"
        self._runtime_commands_dir = root / "runtime_commands"
        self._ensure_private_directory(self._root)
        self._ensure_private_directory(self._commands_dir)
        self._ensure_private_directory(self._batches_dir)
        self._ensure_private_directory(self._logical_dir)
        self._ensure_private_directory(self._snapshots_dir)
        self._ensure_private_directory(self._manifests_dir)
        self._ensure_private_directory(self._commits_dir)
        self._ensure_private_directory(self._runtime_dir)
        self._ensure_private_directory(self._runtime_commands_dir)

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
        lock_path = self._root / ".command.lock"
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
                reviewer_id=reviewer_id,
                reviewer_binding_id=reviewer_binding_id,
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
        reviewer_id: str,
        reviewer_binding_id: str,
    ) -> CreateBatchResult:
        if self._runtime_command_exists(command_id):
            raise IdempotencyConflictError("idempotency_conflict")
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
                "reviewer_id": reviewer_id,
                "reviewer_binding_id": reviewer_binding_id,
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

    def claim_review_session(
        self,
        *,
        command_id: str,
        batch_id: str,
        principal: ReviewPrincipal,
        expected_batch_revision: int,
        lease_seconds: int,
        now: datetime,
    ) -> SessionClaimResult:
        if principal.active is not True:
            raise SessionClaimConflictError("reviewer_binding_inactive")
        if (
            now.tzinfo is None
            or now.utcoffset() != UTC.utcoffset(now)
            or lease_seconds < 1
            or lease_seconds > 300
        ):
            raise SessionClaimConflictError("invalid_session_claim")
        manifest = self._find_committed_manifest(batch_id)
        batch = self._load_committed_batch(manifest)
        batch_envelope = self._read_json(self._batches_dir / f"{batch_id}.json")
        if (
            batch_envelope.get("reviewer_id") != principal.reviewer_id
            or batch_envelope.get("reviewer_binding_id")
            != principal.reviewer_binding_id
        ):
            raise SessionClaimConflictError("reviewer_authority_mismatch")
        command_fingerprint = _hash_json(
            {
                "command_type": "claim_review_session",
                "batch_id": batch_id,
                "reviewer_id": principal.reviewer_id,
                "reviewer_binding_id": principal.reviewer_binding_id,
                "session_owner": principal.session_owner,
                "expected_batch_revision": expected_batch_revision,
                "lease_seconds": lease_seconds,
            }
        )
        lock_fd = self._open_lock(self._root / ".command.lock")
        try:
            state_path = self._runtime_dir / f"{batch_id}.json"
            state = self._load_or_initialize_runtime_state(state_path, batch)
            self._validate_runtime_binding(state, batch)
            prior = self._read_global_runtime_command(
                command_id, command_fingerprint
            )
            if prior is not None:
                return self._session_claim_result(prior, status="replayed")
            if state.get("batch_status") not in {
                "ready",
                "in_review",
                "partially_completed",
            }:
                raise SessionClaimConflictError("batch_not_reviewable")
            if state.get("batch_revision") != expected_batch_revision:
                raise SessionClaimConflictError("batch_revision_stale")
            commands = state.get("commands")
            if not isinstance(commands, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            lease_expires_value = state.get("lease_expires_at")
            lease_expires = (
                datetime.fromisoformat(str(lease_expires_value))
                if lease_expires_value is not None
                else None
            )
            current_owner = state.get("session_owner")
            if (
                lease_expires is not None
                and lease_expires > now
                and current_owner != principal.session_owner
            ):
                raise SessionClaimConflictError("session_lease_active")
            fence = int(state.get("session_fence", 0)) + 1
            lease_expires_at = now + timedelta(seconds=lease_seconds)
            result = {
                "fingerprint": command_fingerprint,
                "batch_id": batch_id,
                "batch_status": str(state["batch_status"]),
                "batch_revision": int(state["batch_revision"]),
                "session_owner": principal.session_owner,
                "session_fence": fence,
                "lease_expires_at": lease_expires_at.isoformat(),
            }
            state["session_owner"] = principal.session_owner
            state["session_fence"] = fence
            state["lease_expires_at"] = lease_expires_at.isoformat()
            commands[command_id] = result
            self._write_replace(state_path, state)
            self._write_global_runtime_command(
                command_id, command_fingerprint, batch_id, result
            )
            return self._session_claim_result(result, status="applied")
        finally:
            os.close(lock_fd)

    def _find_committed_manifest(self, batch_id: str) -> dict[str, object]:
        matches: list[dict[str, object]] = []
        for path in self._manifests_dir.glob("*.json"):
            manifest = self._read_json(path)
            if manifest.get("batch_id") == batch_id:
                matches.append(manifest)
        if len(matches) != 1:
            raise StorageConflictError("daily_feedback_integrity_error")
        self._load_committed_batch(matches[0])
        return matches[0]

    def get_next_review_item(
        self,
        *,
        batch_id: str,
        principal: ReviewPrincipal,
        session_fence: int,
        now: datetime,
    ) -> NextReviewItem:
        manifest = self._find_committed_manifest(batch_id)
        batch = self._load_committed_batch(manifest)
        batch_envelope = self._read_json(self._batches_dir / f"{batch_id}.json")
        if principal.active is not True:
            raise ReviewAuthorizationError("reviewer_binding_inactive")
        if (
            batch_envelope.get("reviewer_id") != principal.reviewer_id
            or batch_envelope.get("reviewer_binding_id")
            != principal.reviewer_binding_id
        ):
            raise ReviewAuthorizationError("reviewer_authority_mismatch")
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ReviewAuthorizationError("invalid_review_time")
        state_path = self._runtime_dir / f"{batch_id}.json"
        if not state_path.exists():
            raise ReviewAuthorizationError("review_session_missing")
        state = self._read_json(state_path)
        self._validate_runtime_binding(state, batch)
        if state.get("batch_id") != batch_id:
            raise StorageConflictError("daily_feedback_runtime_invalid")
        if state.get("session_owner") != principal.session_owner:
            raise ReviewAuthorizationError("session_owner_stale")
        if state.get("session_fence") != session_fence:
            raise ReviewAuthorizationError("session_fence_stale")
        lease_value = state.get("lease_expires_at")
        if not isinstance(lease_value, str):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        if datetime.fromisoformat(lease_value) <= now:
            raise ReviewAuthorizationError("session_lease_expired")
        items = state.get("items")
        if not isinstance(items, list) or len(items) != batch.item_count:
            raise StorageConflictError("daily_feedback_runtime_invalid")
        nonterminal = [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("status")
            not in {"correct", "corrected", "skipped", "feedback_cancelled"}
        ]
        if not nonterminal:
            raise ReviewAuthorizationError("no_review_item_available")
        item = min(nonterminal, key=lambda value: int(value["position"]))
        snapshot_id = item.get("snapshot_id")
        if not isinstance(snapshot_id, str):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        snapshot = self._read_json(self._snapshots_dir / f"{snapshot_id}.json")
        if snapshot.get("sanitized") is not True:
            raise StorageConflictError("daily_feedback_integrity_error")
        release = snapshot.get("release")
        if not isinstance(release, dict):
            raise StorageConflictError("daily_feedback_integrity_error")
        return NextReviewItem(
            fixture_id=str(item["fixture_id"]),
            position=int(item["position"]),
            total=len(items),
            status=str(item["status"]),
            item_revision=int(item["revision"]),
            snapshot_id=snapshot_id,
            context_summary=str(snapshot["context_summary"]),
            apparent_objective=str(snapshot["apparent_objective"]),
            observed_outcome=str(snapshot["observed_outcome"]),
            release_id=str(release["id"]),
            release_version=int(release["version"]),
            payload_hash=f"sha256:{_hash_json(snapshot)}",
        )

    def reserve_review_delivery(
        self,
        *,
        command_id: str,
        batch_id: str,
        snapshot_id: str,
        payload_hash: str,
        reviewer: ReviewPrincipal,
        session_fence: int,
        worker: WorkerPrincipal,
        worker_lease_expires_at: datetime,
        now: datetime,
    ) -> ReviewDeliveryResult:
        command_fingerprint = _hash_json(
            {
                "command_type": "reserve_review_delivery",
                "batch_id": batch_id,
                "snapshot_id": snapshot_id,
                "payload_hash": payload_hash,
                "reviewer_id": reviewer.reviewer_id,
                "reviewer_binding_id": reviewer.reviewer_binding_id,
                "session_owner": reviewer.session_owner,
                "session_fence": session_fence,
                "worker_owner": worker.worker_owner,
                "worker_lease_generation": worker.worker_lease_generation,
                "worker_lease_expires_at": worker_lease_expires_at.isoformat(),
            }
        )
        committed_batch = self._load_committed_batch(
            self._find_committed_manifest(batch_id)
        )
        batch_envelope = self._read_json(self._batches_dir / f"{batch_id}.json")
        if (
            batch_envelope.get("reviewer_id") != reviewer.reviewer_id
            or batch_envelope.get("reviewer_binding_id")
            != reviewer.reviewer_binding_id
        ):
            raise ReviewDeliveryConflictError("reviewer_authority_mismatch")
        lock_fd = self._open_lock(self._root / ".command.lock")
        try:
            state_path = self._runtime_dir / f"{batch_id}.json"
            state = self._read_json(state_path)
            self._validate_runtime_binding(state, committed_batch)
            replay = self._replay_runtime_command(
                state,
                command_id,
                command_fingerprint,
                batch_id=batch_id,
            )
            if replay is not None:
                return replay
            self._validate_active_session(
                state, reviewer, session_fence=session_fence, now=now
            )
            self._validate_worker(
                worker, worker_lease_expires_at=worker_lease_expires_at, now=now
            )
            items = self._runtime_items(state)
            matches = [item for item in items if item.get("snapshot_id") == snapshot_id]
            if len(matches) != 1 or matches[0].get("status") != "pending":
                raise ReviewDeliveryConflictError("item_not_presentable")
            snapshot = self._read_json(self._snapshots_dir / f"{snapshot_id}.json")
            if payload_hash != f"sha256:{_hash_json(snapshot)}":
                raise ReviewDeliveryConflictError("delivery_payload_mismatch")
            semantic_delivery_key = f"delivery_{_hash_json({'batch_id': batch_id, 'snapshot_id': snapshot_id, 'binding_id': reviewer.reviewer_binding_id, 'kind': 'review_item'})}"
            attempts = state.setdefault("delivery_attempts", {})
            if not isinstance(attempts, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            if any(
                isinstance(value, dict)
                and value.get("semantic_delivery_key") == semantic_delivery_key
                for value in attempts.values()
            ):
                raise ReviewDeliveryConflictError("delivery_operation_active")
            attempt_id = f"attempt_{_hash_json({'semantic_delivery_key': semantic_delivery_key, 'number': 1})}"
            attempt: dict[str, object] = {
                "delivery_attempt_id": attempt_id,
                "semantic_delivery_key": semantic_delivery_key,
                "batch_id": batch_id,
                "snapshot_id": snapshot_id,
                "payload_hash": payload_hash,
                "reviewer_id": reviewer.reviewer_id,
                "reviewer_binding_id": reviewer.reviewer_binding_id,
                "session_owner": reviewer.session_owner,
                "session_fence": session_fence,
                "worker_owner": worker.worker_owner,
                "worker_lease_generation": worker.worker_lease_generation,
                "worker_lease_expires_at": worker_lease_expires_at.isoformat(),
                "phase": "reserved",
                "outcome": None,
                "remote_reference": None,
            }
            attempts[attempt_id] = attempt
            result = self._delivery_result_from_state(
                state, attempt, status="applied"
            )
            self._record_runtime_result(
                state, command_id, command_fingerprint, result
            )
            self._write_replace(state_path, state)
            return result
        finally:
            os.close(lock_fd)

    def mark_review_delivery_request_started(
        self,
        *,
        command_id: str,
        delivery_attempt_id: str,
        reviewer: ReviewPrincipal,
        session_fence: int,
        worker: WorkerPrincipal,
        now: datetime,
    ) -> ReviewDeliveryResult:
        command_fingerprint = _hash_json(
            {
                "command_type": "mark_review_delivery_request_started",
                "delivery_attempt_id": delivery_attempt_id,
                "reviewer_id": reviewer.reviewer_id,
                "reviewer_binding_id": reviewer.reviewer_binding_id,
                "session_owner": reviewer.session_owner,
                "session_fence": session_fence,
                "worker_owner": worker.worker_owner,
                "worker_lease_generation": worker.worker_lease_generation,
            }
        )
        batch_id, state_path = self._find_runtime_attempt(delivery_attempt_id)
        lock_fd = self._open_lock(self._root / ".command.lock")
        try:
            state = self._read_json(state_path)
            replay = self._replay_runtime_command(
                state,
                command_id,
                command_fingerprint,
                batch_id=batch_id,
            )
            if replay is not None:
                return replay
            self._validate_active_session(
                state, reviewer, session_fence=session_fence, now=now
            )
            attempt = self._runtime_attempt(state, delivery_attempt_id)
            lease_expires_at = datetime.fromisoformat(
                str(attempt["worker_lease_expires_at"])
            )
            self._validate_worker(
                worker, worker_lease_expires_at=lease_expires_at, now=now
            )
            self._validate_attempt_worker(attempt, worker)
            self._validate_attempt_reviewer(attempt, reviewer, session_fence)
            if attempt.get("phase") != "reserved":
                raise ReviewDeliveryConflictError("delivery_phase_conflict")
            attempt["phase"] = "request_started"
            result = self._delivery_result_from_state(
                state, attempt, status="applied"
            )
            self._record_runtime_result(
                state, command_id, command_fingerprint, result
            )
            self._write_replace(state_path, state)
            return result
        finally:
            os.close(lock_fd)

    def finalize_review_delivery(
        self,
        *,
        command_id: str,
        delivery_attempt_id: str,
        worker: WorkerPrincipal,
        observed_result: str,
        remote_reference: str,
        now: datetime,
    ) -> ReviewDeliveryResult:
        command_fingerprint = _hash_json(
            {
                "command_type": "finalize_review_delivery",
                "delivery_attempt_id": delivery_attempt_id,
                "worker_owner": worker.worker_owner,
                "worker_lease_generation": worker.worker_lease_generation,
                "observed_result": observed_result,
                "remote_reference": remote_reference,
            }
        )
        batch_id, state_path = self._find_runtime_attempt(delivery_attempt_id)
        lock_fd = self._open_lock(self._root / ".command.lock")
        try:
            state = self._read_json(state_path)
            replay = self._replay_runtime_command(
                state,
                command_id,
                command_fingerprint,
                batch_id=batch_id,
            )
            if replay is not None:
                return replay
            attempt = self._runtime_attempt(state, delivery_attempt_id)
            lease_expires_at = datetime.fromisoformat(
                str(attempt["worker_lease_expires_at"])
            )
            self._validate_worker(
                worker, worker_lease_expires_at=lease_expires_at, now=now
            )
            self._validate_attempt_worker(attempt, worker)
            if attempt.get("phase") != "request_started":
                raise ReviewDeliveryConflictError("delivery_phase_conflict")
            if observed_result != "accepted" or not remote_reference:
                raise ReviewDeliveryConflictError("unsupported_simulated_result")
            items = self._runtime_items(state)
            matches = [
                item
                for item in items
                if item.get("snapshot_id") == attempt.get("snapshot_id")
            ]
            if len(matches) != 1 or matches[0].get("status") != "pending":
                raise ReviewDeliveryConflictError("delivery_projection_conflict")
            item = matches[0]
            item["status"] = "presented"
            item["revision"] = int(item["revision"]) + 1
            attempt["phase"] = "finalized"
            attempt["outcome"] = "accepted"
            attempt["remote_reference"] = remote_reference
            if state.get("batch_status") == "ready":
                state["batch_status"] = "in_review"
                state["batch_revision"] = int(state["batch_revision"]) + 1
            result = self._delivery_result_from_state(
                state, attempt, status="applied"
            )
            self._record_runtime_result(
                state, command_id, command_fingerprint, result
            )
            self._write_replace(state_path, state)
            return result
        finally:
            os.close(lock_fd)

    def _validate_active_session(
        self,
        state: dict[str, object],
        reviewer: ReviewPrincipal,
        *,
        session_fence: int,
        now: datetime,
    ) -> None:
        if reviewer.active is not True:
            raise ReviewDeliveryConflictError("reviewer_binding_inactive")
        if state.get("session_owner") != reviewer.session_owner:
            raise ReviewDeliveryConflictError("session_owner_stale")
        if state.get("session_fence") != session_fence:
            raise ReviewDeliveryConflictError("session_fence_stale")
        lease_value = state.get("lease_expires_at")
        if not isinstance(lease_value, str):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        if datetime.fromisoformat(lease_value) <= now:
            raise ReviewDeliveryConflictError("session_lease_expired")

    def _validate_worker(
        self,
        worker: WorkerPrincipal,
        *,
        worker_lease_expires_at: datetime,
        now: datetime,
    ) -> None:
        grant = self._worker_grants.get(worker.worker_owner)
        if (
            worker.active is not True
            or grant is None
            or grant.active is not True
            or grant.worker_owner != worker.worker_owner
            or grant.worker_lease_generation != worker.worker_lease_generation
            or grant.lease_expires_at.tzinfo is None
            or grant.lease_expires_at.utcoffset() != UTC.utcoffset(grant.lease_expires_at)
            or grant.lease_expires_at <= now
            or worker_lease_expires_at > grant.lease_expires_at
        ):
            raise ReviewDeliveryConflictError("worker_fence_stale")

    def _validate_attempt_worker(
        self, attempt: dict[str, object], worker: WorkerPrincipal
    ) -> None:
        if (
            attempt.get("worker_owner") != worker.worker_owner
            or attempt.get("worker_lease_generation")
            != worker.worker_lease_generation
        ):
            raise ReviewDeliveryConflictError("worker_fence_stale")

    def _validate_attempt_reviewer(
        self,
        attempt: dict[str, object],
        reviewer: ReviewPrincipal,
        session_fence: int,
    ) -> None:
        if (
            attempt.get("reviewer_id") != reviewer.reviewer_id
            or attempt.get("reviewer_binding_id") != reviewer.reviewer_binding_id
            or attempt.get("session_owner") != reviewer.session_owner
            or attempt.get("session_fence") != session_fence
        ):
            raise ReviewDeliveryConflictError("reviewer_authority_mismatch")

    def _runtime_items(
        self, state: dict[str, object]
    ) -> list[dict[str, object]]:
        items = state.get("items")
        if not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        return items  # type: ignore[return-value]

    def _validate_runtime_binding(
        self, state: dict[str, object], batch: ReviewBatch
    ) -> None:
        if state.get("batch_id") != batch.batch_id:
            raise StorageConflictError("daily_feedback_runtime_invalid")
        items = self._runtime_items(state)
        expected = [
            (item.fixture_id, item.position, item.snapshot_id) for item in batch.items
        ]
        observed: list[tuple[str, int, str]] = []
        for item in items:
            try:
                observed.append(
                    (
                        str(item["fixture_id"]),
                        int(item["position"]),
                        str(item["snapshot_id"]),
                    )
                )
                revision = int(item["revision"])
            except (KeyError, TypeError, ValueError) as exc:
                raise StorageConflictError(
                    "daily_feedback_runtime_invalid"
                ) from exc
            if (item.get("status"), revision) not in {
                ("pending", 1),
                ("presented", 2),
            }:
                raise StorageConflictError("daily_feedback_runtime_invalid")
        if observed != expected:
            raise StorageConflictError("daily_feedback_runtime_invalid")
        presented_count = sum(item.get("status") == "presented" for item in items)
        try:
            batch_projection = (
                str(state["batch_status"]),
                int(state["batch_revision"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageConflictError("daily_feedback_runtime_invalid") from exc
        if batch_projection == ("ready", 1):
            if presented_count != 0:
                raise StorageConflictError("daily_feedback_runtime_invalid")
        elif batch_projection == ("in_review", 2):
            if presented_count == 0:
                raise StorageConflictError("daily_feedback_runtime_invalid")
        else:
            raise StorageConflictError("daily_feedback_runtime_invalid")

    def _runtime_attempt(
        self, state: dict[str, object], delivery_attempt_id: str
    ) -> dict[str, object]:
        attempts = state.get("delivery_attempts")
        if not isinstance(attempts, dict):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        attempt = attempts.get(delivery_attempt_id)
        if not isinstance(attempt, dict):
            raise ReviewDeliveryConflictError("delivery_attempt_not_found")
        return attempt

    def _find_runtime_attempt(self, delivery_attempt_id: str) -> tuple[str, Path]:
        matches: list[tuple[str, Path]] = []
        for path in self._runtime_dir.glob("*.json"):
            state = self._read_json(path)
            attempts = state.get("delivery_attempts")
            if isinstance(attempts, dict) and delivery_attempt_id in attempts:
                batch_id = state.get("batch_id")
                if isinstance(batch_id, str):
                    self._validate_runtime_binding(
                        state,
                        self._load_committed_batch(
                            self._find_committed_manifest(batch_id)
                        ),
                    )
                    matches.append((batch_id, path))
        if len(matches) != 1:
            raise ReviewDeliveryConflictError("delivery_attempt_not_found")
        return matches[0]

    def _delivery_result_from_state(
        self,
        state: dict[str, object],
        attempt: dict[str, object],
        *,
        status: str,
    ) -> ReviewDeliveryResult:
        matching_items = [
            item
            for item in self._runtime_items(state)
            if item.get("snapshot_id") == attempt.get("snapshot_id")
        ]
        if len(matching_items) != 1:
            raise StorageConflictError("daily_feedback_runtime_invalid")
        return ReviewDeliveryResult(
            status=status,
            delivery_attempt_id=str(attempt["delivery_attempt_id"]),
            semantic_delivery_key=str(attempt["semantic_delivery_key"]),
            phase=str(attempt["phase"]),
            outcome=(
                str(attempt["outcome"])
                if attempt.get("outcome") is not None
                else None
            ),
            item_status=str(matching_items[0]["status"]),
            batch_status=str(state["batch_status"]),
            batch_revision=int(state["batch_revision"]),
        )

    def _record_runtime_result(
        self,
        state: dict[str, object],
        command_id: str,
        fingerprint: str,
        result: ReviewDeliveryResult,
    ) -> None:
        commands = state.get("commands")
        if not isinstance(commands, dict):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        commands[command_id] = {
            "fingerprint": fingerprint,
            "delivery_result": {
                "delivery_attempt_id": result.delivery_attempt_id,
                "semantic_delivery_key": result.semantic_delivery_key,
                "phase": result.phase,
                "outcome": result.outcome,
                "item_status": result.item_status,
                "batch_status": result.batch_status,
                "batch_revision": result.batch_revision,
            },
        }

    def _replay_runtime_command(
        self,
        state: dict[str, object],
        command_id: str,
        fingerprint: str,
        *,
        batch_id: str,
    ) -> ReviewDeliveryResult | None:
        global_result = self._read_global_runtime_command(command_id, fingerprint)
        if global_result is not None:
            delivery_result = global_result.get("delivery_result")
            if not isinstance(delivery_result, dict):
                raise IdempotencyConflictError("idempotency_conflict")
            return self._delivery_result_from_envelope(delivery_result, status="replayed")
        commands = state.get("commands")
        if not isinstance(commands, dict):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        prior = commands.get(command_id)
        if prior is None:
            return None
        if not isinstance(prior, dict) or prior.get("fingerprint") != fingerprint:
            raise IdempotencyConflictError("idempotency_conflict")
        result = prior.get("delivery_result")
        if not isinstance(result, dict):
            raise IdempotencyConflictError("idempotency_conflict")
        self._write_global_runtime_command(
            command_id, fingerprint, batch_id, prior
        )
        return self._delivery_result_from_envelope(result, status="replayed")

    def _delivery_result_from_envelope(
        self, result: dict[str, object], *, status: str
    ) -> ReviewDeliveryResult:
        return ReviewDeliveryResult(
            status=status,
            delivery_attempt_id=str(result["delivery_attempt_id"]),
            semantic_delivery_key=str(result["semantic_delivery_key"]),
            phase=str(result["phase"]),
            outcome=(
                str(result["outcome"])
                if result.get("outcome") is not None
                else None
            ),
            item_status=str(result["item_status"]),
            batch_status=str(result["batch_status"]),
            batch_revision=int(result["batch_revision"]),
        )

    def _load_or_initialize_runtime_state(
        self, path: Path, batch: ReviewBatch
    ) -> dict[str, object]:
        if path.exists():
            return self._read_json(path)
        return {
            "schema_version": 1,
            "batch_id": batch.batch_id,
            "batch_status": batch.status,
            "batch_revision": batch.revision,
            "session_owner": None,
            "session_fence": 0,
            "lease_expires_at": None,
            "items": [
                {
                    "fixture_id": item.fixture_id,
                    "position": item.position,
                    "snapshot_id": item.snapshot_id,
                    "status": "pending",
                    "revision": 1,
                }
                for item in batch.items
            ],
            "commands": {},
        }

    def _session_claim_result(
        self, value: dict[str, object], *, status: str
    ) -> SessionClaimResult:
        return SessionClaimResult(
            status=status,
            batch_id=str(value["batch_id"]),
            batch_status=str(value["batch_status"]),
            batch_revision=int(value["batch_revision"]),
            session_owner=str(value["session_owner"]),
            session_fence=int(value["session_fence"]),
            lease_expires_at=datetime.fromisoformat(str(value["lease_expires_at"])),
        )

    def _runtime_command_path(self, command_id: str) -> Path:
        return self._runtime_commands_dir / f"{_hash_text(command_id)}.json"

    def _runtime_command_exists(self, command_id: str) -> bool:
        if self._runtime_command_path(command_id).exists():
            return True
        for runtime_path in self._runtime_dir.glob("*.json"):
            state = self._read_json(runtime_path)
            commands = state.get("commands")
            if isinstance(commands, dict) and command_id in commands:
                return True
        return False

    def _read_global_runtime_command(
        self, command_id: str, fingerprint: str
    ) -> dict[str, object] | None:
        creation_command_path = self._commands_dir / f"{_hash_text(command_id)}.json"
        if creation_command_path.exists():
            raise IdempotencyConflictError("idempotency_conflict")
        path = self._runtime_command_path(command_id)
        if path.exists():
            envelope = self._read_json(path)
            if (
                envelope.get("command_id") != command_id
                or envelope.get("fingerprint") != fingerprint
                or not isinstance(envelope.get("result"), dict)
            ):
                raise IdempotencyConflictError("idempotency_conflict")
            return envelope["result"]  # type: ignore[return-value]
        recovered: list[tuple[str, dict[str, object]]] = []
        for runtime_path in self._runtime_dir.glob("*.json"):
            state = self._read_json(runtime_path)
            commands = state.get("commands")
            prior = commands.get(command_id) if isinstance(commands, dict) else None
            if isinstance(prior, dict):
                recovered.append((str(state.get("batch_id")), prior))
        if not recovered:
            return None
        if len(recovered) != 1 or recovered[0][1].get("fingerprint") != fingerprint:
            raise IdempotencyConflictError("idempotency_conflict")
        batch_id, result = recovered[0]
        self._write_global_runtime_command(
            command_id, fingerprint, batch_id, result
        )
        return result

    def _write_global_runtime_command(
        self,
        command_id: str,
        fingerprint: str,
        batch_id: str,
        result: dict[str, object],
    ) -> None:
        self._write_once(
            self._runtime_command_path(command_id),
            {
                "schema_version": 1,
                "command_id": command_id,
                "fingerprint": fingerprint,
                "batch_id": batch_id,
                "result": result,
            },
        )

    def _open_lock(self, path: Path) -> int:
        lock_fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return lock_fd

    def _write_replace(self, path: Path, envelope: dict[str, object]) -> None:
        serialized = json.dumps(
            envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(serialized + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
            directory_fd = os.open(
                path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

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
        for item in batch.items:
            snapshot = self._read_json(
                self._snapshots_dir / f"{item.snapshot_id}.json"
            )
            if (
                snapshot.get("snapshot_id") != item.snapshot_id
                or snapshot.get("fixture_id") != item.fixture_id
                or snapshot.get("sanitized") is not True
            ):
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
    reviewer_grant: FixtureReviewerGrant | None = None,
) -> FastAPI:
    """Create the fixture-only internal HTTP boundary for controlled verification."""
    if not operator_grant.token:
        raise ValueError("daily_feedback_operator_token_required")
    if reviewer_grant is not None and not reviewer_grant.token:
        raise ValueError("daily_feedback_reviewer_token_required")
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

    def authorize_reviewer(authorization: str | None) -> ReviewPrincipal:
        if reviewer_grant is None:
            raise HTTPException(status_code=404, detail="reviewer_boundary_disabled")
        expected = f"Bearer {reviewer_grant.token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid_reviewer_token")
        if not reviewer_grant.active:
            raise HTTPException(status_code=403, detail="reviewer_binding_inactive")
        return ReviewPrincipal(
            reviewer_id=reviewer_grant.reviewer_id,
            reviewer_binding_id=reviewer_grant.reviewer_binding_id,
            session_owner=reviewer_grant.session_owner,
            active=True,
        )

    @app.post("/internal/daily-feedback/review-sessions", status_code=201)
    async def claim_review_session_http(
        payload: dict[str, object],
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        principal = authorize_reviewer(authorization)
        if set(payload) != {
            "command_id",
            "batch_id",
            "expected_batch_revision",
            "lease_seconds",
        } or not all(
            (
                isinstance(payload.get("command_id"), str),
                isinstance(payload.get("batch_id"), str),
                isinstance(payload.get("expected_batch_revision"), int),
                isinstance(payload.get("lease_seconds"), int),
            )
        ):
            raise HTTPException(status_code=422, detail="invalid_session_claim_payload")
        try:
            result = store.claim_review_session(
                command_id=str(payload["command_id"]),
                batch_id=str(payload["batch_id"]),
                principal=principal,
                expected_batch_revision=int(payload["expected_batch_revision"]),
                lease_seconds=int(payload["lease_seconds"]),
                now=datetime.now(UTC),
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except SessionClaimConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "status": result.status,
            "batch_id": result.batch_id,
            "batch_status": result.batch_status,
            "batch_revision": result.batch_revision,
            "session_owner": result.session_owner,
            "session_fence": result.session_fence,
            "lease_expires_at": result.lease_expires_at.isoformat(),
        }

    @app.get("/internal/daily-feedback/review-sessions/{batch_id}/next-item")
    async def get_next_review_item_http(
        batch_id: str,
        session_fence: int,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        principal = authorize_reviewer(authorization)
        try:
            item = store.get_next_review_item(
                batch_id=batch_id,
                principal=principal,
                session_fence=session_fence,
                now=datetime.now(UTC),
            )
        except ReviewAuthorizationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "fixture_id": item.fixture_id,
            "position": item.position,
            "total": item.total,
            "status": item.status,
            "item_revision": item.item_revision,
            "presentation_snapshot": {
                "snapshot_id": item.snapshot_id,
                "sanitized": True,
                "context_summary": item.context_summary,
                "apparent_objective": item.apparent_objective,
                "observed_outcome": item.observed_outcome,
                "release": {
                    "id": item.release_id,
                    "version": item.release_version,
                },
                "payload_hash": item.payload_hash,
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
