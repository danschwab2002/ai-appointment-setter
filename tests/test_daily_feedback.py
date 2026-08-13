from datetime import UTC, datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import stat
import threading
import time
from contextlib import contextmanager
from collections.abc import Iterator

import httpx
import pytest
import uvicorn

from bridge.daily_feedback import (
    CreateBatchResult,
    DailyFeedbackBatchStore,
    FeedbackFixture,
    FeedbackFixtureSet,
    FixtureNotSanitizedError,
    IdempotencyConflictError,
    InvalidBatchInputError,
    LogicalBatchConflictError,
    FixtureOperatorGrant,
    StorageConflictError,
    create_daily_feedback_fixture_app,
)


def _fixture_set(*fixture_ids: str) -> FeedbackFixtureSet:
    return FeedbackFixtureSet(
        fixture_set_id="fixtures-v1",
        sanitized=True,
        fixtures=tuple(
            FeedbackFixture(
                fixture_id=fixture_id,
                canonical_conversation_ref=f"conversation-{fixture_id}",
                release_id="release-fixture-1",
                release_version=1,
                context_summary=f"Contexto {fixture_id}",
                apparent_objective="Responder una consulta directa",
                observed_outcome="Sin respuesta posterior",
            )
            for fixture_id in fixture_ids
        ),
    )


def _create_batch(
    store: DailyFeedbackBatchStore,
    *,
    command_id: str = "command-1",
    fixture_set: FeedbackFixtureSet | None = None,
) -> CreateBatchResult:
    return store.create_review_batch(
        command_id=command_id,
        tenant_id="tenant-1",
        scope_id="scope-1",
        window_start=datetime(2026, 8, 12, tzinfo=UTC),
        window_end=datetime(2026, 8, 13, tzinfo=UTC),
        selection_contract_version="fixture-selection-v1",
        selection_config_fingerprint="sha256:selection-v1",
        reviewer_id="reviewer-1",
        reviewer_binding_id="binding-1",
        fixture_set=fixture_set or _fixture_set("a", "b"),
    )


def test_creates_ready_batch_with_stable_fixture_order(tmp_path: Path) -> None:
    store = DailyFeedbackBatchStore(tmp_path / "feedback")

    result = _create_batch(store, fixture_set=_fixture_set("b", "a", "c"))

    assert result.status == "applied"
    assert result.batch.status == "ready"
    assert result.batch.item_count == 3
    assert result.batch.revision == 1
    assert [item.fixture_id for item in result.batch.items] == ["b", "a", "c"]
    assert [item.position for item in result.batch.items] == [1, 2, 3]


def test_exact_command_replay_returns_the_original_batch(tmp_path: Path) -> None:
    root = tmp_path / "feedback"

    first = _create_batch(DailyFeedbackBatchStore(root))
    replay = _create_batch(DailyFeedbackBatchStore(root))

    assert first.status == "applied"
    assert replay.status == "replayed"
    assert replay.batch == first.batch
    assert len(list((root / "commands").glob("*.json"))) == 1
    assert len(list((root / "batches").glob("*.json"))) == 1


def test_reusing_command_id_with_different_payload_fails_closed(
    tmp_path: Path,
) -> None:
    store = DailyFeedbackBatchStore(tmp_path / "feedback")
    _create_batch(store, fixture_set=_fixture_set("a"))

    with pytest.raises(IdempotencyConflictError, match="idempotency_conflict"):
        _create_batch(store, fixture_set=_fixture_set("different"))


def test_same_logical_batch_with_different_inputs_fails_closed(
    tmp_path: Path,
) -> None:
    store = DailyFeedbackBatchStore(tmp_path / "feedback")
    first = _create_batch(
        store, command_id="command-1", fixture_set=_fixture_set("a")
    )

    with pytest.raises(LogicalBatchConflictError, match="logical_batch_conflict"):
        _create_batch(
            store, command_id="command-2", fixture_set=_fixture_set("different")
        )

    assert _create_batch(
        store, command_id="command-3", fixture_set=_fixture_set("a")
    ).batch == first.batch


