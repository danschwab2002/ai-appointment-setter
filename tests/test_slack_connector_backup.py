from __future__ import annotations

from datetime import UTC, datetime
import sqlite3
import subprocess
import sys
import textwrap

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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6


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


def test_backup_and_restore_reject_exact_ddl_with_extra_check(tmp_path) -> None:
    source_path = tmp_path / "source.sqlite3"
    store = NotificationStore(source_path)
    store.initialize()
    with sqlite3.connect(source_path) as connection:
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            """UPDATE sqlite_master
               SET sql = replace(sql, 'team_id TEXT',
                   'team_id TEXT CHECK(team_id IS NULL OR length(team_id) > 0)')
               WHERE type='table' AND name='notifications'"""
        )
        connection.execute("PRAGMA writable_schema = OFF")
        connection.execute("PRAGMA schema_version = 999")

    with pytest.raises(RuntimeError, match="invalid_backup"):
        store.backup_to(tmp_path / "backup-extra-check.sqlite3")
    with pytest.raises(RuntimeError, match="invalid_backup"):
        NotificationStore.restore_from(source_path, tmp_path / "restored.sqlite3")


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


def test_initialize_upgrades_v2_in_place_to_v3_ledgers(tmp_path) -> None:
    path = tmp_path / "connector.sqlite3"
    store = NotificationStore(path)
    store.initialize()
    with sqlite3.connect(path) as connection:
        for table in (
            "correlation_projections",
            "correlation_review_sessions",
            "interaction_replays",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version = 2")

    store.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "interaction_replays",
        "correlation_review_sessions",
        "correlation_projections",
    } <= tables


def test_restore_migrates_v4_snapshot_before_atomic_publish(tmp_path) -> None:
    source = tmp_path / "v4.sqlite3"
    NotificationStore(source).initialize()
    with sqlite3.connect(source) as connection:
        connection.execute("ALTER TABLE correlation_review_sessions DROP COLUMN view_hash")
        connection.execute("ALTER TABLE correlation_review_sessions DROP COLUMN view_id")
        connection.execute("PRAGMA user_version = 4")

    destination = tmp_path / "restored.sqlite3"
    NotificationStore.restore_from(source, destination)

    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(correlation_review_sessions)"
        )}
    assert {"view_id", "view_hash"} <= columns


def _leave_stale_wal(path) -> None:
    script = textwrap.dedent(
        f"""
        import os
        from datetime import UTC, datetime
        from slack_correlation.catalog import NotificationCommand
        from slack_correlation.store import NotificationStore

        path = {str(path)!r}
        store = NotificationStore(path)
        store.initialize()
        connection = store._connect()
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        store.admit(
            tenant_ref="johanna",
            command=NotificationCommand(
                event_id="99999999-9999-4999-8999-999999999999",
                event_code="SYS-002",
                dedupe_key="9" * 64,
                occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
                subject_ref=None,
                reason_code="stale_wal_probe",
            ),
        )
        assert os.path.exists(path + "-wal")
        os._exit(0)
        """
    )
    completed = subprocess.run([sys.executable, "-c", script], check=False)
    assert completed.returncode == 0
    assert path.with_name(path.name + "-wal").exists()


@pytest.mark.parametrize("boundary", ["before_publish", "after_publish"])
def test_restore_crash_boundaries_never_expose_replacement_with_stale_wal(
    tmp_path, boundary: str,
) -> None:
    source = NotificationStore(tmp_path / "source.sqlite3")
    source.initialize()
    backup = tmp_path / "backup.sqlite3"
    source.backup_to(backup)
    destination = tmp_path / "destination.sqlite3"
    _leave_stale_wal(destination)

    crash_patch = (
        "store_module.os.replace = lambda source, target: os._exit(86)"
        if boundary == "before_publish"
        else textwrap.dedent(
            """
            original_replace = store_module.os.replace
            def replace_then_crash(source, target):
                original_replace(source, target)
                os._exit(86)
            store_module.os.replace = replace_then_crash
            """
        )
    )
    script = textwrap.dedent(
        f"""
        import os
        import slack_correlation.store as store_module
        from slack_correlation.store import NotificationStore
        {textwrap.indent(crash_patch, '        ').lstrip()}
        NotificationStore.restore_from({str(backup)!r}, {str(destination)!r})
        """
    )
    completed = subprocess.run([sys.executable, "-c", script], check=False)
    assert completed.returncode == 86
    assert not destination.with_name(destination.name + "-wal").exists()
    assert not destination.with_name(destination.name + "-shm").exists()

    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        count = connection.execute("SELECT count(*) FROM notifications").fetchone()[0]
    assert count == (1 if boundary == "before_publish" else 0)


