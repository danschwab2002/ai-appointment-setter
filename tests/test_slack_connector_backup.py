from __future__ import annotations

from datetime import UTC, datetime
import sqlite3
import subprocess
import sys

import pytest

from slack_correlation.catalog import NotificationCommand
from slack_correlation.store import InstanceLockError, NotificationStore


def test_online_backup_is_consistent_and_validated(tmp_path) -> None:
    source = NotificationStore(tmp_path / "live.sqlite3")
    source.initialize()
    backup = tmp_path / "backups" / "connector.sqlite3"

    source.backup_to(backup)

    with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert backup.stat().st_mode & 0o777 == 0o600


def test_offline_restore_validates_then_atomically_replaces_database(tmp_path) -> None:
    source = NotificationStore(tmp_path / "source.sqlite3")
    source.initialize()
    backup = tmp_path / "backup.sqlite3"
    source.backup_to(backup)
    destination = tmp_path / "restored.sqlite3"

    NotificationStore.restore_from(backup, destination)

    restored = NotificationStore(destination)
    restored.initialize()
    assert restored.state_inventory() == {
        "pending": 0,
        "claimed": 0,
        "request_started": 0,
        "delivery_unknown": 0,
    }


def test_restore_snapshots_a_valid_source_with_wal_state(tmp_path) -> None:
    source_path = tmp_path / "source.sqlite3"
    source = NotificationStore(source_path)
    source.initialize()
    source.admit(
        tenant_ref="johanna",
        command=NotificationCommand(
            event_id="22222222-2222-4222-8222-222222222222",
            event_code="SYS-002",
            dedupe_key="2" * 64,
            occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
            subject_ref=None,
            reason_code="synthetic_probe",
        ),
    )
    destination = tmp_path / "restored.sqlite3"

    NotificationStore.restore_from(source_path, destination)

    restored = NotificationStore(destination)
    restored.initialize()
    assert restored.count() == 1


def test_restore_accepts_a_valid_populated_one_shot_ledger(tmp_path) -> None:
    source = NotificationStore(tmp_path / "source.sqlite3")
    source.initialize()
    source.configure_activation(mode="one_shot", generation=1)
    command = NotificationCommand(
        event_id="11111111-1111-4111-8111-111111111111",
        event_code="SYS-002",
        dedupe_key="1" * 64,
        occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
        subject_ref="C-12345678",
        reason_code="synthetic_probe",
    )
    source.admit(tenant_ref="johanna", command=command)
    claim = source.claim_next(worker_id="worker-1")
    assert claim is not None
    source.mark_request_started(claim)
    source.finalize_accepted(
        claim,
        channel_id="C0C0YEACVT2",
        message_ts="1788861600.000001",
        thread_ts=None,
    )
    backup = tmp_path / "backup.sqlite3"
    source.backup_to(backup)
    destination = tmp_path / "restored.sqlite3"

    NotificationStore.restore_from(backup, destination)

    restored = NotificationStore(destination)
    restored.initialize()
    record = restored.get(tenant_ref="johanna", notification_id=command.event_id)
    assert record is not None
    assert record.state == "accepted"


def test_restore_refuses_live_destination_and_preserves_existing_bytes(tmp_path) -> None:
    destination = tmp_path / "live.sqlite3"
    live = NotificationStore(destination)
    live.acquire_instance_lock()
    live.initialize()
    backup = tmp_path / "backup.sqlite3"
    NotificationStore(tmp_path / "source.sqlite3").initialize()
    NotificationStore(tmp_path / "source.sqlite3").backup_to(backup)

    try:
        with pytest.raises(InstanceLockError, match="connector_instance_already_running"):
            NotificationStore.restore_from(backup, destination)
    finally:
        live.release_instance_lock()

    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_restore_rejects_corrupt_backup_without_touching_destination(tmp_path) -> None:
    destination = tmp_path / "destination.sqlite3"
    destination.write_bytes(b"existing-safe-bytes")
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not sqlite")

    with pytest.raises(RuntimeError, match="invalid_backup"):
        NotificationStore.restore_from(corrupt, destination)

    assert destination.read_bytes() == b"existing-safe-bytes"


def test_restore_rejects_version_only_database_without_required_schema(tmp_path) -> None:
    destination = tmp_path / "destination.sqlite3"
    destination.write_bytes(b"existing-safe-bytes")
    incomplete = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(incomplete) as connection:
        connection.execute("PRAGMA user_version = 2")

    with pytest.raises(RuntimeError, match="invalid_backup"):
        NotificationStore.restore_from(incomplete, destination)

    assert destination.read_bytes() == b"existing-safe-bytes"