def test_secondary_command_id_also_fails_on_later_semantic_reuse(
    tmp_path: Path,
) -> None:
    store = DailyFeedbackBatchStore(tmp_path / "feedback")
    _create_batch(store, command_id="command-primary", fixture_set=_fixture_set("a"))
    replay = _create_batch(
        store, command_id="command-secondary", fixture_set=_fixture_set("a")
    )

    assert replay.status == "replayed"
    exact_replay = _create_batch(
        DailyFeedbackBatchStore(tmp_path / "feedback"),
        command_id="command-secondary",
        fixture_set=_fixture_set("a"),
    )
    assert exact_replay.status == "replayed"
    assert exact_replay.batch == replay.batch
    with pytest.raises(IdempotencyConflictError, match="idempotency_conflict"):
        _create_batch(
            store,
            command_id="command-secondary",
            fixture_set=_fixture_set("different"),
        )


def test_unsanitized_fixture_set_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    store = DailyFeedbackBatchStore(root)
    fixture_set = FeedbackFixtureSet(
        fixture_set_id="unsafe-fixtures",
        sanitized=False,
        fixtures=_fixture_set("a").fixtures,
    )

    with pytest.raises(FixtureNotSanitizedError, match="fixture_not_sanitized"):
        _create_batch(store, fixture_set=fixture_set)

    assert list((root / "commands").glob("*.json")) == []
    assert list((root / "logical").glob("*.json")) == []
    assert list((root / "batches").glob("*.json")) == []


def test_empty_selection_creates_completed_empty_batch(tmp_path: Path) -> None:
    result = _create_batch(
        DailyFeedbackBatchStore(tmp_path / "feedback"),
        fixture_set=_fixture_set(),
    )

    assert result.status == "applied"
    assert result.batch.status == "completed_empty"
    assert result.batch.item_count == 0
    assert result.batch.items == ()


