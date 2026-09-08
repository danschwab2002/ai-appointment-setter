from __future__ import annotations

import sqlite3
import subprocess
import sys

import pytest

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