def test_restore_rejects_valid_schema_without_activation_singleton(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    store = NotificationStore(source)
    store.initialize()
    backup = tmp_path / "backup.sqlite3"
    store.backup_to(backup)
    with sqlite3.connect(backup) as connection:
        connection.execute("DELETE FROM connector_meta")
    with sqlite3.connect(backup) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    destination = tmp_path / "destination.sqlite3"
    destination.write_bytes(b"existing-safe-bytes")

    with pytest.raises(RuntimeError, match="invalid_backup"):
        NotificationStore.restore_from(backup, destination)

    assert destination.read_bytes() == b"existing-safe-bytes"


def test_restore_rejects_logically_impossible_accepted_notification(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    store = NotificationStore(source)
    store.initialize()
    store.admit(
        tenant_ref="johanna",
        command=NotificationCommand(
            event_id="11111111-1111-4111-8111-111111111111",
            event_code="SYS-002",
            dedupe_key="1" * 64,
            occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
            component="slack_queue",
            state="backlogged",
            count=1,
        ),
    )
    backup = tmp_path / "backup.sqlite3"
    store.backup_to(backup)
    with sqlite3.connect(backup) as connection:
        connection.execute(
            "UPDATE notifications SET state = 'accepted' WHERE notification_id = ?",
            ("11111111-1111-4111-8111-111111111111",),
        )
    with sqlite3.connect(backup) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with pytest.raises(RuntimeError, match="invalid_backup"):
        NotificationStore.restore_from(backup, tmp_path / "destination.sqlite3")


def test_restore_rejects_unexpected_schema_objects(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    store = NotificationStore(source)
    store.initialize()
    backup = tmp_path / "backup.sqlite3"
    store.backup_to(backup)
    with sqlite3.connect(backup) as connection:
        connection.execute(
            "CREATE INDEX unexpected_notification_state ON notifications(state)"
        )
    with sqlite3.connect(backup) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with pytest.raises(RuntimeError, match="invalid_backup"):
        NotificationStore.restore_from(backup, tmp_path / "destination.sqlite3")


def test_backup_rejects_consumed_one_shot_without_delivery_evidence(tmp_path) -> None:
    source_path = tmp_path / "source.sqlite3"
    store = NotificationStore(source_path)
    store.initialize()
    store.configure_activation(mode="one_shot", generation=1)
    with sqlite3.connect(source_path) as connection:
        connection.execute(
            "UPDATE connector_meta SET activation_consumed = 1 WHERE singleton = 1"
        )

    with pytest.raises(RuntimeError, match="invalid_backup"):
        store.backup_to(tmp_path / "backup.sqlite3")


def test_backup_rejects_orphan_thread_root(tmp_path) -> None:
    source_path = tmp_path / "source.sqlite3"
    store = NotificationStore(source_path)
    store.initialize()
    with sqlite3.connect(source_path) as connection:
        connection.execute(
            """
            INSERT INTO thread_roots (
                tenant_ref, subject_ref, channel_id, message_ts, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "johanna",
                "C-12345678",
                "C0C0YEACVT2",
                "1788861600.000001",
                datetime.now(UTC).isoformat(),
            ),
        )

    with pytest.raises(RuntimeError, match="invalid_backup"):
        store.backup_to(tmp_path / "backup.sqlite3")


def test_backup_rejects_delivered_audit_for_pending_notification(tmp_path) -> None:
    source_path = tmp_path / "source.sqlite3"
    store = NotificationStore(source_path)
    store.initialize()
    command = NotificationCommand(
        event_id="33333333-3333-4333-8333-333333333333",
        event_code="SYS-002",
        dedupe_key="3" * 64,
        occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
        subject_ref=None,
        reason_code="synthetic_probe",
    )
    store.admit(tenant_ref="johanna", command=command)
    with sqlite3.connect(source_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO reconciliation_audit (
                tenant_ref, notification_id, decision, operator_id, prior_state,
                resulting_state, channel_id, message_ts, thread_ts, decided_at
            ) VALUES (?, ?, 'confirm_delivered', 'operator', 'delivery_unknown',
                      'accepted', ?, ?, NULL, ?)
            """,
            (
                "johanna",
                command.event_id,
                "C0C0YEACVT2",
                "1788861600.000001",
                datetime.now(UTC).isoformat(),
            ),
        )

    with pytest.raises(RuntimeError, match="invalid_backup"):
        store.backup_to(tmp_path / "backup.sqlite3")


def test_backup_accepts_request_started_before_activation_is_initialized(tmp_path) -> None:
    source = NotificationStore(tmp_path / "source.sqlite3")
    source.initialize()
    command = NotificationCommand(
        event_id="44444444-4444-4444-8444-444444444444",
        event_code="SYS-002",
        dedupe_key="4" * 64,
        occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
        subject_ref=None,
        reason_code="synthetic_probe",
    )
    source.admit(tenant_ref="johanna", command=command)
    claim = source.claim_next(worker_id="worker-1")
    assert claim is not None
    source.mark_request_started(claim)
    backup = tmp_path / "backup.sqlite3"

    source.backup_to(backup)

    restored_path = tmp_path / "restored.sqlite3"
    NotificationStore.restore_from(backup, restored_path)
    restored = NotificationStore(restored_path)
    restored.initialize()
    record = restored.get(tenant_ref="johanna", notification_id=command.event_id)
    assert record is not None
    assert record.state == "request_started"


def test_backup_restore_cli_executes_validated_workflow(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    NotificationStore(source).initialize()
    backup = tmp_path / "backup.sqlite3"
    restored = tmp_path / "restored.sqlite3"

    for arguments in (
        ("backup", "--source", str(source), "--destination", str(backup)),
        ("restore", "--source", str(backup), "--destination", str(restored)),
    ):
        completed = subprocess.run(
            [sys.executable, "-m", "slack_correlation.store", *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "ok"

    with sqlite3.connect(restored) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
