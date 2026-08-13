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
from typing import Mapping, cast, Optional

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
class FixtureReconciliationGrant:
    token: str
    reconciliation_owner: str
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
class ReconciliationServiceGrant:
    reconciliation_owner: str
    active: bool


@dataclass(frozen=True)
class ReconciliationPrincipal:
    reconciliation_owner: str
    active: bool


@dataclass(frozen=True)
class ReconciliationClaimResult:
    status: str
    delivery_attempt_id: str
    reconciliation_owner: str
    reconciliation_generation: int
    lease_expires_at: datetime


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


@dataclass(frozen=True)
class LateDeliveryObservationResult:
    status: str
    delivery_attempt_id: str
    observation_fingerprint: str
    observed_result: str
    remote_reference: str
    observed_at: datetime
    submitted_at: datetime


@dataclass(frozen=True)
class SimulatedConnectorResult:
    status: str
    delivery_attempt_id: str
    configured_result: str
    remote_reference: str
    invocation_count: int


@dataclass(frozen=True)
class DeliveryRetryResult:
    status: str
    delivery_attempt_id: str
    previous_delivery_attempt_id: str
    semantic_delivery_key: str
    attempt_number: int
    phase: str


@dataclass(frozen=True)
class ReviewDecisionResult:
    status: str
    decision_id: str
    decision_type: str
    verbatim_feedback: str | None
    item_status: str
    item_revision: int
    batch_status: str
    batch_revision: int
    reviewed_count: int
    with_feedback_count: int
    skipped_count: int


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


class ReviewDecisionConflictError(RuntimeError):
    """A reviewer decision violates its durable authority or item state."""