@pytest.mark.parametrize("version", [2, 3, 4])
def test_projection_migration_and_version_bump_roll_back_together_on_failpoint(
    tmp_path, monkeypatch, version: int,
) -> None:
    import slack_correlation.store as store_module

    path = tmp_path / f"v{version}.sqlite3"
    store = NotificationStore(path)
    store.initialize()
    command = NotificationCommand(
        event_id="77777777-7777-4777-8777-777777777777",
        event_code="COR-001", dedupe_key="7" * 64,
        occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
        subject_ref="C-11111111-1111-4111-8111-111111111111",
    )
    store.admit(tenant_ref="johanna", command=command, channel_id="C0C0YEACVT2")
    claim = store.claim_next(worker_id="worker-1")
    assert claim is not None
    store.mark_request_started(claim)
    store.finalize_accepted(
        claim, channel_id="C0C0YEACVT2", message_ts="1788861600.000001",
        thread_ts=None, team_id="T12345678",
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE correlation_projections SET state='request_started'"
        )
        connection.execute("ALTER TABLE correlation_projections RENAME TO projections_current")
        connection.execute(
            """CREATE TABLE correlation_projections (
                tenant_ref TEXT NOT NULL, notification_id TEXT NOT NULL,
                case_id TEXT NOT NULL, team_id TEXT, channel_id TEXT NOT NULL,
                message_ts TEXT NOT NULL, review_due_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('pending','request_started','accepted','rejected','delivery_unknown')),
                failure_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_ref, notification_id),
                UNIQUE (team_id, channel_id, message_ts),
                FOREIGN KEY (tenant_ref, notification_id)
                    REFERENCES notifications (tenant_ref, notification_id)
            )"""
        )
        connection.execute(
            "INSERT INTO correlation_projections SELECT * FROM projections_current"
        )
        connection.execute("DROP TABLE projections_current")
        connection.execute(f"PRAGMA user_version = {version}")

    original = store_module._execute_statements

    def crash_after_projection_rename(connection, script: str) -> None:
        if "correlation_projections_v3" in script:
            connection.execute(
                "ALTER TABLE correlation_projections RENAME TO correlation_projections_v3"
            )
            raise RuntimeError("migration_failpoint")
        original(connection, script)

    monkeypatch.setattr(store_module, "_execute_statements", crash_after_projection_rename)
    with pytest.raises(RuntimeError, match="migration_failpoint"):
        store.initialize()

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='correlation_projections_v3'"
        ).fetchone() is None
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='correlation_projections'"
        ).fetchone()[0]
        projection_state = connection.execute(
            "SELECT state FROM correlation_projections"
        ).fetchone()[0]
    assert "'claimed'" not in sql
    assert projection_state == "request_started"


def test_backup_rejects_unsafe_pending_opening_trigger(tmp_path) -> None:
    source_path = tmp_path / "source.sqlite3"
    store = NotificationStore(source_path)
    store.initialize()
    command = NotificationCommand(
        event_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        event_code="COR-001", dedupe_key="a" * 64,
        occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
        subject_ref="C-11111111-1111-4111-8111-111111111111",
    )
    store.admit(tenant_ref="johanna", command=command, channel_id="C0C0YEACVT2")
    claim = store.claim_next(worker_id="worker-1")
    assert claim is not None
    store.mark_request_started(claim)
    store.finalize_accepted(
        claim, channel_id="C0C0YEACVT2", message_ts="1788861600.000001",
        thread_ts=None, team_id="T12345678",
    )
    binding = store.find_correlation_binding(
        tenant_ref="johanna", team_id="T12345678", channel_id="C0C0YEACVT2",
        message_ts="1788861600.000001",
    )
    assert binding is not None
    session = store.create_review_session(
        binding=binding, team_id="T12345678", slack_user_id="U12345678",
        expires_at=1789000900,
    )
    with sqlite3.connect(source_path) as connection:
        connection.execute(
            """UPDATE correlation_opening_jobs
               SET trigger_id=?, state='pending' WHERE review_token=?""",
            ("unsafe\ntrigger", session.review_token),
        )

    with pytest.raises(RuntimeError, match="invalid_backup"):
        store.backup_to(tmp_path / "backup.sqlite3")
