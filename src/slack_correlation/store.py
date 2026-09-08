"""SQLite-backed durable notification ledger for one connector replica."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import BinaryIO

from slack_correlation.catalog import NotificationCommand

_TENANT_REF = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_MACHINE_FAILURE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,119}$")


class InstanceLockError(RuntimeError):
    pass


class NotificationCapacityError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdmissionResult:
    outcome: str
    notification_id: str
    state: str


@dataclass(frozen=True)
class NotificationClaim:
    tenant_ref: str
    notification_id: str
    command: NotificationCommand
    worker_id: str
    generation: int


@dataclass(frozen=True)
class StoredNotification:
    tenant_ref: str
    notification_id: str
    event_code: str
    state: str
    failure_code: str | None
    channel_id: str | None
    message_ts: str | None
    thread_ts: str | None


class NotificationStore:
    """Durable single-file ledger; callers must deploy one service replica."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._instance_lock_file: BinaryIO | None = None

    def acquire_instance_lock(self) -> None:
        if self._instance_lock_file is not None:
            return
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".instance.lock")
        lock_file = lock_path.open("a+b")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise InstanceLockError("connector_instance_already_running") from exc
        self._instance_lock_file = lock_file

    def release_instance_lock(self) -> None:
        lock_file = self._instance_lock_file
        if lock_file is None:
            return
        self._instance_lock_file = None
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._path.parent, 0o700)
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1}:
                raise RuntimeError("unsupported_store_version")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    tenant_ref TEXT NOT NULL,
                    notification_id TEXT NOT NULL,
                    event_code TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'pending', 'claimed', 'request_started',
                            'accepted', 'rejected', 'delivery_unknown'
                        )
                    ),
                    claim_owner TEXT,
                    claim_generation INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    request_started_at TEXT,
                    channel_id TEXT,
                    message_ts TEXT,
                    thread_ts TEXT,
                    failure_code TEXT,
                    PRIMARY KEY (tenant_ref, notification_id),
                    UNIQUE (tenant_ref, event_code, dedupe_key)
                );
                CREATE TABLE IF NOT EXISTS thread_roots (
                    tenant_ref TEXT NOT NULL,
                    subject_ref TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_ts TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_ref, subject_ref)
                );
                CREATE TABLE IF NOT EXISTS connector_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    last_write_probe_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO connector_meta (singleton, last_write_probe_at)
                VALUES (1, ?)
                """,
                (datetime.now(UTC).isoformat(),),
            )
            if version == 0:
                connection.execute("PRAGMA user_version = 1")
        os.chmod(self._path, 0o600)

    def probe(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE connector_meta
                SET last_write_probe_at = ?
                WHERE singleton = 1
                """,
                (datetime.now(UTC).isoformat(),),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("store_probe_failed")
            connection.commit()

    def admit(
        self,
        *,
        tenant_ref: str,
        command: NotificationCommand,
        max_nonterminal: int = 10_000,
    ) -> AdmissionResult:
        if _TENANT_REF.fullmatch(tenant_ref) is None:
            raise ValueError("invalid_tenant_ref")
        if not 1 <= max_nonterminal <= 100_000:
            raise ValueError("invalid_notification_capacity")
        payload_json = _serialize_command(command)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT notification_id, event_code, dedupe_key, payload_sha256, state
                FROM notifications
                WHERE tenant_ref = ?
                  AND (
                    notification_id = ?
                    OR (event_code = ? AND dedupe_key = ?)
                  )
                """,
                (
                    tenant_ref,
                    command.event_id,
                    command.event_code,
                    command.dedupe_key,
                ),
            ).fetchall()
            if rows:
                row = rows[0]
                exact = (
                    len(rows) == 1
                    and row["notification_id"] == command.event_id
                    and row["event_code"] == command.event_code
                    and row["dedupe_key"] == command.dedupe_key
                    and row["payload_sha256"] == payload_sha256
                )
                connection.commit()
                return AdmissionResult(
                    outcome="duplicate" if exact else "semantic_conflict",
                    notification_id=str(row["notification_id"]),
                    state=str(row["state"]),
                )
            capacity = connection.execute(
                """
                SELECT count(*) AS count
                FROM notifications
                WHERE state IN ('pending', 'claimed', 'request_started')
                """
            ).fetchone()
            assert capacity is not None
            if int(capacity["count"]) >= max_nonterminal:
                connection.rollback()
                raise NotificationCapacityError("notification_capacity_exhausted")
            connection.execute(
                """
                INSERT INTO notifications (
                    tenant_ref, notification_id, event_code, dedupe_key,
                    payload_json, payload_sha256, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    tenant_ref,
                    command.event_id,
                    command.event_code,
                    command.dedupe_key,
                    payload_json,
                    payload_sha256,
                    now,
                    now,
                ),
            )
            connection.commit()
        return AdmissionResult(
            outcome="admitted",
            notification_id=command.event_id,
            state="pending",
        )

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT count(*) AS count FROM notifications").fetchone()
        assert row is not None
        return int(row["count"])

    def claim_next(self, *, worker_id: str) -> NotificationClaim | None:
        if _TENANT_REF.fullmatch(worker_id) is None:
            raise ValueError("invalid_worker_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT tenant_ref, notification_id, payload_json, claim_generation
                FROM notifications
                WHERE state = 'pending'
                ORDER BY created_at, tenant_ref, notification_id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            generation = int(row["claim_generation"]) + 1
            updated = connection.execute(
                """
                UPDATE notifications
                SET state = 'claimed', claim_owner = ?, claim_generation = ?, updated_at = ?
                WHERE tenant_ref = ? AND notification_id = ? AND state = 'pending'
                """,
                (
                    worker_id,
                    generation,
                    datetime.now(UTC).isoformat(),
                    row["tenant_ref"],
                    row["notification_id"],
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("notification_claim_conflict")
            connection.commit()
        return NotificationClaim(
            tenant_ref=str(row["tenant_ref"]),
            notification_id=str(row["notification_id"]),
            command=_deserialize_command(str(row["payload_json"])),
            worker_id=worker_id,
            generation=generation,
        )

    def mark_request_started(self, claim: NotificationClaim) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE notifications
                SET state = 'request_started', request_started_at = ?, updated_at = ?
                WHERE tenant_ref = ? AND notification_id = ?
                  AND state = 'claimed' AND claim_owner = ? AND claim_generation = ?
                """,
                (
                    now,
                    now,
                    claim.tenant_ref,
                    claim.notification_id,
                    claim.worker_id,
                    claim.generation,
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("notification_claim_lost")
            connection.commit()

    def resolve_thread_ts(self, claim: NotificationClaim) -> str | None:
        subject_ref = claim.command.subject_ref
        if subject_ref is None:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT message_ts
                FROM thread_roots
                WHERE tenant_ref = ? AND subject_ref = ?
                """,
                (claim.tenant_ref, subject_ref),
            ).fetchone()
        return str(row["message_ts"]) if row is not None else None

    def finalize_accepted(
        self,
        claim: NotificationClaim,
        *,
        channel_id: str,
        message_ts: str,
        thread_ts: str | None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        subject_ref = claim.command.subject_ref
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if subject_ref is None and thread_ts is not None:
                connection.rollback()
                raise RuntimeError("thread_without_subject")
            if subject_ref is not None:
                root = connection.execute(
                    """
                    SELECT channel_id, message_ts
                    FROM thread_roots
                    WHERE tenant_ref = ? AND subject_ref = ?
                    """,
                    (claim.tenant_ref, subject_ref),
                ).fetchone()
                if root is None:
                    if thread_ts is not None:
                        connection.rollback()
                        raise RuntimeError("missing_thread_root")
                    connection.execute(
                        """
                        INSERT INTO thread_roots (
                            tenant_ref, subject_ref, channel_id, message_ts, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            claim.tenant_ref,
                            subject_ref,
                            channel_id,
                            message_ts,
                            now,
                        ),
                    )
                elif (
                    thread_ts != root["message_ts"]
                    or channel_id != root["channel_id"]
                ):
                    connection.rollback()
                    raise RuntimeError("thread_identity_mismatch")
            updated = connection.execute(
                """
                UPDATE notifications
                SET state = 'accepted', channel_id = ?, message_ts = ?, thread_ts = ?,
                    failure_code = NULL, claim_owner = NULL, updated_at = ?
                WHERE tenant_ref = ? AND notification_id = ?
                  AND state = 'request_started'
                  AND claim_owner = ? AND claim_generation = ?
                """,
                (
                    channel_id,
                    message_ts,
                    thread_ts,
                    now,
                    claim.tenant_ref,
                    claim.notification_id,
                    claim.worker_id,
                    claim.generation,
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("notification_claim_lost")
            connection.commit()

    def finalize_delivery_unknown(
        self,
        claim: NotificationClaim,
        *,
        failure_code: str,
    ) -> None:
        if _MACHINE_FAILURE.fullmatch(failure_code) is None:
            raise ValueError("invalid_failure_code")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE notifications
                SET state = 'delivery_unknown', failure_code = ?,
                    claim_owner = NULL, updated_at = ?
                WHERE tenant_ref = ? AND notification_id = ?
                  AND state = 'request_started'
                  AND claim_owner = ? AND claim_generation = ?
                """,
                (
                    failure_code,
                    datetime.now(UTC).isoformat(),
                    claim.tenant_ref,
                    claim.notification_id,
                    claim.worker_id,
                    claim.generation,
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("notification_claim_lost")
            connection.commit()

    def release_claim(self, claim: NotificationClaim) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE notifications
                SET state = 'pending', claim_owner = NULL, updated_at = ?
                WHERE tenant_ref = ? AND notification_id = ?
                  AND state = 'claimed'
                  AND claim_owner = ? AND claim_generation = ?
                """,
                (
                    datetime.now(UTC).isoformat(),
                    claim.tenant_ref,
                    claim.notification_id,
                    claim.worker_id,
                    claim.generation,
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("notification_claim_lost")
            connection.commit()

    def finalize_rejected(
        self,
        claim: NotificationClaim,
        *,
        failure_code: str,
    ) -> None:
        if _MACHINE_FAILURE.fullmatch(failure_code) is None:
            raise ValueError("invalid_failure_code")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE notifications
                SET state = 'rejected', failure_code = ?,
                    claim_owner = NULL, updated_at = ?
                WHERE tenant_ref = ? AND notification_id = ?
                  AND state = 'request_started'
                  AND claim_owner = ? AND claim_generation = ?
                """,
                (
                    failure_code,
                    datetime.now(UTC).isoformat(),
                    claim.tenant_ref,
                    claim.notification_id,
                    claim.worker_id,
                    claim.generation,
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("notification_claim_lost")
            connection.commit()

    def recover_incomplete(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE notifications
                SET state = 'delivery_unknown',
                    failure_code = 'process_interrupted_after_request_start',
                    claim_owner = NULL,
                    updated_at = ?
                WHERE state = 'request_started'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE notifications
                SET state = 'pending', claim_owner = NULL, updated_at = ?
                WHERE state = 'claimed'
                """,
                (now,),
            )
            connection.commit()

    def get(
        self,
        *,
        tenant_ref: str,
        notification_id: str,
    ) -> StoredNotification | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT tenant_ref, notification_id, event_code, state, failure_code,
                       channel_id, message_ts, thread_ts
                FROM notifications
                WHERE tenant_ref = ? AND notification_id = ?
                """,
                (tenant_ref, notification_id),
            ).fetchone()
        if row is None:
            return None
        return StoredNotification(
            tenant_ref=str(row["tenant_ref"]),
            notification_id=str(row["notification_id"]),
            event_code=str(row["event_code"]),
            state=str(row["state"]),
            failure_code=(
                str(row["failure_code"]) if row["failure_code"] is not None else None
            ),
            channel_id=(str(row["channel_id"]) if row["channel_id"] is not None else None),
            message_ts=(str(row["message_ts"]) if row["message_ts"] is not None else None),
            thread_ts=(str(row["thread_ts"]) if row["thread_ts"] is not None else None),
        )


def _serialize_command(command: NotificationCommand) -> str:
    payload = asdict(command)
    payload["occurred_at"] = command.occurred_at.astimezone(UTC).isoformat()
    if command.deadline_at is not None:
        payload["deadline_at"] = command.deadline_at.astimezone(UTC).isoformat()
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _deserialize_command(payload_json: str) -> NotificationCommand:
    payload = json.loads(payload_json)
    payload["occurred_at"] = datetime.fromisoformat(payload["occurred_at"])
    if payload["deadline_at"] is not None:
        payload["deadline_at"] = datetime.fromisoformat(payload["deadline_at"])
    return NotificationCommand(**payload)