class DailyFeedbackBatchStore:
    """Materialize immutable, sanitized fixture batches on private storage."""

    def __init__(
        self,
        root: Path,
        *,
        worker_grants: Mapping[str, WorkerLeaseGrant] | None = None,
        reconciliation_grants: Mapping[str, ReconciliationServiceGrant]
        | None = None,
    ) -> None:
        self._root = root
        self._worker_grants = dict(worker_grants or {})
        self._reconciliation_grants = dict(reconciliation_grants or {})
        self._commands_dir = root / "commands"
        self._batches_dir = root / "batches"
        self._logical_dir = root / "logical"
        self._snapshots_dir = root / "snapshots"
        self._manifests_dir = root / "manifests"
        self._commits_dir = root / "commits"
        self._runtime_dir = root / "runtime"
        self._runtime_commands_dir = root / "runtime_commands"
        self._review_decision_intents_dir = root / "review_decision_intents"
        self._review_decisions_dir = root / "review_decisions"
        self._owner_feedback_dir = root / "owner_feedback"
        self._review_decision_results_dir = root / "review_decision_results"
        self._review_decision_commits_dir = root / "review_decision_commits"
        self._ensure_private_directory(self._root)
        self._ensure_private_directory(self._commands_dir)
        self._ensure_private_directory(self._batches_dir)
        self._ensure_private_directory(self._logical_dir)
        self._ensure_private_directory(self._snapshots_dir)
        self._ensure_private_directory(self._manifests_dir)
        self._ensure_private_directory(self._commits_dir)
        self._ensure_private_directory(self._runtime_dir)
        self._ensure_private_directory(self._runtime_commands_dir)
        self._ensure_private_directory(self._review_decision_intents_dir)
        self._ensure_private_directory(self._review_decisions_dir)
        self._ensure_private_directory(self._owner_feedback_dir)
        self._ensure_private_directory(self._review_decision_results_dir)
        self._ensure_private_directory(self._review_decision_commits_dir)

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
            not in {
                "correct", "corrected", "reviewed", "skipped",
                "feedback_cancelled",
            }
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

    def record_review_decision(
        self,
        *,
        command_id: str,
        batch_id: str,
        snapshot_id: str,
        expected_item_revision: int,
        decision_type: str,
        verbatim_feedback: str | None,
        principal: ReviewPrincipal,
        session_fence: int,
        now: datetime,
    ) -> ReviewDecisionResult:
        payload = {
            "batch_id": batch_id,
            "snapshot_id": snapshot_id,
            "expected_item_revision": expected_item_revision,
            "decision_type": decision_type,
            "verbatim_feedback": verbatim_feedback,
            "reviewer_id": principal.reviewer_id,
            "reviewer_binding_id": principal.reviewer_binding_id,
            "session_owner": principal.session_owner,
            "session_fence": session_fence,
        }
        command_fingerprint = _hash_json(
            {"command_type": "record_review_decision", **payload}
        )
        committed_batch = self._load_committed_batch(
            self._find_committed_manifest(batch_id)
        )
        batch_envelope = self._read_json(self._batches_dir / f"{batch_id}.json")
        lock_fd = self._open_lock(self._root / ".command.lock")
        try:
            state_path = self._runtime_dir / f"{batch_id}.json"
            if not state_path.exists():
                raise ReviewDecisionConflictError("review_session_missing")
            state = self._read_json(state_path)
            recovered_decision = self._recover_review_decision_intent(
                command_id=command_id,
                fingerprint=command_fingerprint,
                batch_id=batch_id,
                state_path=state_path,
                state=state,
                committed_batch=committed_batch,
            )
            if recovered_decision is not None:
                return self._review_decision_result(
                    recovered_decision, status="replayed"
                )
            prior = self._read_global_runtime_command(
                command_id, command_fingerprint
            )
            if prior is not None:
                envelope = prior.get("review_decision_result")
                if not isinstance(envelope, dict):
                    raise IdempotencyConflictError("idempotency_conflict")
                return self._review_decision_result(envelope, status="replayed")
            if not (
                (decision_type == "correct" and verbatim_feedback is None)
                or (decision_type == "skip" and verbatim_feedback is None)
                or (
                    decision_type == "correct_with_feedback"
                    and isinstance(verbatim_feedback, str)
                    and bool(verbatim_feedback.strip())
                    and len(
                        verbatim_feedback.replace("\r\n", "\n").replace("\r", "\n")
                    )
                    <= 4000
                )
            ):
                raise ReviewDecisionConflictError("invalid_review_decision")
            if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
                raise ReviewDecisionConflictError("invalid_review_time")
            if (
                batch_envelope.get("reviewer_id") != principal.reviewer_id
                or batch_envelope.get("reviewer_binding_id")
                != principal.reviewer_binding_id
            ):
                raise ReviewDecisionConflictError("reviewer_authority_mismatch")
            try:
                self._validate_active_session(
                    state, principal, session_fence=session_fence, now=now
                )
            except ReviewDeliveryConflictError as exc:
                raise ReviewDecisionConflictError(str(exc)) from exc
            self._validate_runtime_binding(state, committed_batch)
            matches = [
                item
                for item in self._runtime_items(state)
                if item.get("snapshot_id") == snapshot_id
            ]
            if len(matches) != 1:
                raise ReviewDecisionConflictError("review_item_not_found")
            item = matches[0]
            if (
                item.get("status") != "presented"
                or item.get("revision") != expected_item_revision
            ):
                raise ReviewDecisionConflictError("review_item_revision_conflict")
            nonterminal = [
                candidate
                for candidate in self._runtime_items(state)
                if candidate.get("status") not in {"reviewed", "skipped"}
            ]
            if (
                not nonterminal
                or min(
                    nonterminal,
                    key=lambda value: cast(int, value["position"]),
                )
                is not item
            ):
                raise ReviewDecisionConflictError("review_item_out_of_order")
            runtime_before_hash = _hash_json(state)
            decisions = state.setdefault("review_decisions", {})
            if not isinstance(decisions, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            decision_sequence = len(decisions) + 1
            decision_id = f"decision_{_hash_json({
                **payload,
                'decision_sequence': decision_sequence,
            })}"
            if decision_id in decisions:
                raise StorageConflictError("daily_feedback_runtime_invalid")
            feedback_id = None
            if decision_type == "correct_with_feedback":
                assert isinstance(verbatim_feedback, str)
                feedback_id = f"feedback_{_hash_json({
                    'decision_id': decision_id,
                    'snapshot_id': snapshot_id,
                    'verbatim_text': verbatim_feedback,
                    'reviewer_id': principal.reviewer_id,
                    'reviewer_binding_id': principal.reviewer_binding_id,
                })}"
                feedback_records = state.setdefault("owner_feedback", {})
                if (
                    not isinstance(feedback_records, dict)
                    or feedback_id in feedback_records
                ):
                    raise StorageConflictError("daily_feedback_runtime_invalid")
                feedback_records[feedback_id] = {
                    "feedback_id": feedback_id,
                    "decision_id": decision_id,
                    "batch_id": batch_id,
                    "snapshot_id": snapshot_id,
                    "verbatim_text": verbatim_feedback,
                    "content_hash": f"sha256:{_hash_text(verbatim_feedback)}",
                    "reviewer_id": principal.reviewer_id,
                    "reviewer_binding_id": principal.reviewer_binding_id,
                    "command_id": command_id,
                    "created_at": now.isoformat(),
                }
            envelope = {
                "decision_id": decision_id,
                "batch_id": batch_id,
                "snapshot_id": snapshot_id,
                "decision_type": decision_type,
                "verbatim_feedback": None,
                "owner_feedback_id": feedback_id,
                "reviewer_id": principal.reviewer_id,
                "reviewer_binding_id": principal.reviewer_binding_id,
                "session_owner": principal.session_owner,
                "session_fence": session_fence,
                "expected_item_revision": expected_item_revision,
                "decision_sequence": decision_sequence,
                "command_id": command_id,
                "recorded_at": now.isoformat(),
            }
            decisions[decision_id] = envelope
            item["status"] = (
                "skipped" if decision_type == "skip" else "reviewed"
            )
            item["revision"] = expected_item_revision + 1
            item["current_decision_id"] = decision_id
            terminal_statuses = {
                "reviewed", "skipped", "corrected", "feedback_cancelled"
            }
            if all(
                candidate.get("status") in terminal_statuses
                for candidate in self._runtime_items(state)
            ):
                state["batch_status"] = "completed"
                state["batch_revision"] = int(state["batch_revision"]) + 1
            reviewed_count = sum(
                candidate.get("status") == "reviewed"
                for candidate in self._runtime_items(state)
            )
            skipped_count = sum(
                candidate.get("status") == "skipped"
                for candidate in self._runtime_items(state)
            )
            with_feedback_count = sum(
                isinstance(candidate, dict)
                and candidate.get("decision_type") == "correct_with_feedback"
                for candidate in decisions.values()
            )
            result = self._review_decision_result(
                {
                    **envelope,
                    "verbatim_feedback": verbatim_feedback,
                    "item_status": item["status"],
                    "item_revision": item["revision"],
                    "batch_status": state["batch_status"],
                    "batch_revision": state["batch_revision"],
                    "reviewed_count": reviewed_count,
                    "with_feedback_count": with_feedback_count,
                    "skipped_count": skipped_count,
                },
                status="applied",
            )
            commands = state.get("commands")
            if not isinstance(commands, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            commands[command_id] = {
                "fingerprint": command_fingerprint,
                "review_decision_result": {
                    "decision_id": result.decision_id,
                    "decision_type": result.decision_type,
                    "verbatim_feedback": result.verbatim_feedback,
                    "item_status": result.item_status,
                    "item_revision": result.item_revision,
                    "batch_status": result.batch_status,
                    "batch_revision": result.batch_revision,
                    "reviewed_count": result.reviewed_count,
                    "with_feedback_count": result.with_feedback_count,
                    "skipped_count": result.skipped_count,
                },
            }
            result_envelope = commands[command_id]["review_decision_result"]
            assert isinstance(result_envelope, dict)
            feedback_envelope = (
                state["owner_feedback"].get(feedback_id)
                if isinstance(state.get("owner_feedback"), dict)
                and isinstance(feedback_id, str)
                else None
            )
            intent = {
                "schema_version": 1,
                "command_id": command_id,
                "fingerprint": command_fingerprint,
                "batch_id": batch_id,
                "decision_id": decision_id,
                "feedback_id": feedback_id,
                "runtime_before_hash": runtime_before_hash,
                "runtime_after_hash": _hash_json(state),
                "decision_hash": _hash_json(envelope),
                "feedback_hash": (
                    _hash_json(feedback_envelope)
                    if isinstance(feedback_envelope, dict)
                    else None
                ),
                "result_hash": _hash_json(result_envelope),
                "runtime_after": state,
            }
            self._write_once(self._decision_intent_path(command_id), intent)
            self._write_once(
                self._review_decisions_dir / f"{decision_id}.json", envelope
            )
            if isinstance(feedback_id, str) and isinstance(feedback_envelope, dict):
                self._write_once(
                    self._owner_feedback_dir / f"{feedback_id}.json",
                    feedback_envelope,
                )
            self._write_once(
                self._decision_result_path(command_id), result_envelope
            )
            self._write_replace(state_path, state)
            self._write_decision_commit(intent)
            return result
        finally:
            os.close(lock_fd)

    def _review_decision_result(
        self, envelope: dict[str, object], *, status: str
    ) -> ReviewDecisionResult:
        return ReviewDecisionResult(
            status=status,
            decision_id=str(envelope["decision_id"]),
            decision_type=str(envelope["decision_type"]),
            verbatim_feedback=(
                str(envelope["verbatim_feedback"])
                if envelope.get("verbatim_feedback") is not None
                else None
            ),
            item_status=str(envelope["item_status"]),
            item_revision=int(envelope["item_revision"]),
            batch_status=str(envelope["batch_status"]),
            batch_revision=int(envelope["batch_revision"]),
            reviewed_count=int(envelope["reviewed_count"]),
            with_feedback_count=int(envelope["with_feedback_count"]),
            skipped_count=int(envelope["skipped_count"]),
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
                "attempt_number": 1,
                "previous_delivery_attempt_id": None,
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

    def cancel_review_delivery_before_request(
        self,
        *,
        command_id: str,
        delivery_attempt_id: str,
        worker: WorkerPrincipal,
        reason_code: str,
        now: datetime,
    ) -> ReviewDeliveryResult:
        if reason_code not in {
            "reviewer_authority_revoked",
            "worker_cancelled",
            "delivery_policy_revoked",
        }:
            raise ReviewDeliveryConflictError("invalid_cancellation_reason")
        command_fingerprint = _hash_json(
            {
                "command_type": "cancel_review_delivery_before_request",
                "delivery_attempt_id": delivery_attempt_id,
                "worker_owner": worker.worker_owner,
                "worker_lease_generation": worker.worker_lease_generation,
                "reason_code": reason_code,
            }
        )
        batch_id, state_path = self._find_runtime_attempt(delivery_attempt_id)
        lock_fd = self._open_lock(self._root / ".command.lock")
        try:
            state = self._read_json(state_path)
            replay = self._replay_runtime_command(
                state, command_id, command_fingerprint, batch_id=batch_id
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
            if attempt.get("phase") != "reserved":
                raise ReviewDeliveryConflictError("delivery_phase_conflict")
            attempt["phase"] = "finalized"
            attempt["outcome"] = "cancelled_before_request"
            attempt["cancellation_reason"] = reason_code
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

    def invoke_simulated_delivery_connector(
        self,
        *,
        command_id: str,
        delivery_attempt_id: str,
        worker: WorkerPrincipal,
        configured_result: str,
        configured_reconciliation_result: str | None = None,
        now: datetime,
    ) -> SimulatedConnectorResult:
        if configured_result not in {"accepted", "rejected", "delivery_unknown"}:
            raise ReviewDeliveryConflictError("unsupported_simulated_result")
        if (
            configured_result == "delivery_unknown"
            and configured_reconciliation_result not in {"accepted", "not_applied"}
        ) or (
            configured_result != "delivery_unknown"
            and configured_reconciliation_result is not None
        ):
            raise ReviewDeliveryConflictError("unsupported_simulated_result")
        command_fingerprint = _hash_json(
            {
                "command_type": "invoke_simulated_delivery_connector",
                "delivery_attempt_id": delivery_attempt_id,
                "worker_owner": worker.worker_owner,
                "worker_lease_generation": worker.worker_lease_generation,
                "configured_result": configured_result,
                "configured_reconciliation_result": configured_reconciliation_result,
            }
        )
        _batch_id, state_path = self._find_runtime_attempt(delivery_attempt_id)
        lock_fd = self._open_lock(self._root / ".command.lock")
        try:
            state = self._read_json(state_path)
            prior = self._read_global_runtime_command(
                command_id, command_fingerprint
            )
            if prior is not None:
                envelope = prior.get("simulated_connector_result")
                if not isinstance(envelope, dict):
                    raise IdempotencyConflictError("idempotency_conflict")
                return self._simulated_connector_result(
                    envelope, status="replayed"
                )
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
            effects = state.setdefault("simulated_connector_effects", {})
            if not isinstance(effects, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            if delivery_attempt_id in effects:
                raise ReviewDeliveryConflictError("delivery_effect_already_invoked")
            remote_reference = (
                f"simulated_{_hash_json({'attempt_id': delivery_attempt_id, 'result': configured_result})}"
            )
            envelope = {
                "delivery_attempt_id": delivery_attempt_id,
                "configured_result": configured_result,
                "remote_reference": remote_reference,
                "invocation_count": 1,
                "configured_reconciliation_result": configured_reconciliation_result,
                "reconciliation_reference": (
                    f"simulated_reconciliation_{_hash_json({'attempt_id': delivery_attempt_id, 'result': configured_reconciliation_result})}"
                    if configured_reconciliation_result is not None
                    else None
                ),
            }
            effects[delivery_attempt_id] = envelope
            commands = state.get("commands")
            if not isinstance(commands, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            commands[command_id] = {
                "fingerprint": command_fingerprint,
                "simulated_connector_result": envelope,
            }
            self._write_replace(state_path, state)
            return self._simulated_connector_result(envelope, status="applied")
        finally:
            os.close(lock_fd)

    def _simulated_connector_result(
        self, envelope: dict[str, object], *, status: str
    ) -> SimulatedConnectorResult:
        return SimulatedConnectorResult(
            status=status,
            delivery_attempt_id=str(envelope["delivery_attempt_id"]),
            configured_result=str(envelope["configured_result"]),
            remote_reference=str(envelope["remote_reference"]),
            invocation_count=int(envelope["invocation_count"]),
        )

    def finalize_review_delivery(
        self,
        *,
        command_id: str,
        delivery_attempt_id: str,
        worker: WorkerPrincipal,
        observed_result: str,
        remote_reference: str,
        now: datetime,
        reconciliation_deadline: datetime | None = None,
    ) -> ReviewDeliveryResult:
        command_fingerprint = _hash_json(
            {
                "command_type": "finalize_review_delivery",
                "delivery_attempt_id": delivery_attempt_id,
                "worker_owner": worker.worker_owner,
                "worker_lease_generation": worker.worker_lease_generation,
                "observed_result": observed_result,
                "remote_reference": remote_reference,
                "reconciliation_deadline": (
                    reconciliation_deadline.isoformat()
                    if reconciliation_deadline is not None
                    else None
                ),
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
            if not remote_reference:
                raise ReviewDeliveryConflictError("unsupported_simulated_result")
            effects = state.get("simulated_connector_effects", {})
            if not isinstance(effects, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            connector_effect = effects.get(delivery_attempt_id)
            if connector_effect is not None and (
                not isinstance(connector_effect, dict)
                or connector_effect.get("configured_result") != observed_result
                or connector_effect.get("remote_reference") != remote_reference
            ):
                raise ReviewDeliveryConflictError("connector_result_mismatch")
            if observed_result == "delivery_unknown":
                if (
                    reconciliation_deadline is None
                    or reconciliation_deadline.tzinfo is None
                    or reconciliation_deadline.utcoffset()
                    != UTC.utcoffset(reconciliation_deadline)
                    or reconciliation_deadline <= now
                ):
                    raise ReviewDeliveryConflictError(
                        "invalid_reconciliation_deadline"
                    )
                attempt["phase"] = "finalized"
                attempt["outcome"] = "delivery_unknown"
                attempt["remote_reference"] = remote_reference
                attempt["reconciliation_deadline"] = (
                    reconciliation_deadline.isoformat()
                )
                result = self._delivery_result_from_state(
                    state, attempt, status="applied"
                )
                self._record_runtime_result(
                    state, command_id, command_fingerprint, result
                )
                self._write_replace(state_path, state)
                return result
            if observed_result == "rejected" and reconciliation_deadline is None:
                attempt["phase"] = "finalized"
                attempt["outcome"] = "rejected"
                attempt["remote_reference"] = remote_reference
                result = self._delivery_result_from_state(
                    state, attempt, status="applied"
                )
                self._record_runtime_result(
                    state, command_id, command_fingerprint, result
                )
                self._write_replace(state_path, state)
                return result
            if observed_result != "accepted" or reconciliation_deadline is not None:
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

    def submit_late_delivery_observation(
        self,
        *,
        command_id: str,
        delivery_attempt_id: str,
        worker: WorkerPrincipal,
        observed_result: str,
        remote_reference: str,
        observed_at: datetime,
        submitted_at: datetime,
    ) -> LateDeliveryObservationResult:
        if (
            observed_result not in {"accepted", "not_applied"}
            or not remote_reference
            or observed_at.tzinfo is None
            or submitted_at.tzinfo is None
            or observed_at.utcoffset() != UTC.utcoffset(observed_at)
            or submitted_at.utcoffset() != UTC.utcoffset(submitted_at)
            or observed_at > submitted_at
        ):
            raise ReviewDeliveryConflictError("invalid_late_observation")
        payload = {
            "delivery_attempt_id": delivery_attempt_id,
            "worker_owner": worker.worker_owner,
            "worker_lease_generation": worker.worker_lease_generation,
            "observed_result": observed_result,
            "remote_reference": remote_reference,
            "observed_at": observed_at.isoformat(),
            "submitted_at": submitted_at.isoformat(),
        }
        fingerprint = f"sha256:{_hash_json(payload)}"
        command_fingerprint = _hash_json(
            {"command_type": "submit_late_delivery_observation", **payload}
        )
        _batch_id, state_path = self._find_runtime_attempt(delivery_attempt_id)
        lock_fd = self._open_lock(self._root / ".command.lock")
        try:
            state = self._read_json(state_path)
            prior = self._read_global_runtime_command(
                command_id, command_fingerprint
            )
            if prior is not None:
                result = prior.get("late_observation_result")
                if not isinstance(result, dict):
                    raise IdempotencyConflictError("idempotency_conflict")
                return self._late_observation_result(result, status="replayed")
            attempt = self._runtime_attempt(state, delivery_attempt_id)
            historical_grant = self._worker_grants.get(worker.worker_owner)
            if (
                worker.active is not True
                or historical_grant is None
                or historical_grant.active is not True
                or historical_grant.worker_owner != worker.worker_owner
                or historical_grant.worker_lease_generation
                != worker.worker_lease_generation
                or
                attempt.get("worker_owner") != worker.worker_owner
                or attempt.get("worker_lease_generation")
                != worker.worker_lease_generation
            ):
                raise ReviewDeliveryConflictError("worker_fence_stale")
            if attempt.get("outcome") != "delivery_unknown":
                raise ReviewDeliveryConflictError("delivery_phase_conflict")
            observations = state.setdefault("delivery_observations", {})
            if not isinstance(observations, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            envelope = {
                "delivery_attempt_id": delivery_attempt_id,
                "worker_owner": worker.worker_owner,
                "worker_lease_generation": worker.worker_lease_generation,
                "observation_fingerprint": fingerprint,
                "observed_result": observed_result,
                "remote_reference": remote_reference,
                "observed_at": observed_at.isoformat(),
                "submitted_at": submitted_at.isoformat(),
            }
            existing = observations.get(fingerprint)
            if existing is not None and existing != envelope:
                raise ReviewDeliveryConflictError("delivery_result_conflict")
            observations[fingerprint] = envelope
            commands = state.get("commands")
            if not isinstance(commands, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            commands[command_id] = {
                "fingerprint": command_fingerprint,
                "late_observation_result": envelope,
            }
            self._write_replace(state_path, state)
            return self._late_observation_result(envelope, status="applied")
        finally:
            os.close(lock_fd)

    def _late_observation_result(
        self, value: dict[str, object], *, status: str
    ) -> LateDeliveryObservationResult:
        return LateDeliveryObservationResult(
            status=status,
            delivery_attempt_id=str(value["delivery_attempt_id"]),
            observation_fingerprint=str(value["observation_fingerprint"]),
            observed_result=str(value["observed_result"]),
            remote_reference=str(value["remote_reference"]),
            observed_at=datetime.fromisoformat(str(value["observed_at"])),
            submitted_at=datetime.fromisoformat(str(value["submitted_at"])),
        )

    def claim_delivery_reconciliation(
        self,
        *,
        command_id: str,
        delivery_attempt_id: str,
        reconciler: ReconciliationPrincipal,
        lease_seconds: int,
        now: datetime,
    ) -> ReconciliationClaimResult:
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ReviewDeliveryConflictError("invalid_reconciliation_clock")
        grant = self._reconciliation_grants.get(
            reconciler.reconciliation_owner
        )
        if (
            reconciler.active is not True
            or grant is None
            or grant.active is not True
            or grant.reconciliation_owner != reconciler.reconciliation_owner
            or not 1 <= lease_seconds <= 300
        ):
            raise ReviewDeliveryConflictError("reconciliation_authority_invalid")
        fingerprint = _hash_json(
            {
                "command_type": "claim_delivery_reconciliation",
                "delivery_attempt_id": delivery_attempt_id,
                "reconciliation_owner": reconciler.reconciliation_owner,
                "lease_seconds": lease_seconds,
            }
        )
        batch_id, state_path = self._find_runtime_attempt(delivery_attempt_id)
        lock_fd = self._open_lock(self._root / ".command.lock")
        try:
            state = self._read_json(state_path)
            prior = self._read_global_runtime_command(command_id, fingerprint)
            if prior is not None:
                value = prior.get("reconciliation_claim_result")
                if not isinstance(value, dict):
                    raise IdempotencyConflictError("idempotency_conflict")
                return self._reconciliation_claim_result(value, status="replayed")
            attempt = self._runtime_attempt(state, delivery_attempt_id)
            if state.get("batch_status") == "blocked":
                raise ReviewDeliveryConflictError("delivery_result_conflict")
            if attempt.get("outcome") != "delivery_unknown":
                raise ReviewDeliveryConflictError("delivery_phase_conflict")
            lease_value = attempt.get("reconciliation_lease_expires_at")
            lease_active = (
                isinstance(lease_value, str)
                and datetime.fromisoformat(lease_value) > now
            )
            current_owner = attempt.get("reconciliation_owner")
            if lease_active and current_owner != reconciler.reconciliation_owner:
                raise ReviewDeliveryConflictError("reconciliation_lease_active")
            generation = int(attempt.get("reconciliation_generation", 0)) + 1
            lease_expires_at = now + timedelta(seconds=lease_seconds)
            attempt["reconciliation_owner"] = reconciler.reconciliation_owner
            attempt["reconciliation_generation"] = generation
            attempt["reconciliation_lease_expires_at"] = lease_expires_at.isoformat()
            envelope = {
                "delivery_attempt_id": delivery_attempt_id,
                "reconciliation_owner": reconciler.reconciliation_owner,
                "reconciliation_generation": generation,
                "lease_expires_at": lease_expires_at.isoformat(),
            }
            commands = state.get("commands")
            if not isinstance(commands, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            commands[command_id] = {
                "fingerprint": fingerprint,
                "reconciliation_claim_result": envelope,
            }
            self._write_replace(state_path, state)
            return self._reconciliation_claim_result(envelope, status="applied")
        finally:
            os.close(lock_fd)

    def _reconciliation_claim_result(
        self, value: dict[str, object], *, status: str
    ) -> ReconciliationClaimResult:
        return ReconciliationClaimResult(
            status=status,
            delivery_attempt_id=str(value["delivery_attempt_id"]),
            reconciliation_owner=str(value["reconciliation_owner"]),
            reconciliation_generation=int(value["reconciliation_generation"]),
            lease_expires_at=datetime.fromisoformat(str(value["lease_expires_at"])),
        )

    def reconcile_review_delivery(
        self,
        *,
        command_id: str,
        delivery_attempt_id: str,
        reconciler: ReconciliationPrincipal,
        reconciliation_generation: int,
        resolution: str,
        observation_fingerprint: str | None,
        now: datetime,
    ) -> ReviewDeliveryResult:
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise ReviewDeliveryConflictError("invalid_reconciliation_clock")
        fingerprint = _hash_json(
            {
                "command_type": "reconcile_review_delivery",
                "delivery_attempt_id": delivery_attempt_id,
                "reconciliation_owner": reconciler.reconciliation_owner,
                "reconciliation_generation": reconciliation_generation,
                "resolution": resolution,
                "observation_fingerprint": observation_fingerprint,
            }
        )
        batch_id, state_path = self._find_runtime_attempt(delivery_attempt_id)
        lock_fd = self._open_lock(self._root / ".command.lock")
        try:
            state = self._read_json(state_path)
            replay = self._replay_runtime_command(
                state, command_id, fingerprint, batch_id=batch_id
            )
            if replay is not None:
                return replay
            grant = self._reconciliation_grants.get(
                reconciler.reconciliation_owner
            )
            attempt = self._runtime_attempt(state, delivery_attempt_id)
            lease_value = attempt.get("reconciliation_lease_expires_at")
            if (
                reconciler.active is not True
                or grant is None
                or grant.active is not True
                or attempt.get("reconciliation_owner")
                != reconciler.reconciliation_owner
                or attempt.get("reconciliation_generation")
                != reconciliation_generation
                or not isinstance(lease_value, str)
                or datetime.fromisoformat(lease_value) <= now
            ):
                raise ReviewDeliveryConflictError("reconciliation_fence_stale")
            if attempt.get("outcome") != "delivery_unknown":
                raise ReviewDeliveryConflictError("delivery_result_conflict")
            if state.get("batch_status") == "blocked":
                raise ReviewDeliveryConflictError("delivery_result_conflict")
            if resolution == "unresolved":
                if observation_fingerprint is not None:
                    raise ReviewDeliveryConflictError("delivery_result_conflict")
                deadline_value = attempt.get("reconciliation_deadline")
                if not isinstance(deadline_value, str):
                    raise StorageConflictError("daily_feedback_runtime_invalid")
                if now >= datetime.fromisoformat(deadline_value):
                    state["batch_status"] = "blocked"
                    state["batch_revision"] = 2
                    attempt["reconciliation_blocked_at"] = now.isoformat()
                result = self._delivery_result_from_state(
                    state, attempt, status="applied"
                )
                self._record_runtime_result(
                    state, command_id, fingerprint, result
                )
                self._write_replace(state_path, state)
                return result
            if resolution != "found" or observation_fingerprint is None:
                raise ReviewDeliveryConflictError("delivery_result_conflict")
            observations = state.get("delivery_observations")
            observation = (
                observations.get(observation_fingerprint)
                if isinstance(observations, dict)
                else None
            )
            if (
                not isinstance(observation, dict)
                or observation.get("delivery_attempt_id") != delivery_attempt_id
                or observation.get("observed_result") != "accepted"
                or not observation.get("remote_reference")
            ):
                raise ReviewDeliveryConflictError("delivery_result_conflict")
            other_results = {
                value.get("observed_result")
                for value in observations.values()
                if isinstance(value, dict)
                and value.get("delivery_attempt_id") == delivery_attempt_id
            }
            if other_results != {"accepted"}:
                raise ReviewDeliveryConflictError("delivery_result_conflict")
            remote_references = {
                value.get("remote_reference")
                for value in observations.values()
                if isinstance(value, dict)
                and value.get("delivery_attempt_id") == delivery_attempt_id
                and value.get("observed_result") == "accepted"
            }
            if remote_references != {observation.get("remote_reference")}:
                raise ReviewDeliveryConflictError("delivery_result_conflict")
            items = self._runtime_items(state)
            matches = [
                item for item in items
                if item.get("snapshot_id") == attempt.get("snapshot_id")
            ]
            if len(matches) != 1 or matches[0].get("status") != "pending":
                raise ReviewDeliveryConflictError("delivery_projection_conflict")
            matches[0]["status"] = "presented"
            matches[0]["revision"] = 2
            attempt["outcome"] = "accepted"
            attempt["remote_reference"] = observation["remote_reference"]
            attempt["reconciled_from_observation"] = observation_fingerprint
            if state.get("batch_status") == "ready":
                state["batch_status"] = "in_review"
                state["batch_revision"] = 2
            result = self._delivery_result_from_state(
                state, attempt, status="applied"
            )
            self._record_runtime_result(state, command_id, fingerprint, result)
            self._write_replace(state_path, state)
            return result
        finally:
            os.close(lock_fd)

    def reconcile_review_delivery_not_applied(
        self,
        *,
        command_id: str,
        delivery_attempt_id: str,
        reconciler: ReconciliationPrincipal,
        reconciliation_generation: int,
        observation_fingerprint: str,
        reviewer: ReviewPrincipal,
        session_fence: int,
        retry_worker: WorkerPrincipal,
        retry_worker_lease_expires_at: datetime,
        now: datetime,
    ) -> DeliveryRetryResult:
        fingerprint = _hash_json(
            {
                "command_type": "reconcile_review_delivery_not_applied",
                "delivery_attempt_id": delivery_attempt_id,
                "reconciliation_owner": reconciler.reconciliation_owner,
                "reconciliation_generation": reconciliation_generation,
                "observation_fingerprint": observation_fingerprint,
                "reviewer_id": reviewer.reviewer_id,
                "reviewer_binding_id": reviewer.reviewer_binding_id,
                "session_owner": reviewer.session_owner,
                "session_fence": session_fence,
                "retry_worker_owner": retry_worker.worker_owner,
                "retry_worker_lease_generation": retry_worker.worker_lease_generation,
                "retry_worker_lease_expires_at": retry_worker_lease_expires_at.isoformat(),
            }
        )
        batch_id, state_path = self._find_runtime_attempt(delivery_attempt_id)
        lock_fd = self._open_lock(self._root / ".command.lock")
        try:
            state = self._read_json(state_path)
            prior = self._read_global_runtime_command(command_id, fingerprint)
            if prior is not None:
                envelope = prior.get("delivery_retry_result")
                if not isinstance(envelope, dict):
                    raise IdempotencyConflictError("idempotency_conflict")
                return self._delivery_retry_result(envelope, status="replayed")
            grant = self._reconciliation_grants.get(reconciler.reconciliation_owner)
            attempt = self._runtime_attempt(state, delivery_attempt_id)
            reconciliation_lease = attempt.get("reconciliation_lease_expires_at")
            if (
                now.tzinfo is None
                or now.utcoffset() != UTC.utcoffset(now)
                or reconciler.active is not True
                or grant is None
                or grant.active is not True
                or attempt.get("reconciliation_owner") != reconciler.reconciliation_owner
                or attempt.get("reconciliation_generation") != reconciliation_generation
                or not isinstance(reconciliation_lease, str)
                or datetime.fromisoformat(reconciliation_lease) <= now
            ):
                raise ReviewDeliveryConflictError("reconciliation_fence_stale")
            if state.get("batch_status") == "blocked" or attempt.get("outcome") != "delivery_unknown":
                raise ReviewDeliveryConflictError("delivery_result_conflict")
            observations = state.get("delivery_observations")
            observation = observations.get(observation_fingerprint) if isinstance(observations, dict) else None
            if (
                not isinstance(observation, dict)
                or observation.get("delivery_attempt_id") != delivery_attempt_id
                or observation.get("observed_result") != "not_applied"
                or not observation.get("remote_reference")
            ):
                raise ReviewDeliveryConflictError("delivery_result_conflict")
            effects = state.get("simulated_connector_effects")
            effect = (
                effects.get(delivery_attempt_id)
                if isinstance(effects, dict)
                else None
            )
            if (
                not isinstance(effect, dict)
                or effect.get("configured_result") != "delivery_unknown"
                or effect.get("configured_reconciliation_result") != "not_applied"
                or effect.get("reconciliation_reference")
                != observation.get("remote_reference")
            ):
                raise ReviewDeliveryConflictError("delivery_result_conflict")
            observed_results = {
                value.get("observed_result")
                for value in observations.values()
                if isinstance(value, dict)
                and value.get("delivery_attempt_id") == delivery_attempt_id
            }
            if observed_results != {"not_applied"}:
                raise ReviewDeliveryConflictError("delivery_result_conflict")
            self._validate_active_session(
                state, reviewer, session_fence=session_fence, now=now
            )
            if (
                attempt.get("reviewer_id") != reviewer.reviewer_id
                or attempt.get("reviewer_binding_id")
                != reviewer.reviewer_binding_id
            ):
                raise ReviewDeliveryConflictError("reviewer_authority_mismatch")
            try:
                prior_session_fence = int(attempt["session_fence"])
                prior_worker_generation = int(attempt["worker_lease_generation"])
            except (KeyError, TypeError, ValueError) as exc:
                raise StorageConflictError("daily_feedback_runtime_invalid") from exc
            if (
                reviewer.session_owner == attempt.get("session_owner")
                or session_fence <= prior_session_fence
                or retry_worker.worker_owner == attempt.get("worker_owner")
                or retry_worker.worker_lease_generation <= prior_worker_generation
            ):
                raise ReviewDeliveryConflictError(
                    "delivery_retry_authority_stale"
                )
            self._validate_worker(
                retry_worker,
                worker_lease_expires_at=retry_worker_lease_expires_at,
                now=now,
            )
            semantic_key = str(attempt["semantic_delivery_key"])
            attempts = state.get("delivery_attempts")
            if not isinstance(attempts, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            related = [
                value for value in attempts.values()
                if isinstance(value, dict)
                and value.get("semantic_delivery_key") == semantic_key
            ]
            if len(related) != 1 or int(related[0].get("attempt_number", 1)) != 1:
                raise ReviewDeliveryConflictError("delivery_retry_conflict")
            successor_number = 2
            successor_id = f"attempt_{_hash_json({'semantic_delivery_key': semantic_key, 'number': successor_number})}"
            successor = {
                "delivery_attempt_id": successor_id,
                "semantic_delivery_key": semantic_key,
                "attempt_number": successor_number,
                "previous_delivery_attempt_id": delivery_attempt_id,
                "batch_id": attempt["batch_id"],
                "snapshot_id": attempt["snapshot_id"],
                "payload_hash": attempt["payload_hash"],
                "reviewer_id": reviewer.reviewer_id,
                "reviewer_binding_id": reviewer.reviewer_binding_id,
                "session_owner": reviewer.session_owner,
                "session_fence": session_fence,
                "worker_owner": retry_worker.worker_owner,
                "worker_lease_generation": retry_worker.worker_lease_generation,
                "worker_lease_expires_at": retry_worker_lease_expires_at.isoformat(),
                "phase": "reserved",
                "outcome": None,
                "remote_reference": None,
            }
            attempt["outcome"] = "not_applied"
            attempt["reconciled_from_observation"] = observation_fingerprint
            attempts[successor_id] = successor
            envelope = {
                "delivery_attempt_id": successor_id,
                "previous_delivery_attempt_id": delivery_attempt_id,
                "semantic_delivery_key": semantic_key,
                "attempt_number": successor_number,
                "phase": "reserved",
            }
            commands = state.get("commands")
            if not isinstance(commands, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            commands[command_id] = {
                "fingerprint": fingerprint,
                "delivery_retry_result": envelope,
            }
            self._write_replace(state_path, state)
            return self._delivery_retry_result(envelope, status="applied")
        finally:
            os.close(lock_fd)

    def _delivery_retry_result(
        self, envelope: dict[str, object], *, status: str
    ) -> DeliveryRetryResult:
        return DeliveryRetryResult(
            status=status,
            delivery_attempt_id=str(envelope["delivery_attempt_id"]),
            previous_delivery_attempt_id=str(envelope["previous_delivery_attempt_id"]),
            semantic_delivery_key=str(envelope["semantic_delivery_key"]),
            attempt_number=int(envelope["attempt_number"]),
            phase=str(envelope["phase"]),
        )

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
            now.tzinfo is None
            or now.utcoffset() != UTC.utcoffset(now)
            or worker_lease_expires_at.tzinfo is None
            or worker_lease_expires_at.utcoffset()
            != UTC.utcoffset(worker_lease_expires_at)
            or worker.active is not True
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
                ("reviewed", 3),
                ("skipped", 3),
            }:
                raise StorageConflictError("daily_feedback_runtime_invalid")
        if observed != expected:
            raise StorageConflictError("daily_feedback_runtime_invalid")
        engaged_count = sum(
            item.get("status") in {"presented", "reviewed", "skipped"}
            for item in items
        )
        decisions_value = state.get("review_decisions", {})
        feedback_value = state.get("owner_feedback", {})
        if not isinstance(decisions_value, dict) or not isinstance(
            feedback_value, dict
        ):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        current_decisions: set[str] = set()
        current_feedback: set[str] = set()
        try:
            decision_sequences = sorted(
                int(decision["decision_sequence"])
                for decision in decisions_value.values()
                if isinstance(decision, dict)
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageConflictError("daily_feedback_runtime_invalid") from exc
        if decision_sequences != list(range(1, len(decisions_value) + 1)):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        for item in items:
            decision_id = item.get("current_decision_id")
            if item.get("status") in {"reviewed", "skipped"}:
                decision = (
                    decisions_value.get(decision_id)
                    if isinstance(decision_id, str)
                    else None
                )
                feedback_id = (
                    decision.get("owner_feedback_id")
                    if isinstance(decision, dict)
                    else None
                )
                feedback = (
                    feedback_value.get(feedback_id)
                    if isinstance(feedback_id, str)
                    else None
                )
                verbatim = (
                    feedback.get("verbatim_text")
                    if isinstance(feedback, dict)
                    else None
                )
                decision_type = (
                    decision.get("decision_type")
                    if isinstance(decision, dict)
                    else None
                )
                command_id = (
                    decision.get("command_id")
                    if isinstance(decision, dict)
                    else None
                )
                commands_value = state.get("commands")
                command = (
                    commands_value.get(command_id)
                    if isinstance(commands_value, dict)
                    and isinstance(command_id, str)
                    else None
                )
                recorded_at = (
                    decision.get("recorded_at")
                    if isinstance(decision, dict)
                    else None
                )
                try:
                    recorded_time = (
                        datetime.fromisoformat(recorded_at)
                        if isinstance(recorded_at, str)
                        else None
                    )
                except ValueError:
                    recorded_time = None
                expected_command_fingerprint = (
                    _hash_json(
                        {
                            "command_type": "record_review_decision",
                            "batch_id": decision.get("batch_id"),
                            "snapshot_id": decision.get("snapshot_id"),
                            "expected_item_revision": decision.get(
                                "expected_item_revision"
                            ),
                            "decision_type": decision_type,
                            "verbatim_feedback": verbatim,
                            "reviewer_id": decision.get("reviewer_id"),
                            "reviewer_binding_id": decision.get(
                                "reviewer_binding_id"
                            ),
                            "session_owner": decision.get("session_owner"),
                            "session_fence": decision.get("session_fence"),
                        }
                    )
                    if isinstance(decision, dict)
                    else None
                )
                decision_sequence = (
                    decision.get("decision_sequence")
                    if isinstance(decision, dict)
                    else None
                )
                historical_decisions = [
                    candidate
                    for candidate in decisions_value.values()
                    if isinstance(candidate, dict)
                    and isinstance(candidate.get("decision_sequence"), int)
                    and isinstance(decision_sequence, int)
                    and candidate["decision_sequence"] <= decision_sequence
                ]
                expected_command_result = {
                    "decision_id": decision_id,
                    "decision_type": decision_type,
                    "verbatim_feedback": verbatim,
                    "item_status": (
                        "skipped" if decision_type == "skip" else "reviewed"
                    ),
                    "item_revision": 3,
                    "batch_status": (
                        "completed"
                        if decision_sequence == len(items)
                        else "in_review"
                    ),
                    "batch_revision": 3 if decision_sequence == len(items) else 2,
                    "reviewed_count": sum(
                        candidate.get("decision_type") != "skip"
                        for candidate in historical_decisions
                    ),
                    "with_feedback_count": sum(
                        candidate.get("decision_type") == "correct_with_feedback"
                        for candidate in historical_decisions
                    ),
                    "skipped_count": sum(
                        candidate.get("decision_type") == "skip"
                        for candidate in historical_decisions
                    ),
                }
                valid_feedback = (
                    decision_type == "correct_with_feedback"
                    and isinstance(feedback_id, str)
                    and isinstance(feedback, dict)
                    and feedback.get("feedback_id") == feedback_id
                    and feedback.get("decision_id") == decision_id
                    and feedback.get("batch_id") == state.get("batch_id")
                    and feedback.get("snapshot_id") == item.get("snapshot_id")
                    and isinstance(verbatim, str)
                    and bool(verbatim.strip())
                    and len(verbatim.replace("\r\n", "\n").replace("\r", "\n"))
                    <= 4000
                    and feedback.get("content_hash")
                    == f"sha256:{_hash_text(verbatim)}"
                    and feedback.get("command_id") == command_id
                    and feedback.get("created_at") == recorded_at
                    and feedback_id
                    == f"feedback_{_hash_json({
                        'decision_id': decision_id,
                        'snapshot_id': item.get('snapshot_id'),
                        'verbatim_text': verbatim,
                        'reviewer_id': decision.get('reviewer_id'),
                        'reviewer_binding_id': decision.get('reviewer_binding_id'),
                    })}"
                )
                valid_without_feedback = (
                    decision_type in {"correct", "skip"}
                    and feedback_id is None
                    and feedback is None
                )
                if (
                    not isinstance(decision, dict)
                    or not isinstance(decision_id, str)
                    or decision_id in current_decisions
                    or decision.get("decision_id") != decision_id
                    or decision.get("batch_id") != state.get("batch_id")
                    or decision.get("snapshot_id") != item.get("snapshot_id")
                    or decision.get("verbatim_feedback") is not None
                    or not (valid_feedback or valid_without_feedback)
                    or not isinstance(command_id, str)
                    or not command_id
                    or not isinstance(command, dict)
                    or command.get("fingerprint") != expected_command_fingerprint
                    or command.get("review_decision_result")
                    != expected_command_result
                    or recorded_time is None
                    or recorded_time.tzinfo is None
                    or recorded_time.utcoffset() != UTC.utcoffset(recorded_time)
                    or (
                        item.get("status") == "skipped"
                        and decision_type != "skip"
                    )
                    or (
                        item.get("status") == "reviewed"
                        and decision_type == "skip"
                    )
                    or decision.get("expected_item_revision") != 2
                    or decision_id
                    != f"decision_{_hash_json({
                        'batch_id': decision.get('batch_id'),
                        'snapshot_id': decision.get('snapshot_id'),
                        'expected_item_revision': decision.get('expected_item_revision'),
                        'decision_type': decision_type,
                        'verbatim_feedback': verbatim,
                        'reviewer_id': decision.get('reviewer_id'),
                        'reviewer_binding_id': decision.get('reviewer_binding_id'),
                        'session_owner': decision.get('session_owner'),
                        'session_fence': decision.get('session_fence'),
                        'decision_sequence': decision_sequence,
                    })}"
                ):
                    raise StorageConflictError("daily_feedback_runtime_invalid")
                self._validate_committed_review_decision(
                    command_id=command_id,
                    decision_id=decision_id,
                    feedback_id=feedback_id,
                    decision=decision,
                    feedback=feedback,
                    result=expected_command_result,
                    state=state,
                )
                current_decisions.add(decision_id)
                if isinstance(feedback_id, str):
                    current_feedback.add(feedback_id)
            elif decision_id is not None:
                raise StorageConflictError("daily_feedback_runtime_invalid")
        if current_decisions != set(decisions_value) or current_feedback != set(
            feedback_value
        ):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        immutable_batch_decisions = {
            path.stem
            for path in self._review_decisions_dir.glob("*.json")
            if self._read_json(path).get("batch_id") == state.get("batch_id")
        }
        immutable_batch_feedback = {
            path.stem
            for path in self._owner_feedback_dir.glob("*.json")
            if self._read_json(path).get("batch_id") == state.get("batch_id")
        }
        intent_paths = list(self._review_decision_intents_dir.glob("*.json"))
        result_stems = {
            path.stem for path in self._review_decision_results_dir.glob("*.json")
        }
        commit_stems = {
            path.stem for path in self._review_decision_commits_dir.glob("*.json")
        }
        intent_stems = {path.stem for path in intent_paths}
        if intent_stems != result_stems or intent_stems != commit_stems:
            raise StorageConflictError("daily_feedback_runtime_invalid")
        expected_batch_commands = {
            str(decision["command_id"])
            for decision in decisions_value.values()
            if isinstance(decision, dict)
        }
        batch_intents: dict[str, dict[str, object]] = {}
        for path in intent_paths:
            intent = self._read_json(path)
            command_id = intent.get("command_id")
            if (
                not isinstance(command_id, str)
                or path.stem != _hash_text(command_id)
            ):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            if intent.get("batch_id") == state.get("batch_id"):
                if command_id in batch_intents:
                    raise StorageConflictError("daily_feedback_runtime_invalid")
                batch_intents[command_id] = intent
        if set(batch_intents) != expected_batch_commands:
            raise StorageConflictError("daily_feedback_runtime_invalid")
        for command_id, intent in batch_intents.items():
            decision_id = intent.get("decision_id")
            decision = (
                decisions_value.get(decision_id)
                if isinstance(decision_id, str)
                else None
            )
            if (
                not isinstance(decision, dict)
                or decision.get("command_id") != command_id
                or intent.get("feedback_id") != decision.get("owner_feedback_id")
            ):
                raise StorageConflictError("daily_feedback_runtime_invalid")
        if (
            immutable_batch_decisions != current_decisions
            or immutable_batch_feedback != current_feedback
        ):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        attempts_value = state.get("delivery_attempts", {})
        observations_value = state.get("delivery_observations", {})
        if not isinstance(attempts_value, dict) or not isinstance(
            observations_value, dict
        ):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        for observation_key, observation in observations_value.items():
            observation_payload = (
                {
                    "delivery_attempt_id": observation.get("delivery_attempt_id"),
                    "worker_owner": observation.get("worker_owner"),
                    "worker_lease_generation": observation.get(
                        "worker_lease_generation"
                    ),
                    "observed_result": observation.get("observed_result"),
                    "remote_reference": observation.get("remote_reference"),
                    "observed_at": observation.get("observed_at"),
                    "submitted_at": observation.get("submitted_at"),
                }
                if isinstance(observation, dict)
                else None
            )
            if (
                not isinstance(observation_key, str)
                or not isinstance(observation, dict)
                or observation.get("observation_fingerprint") != observation_key
                or observation_key
                != f"sha256:{_hash_json(observation_payload)}"
                or observation.get("delivery_attempt_id") not in attempts_value
                or observation.get("observed_result")
                not in {"accepted", "not_applied"}
                or not observation.get("remote_reference")
            ):
                raise StorageConflictError("daily_feedback_runtime_invalid")
        attempts_by_semantic_key: dict[str, list[dict[str, object]]] = {}
        for attempt_key, attempt in attempts_value.items():
            if not isinstance(attempt_key, str) or not isinstance(attempt, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            try:
                semantic_key = str(attempt["semantic_delivery_key"])
                attempt_number = int(attempt.get("attempt_number", 1))
            except (KeyError, TypeError, ValueError) as exc:
                raise StorageConflictError("daily_feedback_runtime_invalid") from exc
            expected_attempt_id = f"attempt_{_hash_json({'semantic_delivery_key': semantic_key, 'number': attempt_number})}"
            if (
                attempt_key != expected_attempt_id
                or attempt.get("delivery_attempt_id") != attempt_key
                or attempt_number not in {1, 2}
            ):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            attempts_by_semantic_key.setdefault(semantic_key, []).append(attempt)
        for related in attempts_by_semantic_key.values():
            ordered = sorted(related, key=lambda value: int(value.get("attempt_number", 1)))
            numbers = [int(value.get("attempt_number", 1)) for value in ordered]
            if numbers == [1]:
                predecessor = ordered[0].get("previous_delivery_attempt_id")
                if predecessor not in {None, ""}:
                    raise StorageConflictError("daily_feedback_runtime_invalid")
                continue
            if numbers != [1, 2]:
                raise StorageConflictError("daily_feedback_runtime_invalid")
            first, second = ordered
            immutable_fields = (
                "semantic_delivery_key", "batch_id", "snapshot_id", "payload_hash",
                "reviewer_id", "reviewer_binding_id",
            )
            observation_key = first.get("reconciled_from_observation")
            observation = (
                observations_value.get(observation_key)
                if isinstance(observation_key, str)
                else None
            )
            effects = state.get("simulated_connector_effects")
            predecessor_effect = (
                effects.get(first.get("delivery_attempt_id"))
                if isinstance(effects, dict)
                else None
            )
            if (
                second.get("previous_delivery_attempt_id")
                != first.get("delivery_attempt_id")
                or any(first.get(field) != second.get(field) for field in immutable_fields)
                or first.get("outcome") != "not_applied"
                or not isinstance(observation, dict)
                or observation.get("delivery_attempt_id")
                != first.get("delivery_attempt_id")
                or observation.get("observed_result") != "not_applied"
                or not isinstance(predecessor_effect, dict)
                or predecessor_effect.get("configured_result")
                != "delivery_unknown"
                or predecessor_effect.get("configured_reconciliation_result")
                != "not_applied"
                or predecessor_effect.get("reconciliation_reference")
                != observation.get("remote_reference")
            ):
                raise StorageConflictError("daily_feedback_runtime_invalid")
        effects_value = state.get("simulated_connector_effects", {})
        if not isinstance(effects_value, dict):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        for attempt_id, effect in effects_value.items():
            if not isinstance(effect, dict) or attempt_id not in attempts_value:
                raise StorageConflictError("daily_feedback_runtime_invalid")
            attempt = attempts_value[attempt_id]
            if not isinstance(attempt, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            configured_result = effect.get("configured_result")
            expected_reference = (
                f"simulated_{_hash_json({'attempt_id': attempt_id, 'result': configured_result})}"
            )
            if (
                configured_result not in {"accepted", "rejected", "delivery_unknown"}
                or effect.get("delivery_attempt_id") != attempt_id
                or effect.get("invocation_count") != 1
                or effect.get("remote_reference") != expected_reference
                or (
                    configured_result == "delivery_unknown"
                    and effect.get("configured_reconciliation_result")
                    not in {"accepted", "not_applied"}
                )
                or (
                    configured_result != "delivery_unknown"
                    and effect.get("configured_reconciliation_result") is not None
                )
                or effect.get("reconciliation_reference")
                != (
                    f"simulated_reconciliation_{_hash_json({'attempt_id': attempt_id, 'result': effect.get('configured_reconciliation_result')})}"
                    if effect.get("configured_reconciliation_result") is not None
                    else None
                )
            ):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            attempt_outcome = attempt.get("outcome")
            if attempt_outcome in {"accepted", "rejected", "delivery_unknown"} and (
                configured_result != attempt_outcome
                or effect.get("remote_reference")
                != attempt.get("remote_reference")
            ):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            if attempt_outcome == "cancelled_before_request":
                raise StorageConflictError("daily_feedback_runtime_invalid")
        try:
            batch_projection = (
                str(state["batch_status"]),
                int(state["batch_revision"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageConflictError("daily_feedback_runtime_invalid") from exc
        if batch_projection == ("ready", 1):
            if engaged_count != 0:
                raise StorageConflictError("daily_feedback_runtime_invalid")
        elif batch_projection == ("in_review", 2):
            if engaged_count == 0:
                raise StorageConflictError("daily_feedback_runtime_invalid")
        elif batch_projection == ("completed", 3):
            if engaged_count != len(items) or any(
                item.get("status") not in {"reviewed", "skipped"}
                for item in items
            ):
                raise StorageConflictError("daily_feedback_runtime_invalid")
        elif batch_projection == ("blocked", 2):
            blocked_attempts = []
            for attempt in attempts_value.values():
                if not isinstance(attempt, dict):
                    raise StorageConflictError("daily_feedback_runtime_invalid")
                if attempt.get("reconciliation_blocked_at") is not None:
                    deadline = attempt.get("reconciliation_deadline")
                    blocked_at = attempt.get("reconciliation_blocked_at")
                    if (
                        attempt.get("outcome") != "delivery_unknown"
                        or not isinstance(deadline, str)
                        or not isinstance(blocked_at, str)
                        or datetime.fromisoformat(blocked_at)
                        < datetime.fromisoformat(deadline)
                    ):
                        raise StorageConflictError("daily_feedback_runtime_invalid")
                    blocked_attempts.append(attempt)
            if engaged_count != 0 or len(blocked_attempts) != 1:
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

    def _decision_intent_path(self, command_id: str) -> Path:
        return self._review_decision_intents_dir / f"{_hash_text(command_id)}.json"

    def _decision_result_path(self, command_id: str) -> Path:
        return self._review_decision_results_dir / f"{_hash_text(command_id)}.json"

    def _decision_commit_path(self, command_id: str) -> Path:
        return self._review_decision_commits_dir / f"{_hash_text(command_id)}.json"

    def _write_decision_commit(self, intent: dict[str, object]) -> None:
        command_id = intent.get("command_id")
        if not isinstance(command_id, str):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        self._write_once(
            self._decision_commit_path(command_id),
            {
                "schema_version": 1,
                "command_id": command_id,
                "intent_hash": _hash_json(intent),
                "decision_hash": intent.get("decision_hash"),
                "feedback_hash": intent.get("feedback_hash"),
                "result_hash": intent.get("result_hash"),
                "runtime_after_hash": intent.get("runtime_after_hash"),
            },
        )

    def _recover_review_decision_intent(
        self,
        *,
        command_id: str,
        fingerprint: str,
        batch_id: str,
        state_path: Path,
        state: dict[str, object],
        committed_batch: ReviewBatch,
    ) -> dict[str, object] | None:
        intent_path = self._decision_intent_path(command_id)
        if not intent_path.exists():
            return None
        intent = self._read_json(intent_path)
        runtime_after = intent.get("runtime_after")
        if (
            intent.get("schema_version") != 1
            or intent.get("command_id") != command_id
            or intent.get("fingerprint") != fingerprint
            or intent.get("batch_id") != batch_id
            or not isinstance(runtime_after, dict)
            or intent.get("runtime_after_hash") != _hash_json(runtime_after)
        ):
            raise IdempotencyConflictError("idempotency_conflict")
        current_hash = _hash_json(state)
        if self._decision_commit_path(command_id).exists():
            self._validate_runtime_binding(state, committed_batch)
            result = self._read_json(self._decision_result_path(command_id))
            if _hash_json(result) != intent.get("result_hash"):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            commands = state.get("commands")
            prior = commands.get(command_id) if isinstance(commands, dict) else None
            if not isinstance(prior, dict):
                raise StorageConflictError("daily_feedback_runtime_invalid")
            self._write_global_runtime_command(
                command_id, fingerprint, batch_id, prior
            )
            return result
        if current_hash == intent.get("runtime_before_hash"):
            self._publish_review_decision_artifacts(intent, runtime_after)
            self._write_replace(state_path, runtime_after)
        elif current_hash != intent.get("runtime_after_hash"):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        self._publish_review_decision_artifacts(intent, runtime_after)
        self._write_decision_commit(intent)
        commands = runtime_after.get("commands")
        prior = commands.get(command_id) if isinstance(commands, dict) else None
        result = (
            prior.get("review_decision_result")
            if isinstance(prior, dict)
            else None
        )
        if not isinstance(prior, dict) or not isinstance(result, dict):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        self._write_global_runtime_command(command_id, fingerprint, batch_id, prior)
        return result

    def _publish_review_decision_artifacts(
        self,
        intent: dict[str, object],
        runtime_after: dict[str, object],
    ) -> None:
        decision_id = intent.get("decision_id")
        feedback_id = intent.get("feedback_id")
        command_id = intent.get("command_id")
        decisions = runtime_after.get("review_decisions")
        feedback_records = runtime_after.get("owner_feedback")
        commands = runtime_after.get("commands")
        decision = (
            decisions.get(decision_id)
            if isinstance(decisions, dict) and isinstance(decision_id, str)
            else None
        )
        command = (
            commands.get(command_id)
            if isinstance(commands, dict) and isinstance(command_id, str)
            else None
        )
        result = (
            command.get("review_decision_result")
            if isinstance(command, dict)
            else None
        )
        feedback = (
            feedback_records.get(feedback_id)
            if isinstance(feedback_records, dict) and isinstance(feedback_id, str)
            else None
        )
        if (
            not isinstance(decision_id, str)
            or not isinstance(command_id, str)
            or not isinstance(decision, dict)
            or not isinstance(result, dict)
            or _hash_json(decision) != intent.get("decision_hash")
            or _hash_json(result) != intent.get("result_hash")
            or (
                isinstance(feedback_id, str)
                and (
                    not isinstance(feedback, dict)
                    or _hash_json(feedback) != intent.get("feedback_hash")
                )
            )
            or (feedback_id is None and intent.get("feedback_hash") is not None)
        ):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        self._write_once(
            self._review_decisions_dir / f"{decision_id}.json", decision
        )
        if isinstance(feedback_id, str) and isinstance(feedback, dict):
            self._write_once(
                self._owner_feedback_dir / f"{feedback_id}.json", feedback
            )
        self._write_once(self._decision_result_path(command_id), result)

    def _validate_committed_review_decision(
        self,
        *,
        command_id: str,
        decision_id: str,
        feedback_id: object,
        decision: dict[str, object],
        feedback: object,
        result: dict[str, object],
        state: dict[str, object],
    ) -> None:
        intent_path = self._decision_intent_path(command_id)
        commit_path = self._decision_commit_path(command_id)
        decision_path = self._review_decisions_dir / f"{decision_id}.json"
        result_path = self._decision_result_path(command_id)
        if not all(
            path.exists()
            for path in (intent_path, commit_path, decision_path, result_path)
        ):
            raise StorageConflictError("daily_feedback_runtime_invalid")
        intent = self._read_json(intent_path)
        commit = self._read_json(commit_path)
        immutable_decision = self._read_json(decision_path)
        immutable_result = self._read_json(result_path)
        commands = state.get("commands")
        command = commands.get(command_id) if isinstance(commands, dict) else None
        fingerprint = (
            command.get("fingerprint") if isinstance(command, dict) else None
        )
        immutable_feedback = None
        if isinstance(feedback_id, str):
            feedback_path = self._owner_feedback_dir / f"{feedback_id}.json"
            if not feedback_path.exists():
                raise StorageConflictError("daily_feedback_runtime_invalid")
            immutable_feedback = self._read_json(feedback_path)
        if (
            intent.get("schema_version") != 1
            or intent.get("command_id") != command_id
            or intent.get("fingerprint") != fingerprint
            or intent.get("batch_id") != state.get("batch_id")
            or intent.get("decision_id") != decision_id
            or intent.get("feedback_id") != feedback_id
            or intent.get("decision_hash") != _hash_json(decision)
            or intent.get("feedback_hash")
            != (_hash_json(feedback) if isinstance(feedback, dict) else None)
            or intent.get("result_hash") != _hash_json(result)
            or intent.get("runtime_after_hash") != _hash_json(intent.get("runtime_after"))
            or immutable_decision != decision
            or immutable_result != result
            or immutable_feedback != feedback
            or commit
            != {
                "schema_version": 1,
                "command_id": command_id,
                "intent_hash": _hash_json(intent),
                "decision_hash": intent.get("decision_hash"),
                "feedback_hash": intent.get("feedback_hash"),
                "result_hash": intent.get("result_hash"),
                "runtime_after_hash": intent.get("runtime_after_hash"),
            }
        ):
            raise StorageConflictError("daily_feedback_runtime_invalid")

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
            result = envelope["result"]
            if isinstance(result, dict) and isinstance(
                result.get("review_decision_result"), dict
            ):
                batch_id = envelope.get("batch_id")
                if not isinstance(batch_id, str):
                    raise IdempotencyConflictError("idempotency_conflict")
                state_path = self._runtime_dir / f"{batch_id}.json"
                if not state_path.exists():
                    raise StorageConflictError("daily_feedback_runtime_invalid")
                state = self._read_json(state_path)
                batch = self._load_committed_batch(
                    self._find_committed_manifest(batch_id)
                )
                self._validate_runtime_binding(state, batch)
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
    reconciliation_grant: FixtureReconciliationGrant | None = None,
) -> FastAPI:
    """Create the fixture-only internal HTTP boundary for controlled verification."""
    if not operator_grant.token:
        raise ValueError("daily_feedback_operator_token_required")
    if reviewer_grant is not None and not reviewer_grant.token:
        raise ValueError("daily_feedback_reviewer_token_required")
    if reconciliation_grant is not None and not reconciliation_grant.token:
        raise ValueError("daily_feedback_reconciliation_token_required")
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

    def authorize_reconciler(
        authorization: Optional[str],
    ) -> ReconciliationPrincipal:
        if reconciliation_grant is None:
            raise HTTPException(
                status_code=404, detail="reconciliation_boundary_disabled"
            )
        expected = f"Bearer {reconciliation_grant.token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="invalid_reconciliation_token")
        if not reconciliation_grant.active:
            raise HTTPException(
                status_code=403, detail="reconciliation_binding_inactive"
            )
        return ReconciliationPrincipal(
            reconciliation_owner=reconciliation_grant.reconciliation_owner,
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

    @app.post("/internal/daily-feedback/review-decisions")
    async def record_review_decision_http(
        payload: dict[str, object],
        response: Response,
        authorization: str | None = Header(default=None),
    ) -> dict[str, object]:
        principal = authorize_reviewer(authorization)
        required = {
            "command_id", "batch_id", "snapshot_id", "expected_item_revision",
            "decision_type", "verbatim_feedback", "session_fence",
        }
        if (
            set(payload) != required
            or not all(
                isinstance(payload.get(field), str) and bool(payload[field])
                for field in (
                    "command_id", "batch_id", "snapshot_id", "decision_type"
                )
            )
            or type(payload.get("expected_item_revision")) is not int
            or type(payload.get("session_fence")) is not int
            or (
                payload.get("verbatim_feedback") is not None
                and not isinstance(payload.get("verbatim_feedback"), str)
            )
        ):
            raise HTTPException(
                status_code=422, detail="invalid_review_decision_payload"
            )
        try:
            result = store.record_review_decision(
                command_id=str(payload["command_id"]),
                batch_id=str(payload["batch_id"]),
                snapshot_id=str(payload["snapshot_id"]),
                expected_item_revision=int(payload["expected_item_revision"]),
                decision_type=str(payload["decision_type"]),
                verbatim_feedback=(
                    str(payload["verbatim_feedback"])
                    if payload["verbatim_feedback"] is not None
                    else None
                ),
                principal=principal,
                session_fence=int(payload["session_fence"]),
                now=datetime.now(UTC),
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ReviewDecisionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        response.status_code = 201 if result.status == "applied" else 200
        return {
            "status": result.status,
            "decision_id": result.decision_id,
            "decision_type": result.decision_type,
            "verbatim_feedback": result.verbatim_feedback,
            "item_status": result.item_status,
            "item_revision": result.item_revision,
            "batch_status": result.batch_status,
            "batch_revision": result.batch_revision,
            "reviewed_count": result.reviewed_count,
            "with_feedback_count": result.with_feedback_count,
            "skipped_count": result.skipped_count,
        }

    @app.post("/internal/daily-feedback/deliveries/reconciliation-claims")
    async def claim_delivery_reconciliation_http(
        payload: dict[str, object],
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        reconciler = authorize_reconciler(authorization)
        if set(payload) != {
            "command_id", "delivery_attempt_id", "lease_seconds"
        } or not all(
            (
                isinstance(payload.get("command_id"), str),
                isinstance(payload.get("delivery_attempt_id"), str),
                isinstance(payload.get("lease_seconds"), int),
            )
        ):
            raise HTTPException(
                status_code=422, detail="invalid_reconciliation_claim_payload"
            )
        try:
            result = store.claim_delivery_reconciliation(
                command_id=str(payload["command_id"]),
                delivery_attempt_id=str(payload["delivery_attempt_id"]),
                reconciler=reconciler,
                lease_seconds=int(payload["lease_seconds"]),
                now=datetime.now(UTC),
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ReviewDeliveryConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "status": result.status,
            "delivery_attempt_id": result.delivery_attempt_id,
            "reconciliation_owner": result.reconciliation_owner,
            "reconciliation_generation": result.reconciliation_generation,
            "lease_expires_at": result.lease_expires_at.isoformat(),
        }

    @app.post("/internal/daily-feedback/deliveries/reconcile")
    async def reconcile_review_delivery_http(
        payload: dict[str, object],
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, object]:
        reconciler = authorize_reconciler(authorization)
        required = {
            "command_id", "delivery_attempt_id", "reconciliation_generation",
            "resolution", "observation_fingerprint",
        }
        if set(payload) != required or not all(
            (
                isinstance(payload.get("command_id"), str),
                isinstance(payload.get("delivery_attempt_id"), str),
                isinstance(payload.get("reconciliation_generation"), int),
                isinstance(payload.get("resolution"), str),
                payload.get("observation_fingerprint") is None
                or isinstance(payload.get("observation_fingerprint"), str),
            )
        ):
            raise HTTPException(
                status_code=422, detail="invalid_reconciliation_payload"
            )
        try:
            result = store.reconcile_review_delivery(
                command_id=str(payload["command_id"]),
                delivery_attempt_id=str(payload["delivery_attempt_id"]),
                reconciler=reconciler,
                reconciliation_generation=int(
                    payload["reconciliation_generation"]
                ),
                resolution=str(payload["resolution"]),
                observation_fingerprint=(
                    str(payload["observation_fingerprint"])
                    if payload["observation_fingerprint"] is not None
                    else None
                ),
                now=datetime.now(UTC),
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ReviewDeliveryConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "status": result.status,
            "delivery_attempt_id": result.delivery_attempt_id,
            "phase": result.phase,
            "outcome": result.outcome,
            "item_status": result.item_status,
            "batch_status": result.batch_status,
            "batch_revision": result.batch_revision,
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
