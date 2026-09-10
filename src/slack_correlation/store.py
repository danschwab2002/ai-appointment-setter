"""SQLite-backed durable notification ledger for one connector replica."""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import BinaryIO
from uuid import UUID, uuid4

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
    channel_id: str | None
    team_id: str | None
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


@dataclass(frozen=True)
class CorrelationBinding:
    tenant_ref: str
    notification_id: str
    case_id: str
    channel_id: str
    message_ts: str
    review_due_at: str


@dataclass(frozen=True)
class ReviewSession:
    review_token: str
    tenant_ref: str
    case_id: str
    team_id: str
    channel_id: str
    message_ts: str
    slack_user_id: str
    expires_at: int
    state: str
    action: str | None
    candidate_id: str | None
    verification_basis: str | None
    idempotency_key: str | None
    prepared_command: dict[str, object] | None
    result: dict[str, object] | None
    view_id: str | None
    view_hash: str | None


@dataclass(frozen=True)
class OpeningJob:
    review_token: str
    trigger_id: str
    state: str


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
        with closing(self._connect()) as connection, connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1, 2, 3, 4, 5, 6}:
                raise RuntimeError("unsupported_store_version")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            _execute_statements(
                connection,
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
                CREATE TABLE IF NOT EXISTS reconciliation_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_ref TEXT NOT NULL,
                    notification_id TEXT NOT NULL,
                    decision TEXT NOT NULL CHECK (
                        decision IN ('confirm_delivered', 'confirm_not_delivered')
                    ),
                    operator_id TEXT NOT NULL,
                    prior_state TEXT NOT NULL,
                    resulting_state TEXT NOT NULL,
                    channel_id TEXT,
                    message_ts TEXT,
                    thread_ts TEXT,
                    decided_at TEXT NOT NULL,
                    FOREIGN KEY (tenant_ref, notification_id)
                        REFERENCES notifications (tenant_ref, notification_id)
                );
                CREATE TABLE IF NOT EXISTS interaction_replays (
                    fingerprint TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK (state IN ('request_started', 'responded')),
                    response_status INTEGER,
                    response_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS correlation_review_sessions (
                    review_token TEXT PRIMARY KEY,
                    tenant_ref TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_ts TEXT NOT NULL,
                    slack_user_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('opened','preparing','prepared','confirming','resolved','failed')),
                    action TEXT,
                    candidate_id TEXT,
                    verification_basis TEXT,
                    idempotency_key TEXT UNIQUE,
                    prepared_command_json TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS correlation_opening_jobs (
                    review_token TEXT PRIMARY KEY,
                    trigger_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('pending','request_started','completed','failed')
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (review_token)
                        REFERENCES correlation_review_sessions (review_token)
                );
                CREATE TABLE IF NOT EXISTS correlation_projections (
                    tenant_ref TEXT NOT NULL,
                    notification_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    team_id TEXT,
                    channel_id TEXT NOT NULL,
                    message_ts TEXT NOT NULL,
                    review_due_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending','claimed','request_started','accepted','rejected','delivery_unknown')),
                    failure_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_ref, notification_id),
                    UNIQUE (team_id, channel_id, message_ts),
                    FOREIGN KEY (tenant_ref, notification_id)
                        REFERENCES notifications (tenant_ref, notification_id)
                );
                """
            )
            notification_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(notifications)")
            }
            if "activation_generation_started" not in notification_columns:
                connection.execute(
                    "ALTER TABLE notifications ADD COLUMN activation_generation_started INTEGER"
                )
            if "team_id" not in notification_columns:
                connection.execute("ALTER TABLE notifications ADD COLUMN team_id TEXT")
            meta_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(connector_meta)")
            }
            for column, definition in (
                ("activation_initialized", "INTEGER NOT NULL DEFAULT 0"),
                ("activation_mode", "TEXT NOT NULL DEFAULT 'inactive'"),
                ("activation_generation", "INTEGER NOT NULL DEFAULT 0"),
                ("activation_budget", "INTEGER"),
                ("activation_consumed", "INTEGER NOT NULL DEFAULT 0"),
                ("activation_verified", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in meta_columns:
                    connection.execute(
                        f"ALTER TABLE connector_meta ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                """
                INSERT OR IGNORE INTO connector_meta (singleton, last_write_probe_at)
                VALUES (1, ?)
                """,
                (datetime.now(UTC).isoformat(),),
            )
            now = datetime.now(UTC).isoformat()
            for row in connection.execute(
                """SELECT tenant_ref, notification_id, payload_json, channel_id, message_ts
                   FROM notifications
                   WHERE state='accepted' AND event_code IN ('COR-001','COR-002','COR-003')
                     AND thread_ts IS NULL"""
            ):
                try:
                    command = _deserialize_command(str(row["payload_json"]))
                    if command.subject_ref is None:
                        continue
                    case_id = str(UUID(command.subject_ref[2:]))
                    channel_id = str(row["channel_id"])
                    message_ts = str(row["message_ts"])
                    if re.fullmatch(r"C[A-Z0-9]{8,}", channel_id) is None or re.fullmatch(r"[0-9]{10,16}\.[0-9]{6}", message_ts) is None:
                        continue
                    review_due_at = (command.deadline_at or command.occurred_at).astimezone(UTC).isoformat().replace("+00:00", "Z")
                except (ValueError, KeyError, TypeError):
                    continue
                connection.execute(
                    """INSERT OR IGNORE INTO correlation_projections
                       (tenant_ref, notification_id, case_id, team_id, channel_id, message_ts,
                        review_due_at, state, created_at, updated_at)
                       VALUES (?, ?, ?, NULL, ?, ?, ?, 'pending', ?, ?)""",
                    (row["tenant_ref"], row["notification_id"], case_id, channel_id,
                     message_ts, review_due_at, now, now),
                )
            session_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(correlation_review_sessions)"
                )
            }
            if "view_id" not in session_columns:
                connection.execute(
                    "ALTER TABLE correlation_review_sessions ADD COLUMN view_id TEXT"
                )
            if "view_hash" not in session_columns:
                connection.execute(
                    "ALTER TABLE correlation_review_sessions ADD COLUMN view_hash TEXT"
                )
            connection.execute(
                """INSERT OR IGNORE INTO correlation_opening_jobs
                   (review_token, trigger_id, state, created_at, updated_at)
                   SELECT review_token, '',
                          CASE WHEN state='opened' THEN 'failed' ELSE 'completed' END,
                          created_at, updated_at
                   FROM correlation_review_sessions"""
            )
            connection.execute(
                """UPDATE correlation_review_sessions SET state='failed'
                   WHERE state='opened' AND review_token IN (
                       SELECT review_token FROM correlation_opening_jobs
                       WHERE state='failed'
                   )"""
            )
            projection_sql = str(connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='correlation_projections'"
            ).fetchone()[0])
            if "'claimed'" not in projection_sql:
                _execute_statements(
                    connection,
                    """
                    ALTER TABLE correlation_projections RENAME TO correlation_projections_v3;
                    CREATE TABLE correlation_projections (
                        tenant_ref TEXT NOT NULL,
                        notification_id TEXT NOT NULL,
                        case_id TEXT NOT NULL,
                        team_id TEXT,
                        channel_id TEXT NOT NULL,
                        message_ts TEXT NOT NULL,
                        review_due_at TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('pending','claimed','request_started','accepted','rejected','delivery_unknown')),
                        failure_code TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (tenant_ref, notification_id),
                        UNIQUE (team_id, channel_id, message_ts),
                        FOREIGN KEY (tenant_ref, notification_id)
                            REFERENCES notifications (tenant_ref, notification_id)
                    );
                    INSERT INTO correlation_projections SELECT * FROM correlation_projections_v3;
                    DROP TABLE correlation_projections_v3;
                    """
                )
            if version < 6:
                connection.execute("PRAGMA user_version = 6")
        os.chmod(self._path, 0o600)

    def bind_legacy_team(
        self, *, team_id: str, tenant_channels: dict[str, str]
    ) -> None:
        """Bind pre-team-schema rows only through exact trusted tenant routes."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            unbound = connection.execute(
                """SELECT tenant_ref, channel_id FROM notifications
                   WHERE team_id IS NULL"""
            ).fetchall()
            if any(
                row["channel_id"] is None
                or tenant_channels.get(str(row["tenant_ref"])) != str(row["channel_id"])
                for row in unbound
            ):
                connection.rollback()
                raise RuntimeError("legacy_team_binding_mismatch")
            connection.execute(
                "UPDATE notifications SET team_id = ? WHERE team_id IS NULL",
                (team_id,),
            )
            projection_rows = connection.execute(
                """SELECT tenant_ref, channel_id FROM correlation_projections
                   WHERE team_id IS NULL"""
            ).fetchall()
            if any(
                tenant_channels.get(str(row["tenant_ref"])) != str(row["channel_id"])
                for row in projection_rows
            ):
                connection.rollback()
                raise RuntimeError("legacy_team_binding_mismatch")
            connection.execute(
                "UPDATE correlation_projections SET team_id = ? WHERE team_id IS NULL",
                (team_id,),
            )
            connection.commit()

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

    def state_inventory(self) -> dict[str, int]:
        states = ("pending", "claimed", "request_started", "delivery_unknown")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT state, count(*) AS count
                FROM notifications
                WHERE state IN ('pending', 'claimed', 'request_started', 'delivery_unknown')
                GROUP BY state
                """
            ).fetchall()
        found = {str(row["state"]): int(row["count"]) for row in rows}
        return {state: found.get(state, 0) for state in states}

    def activation_status(self) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT activation_mode, activation_generation, activation_budget,
                       activation_consumed, activation_verified
                FROM connector_meta WHERE singleton = 1
                """
            ).fetchone()
        if row is None:
            raise RuntimeError("store_not_initialized")
        return {
            "mode": str(row["activation_mode"]),
            "generation": int(row["activation_generation"]),
            "budget": int(row["activation_budget"]) if row["activation_budget"] is not None else None,
            "consumed": int(row["activation_consumed"]),
            "verified": bool(row["activation_verified"]),
        }

    def configure_activation(self, *, mode: str, generation: int) -> None:
        if mode not in {"inactive", "one_shot", "continuous"}:
            raise ValueError("invalid_activation_mode")
        if isinstance(generation, bool) or generation < 0 or (mode != "inactive" and generation < 1):
            raise ValueError("invalid_activation_generation")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM connector_meta WHERE singleton = 1").fetchone()
            if row is None:
                connection.rollback()
                raise RuntimeError("store_not_initialized")
            initialized = bool(row["activation_initialized"])
            current_generation = int(row["activation_generation"])
            current_mode = str(row["activation_mode"])
            if initialized and generation < current_generation:
                connection.rollback()
                raise RuntimeError("activation_generation_regression")
            if initialized and generation == current_generation:
                if mode != current_mode:
                    connection.rollback()
                    raise RuntimeError("activation_generation_conflict")
                connection.commit()
                return
            unresolved = connection.execute(
                """
                SELECT count(*) FROM notifications
                WHERE state IN ('claimed', 'request_started', 'delivery_unknown')
                """
            ).fetchone()
            assert unresolved is not None
            if int(unresolved[0]) != 0:
                connection.rollback()
                raise RuntimeError("unresolved_delivery_blocks_activation")
            if mode == "continuous" and (
                not initialized
                or current_mode != "one_shot"
                or not bool(row["activation_verified"])
            ):
                connection.rollback()
                raise RuntimeError("activation_verification_required")
            connection.execute(
                """
                UPDATE connector_meta
                SET activation_initialized = 1, activation_mode = ?,
                    activation_generation = ?, activation_budget = ?,
                    activation_consumed = 0, activation_verified = 0
                WHERE singleton = 1
                """,
                (mode, generation, 1 if mode == "one_shot" else None),
            )
            connection.commit()

    def mark_activation_verified(self, *, generation: int, operator_id: str) -> None:
        if _TENANT_REF.fullmatch(operator_id) is None:
            raise ValueError("invalid_operator_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE connector_meta SET activation_verified = 1
                WHERE singleton = 1 AND activation_initialized = 1
                  AND activation_mode = 'one_shot'
                  AND activation_generation = ? AND activation_consumed = 1
                  AND EXISTS (
                    SELECT 1 FROM notifications
                    WHERE activation_generation_started = ? AND state = 'accepted'
                  )
                """,
                (generation, generation),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("activation_not_verifiable")
            connection.commit()

    def admit(
        self,
        *,
        tenant_ref: str,
        command: NotificationCommand,
        team_id: str | None = None,
        channel_id: str | None = None,
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
                SELECT notification_id, event_code, dedupe_key, payload_sha256,
                       team_id, channel_id, state
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
                    and row["team_id"] == team_id
                    and row["channel_id"] == channel_id
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
                WHERE state IN (
                    'pending', 'claimed', 'request_started', 'delivery_unknown'
                )
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
                    payload_json, payload_sha256, state, team_id, channel_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    tenant_ref,
                    command.event_id,
                    command.event_code,
                    command.dedupe_key,
                    payload_json,
                    payload_sha256,
                    team_id,
                    channel_id,
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
            activation = connection.execute(
                "SELECT * FROM connector_meta WHERE singleton = 1"
            ).fetchone()
            if activation is not None and bool(activation["activation_initialized"]):
                budget = activation["activation_budget"]
                if str(activation["activation_mode"]) == "inactive" or (
                    budget is not None
                    and int(activation["activation_consumed"]) >= int(budget)
                ):
                    connection.commit()
                    return None
            row = connection.execute(
                """
                SELECT tenant_ref, notification_id, payload_json, team_id, channel_id,
                       claim_generation
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
            channel_id=(
                str(row["channel_id"]) if row["channel_id"] is not None else None
            ),
            team_id=(str(row["team_id"]) if row["team_id"] is not None else None),
            worker_id=worker_id,
            generation=generation,
        )

    def mark_request_started(self, claim: NotificationClaim) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            activation = connection.execute(
                "SELECT * FROM connector_meta WHERE singleton = 1"
            ).fetchone()
            activation_generation: int | None = None
            if activation is not None and bool(activation["activation_initialized"]):
                budget = activation["activation_budget"]
                if str(activation["activation_mode"]) == "inactive" or (
                    budget is not None
                    and int(activation["activation_consumed"]) >= int(budget)
                ):
                    connection.rollback()
                    raise RuntimeError("activation_budget_exhausted")
                activation_generation = int(activation["activation_generation"])
                connection.execute(
                    """
                    UPDATE connector_meta
                    SET activation_consumed = activation_consumed + 1
                    WHERE singleton = 1
                    """
                )
            updated = connection.execute(
                """
                UPDATE notifications
                SET state = 'request_started', request_started_at = ?, updated_at = ?,
                    activation_generation_started = ?
                WHERE tenant_ref = ? AND notification_id = ?
                  AND state = 'claimed' AND claim_owner = ? AND claim_generation = ?
                """,
                (
                    now,
                    now,
                    activation_generation,
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
        team_id: str | None = None,
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
            if (
                claim.command.event_code in {"COR-001", "COR-002", "COR-003"}
                and thread_ts is None
                and claim.command.subject_ref is not None
            ):
                if team_id is None or re.fullmatch(r"T[A-Z0-9]{8,}", team_id) is None:
                    connection.rollback()
                    raise RuntimeError("correlation_team_id_required")
                try:
                    case_id = str(UUID(claim.command.subject_ref[2:]))
                except (ValueError, IndexError) as exc:
                    connection.rollback()
                    raise RuntimeError("invalid_correlation_subject") from exc
                review_due_at = (
                    claim.command.deadline_at or claim.command.occurred_at
                ).astimezone(UTC).isoformat().replace("+00:00", "Z")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO correlation_projections (
                        tenant_ref, notification_id, case_id, team_id, channel_id, message_ts,
                        review_due_at, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        claim.tenant_ref, claim.notification_id, case_id, team_id, channel_id,
                        message_ts, review_due_at, now, now,
                    ),
                )
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

    def find_correlation_binding(
        self, *, tenant_ref: str, team_id: str, channel_id: str, message_ts: str
    ) -> CorrelationBinding | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT p.* FROM correlation_projections p
                JOIN notifications n USING (tenant_ref, notification_id)
                WHERE p.tenant_ref = ? AND p.team_id = ?
                  AND p.channel_id = ? AND p.message_ts = ?
                  AND n.state = 'accepted'
                """,
                (tenant_ref, team_id, channel_id, message_ts),
            ).fetchone()
        if row is None:
            return None
        return CorrelationBinding(
            tenant_ref=str(row["tenant_ref"]), notification_id=str(row["notification_id"]),
            case_id=str(row["case_id"]), channel_id=str(row["channel_id"]),
            message_ts=str(row["message_ts"]), review_due_at=str(row["review_due_at"]),
        )

    @staticmethod
    def _prior_interaction(
        connection: sqlite3.Connection, fingerprint: str
    ) -> tuple[bool, int | None, dict[str, object] | None]:
        if re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None:
            raise ValueError("invalid_fingerprint")
        row = connection.execute(
            "SELECT response_status, response_json FROM interaction_replays WHERE fingerprint=?",
            (fingerprint,),
        ).fetchone()
        if row is None:
            return True, None, None
        response = json.loads(row["response_json"]) if row["response_json"] is not None else None
        return False, row["response_status"], response

    @staticmethod
    def _insert_completed_interaction(
        connection: sqlite3.Connection,
        *,
        fingerprint: str,
        status: int,
        response: dict[str, object],
        now: str,
    ) -> None:
        if int(connection.execute(
            "SELECT count(*) FROM interaction_replays"
        ).fetchone()[0]) >= 10_000:
            raise RuntimeError("interaction_replay_capacity_exhausted")
        serialized = json.dumps(
            response, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        connection.execute(
            "INSERT INTO interaction_replays VALUES (?, 'responded', ?, ?, ?, ?)",
            (fingerprint, status, serialized, now, now),
        )

    def admit_open_interaction(
        self,
        *,
        fingerprint: str,
        binding: CorrelationBinding,
        team_id: str,
        slack_user_id: str,
        trigger_id: str,
        expires_at: int,
    ) -> tuple[bool, int | None, dict[str, object] | None, str | None]:
        now = datetime.now(UTC).isoformat()
        token = str(uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            is_new, status, response = self._prior_interaction(connection, fingerprint)
            if not is_new:
                connection.commit()
                return False, status, response, None
            row = connection.execute(
                """SELECT 1 FROM correlation_projections p
                   JOIN notifications n USING (tenant_ref, notification_id)
                   WHERE p.tenant_ref=? AND p.notification_id=? AND p.case_id=?
                     AND p.team_id=? AND p.channel_id=? AND p.message_ts=?
                     AND n.state='accepted'""",
                (
                    binding.tenant_ref, binding.notification_id, binding.case_id,
                    team_id, binding.channel_id, binding.message_ts,
                ),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RuntimeError("correlation_binding_changed")
            if int(connection.execute(
                "SELECT count(*) FROM correlation_review_sessions"
            ).fetchone()[0]) >= 10_000:
                connection.rollback()
                raise RuntimeError("review_session_capacity_exhausted")
            connection.execute(
                """INSERT INTO correlation_review_sessions
                   (review_token, tenant_ref, case_id, team_id, channel_id, message_ts,
                    slack_user_id, expires_at, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'opened', ?, ?)""",
                (
                    token, binding.tenant_ref, binding.case_id, team_id,
                    binding.channel_id, binding.message_ts, slack_user_id,
                    expires_at, now, now,
                ),
            )
            connection.execute(
                """INSERT INTO correlation_opening_jobs
                   (review_token, trigger_id, state, created_at, updated_at)
                   VALUES (?, ?, 'pending', ?, ?)""",
                (token, trigger_id, now, now),
            )
            self._insert_completed_interaction(
                connection, fingerprint=fingerprint, status=200, response={}, now=now
            )
            connection.commit()
        return True, 200, {}, token

    def admit_prepare_interaction(
        self,
        *,
        fingerprint: str,
        review_token: str,
        team_id: str,
        slack_user_id: str,
        now_epoch: int,
        action: str,
        candidate_id: str | None,
        verification_basis: str,
        view_id: str,
        view_hash: str | None,
        response: dict[str, object],
    ) -> tuple[bool, int | None, dict[str, object] | None]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            is_new, status, prior = self._prior_interaction(connection, fingerprint)
            if not is_new:
                connection.commit()
                return False, status, prior
            row = connection.execute(
                """SELECT s.*, j.state opening_state
                   FROM correlation_review_sessions s
                   JOIN correlation_opening_jobs j USING (review_token)
                   WHERE s.review_token=?""",
                (review_token,),
            ).fetchone()
            if (
                row is None or row["team_id"] != team_id
                or row["slack_user_id"] != slack_user_id
                or int(row["expires_at"]) < now_epoch
                or row["opening_state"] != "completed"
            ):
                connection.rollback()
                raise RuntimeError("review_session_binding_changed")
            if row["state"] == "opened":
                connection.execute(
                    """UPDATE correlation_review_sessions
                       SET state='preparing', action=?, candidate_id=?,
                           verification_basis=?, idempotency_key=?, view_id=?,
                           view_hash=?, updated_at=? WHERE review_token=?""",
                    (
                        action, candidate_id, verification_basis, str(uuid4()),
                        view_id, view_hash, now, review_token,
                    ),
                )
            elif (
                row["state"] not in {"preparing", "prepared"}
                or row["action"] != action
                or row["candidate_id"] != candidate_id
                or row["verification_basis"] != verification_basis
            ):
                connection.rollback()
                raise RuntimeError("review_session_semantic_conflict")
            self._insert_completed_interaction(
                connection, fingerprint=fingerprint, status=200,
                response=response, now=now,
            )
            connection.commit()
        return True, 200, response

    def admit_confirm_interaction(
        self,
        *,
        fingerprint: str,
        review_token: str,
        team_id: str,
        slack_user_id: str,
        now_epoch: int,
        view_id: str,
        view_hash: str | None,
        response: dict[str, object],
    ) -> tuple[bool, int | None, dict[str, object] | None]:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            is_new, status, prior = self._prior_interaction(connection, fingerprint)
            if not is_new:
                connection.commit()
                return False, status, prior
            row = connection.execute(
                "SELECT * FROM correlation_review_sessions WHERE review_token=?",
                (review_token,),
            ).fetchone()
            if (
                row is None or row["team_id"] != team_id
                or row["slack_user_id"] != slack_user_id
                or int(row["expires_at"]) < now_epoch
                or row["state"] not in {"prepared", "confirming"}
            ):
                connection.rollback()
                raise RuntimeError("review_session_binding_changed")
            connection.execute(
                """UPDATE correlation_review_sessions
                   SET state='confirming', view_id=?, view_hash=?, updated_at=?
                   WHERE review_token=?""",
                (view_id, view_hash, now, review_token),
            )
            self._insert_completed_interaction(
                connection, fingerprint=fingerprint, status=200,
                response=response, now=now,
            )
            connection.commit()
        return True, 200, response

    def pending_opening_jobs(self) -> list[OpeningJob]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT review_token, trigger_id, state
                   FROM correlation_opening_jobs WHERE state='pending'
                   ORDER BY created_at, review_token"""
            ).fetchall()
        return [OpeningJob(str(row[0]), str(row[1]), str(row[2])) for row in rows]

    def mark_open_request_started(self, *, review_token: str) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                """UPDATE correlation_opening_jobs
                   SET state='request_started', updated_at=?
                   WHERE review_token=? AND state='pending'""",
                (datetime.now(UTC).isoformat(), review_token),
            ).rowcount
            if updated != 1:
                raise RuntimeError("opening_job_not_pending")

    def finish_opening(self, *, review_token: str) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                """UPDATE correlation_opening_jobs
                   SET state='completed', trigger_id='', updated_at=?
                   WHERE review_token=? AND state='request_started'""",
                (datetime.now(UTC).isoformat(), review_token),
            ).rowcount
            if updated != 1:
                raise RuntimeError("opening_job_not_started")

    def fail_opening(self, *, review_token: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """UPDATE correlation_opening_jobs
                   SET state='failed', trigger_id='', updated_at=?
                   WHERE review_token=? AND state IN ('pending','request_started')""",
                (now, review_token),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise RuntimeError("opening_job_not_active")
            connection.execute(
                """UPDATE correlation_review_sessions SET state='failed', updated_at=?
                   WHERE review_token=? AND state='opened'""",
                (now, review_token),
            )
            connection.commit()

    def reserve_interaction(self, *, fingerprint: str) -> tuple[bool, int | None, dict[str, object] | None]:
        if re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None:
            raise ValueError("invalid_fingerprint")
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT response_status, response_json FROM interaction_replays WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is not None:
                connection.commit()
                response = json.loads(row["response_json"]) if row["response_json"] is not None else None
                return False, row["response_status"], response
            connection.execute(
                "INSERT INTO interaction_replays VALUES (?, 'request_started', NULL, NULL, ?, ?)",
                (fingerprint, now, now),
            )
            connection.commit()
        return True, None, None

    def prune_interaction_history(
        self,
        *,
        now_epoch: int,
        max_replays: int = 10_000,
        max_sessions: int = 10_000,
        replay_retention_seconds: int = 86_400,
        terminal_session_retention_seconds: int = 2_592_000,
    ) -> None:
        """Bound replay history while preserving every nonterminal workflow."""
        if not 1 <= max_replays <= 100_000 or not 1 <= max_sessions <= 100_000:
            raise ValueError("invalid_interaction_capacity")
        if replay_retention_seconds < 300 or terminal_session_retention_seconds < 900:
            raise ValueError("invalid_interaction_retention")
        replay_cutoff = datetime.fromtimestamp(now_epoch, UTC) - timedelta(
            seconds=replay_retention_seconds
        )
        session_cutoff = datetime.fromtimestamp(now_epoch, UTC) - timedelta(
            seconds=terminal_session_retention_seconds
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM interaction_replays WHERE state='responded' AND updated_at < ?",
                (replay_cutoff.isoformat(),),
            )
            connection.execute(
                """DELETE FROM interaction_replays WHERE fingerprint IN (
                       SELECT fingerprint FROM interaction_replays WHERE state='responded'
                       ORDER BY updated_at DESC, fingerprint DESC LIMIT -1 OFFSET ?
                   )""",
                (max_replays - 1,),
            )
            terminal_expired = """SELECT review_token FROM correlation_review_sessions
                                  WHERE state IN ('resolved','failed') AND updated_at < ?"""
            opened_expired = """SELECT review_token FROM correlation_review_sessions
                                WHERE state='opened' AND expires_at < ?"""
            connection.execute(
                f"DELETE FROM correlation_opening_jobs WHERE review_token IN ({terminal_expired})",
                (session_cutoff.isoformat(),),
            )
            connection.execute(
                f"DELETE FROM correlation_review_sessions WHERE review_token IN ({terminal_expired})",
                (session_cutoff.isoformat(),),
            )
            connection.execute(
                f"DELETE FROM correlation_opening_jobs WHERE review_token IN ({opened_expired})",
                (now_epoch,),
            )
            connection.execute(
                f"DELETE FROM correlation_review_sessions WHERE review_token IN ({opened_expired})",
                (now_epoch,),
            )
            active = int(connection.execute(
                """SELECT count(*) FROM correlation_review_sessions
                   WHERE state NOT IN ('resolved','failed')"""
            ).fetchone()[0])
            terminal_budget = max(0, max_sessions - active)
            excess_terminal = """SELECT review_token FROM correlation_review_sessions
                                  WHERE state IN ('resolved','failed')
                                  ORDER BY updated_at DESC, review_token DESC
                                  LIMIT -1 OFFSET ?"""
            connection.execute(
                f"DELETE FROM correlation_opening_jobs WHERE review_token IN ({excess_terminal})",
                (terminal_budget,),
            )
            connection.execute(
                f"DELETE FROM correlation_review_sessions WHERE review_token IN ({excess_terminal})",
                (terminal_budget,),
            )
            connection.commit()

    def complete_interaction(self, *, fingerprint: str, status: int, response: dict[str, object]) -> None:
        serialized = json.dumps(response, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            updated = connection.execute(
                """UPDATE interaction_replays SET state='responded', response_status=?, response_json=?, updated_at=?
                   WHERE fingerprint=? AND state='request_started'""",
                (status, serialized, datetime.now(UTC).isoformat(), fingerprint),
            ).rowcount
            if updated != 1:
                raise RuntimeError("interaction_replay_conflict")

    def interaction_replay_count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT count(*) FROM interaction_replays").fetchone()[0])

    def create_review_session(
        self, *, binding: CorrelationBinding, team_id: str, slack_user_id: str, expires_at: int
    ) -> ReviewSession:
        token = str(uuid4())
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = int(connection.execute(
                "SELECT count(*) FROM correlation_review_sessions"
            ).fetchone()[0])
            if count >= 10_000:
                connection.rollback()
                raise RuntimeError("review_session_capacity_exhausted")
            connection.execute(
                """INSERT INTO correlation_review_sessions
                   (review_token, tenant_ref, case_id, team_id, channel_id, message_ts,
                    slack_user_id, expires_at, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'opened', ?, ?)""",
                (token, binding.tenant_ref, binding.case_id, team_id, binding.channel_id,
                 binding.message_ts, slack_user_id, expires_at, now, now),
            )
            connection.execute(
                """INSERT INTO correlation_opening_jobs
                   (review_token, trigger_id, state, created_at, updated_at)
                   VALUES (?, '', 'completed', ?, ?)""",
                (token, now, now),
            )
        session = self.get_review_session(review_token=token)
        assert session is not None
        return session

    def get_review_session(self, *, review_token: str) -> ReviewSession | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM correlation_review_sessions WHERE review_token = ?", (review_token,)
            ).fetchone()
        if row is None:
            return None
        return ReviewSession(
            review_token=str(row["review_token"]), tenant_ref=str(row["tenant_ref"]),
            case_id=str(row["case_id"]), team_id=str(row["team_id"]),
            channel_id=str(row["channel_id"]), message_ts=str(row["message_ts"]),
            slack_user_id=str(row["slack_user_id"]), expires_at=int(row["expires_at"]),
            state=str(row["state"]), action=row["action"], candidate_id=row["candidate_id"],
            verification_basis=row["verification_basis"], idempotency_key=row["idempotency_key"],
            prepared_command=json.loads(row["prepared_command_json"]) if row["prepared_command_json"] else None,
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            view_id=row["view_id"], view_hash=row["view_hash"],
        )

    def begin_prepare(
        self, *, review_token: str, action: str, candidate_id: str | None,
        verification_basis: str, view_id: str | None = None,
        view_hash: str | None = None,
    ) -> ReviewSession:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM correlation_review_sessions WHERE review_token=?", (review_token,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RuntimeError("review_session_not_open")
            if row["state"] == "opened":
                connection.execute(
                    """UPDATE correlation_review_sessions SET state='preparing', action=?, candidate_id=?,
                       verification_basis=?, idempotency_key=?, view_id=?, view_hash=?, updated_at=?
                       WHERE review_token=?""",
                    (action, candidate_id, verification_basis, str(uuid4()), view_id, view_hash,
                     datetime.now(UTC).isoformat(), review_token),
                )
            elif (
                row["state"] not in {"preparing", "prepared"}
                or row["action"] != action
                or row["candidate_id"] != candidate_id
                or row["verification_basis"] != verification_basis
            ):
                connection.rollback()
                raise RuntimeError("review_session_semantic_conflict")
            connection.commit()
        session = self.get_review_session(review_token=review_token)
        assert session is not None
        return session

    def active_review_sessions(self) -> list[ReviewSession]:
        with self._connect() as connection:
            tokens = [
                str(row[0])
                for row in connection.execute(
                    """SELECT review_token FROM correlation_review_sessions
                       WHERE state IN ('preparing','confirming')
                       ORDER BY updated_at, review_token"""
                )
            ]
        return [
            session
            for token in tokens
            if (session := self.get_review_session(review_token=token)) is not None
        ]

    def finish_prepare(self, *, review_token: str, command: dict[str, object]) -> ReviewSession:
        serialized = json.dumps(command, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE correlation_review_sessions SET state='prepared', prepared_command_json=?, updated_at=? WHERE review_token=? AND state='preparing'",
                (serialized, datetime.now(UTC).isoformat(), review_token),
            ).rowcount
            if updated != 1:
                raise RuntimeError("review_session_not_preparing")
        session = self.get_review_session(review_token=review_token)
        assert session is not None
        return session

    def begin_confirm(
        self, *, review_token: str, view_id: str | None = None,
        view_hash: str | None = None,
    ) -> ReviewSession:
        with self._connect() as connection:
            updated = connection.execute(
                """UPDATE correlation_review_sessions
                   SET state='confirming', view_id=COALESCE(?, view_id),
                       view_hash=?, updated_at=?
                   WHERE review_token=? AND state IN ('prepared','confirming')""",
                (view_id, view_hash, datetime.now(UTC).isoformat(), review_token),
            ).rowcount
            if updated != 1:
                raise RuntimeError("review_session_not_prepared")
        session = self.get_review_session(review_token=review_token)
        assert session is not None
        return session

    def fail_session(self, *, review_token: str) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE correlation_review_sessions SET state='failed', updated_at=? WHERE review_token=? AND state IN ('preparing','confirming')",
                (datetime.now(UTC).isoformat(), review_token),
            ).rowcount
            if updated != 1:
                raise RuntimeError("review_session_not_active")

    def finish_resolution_and_begin_projection(
        self, *, review_token: str, result: dict[str, object]
    ) -> CorrelationBinding:
        serialized = json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT * FROM correlation_review_sessions WHERE review_token=? AND state='confirming'",
                (review_token,),
            ).fetchone()
            if session is None:
                connection.rollback()
                raise RuntimeError("review_session_not_confirming")
            projection = connection.execute(
                """SELECT * FROM correlation_projections WHERE tenant_ref=? AND case_id=?
                   AND team_id=? AND channel_id=? AND message_ts=?
                   AND state IN ('pending','accepted')""",
                (
                    session["tenant_ref"], session["case_id"], session["team_id"],
                    session["channel_id"], session["message_ts"],
                ),
            ).fetchone()
            if projection is None:
                connection.rollback()
                raise RuntimeError("projection_not_available")
            connection.execute(
                "UPDATE correlation_review_sessions SET state='resolved', result_json=?, updated_at=? WHERE review_token=?",
                (serialized, now, review_token),
            )
            connection.execute(
                "UPDATE correlation_projections SET state='request_started', failure_code=NULL, updated_at=? WHERE tenant_ref=? AND notification_id=?",
                (now, projection["tenant_ref"], projection["notification_id"]),
            )
            connection.commit()
        return CorrelationBinding(
            tenant_ref=str(projection["tenant_ref"]),
            notification_id=str(projection["notification_id"]),
            case_id=str(projection["case_id"]),
            channel_id=str(projection["channel_id"]),
            message_ts=str(projection["message_ts"]),
            review_due_at=str(projection["review_due_at"]),
        )

    def finish_resolution(self, *, review_token: str, result: dict[str, object]) -> None:
        serialized = json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE correlation_review_sessions SET state='resolved', result_json=?, updated_at=? WHERE review_token=? AND state='confirming'",
                (serialized, datetime.now(UTC).isoformat(), review_token),
            ).rowcount
            if updated != 1:
                raise RuntimeError("review_session_not_confirming")

    def begin_bound_projection(self, *, tenant_ref: str, channel_id: str, message_ts: str, team_id: str) -> CorrelationBinding:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM correlation_projections WHERE tenant_ref=? AND team_id=? AND channel_id=? AND message_ts=?",
                (tenant_ref, team_id, channel_id, message_ts),
            ).fetchone()
            if row is None or row["state"] not in {"pending", "accepted"}:
                connection.rollback()
                raise RuntimeError("projection_not_available")
            connection.execute(
                "UPDATE correlation_projections SET state='request_started', team_id=?, failure_code=NULL, updated_at=? WHERE tenant_ref=? AND notification_id=?",
                (team_id, now, tenant_ref, row["notification_id"]),
            )
            connection.commit()
        return CorrelationBinding(
            tenant_ref=str(row["tenant_ref"]), notification_id=str(row["notification_id"]),
            case_id=str(row["case_id"]), channel_id=str(row["channel_id"]),
            message_ts=str(row["message_ts"]), review_due_at=str(row["review_due_at"]),
        )

    def claim_projection(self, *, tenant_ref: str, team_id: str, channel_id: str) -> CorrelationBinding | None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM correlation_projections WHERE tenant_ref=?
                   AND (team_id=? OR team_id IS NULL) AND channel_id=?
                   AND state='pending' ORDER BY created_at, notification_id LIMIT 1""",
                (tenant_ref, team_id, channel_id),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                "UPDATE correlation_projections SET state='claimed', team_id=?, updated_at=? WHERE tenant_ref=? AND notification_id=? AND state='pending'",
                (team_id, now, tenant_ref, row["notification_id"]),
            )
            connection.commit()
        return CorrelationBinding(
            tenant_ref=str(row["tenant_ref"]), notification_id=str(row["notification_id"]),
            case_id=str(row["case_id"]), channel_id=str(row["channel_id"]),
            message_ts=str(row["message_ts"]), review_due_at=str(row["review_due_at"]),
        )

    def mark_projection_request_started(self, *, binding: CorrelationBinding) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE correlation_projections SET state='request_started', updated_at=? WHERE tenant_ref=? AND notification_id=? AND state='claimed'",
                (datetime.now(UTC).isoformat(), binding.tenant_ref, binding.notification_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError("projection_not_claimed")

    def release_projection_claim(self, *, binding: CorrelationBinding) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE correlation_projections SET state='pending', failure_code=NULL, updated_at=? WHERE tenant_ref=? AND notification_id=? AND state='claimed'",
                (datetime.now(UTC).isoformat(), binding.tenant_ref, binding.notification_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError("projection_not_claimed")

    def finish_projection(self, *, binding: CorrelationBinding, state: str, failure_code: str | None = None) -> None:
        if state not in {"accepted", "rejected", "delivery_unknown"}:
            raise ValueError("invalid_projection_state")
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE correlation_projections SET state=?, failure_code=?, updated_at=? WHERE tenant_ref=? AND notification_id=? AND state='request_started'",
                (state, failure_code, datetime.now(UTC).isoformat(), binding.tenant_ref, binding.notification_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError("projection_not_started")

    def projection_inventory(self) -> dict[str, int]:
        states = ("pending", "claimed", "request_started", "accepted", "rejected", "delivery_unknown")
        with self._connect() as connection:
            rows = connection.execute("SELECT state, count(*) count FROM correlation_projections GROUP BY state").fetchall()
        found = {str(row["state"]): int(row["count"]) for row in rows}
        return {state: found.get(state, 0) for state in states}

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
            connection.execute(
                """
                UPDATE correlation_projections
                SET state = 'pending', failure_code = NULL, updated_at = ?
                WHERE state = 'claimed'
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE correlation_projections
                SET state = 'delivery_unknown', failure_code = 'process_interrupted_after_request_start',
                    updated_at = ?
                WHERE state = 'request_started'
                """,
                (now,),
            )
            interrupted_openings = [
                str(row[0])
                for row in connection.execute(
                    """SELECT review_token FROM correlation_opening_jobs
                       WHERE state IN ('pending','request_started')"""
                )
            ]
            connection.execute(
                """UPDATE correlation_opening_jobs
                   SET state='failed', trigger_id='', updated_at=?
                   WHERE state IN ('pending','request_started')""",
                (now,),
            )
            if interrupted_openings:
                placeholders = ",".join("?" for _ in interrupted_openings)
                connection.execute(
                    f"""UPDATE correlation_review_sessions SET state='failed', updated_at=?
                        WHERE state='opened' AND review_token IN ({placeholders})""",
                    (now, *interrupted_openings),
                )
            connection.execute(
                """
                DELETE FROM interaction_replays
                WHERE state = 'request_started'
                """,
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

    def reconcile_delivery_unknown(
        self,
        *,
        tenant_ref: str,
        notification_id: str,
        decision: str,
        operator_id: str,
        channel_id: str,
        message_ts: str | None = None,
        thread_ts: str | None = None,
    ) -> StoredNotification:
        if tenant_ref not in {"johanna", "att1"}:
            raise ValueError("invalid_tenant_ref")
        if decision not in {"confirm_delivered", "confirm_not_delivered"}:
            raise ValueError("invalid_reconciliation_decision")
        if _TENANT_REF.fullmatch(operator_id) is None:
            raise ValueError("invalid_operator_id")
        if decision == "confirm_delivered" and not message_ts:
            raise ValueError("message_ts_required")
        if decision == "confirm_not_delivered" and (message_ts is not None or thread_ts is not None):
            raise ValueError("delivery_evidence_not_allowed")
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT payload_json, state, activation_generation_started
                FROM notifications WHERE tenant_ref = ? AND notification_id = ?
                """,
                (tenant_ref, notification_id),
            ).fetchone()
            if row is None or row["state"] != "delivery_unknown":
                connection.rollback()
                raise RuntimeError("delivery_unknown_not_found")
            command = _deserialize_command(str(row["payload_json"]))
            resulting_state = "accepted" if decision == "confirm_delivered" else "pending"
            if decision == "confirm_delivered":
                if command.subject_ref is not None:
                    root = connection.execute(
                        "SELECT channel_id, message_ts FROM thread_roots WHERE tenant_ref = ? AND subject_ref = ?",
                        (tenant_ref, command.subject_ref),
                    ).fetchone()
                    if root is None:
                        if thread_ts is not None:
                            connection.rollback()
                            raise RuntimeError("missing_thread_root")
                        connection.execute(
                            """
                            INSERT INTO thread_roots
                                (tenant_ref, subject_ref, channel_id, message_ts, created_at)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (tenant_ref, command.subject_ref, channel_id, message_ts, now),
                        )
                    elif root["channel_id"] != channel_id or root["message_ts"] != thread_ts:
                        connection.rollback()
                        raise RuntimeError("thread_identity_mismatch")
                connection.execute(
                    """
                    UPDATE notifications
                    SET state = 'accepted', channel_id = ?, message_ts = ?, thread_ts = ?,
                        failure_code = NULL, updated_at = ?
                    WHERE tenant_ref = ? AND notification_id = ? AND state = 'delivery_unknown'
                    """,
                    (channel_id, message_ts, thread_ts, now, tenant_ref, notification_id),
                )
            else:
                activation_generation = row["activation_generation_started"]
                if activation_generation is not None:
                    connection.execute(
                        """
                        UPDATE connector_meta
                        SET activation_consumed = activation_consumed - 1,
                            activation_verified = 0
                        WHERE singleton = 1 AND activation_mode = 'one_shot'
                          AND activation_generation = ? AND activation_consumed > 0
                        """,
                        (activation_generation,),
                    )
                connection.execute(
                    """
                    UPDATE notifications
                    SET state = 'pending', failure_code = NULL, request_started_at = NULL,
                        activation_generation_started = NULL, updated_at = ?
                    WHERE tenant_ref = ? AND notification_id = ? AND state = 'delivery_unknown'
                    """,
                    (now, tenant_ref, notification_id),
                )
            connection.execute(
                """
                INSERT INTO reconciliation_audit (
                    tenant_ref, notification_id, decision, operator_id, prior_state,
                    resulting_state, channel_id, message_ts, thread_ts, decided_at
                ) VALUES (?, ?, ?, ?, 'delivery_unknown', ?, ?, ?, ?, ?)
                """,
                (
                    tenant_ref, notification_id, decision, operator_id,
                    resulting_state,
                    channel_id if decision == "confirm_delivered" else None,
                    message_ts, thread_ts, now,
                ),
            )
            connection.commit()
        result = self.get(tenant_ref=tenant_ref, notification_id=notification_id)
        assert result is not None
        return result

    def reconciliation_audit_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT count(*) FROM reconciliation_audit").fetchone()
        assert row is not None
        return int(row[0])

    def backup_to(self, destination: str | Path) -> None:
        target = Path(destination)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with self._connect() as source, sqlite3.connect(temporary) as backup:
                source.backup(backup)
                backup.execute("PRAGMA synchronous = FULL")
            _validate_database(temporary)
            os.chmod(temporary, 0o600)
            _fsync_file(temporary)
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def restore_from(cls, source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        target = Path(destination)
        store = cls(target)
        store.acquire_instance_lock()
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.restore.", dir=target.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                try:
                    with (
                        closing(sqlite3.connect(
                            f"file:{source_path}?mode=ro", uri=True
                        )) as source_connection,
                        closing(sqlite3.connect(temporary)) as snapshot,
                    ):
                        source_connection.backup(snapshot)
                    version = _migration_source_version(temporary)
                    if version < 6:
                        cls(temporary).initialize()
                        with sqlite3.connect(temporary) as migrated:
                            checkpoint = migrated.execute(
                                "PRAGMA wal_checkpoint(TRUNCATE)"
                            ).fetchone()
                            if checkpoint is None or int(checkpoint[0]) != 0:
                                raise RuntimeError("migration_checkpoint_busy")
                            migrated.execute("PRAGMA journal_mode = DELETE").fetchone()
                    _validate_database(temporary)
                except (
                    OSError,
                    sqlite3.DatabaseError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    raise RuntimeError("invalid_backup") from exc
                os.chmod(temporary, 0o600)
                _fsync_file(temporary)
                if target.exists():
                    try:
                        with closing(sqlite3.connect(target)) as existing:
                            existing.execute("PRAGMA busy_timeout = 15000")
                            checkpoint = existing.execute(
                                "PRAGMA wal_checkpoint(TRUNCATE)"
                            ).fetchone()
                            if checkpoint is None or int(checkpoint[0]) != 0:
                                raise RuntimeError("destination_checkpoint_busy")
                    except sqlite3.DatabaseError as exc:
                        raise RuntimeError("invalid_restore_destination") from exc
                target.with_name(target.name + "-wal").unlink(missing_ok=True)
                target.with_name(target.name + "-shm").unlink(missing_ok=True)
                _fsync_directory(target.parent)
                os.replace(temporary, target)
                _fsync_directory(target.parent)
            finally:
                temporary.unlink(missing_ok=True)
        finally:
            store.release_instance_lock()


def _migration_source_version(path: Path) -> int:
    """Reject executable/unrelated schema objects before an in-place upgrade."""
    allowed_tables = {
        "notifications", "thread_roots", "connector_meta", "reconciliation_audit",
        "interaction_replays", "correlation_review_sessions", "correlation_opening_jobs",
        "correlation_projections",
        "sqlite_sequence",
    }
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {2, 3, 4, 5, 6}:
            raise RuntimeError("invalid_backup")
        objects = connection.execute(
            """SELECT type, name FROM sqlite_master
               WHERE type IN ('table','trigger','view')"""
        ).fetchall()
        table_names = {name for object_type, name in objects if object_type == "table"}
        required_tables = {
            "notifications", "thread_roots", "connector_meta", "reconciliation_audit"
        }
        if not required_tables <= table_names or any(
            object_type != "table" or name not in allowed_tables
            for object_type, name in objects
        ):
            raise RuntimeError("invalid_backup")
    return version


def _execute_statements(connection: sqlite3.Connection, script: str) -> None:
    """Execute DDL without executescript's implicit pre-commit."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                connection.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("incomplete_migration_statement")


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


def _validate_database(path: Path) -> None:
    def valid_datetime(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return False
        return parsed.tzinfo is not None

    def valid_channel(value: object) -> bool:
        return (
            isinstance(value, str)
            and re.fullmatch(r"[CGD][A-Z0-9]{8,}", value) is not None
        )

    def valid_slack_ts(value: object) -> bool:
        return (
            isinstance(value, str)
            and re.fullmatch(r"[0-9]{10,16}\.[0-9]{6}", value) is not None
        )

    expected_columns = {
        "notifications": (
            ("tenant_ref", "TEXT", 1, None, 1),
            ("notification_id", "TEXT", 1, None, 2),
            ("event_code", "TEXT", 1, None, 0),
            ("dedupe_key", "TEXT", 1, None, 0),
            ("payload_json", "TEXT", 1, None, 0),
            ("payload_sha256", "TEXT", 1, None, 0),
            ("state", "TEXT", 1, None, 0),
            ("claim_owner", "TEXT", 0, None, 0),
            ("claim_generation", "INTEGER", 1, "0", 0),
            ("created_at", "TEXT", 1, None, 0),
            ("updated_at", "TEXT", 1, None, 0),
            ("request_started_at", "TEXT", 0, None, 0),
            ("channel_id", "TEXT", 0, None, 0),
            ("message_ts", "TEXT", 0, None, 0),
            ("thread_ts", "TEXT", 0, None, 0),
            ("failure_code", "TEXT", 0, None, 0),
            ("activation_generation_started", "INTEGER", 0, None, 0),
            ("team_id", "TEXT", 0, None, 0),
        ),
        "thread_roots": (
            ("tenant_ref", "TEXT", 1, None, 1),
            ("subject_ref", "TEXT", 1, None, 2),
            ("channel_id", "TEXT", 1, None, 0),
            ("message_ts", "TEXT", 1, None, 0),
            ("created_at", "TEXT", 1, None, 0),
        ),
        "connector_meta": (
            ("singleton", "INTEGER", 0, None, 1),
            ("last_write_probe_at", "TEXT", 1, None, 0),
            ("activation_initialized", "INTEGER", 1, "0", 0),
            ("activation_mode", "TEXT", 1, "'inactive'", 0),
            ("activation_generation", "INTEGER", 1, "0", 0),
            ("activation_budget", "INTEGER", 0, None, 0),
            ("activation_consumed", "INTEGER", 1, "0", 0),
            ("activation_verified", "INTEGER", 1, "0", 0),
        ),
        "reconciliation_audit": (
            ("audit_id", "INTEGER", 0, None, 1),
            ("tenant_ref", "TEXT", 1, None, 0),
            ("notification_id", "TEXT", 1, None, 0),
            ("decision", "TEXT", 1, None, 0),
            ("operator_id", "TEXT", 1, None, 0),
            ("prior_state", "TEXT", 1, None, 0),
            ("resulting_state", "TEXT", 1, None, 0),
            ("channel_id", "TEXT", 0, None, 0),
            ("message_ts", "TEXT", 0, None, 0),
            ("thread_ts", "TEXT", 0, None, 0),
            ("decided_at", "TEXT", 1, None, 0),
        ),
        "interaction_replays": (
            ("fingerprint", "TEXT", 0, None, 1),
            ("state", "TEXT", 1, None, 0),
            ("response_status", "INTEGER", 0, None, 0),
            ("response_json", "TEXT", 0, None, 0),
            ("created_at", "TEXT", 1, None, 0),
            ("updated_at", "TEXT", 1, None, 0),
        ),
        "correlation_review_sessions": (
            ("review_token", "TEXT", 0, None, 1),
            ("tenant_ref", "TEXT", 1, None, 0),
            ("case_id", "TEXT", 1, None, 0),
            ("team_id", "TEXT", 1, None, 0),
            ("channel_id", "TEXT", 1, None, 0),
            ("message_ts", "TEXT", 1, None, 0),
            ("slack_user_id", "TEXT", 1, None, 0),
            ("expires_at", "INTEGER", 1, None, 0),
            ("state", "TEXT", 1, None, 0),
            ("action", "TEXT", 0, None, 0),
            ("candidate_id", "TEXT", 0, None, 0),
            ("verification_basis", "TEXT", 0, None, 0),
            ("idempotency_key", "TEXT", 0, None, 0),
            ("prepared_command_json", "TEXT", 0, None, 0),
            ("result_json", "TEXT", 0, None, 0),
            ("created_at", "TEXT", 1, None, 0),
            ("updated_at", "TEXT", 1, None, 0),
            ("view_id", "TEXT", 0, None, 0),
            ("view_hash", "TEXT", 0, None, 0),
        ),
        "correlation_opening_jobs": (
            ("review_token", "TEXT", 0, None, 1),
            ("trigger_id", "TEXT", 1, None, 0),
            ("state", "TEXT", 1, None, 0),
            ("created_at", "TEXT", 1, None, 0),
            ("updated_at", "TEXT", 1, None, 0),
        ),
        "correlation_projections": (
            ("tenant_ref", "TEXT", 1, None, 1),
            ("notification_id", "TEXT", 1, None, 2),
            ("case_id", "TEXT", 1, None, 0),
            ("team_id", "TEXT", 0, None, 0),
            ("channel_id", "TEXT", 1, None, 0),
            ("message_ts", "TEXT", 1, None, 0),
            ("review_due_at", "TEXT", 1, None, 0),
            ("state", "TEXT", 1, None, 0),
            ("failure_code", "TEXT", 0, None, 0),
            ("created_at", "TEXT", 1, None, 0),
            ("updated_at", "TEXT", 1, None, 0),
        ),
    }
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        table_rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {str(row["name"]): str(row["sql"]) for row in table_rows}
        if set(tables) != set(expected_columns):
            raise RuntimeError("invalid_backup")
        objects = {
            (str(row["type"]), str(row["name"]), str(row["tbl_name"]))
            for row in connection.execute(
                """
                SELECT type, name, tbl_name FROM sqlite_master
                WHERE type IN ('table', 'index', 'trigger', 'view')
                """
            )
        }
        if objects != {
            ("table", "notifications", "notifications"),
            ("table", "thread_roots", "thread_roots"),
            ("table", "connector_meta", "connector_meta"),
            ("table", "reconciliation_audit", "reconciliation_audit"),
            ("table", "interaction_replays", "interaction_replays"),
            ("table", "correlation_review_sessions", "correlation_review_sessions"),
            ("table", "correlation_opening_jobs", "correlation_opening_jobs"),
            ("table", "correlation_projections", "correlation_projections"),
            ("table", "sqlite_sequence", "sqlite_sequence"),
            ("index", "sqlite_autoindex_notifications_1", "notifications"),
            ("index", "sqlite_autoindex_notifications_2", "notifications"),
            ("index", "sqlite_autoindex_thread_roots_1", "thread_roots"),
            ("index", "sqlite_autoindex_interaction_replays_1", "interaction_replays"),
            ("index", "sqlite_autoindex_correlation_review_sessions_1", "correlation_review_sessions"),
            ("index", "sqlite_autoindex_correlation_review_sessions_2", "correlation_review_sessions"),
            ("index", "sqlite_autoindex_correlation_opening_jobs_1", "correlation_opening_jobs"),
            ("index", "sqlite_autoindex_correlation_projections_1", "correlation_projections"),
            ("index", "sqlite_autoindex_correlation_projections_2", "correlation_projections"),
        }:
            raise RuntimeError("invalid_backup")
        for table, expected in expected_columns.items():
            actual = tuple(
                (str(row["name"]), str(row["type"]), int(row["notnull"]), row["dflt_value"], int(row["pk"]))
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual != expected:
                raise RuntimeError("invalid_backup")
        unique_indexes: dict[str, set[tuple[str, ...]]] = {}
        for table in expected_columns:
            unique_indexes[table] = {
                tuple(
                    str(column["name"])
                    for column in connection.execute(
                        f"PRAGMA index_info({row['name']})"
                    )
                )
                for row in connection.execute(f"PRAGMA index_list({table})")
                if bool(row["unique"])
            }
        if unique_indexes != {
            "notifications": {
                ("tenant_ref", "notification_id"),
                ("tenant_ref", "event_code", "dedupe_key"),
            },
            "thread_roots": {("tenant_ref", "subject_ref")},
            "connector_meta": set(),
            "reconciliation_audit": set(),
            "interaction_replays": {("fingerprint",)},
            "correlation_review_sessions": {
                ("review_token",),
                ("idempotency_key",),
            },
            "correlation_opening_jobs": {("review_token",)},
            "correlation_projections": {
                ("tenant_ref", "notification_id"),
                ("team_id", "channel_id", "message_ts"),
            },
        }:
            raise RuntimeError("invalid_backup")
        foreign_keys = {
            (
                int(row["id"]),
                int(row["seq"]),
                str(row["table"]),
                str(row["from"]),
                str(row["to"]),
                str(row["on_update"]),
                str(row["on_delete"]),
                str(row["match"]),
            )
            for row in connection.execute("PRAGMA foreign_key_list(reconciliation_audit)")
        }
        if foreign_keys != {
            (0, 0, "notifications", "tenant_ref", "tenant_ref", "NO ACTION", "NO ACTION", "NONE"),
            (0, 1, "notifications", "notification_id", "notification_id", "NO ACTION", "NO ACTION", "NONE"),
        }:
            raise RuntimeError("invalid_backup")
        projection_foreign_keys = {
            (
                int(row["id"]), int(row["seq"]), str(row["table"]),
                str(row["from"]), str(row["to"]), str(row["on_update"]),
                str(row["on_delete"]), str(row["match"]),
            )
            for row in connection.execute("PRAGMA foreign_key_list(correlation_projections)")
        }
        if projection_foreign_keys != {
            (0, 0, "notifications", "tenant_ref", "tenant_ref", "NO ACTION", "NO ACTION", "NONE"),
            (0, 1, "notifications", "notification_id", "notification_id", "NO ACTION", "NO ACTION", "NONE"),
        }:
            raise RuntimeError("invalid_backup")
        opening_foreign_keys = {
            (
                int(row["id"]), int(row["seq"]), str(row["table"]),
                str(row["from"]), str(row["to"]), str(row["on_update"]),
                str(row["on_delete"]), str(row["match"]),
            )
            for row in connection.execute(
                "PRAGMA foreign_key_list(correlation_opening_jobs)"
            )
        }
        if opening_foreign_keys != {
            (0, 0, "correlation_review_sessions", "review_token", "review_token",
             "NO ACTION", "NO ACTION", "NONE")
        }:
            raise RuntimeError("invalid_backup")
        normalized_sql = {
            table: "".join(sql.lower().split()) for table, sql in tables.items()
        }
        expected_normalized_sql = {
            "notifications": "createtablenotifications(tenant_reftextnotnull,notification_idtextnotnull,event_codetextnotnull,dedupe_keytextnotnull,payload_jsontextnotnull,payload_sha256textnotnull,statetextnotnullcheck(statein('pending','claimed','request_started','accepted','rejected','delivery_unknown')),claim_ownertext,claim_generationintegernotnulldefault0,created_attextnotnull,updated_attextnotnull,request_started_attext,channel_idtext,message_tstext,thread_tstext,failure_codetext,activation_generation_startedinteger,team_idtext,primarykey(tenant_ref,notification_id),unique(tenant_ref,event_code,dedupe_key))",
            "thread_roots": "createtablethread_roots(tenant_reftextnotnull,subject_reftextnotnull,channel_idtextnotnull,message_tstextnotnull,created_attextnotnull,primarykey(tenant_ref,subject_ref))",
            "connector_meta": "createtableconnector_meta(singletonintegerprimarykeycheck(singleton=1),last_write_probe_attextnotnull,activation_initializedintegernotnulldefault0,activation_modetextnotnulldefault'inactive',activation_generationintegernotnulldefault0,activation_budgetinteger,activation_consumedintegernotnulldefault0,activation_verifiedintegernotnulldefault0)",
            "reconciliation_audit": "createtablereconciliation_audit(audit_idintegerprimarykeyautoincrement,tenant_reftextnotnull,notification_idtextnotnull,decisiontextnotnullcheck(decisionin('confirm_delivered','confirm_not_delivered')),operator_idtextnotnull,prior_statetextnotnull,resulting_statetextnotnull,channel_idtext,message_tstext,thread_tstext,decided_attextnotnull,foreignkey(tenant_ref,notification_id)referencesnotifications(tenant_ref,notification_id))",
            "interaction_replays": "createtableinteraction_replays(fingerprinttextprimarykey,statetextnotnullcheck(statein('request_started','responded')),response_statusinteger,response_jsontext,created_attextnotnull,updated_attextnotnull)",
            "correlation_review_sessions": "createtablecorrelation_review_sessions(review_tokentextprimarykey,tenant_reftextnotnull,case_idtextnotnull,team_idtextnotnull,channel_idtextnotnull,message_tstextnotnull,slack_user_idtextnotnull,expires_atintegernotnull,statetextnotnullcheck(statein('opened','preparing','prepared','confirming','resolved','failed')),actiontext,candidate_idtext,verification_basistext,idempotency_keytextunique,prepared_command_jsontext,result_jsontext,created_attextnotnull,updated_attextnotnull,view_idtext,view_hashtext)",
            "correlation_opening_jobs": "createtablecorrelation_opening_jobs(review_tokentextprimarykey,trigger_idtextnotnull,statetextnotnullcheck(statein('pending','request_started','completed','failed')),created_attextnotnull,updated_attextnotnull,foreignkey(review_token)referencescorrelation_review_sessions(review_token))",
            "correlation_projections": "createtablecorrelation_projections(tenant_reftextnotnull,notification_idtextnotnull,case_idtextnotnull,team_idtext,channel_idtextnotnull,message_tstextnotnull,review_due_attextnotnull,statetextnotnullcheck(statein('pending','claimed','request_started','accepted','rejected','delivery_unknown')),failure_codetext,created_attextnotnull,updated_attextnotnull,primarykey(tenant_ref,notification_id),unique(team_id,channel_id,message_ts),foreignkey(tenant_ref,notification_id)referencesnotifications(tenant_ref,notification_id))",
        }
        if normalized_sql != expected_normalized_sql:
            raise RuntimeError("invalid_backup")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("invalid_backup")
        meta_rows = connection.execute("SELECT * FROM connector_meta").fetchall()
        if len(meta_rows) != 1 or type(meta_rows[0]["singleton"]) is not int or meta_rows[0]["singleton"] != 1:
            raise RuntimeError("invalid_backup")
        meta = meta_rows[0]
        if any(
            type(meta[field]) is not int
            for field in (
                "activation_initialized",
                "activation_generation",
                "activation_consumed",
                "activation_verified",
            )
        ) or (meta["activation_budget"] is not None and type(meta["activation_budget"]) is not int):
            raise RuntimeError("invalid_backup")
        initialized = meta["activation_initialized"]
        mode = str(meta["activation_mode"])
        generation = meta["activation_generation"]
        budget = meta["activation_budget"]
        consumed = meta["activation_consumed"]
        verified = meta["activation_verified"]
        if not valid_datetime(meta["last_write_probe_at"]):
            raise RuntimeError("invalid_backup")
        if initialized not in {0, 1} or verified not in {0, 1} or generation < 0:
            raise RuntimeError("invalid_backup")
        if mode not in {"inactive", "one_shot", "continuous"}:
            raise RuntimeError("invalid_backup")
        if not initialized and (mode != "inactive" or generation != 0 or budget is not None or consumed != 0 or verified):
            raise RuntimeError("invalid_backup")
        if initialized and mode == "one_shot" and (budget != 1 or consumed not in {0, 1}):
            raise RuntimeError("invalid_backup")
        if initialized and mode != "one_shot" and (budget is not None or consumed != 0 or verified):
            raise RuntimeError("invalid_backup")
        notification_rows: list[tuple[sqlite3.Row, NotificationCommand]] = []
        for row in connection.execute("SELECT * FROM notifications"):
            payload = str(row["payload_json"])
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != row["payload_sha256"]:
                raise RuntimeError("invalid_backup")
            try:
                command = _deserialize_command(payload)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError("invalid_backup") from exc
            if command.event_id != row["notification_id"] or command.event_code != row["event_code"] or command.dedupe_key != row["dedupe_key"]:
                raise RuntimeError("invalid_backup")
            notification_rows.append((row, command))
            state = str(row["state"])
            claim_owner = row["claim_owner"]
            if (
                row["tenant_ref"] not in {"johanna", "att1"}
                or type(row["claim_generation"]) is not int
                or row["claim_generation"] < 0
            ):
                raise RuntimeError("invalid_backup")
            if state in {"claimed", "request_started"} and claim_owner is None:
                raise RuntimeError("invalid_backup")
            if state != "pending" and row["claim_generation"] == 0:
                raise RuntimeError("invalid_backup")
            started_generation = row["activation_generation_started"]
            if started_generation is not None and (
                type(started_generation) is not int
                or started_generation < 1
                or started_generation > generation
            ):
                raise RuntimeError("invalid_backup")
            if state not in {"claimed", "request_started"} and claim_owner is not None:
                raise RuntimeError("invalid_backup")
            if claim_owner is not None and _TENANT_REF.fullmatch(str(claim_owner)) is None:
                raise RuntimeError("invalid_backup")
            if not valid_datetime(row["created_at"]) or not valid_datetime(row["updated_at"]):
                raise RuntimeError("invalid_backup")
            if row["request_started_at"] is not None and not valid_datetime(row["request_started_at"]):
                raise RuntimeError("invalid_backup")
            if row["channel_id"] is not None and not valid_channel(row["channel_id"]):
                raise RuntimeError("invalid_backup")
            if state in {"pending", "claimed"} and any(
                row[field] is not None
                for field in (
                    "request_started_at",
                    "message_ts",
                    "thread_ts",
                    "failure_code",
                    "activation_generation_started",
                )
            ):
                raise RuntimeError("invalid_backup")
            if state == "request_started" and (
                row["request_started_at"] is None
                or (initialized and row["activation_generation_started"] is None)
                or any(row[field] is not None for field in ("message_ts", "thread_ts", "failure_code"))
            ):
                raise RuntimeError("invalid_backup")
            if state == "accepted" and (
                row["request_started_at"] is None
                or not valid_channel(row["channel_id"])
                or not valid_slack_ts(row["message_ts"])
                or (row["thread_ts"] is not None and not valid_slack_ts(row["thread_ts"]))
                or (command.subject_ref is None and row["thread_ts"] is not None)
                or row["failure_code"] is not None
            ):
                raise RuntimeError("invalid_backup")
            if state in {"rejected", "delivery_unknown"} and (
                row["request_started_at"] is None
                or row["failure_code"] is None
                or any(row[field] is not None for field in ("message_ts", "thread_ts"))
            ):
                raise RuntimeError("invalid_backup")
            if (
                row["failure_code"] is not None
                and _MACHINE_FAILURE.fullmatch(str(row["failure_code"])) is None
            ):
                raise RuntimeError("invalid_backup")
        if initialized and mode == "one_shot":
            generation_rows = [
                row
                for row, _command in notification_rows
                if row["activation_generation_started"] == generation
            ]
            if consumed != len(generation_rows) or len(generation_rows) > 1:
                raise RuntimeError("invalid_backup")
            if verified and (
                len(generation_rows) != 1 or generation_rows[0]["state"] != "accepted"
            ):
                raise RuntimeError("invalid_backup")

        thread_roots = {
            (str(row["tenant_ref"]), str(row["subject_ref"])): row
            for row in connection.execute("SELECT * FROM thread_roots")
        }
        accepted_roots: set[tuple[str, str, str, str]] = set()
        for row, command in notification_rows:
            if row["state"] != "accepted" or command.subject_ref is None:
                continue
            root = thread_roots.get((str(row["tenant_ref"]), command.subject_ref))
            if root is None or root["channel_id"] != row["channel_id"]:
                raise RuntimeError("invalid_backup")
            if row["message_ts"] == root["message_ts"]:
                if row["thread_ts"] is not None:
                    raise RuntimeError("invalid_backup")
                accepted_roots.add(
                    (
                        str(row["tenant_ref"]),
                        command.subject_ref,
                        str(row["channel_id"]),
                        str(row["message_ts"]),
                    )
                )
            elif row["thread_ts"] != root["message_ts"]:
                raise RuntimeError("invalid_backup")
        for (tenant_ref, subject_ref), root in thread_roots.items():
            if (
                tenant_ref not in {"johanna", "att1"}
                or not valid_channel(root["channel_id"])
                or not valid_slack_ts(root["message_ts"])
                or not valid_datetime(root["created_at"])
                or (
                    tenant_ref,
                    subject_ref,
                    str(root["channel_id"]),
                    str(root["message_ts"]),
                )
                not in accepted_roots
            ):
                raise RuntimeError("invalid_backup")

        notifications_by_id = {
            (str(row["tenant_ref"]), str(row["notification_id"])): row
            for row, _command in notification_rows
        }
        for audit in connection.execute("SELECT * FROM reconciliation_audit"):
            notification = notifications_by_id.get(
                (str(audit["tenant_ref"]), str(audit["notification_id"]))
            )
            if (
                notification is None
                or audit["prior_state"] != "delivery_unknown"
                or re.fullmatch(
                    r"[a-z0-9][a-z0-9_-]{0,39}", str(audit["operator_id"])
                )
                is None
                or not valid_datetime(audit["decided_at"])
            ):
                raise RuntimeError("invalid_backup")
            if audit["decision"] == "confirm_delivered":
                if (
                    audit["resulting_state"] != "accepted"
                    or notification["state"] != "accepted"
                    or not valid_channel(audit["channel_id"])
                    or not valid_slack_ts(audit["message_ts"])
                    or (
                        audit["thread_ts"] is not None
                        and not valid_slack_ts(audit["thread_ts"])
                    )
                    or notification["channel_id"] != audit["channel_id"]
                    or notification["message_ts"] != audit["message_ts"]
                    or notification["thread_ts"] != audit["thread_ts"]
                ):
                    raise RuntimeError("invalid_backup")
            elif (
                audit["resulting_state"] != "pending"
                or any(
                    audit[field] is not None
                    for field in ("channel_id", "message_ts", "thread_ts")
                )
            ):
                raise RuntimeError("invalid_backup")

        for replay in connection.execute("SELECT * FROM interaction_replays"):
            responded = replay["state"] == "responded"
            if (
                re.fullmatch(r"[a-f0-9]{64}", str(replay["fingerprint"])) is None
                or not valid_datetime(replay["created_at"])
                or not valid_datetime(replay["updated_at"])
                or responded != (replay["response_status"] is not None and replay["response_json"] is not None)
            ):
                raise RuntimeError("invalid_backup")
            if responded:
                try:
                    response = json.loads(str(replay["response_json"]))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("invalid_backup") from exc
                if not isinstance(response, dict) or type(replay["response_status"]) is not int:
                    raise RuntimeError("invalid_backup")

        opening_jobs = {
            str(row["review_token"]): row
            for row in connection.execute("SELECT * FROM correlation_opening_jobs")
        }
        for job in opening_jobs.values():
            trigger = str(job["trigger_id"])
            job_state = str(job["state"])
            if (
                not valid_datetime(job["created_at"])
                or not valid_datetime(job["updated_at"])
                or len(trigger) > 4096
                or any(ord(character) < 32 or ord(character) == 127 for character in trigger)
                or (job_state in {"pending", "request_started"} and not trigger)
                or (job_state in {"completed", "failed"} and trigger != "")
            ):
                raise RuntimeError("invalid_backup")

        session_tokens: set[str] = set()
        for session in connection.execute("SELECT * FROM correlation_review_sessions"):
            session_tokens.add(str(session["review_token"]))
            try:
                canonical_token = str(UUID(str(session["review_token"])))
                canonical_case = str(UUID(str(session["case_id"])))
                key = session["idempotency_key"]
                if key is not None:
                    key = str(UUID(str(key)))
            except ValueError as exc:
                raise RuntimeError("invalid_backup") from exc
            state = str(session["state"])
            opening = opening_jobs.get(str(session["review_token"]))
            opening_state = str(opening["state"]) if opening is not None else ""
            prepared = session["prepared_command_json"]
            result = session["result_json"]
            bound_projection = connection.execute(
                """SELECT 1 FROM correlation_projections
                   WHERE tenant_ref=? AND case_id=? AND team_id=? AND channel_id=? AND message_ts=?""",
                (
                    session["tenant_ref"], session["case_id"], session["team_id"],
                    session["channel_id"], session["message_ts"],
                ),
            ).fetchone()
            if (
                canonical_token != session["review_token"] or canonical_case != session["case_id"]
                or opening is None
                or (opening_state in {"pending", "request_started"} and state != "opened")
                or (opening_state == "failed" and state != "failed")
                or session["tenant_ref"] not in {"johanna", "att1"}
                or re.fullmatch(r"T[A-Z0-9]{8,}", str(session["team_id"])) is None
                or not valid_channel(session["channel_id"])
                or not valid_slack_ts(session["message_ts"])
                or re.fullmatch(r"U[A-Z0-9]{8,}", str(session["slack_user_id"])) is None
                or type(session["expires_at"]) is not int
                or not valid_datetime(session["created_at"])
                or not valid_datetime(session["updated_at"])
                or (session["view_id"] is not None and (
                    not isinstance(session["view_id"], str) or not session["view_id"]
                ))
                or (session["view_hash"] is not None and not isinstance(session["view_hash"], str))
                or (state in {"preparing", "prepared", "confirming"} and session["view_id"] is None)
                or bound_projection is None
                or (state == "opened" and any(session[field] is not None for field in ("action", "candidate_id", "verification_basis", "idempotency_key", "prepared_command_json", "result_json")))
                or (state in {"preparing", "prepared", "confirming", "resolved"} and (session["action"] not in {"resolve_with_candidate", "close_without_match"} or session["verification_basis"] is None or key is None))
                or (state in {"prepared", "confirming", "resolved"} and prepared is None)
                or (state in {"opened", "preparing"} and prepared is not None)
                or (state == "resolved") != (result is not None)
            ):
                raise RuntimeError("invalid_backup")
            for serialized in (prepared, result):
                if serialized is not None:
                    try:
                        if not isinstance(json.loads(str(serialized)), dict):
                            raise RuntimeError("invalid_backup")
                    except ValueError as exc:
                        raise RuntimeError("invalid_backup") from exc
        if set(opening_jobs) != session_tokens:
            raise RuntimeError("invalid_backup")

        commands_by_id = {
            (str(row["tenant_ref"]), str(row["notification_id"])): command
            for row, command in notification_rows
        }
        for projection in connection.execute("SELECT * FROM correlation_projections"):
            key = (str(projection["tenant_ref"]), str(projection["notification_id"]))
            command = commands_by_id.get(key)
            notification = notifications_by_id.get(key)
            state = str(projection["state"])
            try:
                canonical_case = str(UUID(str(projection["case_id"])))
            except ValueError as exc:
                raise RuntimeError("invalid_backup") from exc
            if (
                command is None or notification is None or notification["state"] != "accepted"
                or command.event_code not in {"COR-001", "COR-002", "COR-003"}
                or command.subject_ref != f"C-{canonical_case}"
                or not valid_channel(projection["channel_id"])
                or not valid_slack_ts(projection["message_ts"])
                or not valid_datetime(str(projection["review_due_at"]).replace("Z", "+00:00"))
                or not valid_datetime(projection["created_at"])
                or not valid_datetime(projection["updated_at"])
                or (
                    projection["team_id"] is None and state != "pending"
                )
                or (
                    projection["team_id"] is not None
                    and re.fullmatch(r"T[A-Z0-9]{8,}", str(projection["team_id"])) is None
                )
                or (state in {"rejected", "delivery_unknown"}) != (projection["failure_code"] is not None)
            ):
                raise RuntimeError("invalid_backup")
    if integrity is None or integrity[0] != "ok" or version != 6:
        raise RuntimeError("invalid_backup")


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Validated Slack ledger backup/restore")
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    arguments = parser.parse_args()
    if arguments.operation == "backup":
        NotificationStore(arguments.source).backup_to(arguments.destination)
    else:
        NotificationStore.restore_from(arguments.source, arguments.destination)
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