def test_materializes_private_sanitized_snapshots_before_returning(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    result = _create_batch(
        DailyFeedbackBatchStore(root), fixture_set=_fixture_set("a", "b")
    )

    batch_record = json.loads(
        next((root / "batches").glob("*.json")).read_text(encoding="utf-8")
    )
    assert [item["snapshot_id"] for item in batch_record["items"]] == [
        result.batch.items[0].snapshot_id,
        result.batch.items[1].snapshot_id,
    ]
    for item in result.batch.items:
        snapshot_path = root / "snapshots" / f"{item.snapshot_id}.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert snapshot["fixture_id"] == item.fixture_id
        assert snapshot["context_summary"] == f"Contexto {item.fixture_id}"
        assert snapshot["release"] == {"id": "release-fixture-1", "version": 1}
        assert snapshot["sanitized"] is True
        assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o600

    for directory in (root, root / "commands", root / "logical", root / "batches", root / "snapshots"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("window_start", "window_end"),
    [
        (datetime(2026, 8, 13, tzinfo=UTC), datetime(2026, 8, 12, tzinfo=UTC)),
        (datetime(2026, 8, 12), datetime(2026, 8, 13)),
        (
            datetime(2026, 8, 12, tzinfo=timezone(timedelta(hours=-3))),
            datetime(2026, 8, 13, tzinfo=timezone(timedelta(hours=-3))),
        ),
    ],
)
def test_invalid_window_fails_before_writing(
    tmp_path: Path, window_start: datetime, window_end: datetime
) -> None:
    root = tmp_path / "feedback"
    store = DailyFeedbackBatchStore(root)

    with pytest.raises(InvalidBatchInputError, match="invalid_review_window"):
        store.create_review_batch(
            command_id="command-1",
            tenant_id="tenant-1",
            scope_id="scope-1",
            window_start=window_start,
            window_end=window_end,
            selection_contract_version="fixture-selection-v1",
            selection_config_fingerprint="sha256:selection-v1",
            reviewer_id="reviewer-1",
            reviewer_binding_id="binding-1",
            fixture_set=_fixture_set("a"),
        )

    assert list(root.rglob("*.json")) == []


def test_duplicate_fixture_identity_fails_before_writing(tmp_path: Path) -> None:
    root = tmp_path / "feedback"

    with pytest.raises(InvalidBatchInputError, match="duplicate_fixture_identity"):
        _create_batch(
            DailyFeedbackBatchStore(root), fixture_set=_fixture_set("a", "a")
        )

    assert list(root.rglob("*.json")) == []


def test_duplicate_canonical_conversation_fails_before_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    first, second = _fixture_set("a", "b").fixtures
    duplicate = FeedbackFixtureSet(
        fixture_set_id="fixtures-v1",
        sanitized=True,
        fixtures=(
            first,
            FeedbackFixture(
                fixture_id=second.fixture_id,
                canonical_conversation_ref=first.canonical_conversation_ref,
                release_id=second.release_id,
                release_version=second.release_version,
                context_summary=second.context_summary,
                apparent_objective=second.apparent_objective,
                observed_outcome=second.observed_outcome,
            ),
        ),
    )

    with pytest.raises(
        InvalidBatchInputError, match="duplicate_canonical_conversation"
    ):
        _create_batch(DailyFeedbackBatchStore(root), fixture_set=duplicate)

    assert list(root.rglob("*.json")) == []


def test_preexisting_conflicting_batch_artifact_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    first = _create_batch(DailyFeedbackBatchStore(root), fixture_set=_fixture_set("a"))
    batch_path = root / "batches" / f"{first.batch.batch_id}.json"
    batch_path.write_text('{"schema_version":1,"batch_id":"corrupt"}\n', encoding="utf-8")
    batch_path.chmod(0o600)
    (root / "logical" / next((root / "logical").iterdir()).name).unlink()
    (root / "commands" / next((root / "commands").iterdir()).name).unlink()

    with pytest.raises(StorageConflictError, match="daily_feedback_integrity_error"):
        _create_batch(DailyFeedbackBatchStore(root), fixture_set=_fixture_set("a"))


@contextmanager
def _real_http_server(app: object) -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = int(sock.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", lifespan="on")  # type: ignore[arg-type]
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def test_fixture_batch_creation_over_real_http(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    app = create_daily_feedback_fixture_app(
        store=DailyFeedbackBatchStore(root),
        operator_grant=FixtureOperatorGrant(
            token="controlled-operator-token",
            tenant_id="tenant-1",
            scope_id="scope-1",
            reviewer_id="reviewer-1",
            reviewer_binding_id="binding-1",
            fixture_set_ids=frozenset({"fixtures-v1"}),
            active=True,
        ),
        fixture_sets={"fixtures-v1": _fixture_set("a", "b", "c")},
    )
    payload = {
        "command_id": "command-http-1",
        "tenant_id": "tenant-1",
        "scope_id": "scope-1",
        "window_start": "2026-08-12T00:00:00Z",
        "window_end": "2026-08-13T00:00:00Z",
        "selection_contract_version": "fixture-selection-v1",
        "selection_config_fingerprint": "sha256:selection-v1",
        "reviewer_id": "reviewer-1",
        "reviewer_binding_id": "binding-1",
        "fixture_set_id": "fixtures-v1",
    }

    with _real_http_server(app) as base_url:
        health = httpx.get(f"{base_url}/health", timeout=5)
        created = httpx.post(
            f"{base_url}/internal/daily-feedback/fixture-batches",
            headers={"Authorization": "Bearer controlled-operator-token"},
            json=payload,
            timeout=5,
        )
        replay = httpx.post(
            f"{base_url}/internal/daily-feedback/fixture-batches",
            headers={"Authorization": "Bearer controlled-operator-token"},
            json=payload,
            timeout=5,
        )

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert created.status_code == 201
    assert created.json()["status"] == "applied"
    assert created.json()["batch"]["item_count"] == 3
    assert replay.status_code == 200
    assert replay.json()["status"] == "replayed"
    assert replay.json()["batch"] == created.json()["batch"]


def test_fixture_batch_http_rejects_missing_operator_token(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    app = create_daily_feedback_fixture_app(
        store=DailyFeedbackBatchStore(root),
        operator_grant=FixtureOperatorGrant(
            token="controlled-operator-token",
            tenant_id="tenant-1",
            scope_id="scope-1",
            reviewer_id="reviewer-1",
            reviewer_binding_id="binding-1",
            fixture_set_ids=frozenset({"fixtures-v1"}),
            active=True,
        ),
        fixture_sets={"fixtures-v1": _fixture_set("a")},
    )

    with _real_http_server(app) as base_url:
        response = httpx.post(
            f"{base_url}/internal/daily-feedback/fixture-batches",
            json={"command_id": "not-enough"},
            timeout=5,
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_operator_token"}
    assert list(root.rglob("*.json")) == []


@pytest.mark.parametrize("crash_after_write", range(1, 8))
def test_crash_at_each_publish_point_preserves_original_command_semantics(
    tmp_path: Path, crash_after_write: int
) -> None:
    root = tmp_path / "feedback"
    crashing = DailyFeedbackBatchStore(root)
    original_write = crashing._write_once
    writes = 0

    def crash_after(path: Path, envelope: dict[str, object]) -> None:
        nonlocal writes
        original_write(path, envelope)
        writes += 1
        if writes == crash_after_write:
            raise RuntimeError("simulated_crash")

    crashing._write_once = crash_after  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="simulated_crash"):
        _create_batch(crashing, fixture_set=_fixture_set("a", "b"))

    recovered = DailyFeedbackBatchStore(root)
    with pytest.raises(IdempotencyConflictError, match="idempotency_conflict"):
        _create_batch(recovered, fixture_set=_fixture_set("different"))

    replay = _create_batch(recovered, fixture_set=_fixture_set("a", "b"))
    assert replay.status == "replayed"
    assert [item.fixture_id for item in replay.batch.items] == ["a", "b"]
    assert len(list((root / "batches").glob("*.json"))) == 1


def test_replay_fails_closed_when_snapshot_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    first = _create_batch(DailyFeedbackBatchStore(root), fixture_set=_fixture_set("a"))
    (root / "snapshots" / f"{first.batch.items[0].snapshot_id}.json").unlink()

    with pytest.raises(StorageConflictError, match="daily_feedback_integrity_error"):
        _create_batch(DailyFeedbackBatchStore(root), fixture_set=_fixture_set("a"))


def test_replay_fails_closed_when_batch_is_tampered(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    first = _create_batch(DailyFeedbackBatchStore(root), fixture_set=_fixture_set("a"))
    batch_path = root / "batches" / f"{first.batch.batch_id}.json"
    batch_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "batch_id": first.batch.batch_id,
                "status": "completed_empty",
                "revision": 1,
                "items": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    batch_path.chmod(0o600)

    with pytest.raises(StorageConflictError, match="daily_feedback_integrity_error"):
        _create_batch(DailyFeedbackBatchStore(root), fixture_set=_fixture_set("a"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", "wrong-tenant"),
        ("scope_id", "wrong-scope"),
        ("reviewer_id", "wrong-reviewer"),
        ("reviewer_binding_id", "wrong-binding"),
    ],
)
def test_http_operator_grant_rejects_wrong_authority_dimension_without_writes(
    tmp_path: Path, field: str, value: str
) -> None:
    root = tmp_path / "feedback"
    app = create_daily_feedback_fixture_app(
        store=DailyFeedbackBatchStore(root),
        operator_grant=FixtureOperatorGrant(
            token="controlled-operator-token",
            tenant_id="tenant-1",
            scope_id="scope-1",
            reviewer_id="reviewer-1",
            reviewer_binding_id="binding-1",
            fixture_set_ids=frozenset({"fixtures-v1"}),
            active=True,
        ),
        fixture_sets={"fixtures-v1": _fixture_set("a")},
    )
    payload = {
        "command_id": "command-http-denied",
        "tenant_id": "tenant-1",
        "scope_id": "scope-1",
        "window_start": "2026-08-12T00:00:00Z",
        "window_end": "2026-08-13T00:00:00Z",
        "selection_contract_version": "fixture-selection-v1",
        "selection_config_fingerprint": "sha256:selection-v1",
        "reviewer_id": "reviewer-1",
        "reviewer_binding_id": "binding-1",
        "fixture_set_id": "fixtures-v1",
    }
    payload[field] = value

    with _real_http_server(app) as base_url:
        response = httpx.post(
            f"{base_url}/internal/daily-feedback/fixture-batches",
            headers={"Authorization": "Bearer controlled-operator-token"},
            json=payload,
            timeout=5,
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "operator_scope_denied"}
    assert list(root.rglob("*.json")) == []


def test_http_revoked_operator_grant_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    app = create_daily_feedback_fixture_app(
        store=DailyFeedbackBatchStore(root),
        operator_grant=FixtureOperatorGrant(
            token="controlled-operator-token",
            tenant_id="tenant-1",
            scope_id="scope-1",
            reviewer_id="reviewer-1",
            reviewer_binding_id="binding-1",
            fixture_set_ids=frozenset({"fixtures-v1"}),
            active=False,
        ),
        fixture_sets={"fixtures-v1": _fixture_set("a")},
    )

    with _real_http_server(app) as base_url:
        response = httpx.post(
            f"{base_url}/internal/daily-feedback/fixture-batches",
            headers={"Authorization": "Bearer controlled-operator-token"},
            json={"command_id": "not-enough"},
            timeout=5,
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "operator_binding_inactive"}
    assert list(root.rglob("*.json")) == []
