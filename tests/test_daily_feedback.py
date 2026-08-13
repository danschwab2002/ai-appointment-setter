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
    FixtureReviewerGrant,
    ReviewPrincipal,
    ReviewAuthorizationError,
    ReviewDeliveryConflictError,
    SessionClaimConflictError,
    StorageConflictError,
    WorkerPrincipal,
    WorkerLeaseGrant,
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


def _worker_store(
    root: Path, now: datetime, *, generation: int = 1
) -> DailyFeedbackBatchStore:
    return DailyFeedbackBatchStore(
        root,
        worker_grants={
            "fixture-worker": WorkerLeaseGrant(
                worker_owner="fixture-worker",
                worker_lease_generation=generation,
                lease_expires_at=now + timedelta(minutes=5),
                active=True,
            )
        },
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


def test_claims_review_session_without_moving_ready_batch(tmp_path: Path) -> None:
    store = DailyFeedbackBatchStore(tmp_path / "feedback")
    created = _create_batch(store, fixture_set=_fixture_set("a", "b"))
    principal = ReviewPrincipal(
        reviewer_id="reviewer-1",
        reviewer_binding_id="binding-1",
        session_owner="session-owner-1",
        active=True,
    )
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

    claimed = store.claim_review_session(
        command_id="claim-1",
        batch_id=created.batch.batch_id,
        principal=principal,
        expected_batch_revision=1,
        lease_seconds=120,
        now=now,
    )

    assert claimed.status == "applied"
    assert claimed.batch_status == "ready"
    assert claimed.batch_revision == 1
    assert claimed.session_owner == "session-owner-1"
    assert claimed.session_fence == 1
    assert claimed.lease_expires_at == now + timedelta(seconds=120)


def test_session_claim_replays_and_takeover_requires_expiry(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    store = DailyFeedbackBatchStore(root)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    first_principal = ReviewPrincipal(
        reviewer_id="reviewer-1",
        reviewer_binding_id="binding-1",
        session_owner="session-owner-1",
        active=True,
    )
    second_principal = ReviewPrincipal(
        reviewer_id="reviewer-1",
        reviewer_binding_id="binding-1",
        session_owner="session-owner-2",
        active=True,
    )
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    first = store.claim_review_session(
        command_id="claim-1",
        batch_id=created.batch.batch_id,
        principal=first_principal,
        expected_batch_revision=1,
        lease_seconds=120,
        now=now,
    )

    replay = DailyFeedbackBatchStore(root).claim_review_session(
        command_id="claim-1",
        batch_id=created.batch.batch_id,
        principal=first_principal,
        expected_batch_revision=1,
        lease_seconds=120,
        now=now + timedelta(seconds=30),
    )
    assert replay.status == "replayed"
    assert replay == first.__class__(status="replayed", **{
        field: getattr(first, field)
        for field in (
            "batch_id", "batch_status", "batch_revision", "session_owner",
            "session_fence", "lease_expires_at"
        )
    })

    with pytest.raises(SessionClaimConflictError, match="session_lease_active"):
        store.claim_review_session(
            command_id="claim-2",
            batch_id=created.batch.batch_id,
            principal=second_principal,
            expected_batch_revision=1,
            lease_seconds=120,
            now=now + timedelta(seconds=119),
        )

    takeover = store.claim_review_session(
        command_id="claim-3",
        batch_id=created.batch.batch_id,
        principal=second_principal,
        expected_batch_revision=1,
        lease_seconds=120,
        now=now + timedelta(seconds=120),
    )
    assert takeover.session_fence == 2
    assert takeover.session_owner == "session-owner-2"


@pytest.mark.parametrize(
    ("principal", "expected_revision", "lease_seconds", "reason"),
    [
        (
            ReviewPrincipal("reviewer-1", "binding-1", "owner", False),
            1,
            120,
            "reviewer_binding_inactive",
        ),
        (
            ReviewPrincipal("wrong-reviewer", "binding-1", "owner", True),
            1,
            120,
            "reviewer_authority_mismatch",
        ),
        (
            ReviewPrincipal("reviewer-1", "binding-1", "owner", True),
            2,
            120,
            "batch_revision_stale",
        ),
        (
            ReviewPrincipal("reviewer-1", "binding-1", "owner", True),
            1,
            301,
            "invalid_session_claim",
        ),
    ],
)
def test_session_claim_fails_closed_without_mutation(
    tmp_path: Path,
    principal: ReviewPrincipal,
    expected_revision: int,
    lease_seconds: int,
    reason: str,
) -> None:
    root = tmp_path / "feedback"
    store = DailyFeedbackBatchStore(root)
    created = _create_batch(store, fixture_set=_fixture_set("a"))

    with pytest.raises(SessionClaimConflictError, match=reason):
        store.claim_review_session(
            command_id="claim-denied",
            batch_id=created.batch.batch_id,
            principal=principal,
            expected_batch_revision=expected_revision,
            lease_seconds=lease_seconds,
            now=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        )

    assert list((root / "runtime").glob("*.json")) == []


def test_session_claim_command_id_conflict_fails_closed(tmp_path: Path) -> None:
    store = DailyFeedbackBatchStore(tmp_path / "feedback")
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    principal = ReviewPrincipal("reviewer-1", "binding-1", "owner", True)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store.claim_review_session(
        command_id="claim-1",
        batch_id=created.batch.batch_id,
        principal=principal,
        expected_batch_revision=1,
        lease_seconds=120,
        now=now,
    )

    with pytest.raises(IdempotencyConflictError, match="idempotency_conflict"):
        store.claim_review_session(
            command_id="claim-1",
            batch_id=created.batch.batch_id,
            principal=principal,
            expected_batch_revision=1,
            lease_seconds=60,
            now=now,
        )


@pytest.mark.parametrize(
    ("item_status", "item_revision", "batch_status", "batch_revision"),
    [
        ("presented", 1, "in_review", 2),
        ("pending", 2, "in_review", 2),
        ("presented", 2, "ready", 1),
        ("pending", 1, "in_review", 2),
    ],
)
def test_runtime_projection_semantics_tampering_fails_closed(
    tmp_path: Path,
    item_status: str,
    item_revision: int,
    batch_status: str,
    batch_revision: int,
) -> None:
    root = tmp_path / "feedback"
    store = DailyFeedbackBatchStore(root)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "owner", True)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    claim = store.claim_review_session(
        command_id="claim", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1,
        lease_seconds=120, now=now,
    )
    runtime_path = root / "runtime" / f"{created.batch.batch_id}.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["items"][0]["status"] = item_status
    runtime["items"][0]["revision"] = item_revision
    runtime["batch_status"] = batch_status
    runtime["batch_revision"] = batch_revision
    runtime_path.write_text(json.dumps(runtime) + "\n")
    runtime_path.chmod(0o600)

    with pytest.raises(StorageConflictError, match="daily_feedback_runtime_invalid"):
        store.get_next_review_item(
            batch_id=created.batch.batch_id, principal=reviewer,
            session_fence=claim.session_fence, now=now,
        )


def test_get_next_review_item_is_a_pure_authorized_read(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    store = DailyFeedbackBatchStore(root)
    created = _create_batch(store, fixture_set=_fixture_set("b", "a"))
    principal = ReviewPrincipal("reviewer-1", "binding-1", "owner", True)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    claim = store.claim_review_session(
        command_id="claim-1",
        batch_id=created.batch.batch_id,
        principal=principal,
        expected_batch_revision=1,
        lease_seconds=120,
        now=now,
    )
    runtime_path = root / "runtime" / f"{created.batch.batch_id}.json"
    before = runtime_path.read_bytes()

    item = store.get_next_review_item(
        batch_id=created.batch.batch_id,
        principal=principal,
        session_fence=claim.session_fence,
        now=now + timedelta(seconds=30),
    )

    assert item.fixture_id == "b"
    assert item.position == 1
    assert item.total == 2
    assert item.status == "pending"
    assert item.item_revision == 1
    assert item.context_summary == "Contexto b"
    assert item.apparent_objective == "Responder una consulta directa"
    assert item.observed_outcome == "Sin respuesta posterior"
    assert item.release_id == "release-fixture-1"
    assert item.release_version == 1
    assert item.payload_hash.startswith("sha256:")
    assert runtime_path.read_bytes() == before


@pytest.mark.parametrize(
    ("owner", "fence", "offset_seconds", "reason"),
    [
        ("wrong-owner", 1, 30, "session_owner_stale"),
        ("owner", 0, 30, "session_fence_stale"),
        ("owner", 1, 120, "session_lease_expired"),
    ],
)
def test_get_next_review_item_rejects_stale_session(
    tmp_path: Path,
    owner: str,
    fence: int,
    offset_seconds: int,
    reason: str,
) -> None:
    store = DailyFeedbackBatchStore(tmp_path / "feedback")
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    principal = ReviewPrincipal("reviewer-1", "binding-1", "owner", True)
    store.claim_review_session(
        command_id="claim-1",
        batch_id=created.batch.batch_id,
        principal=principal,
        expected_batch_revision=1,
        lease_seconds=120,
        now=now,
    )

    with pytest.raises(ReviewAuthorizationError, match=reason):
        store.get_next_review_item(
            batch_id=created.batch.batch_id,
            principal=ReviewPrincipal(
                "reviewer-1", "binding-1", owner, True
            ),
            session_fence=fence,
            now=now + timedelta(seconds=offset_seconds),
        )


def test_simulated_delivery_projects_presented_and_in_review_atomically(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store = _worker_store(root, now)
    created = _create_batch(store, fixture_set=_fixture_set("a", "b"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "session-1", True)
    worker = WorkerPrincipal("fixture-worker", 1, True)
    claim = store.claim_review_session(
        command_id="claim-1",
        batch_id=created.batch.batch_id,
        principal=reviewer,
        expected_batch_revision=1,
        lease_seconds=120,
        now=now,
    )
    item = store.get_next_review_item(
        batch_id=created.batch.batch_id,
        principal=reviewer,
        session_fence=claim.session_fence,
        now=now,
    )

    reserved = store.reserve_review_delivery(
        command_id="reserve-1",
        batch_id=created.batch.batch_id,
        snapshot_id=item.snapshot_id,
        payload_hash=item.payload_hash,
        reviewer=reviewer,
        session_fence=claim.session_fence,
        worker=worker,
        worker_lease_expires_at=now + timedelta(seconds=90),
        now=now,
    )
    assert reserved.status == "applied"
    assert reserved.phase == "reserved"
    assert reserved.semantic_delivery_key.startswith("delivery_")

    started = store.mark_review_delivery_request_started(
        command_id="start-1",
        delivery_attempt_id=reserved.delivery_attempt_id,
        reviewer=reviewer,
        session_fence=claim.session_fence,
        worker=worker,
        now=now + timedelta(seconds=1),
    )
    assert started.phase == "request_started"

    finalized = store.finalize_review_delivery(
        command_id="finalize-1",
        delivery_attempt_id=reserved.delivery_attempt_id,
        worker=worker,
        observed_result="accepted",
        remote_reference="simulated-message-1",
        now=now + timedelta(seconds=2),
    )
    assert finalized.phase == "finalized"
    assert finalized.outcome == "accepted"
    assert finalized.item_status == "presented"
    assert finalized.batch_status == "in_review"
    assert finalized.batch_revision == 2

    next_item = store.get_next_review_item(
        batch_id=created.batch.batch_id,
        principal=reviewer,
        session_fence=claim.session_fence,
        now=now + timedelta(seconds=3),
    )
    assert next_item.fixture_id == "a"
    assert next_item.status == "presented"
    assert next_item.item_revision == 2


def test_simulated_delivery_commands_replay_exact_durable_results(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store = _worker_store(root, now)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "session-1", True)
    worker = WorkerPrincipal("fixture-worker", 1, True)
    claim = store.claim_review_session(
        command_id="claim-1", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1, lease_seconds=120, now=now,
    )
    item = store.get_next_review_item(
        batch_id=created.batch.batch_id, principal=reviewer,
        session_fence=claim.session_fence, now=now,
    )
    reserve_args = dict(
        command_id="reserve-1", batch_id=created.batch.batch_id,
        snapshot_id=item.snapshot_id, payload_hash=item.payload_hash,
        reviewer=reviewer, session_fence=claim.session_fence, worker=worker,
        worker_lease_expires_at=now + timedelta(seconds=90), now=now,
    )
    first = store.reserve_review_delivery(**reserve_args)
    replay = _worker_store(root, now).reserve_review_delivery(**reserve_args)
    assert replay.status == "replayed"
    assert replay.delivery_attempt_id == first.delivery_attempt_id

    start_args = dict(
        command_id="start-1", delivery_attempt_id=first.delivery_attempt_id,
        reviewer=reviewer, session_fence=claim.session_fence, worker=worker,
        now=now + timedelta(seconds=1),
    )
    store.mark_review_delivery_request_started(**start_args)
    assert _worker_store(root, now).mark_review_delivery_request_started(
        **start_args
    ).status == "replayed"

    finalize_args = dict(
        command_id="finalize-1", delivery_attempt_id=first.delivery_attempt_id,
        worker=worker, observed_result="accepted",
        remote_reference="simulated-message-1", now=now + timedelta(seconds=2),
    )
    store.finalize_review_delivery(**finalize_args)
    assert _worker_store(root, now).finalize_review_delivery(
        **finalize_args
    ).status == "replayed"


def test_delivery_reservation_rejects_tampered_payload_and_duplicate_operation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store = _worker_store(root, now)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "session-1", True)
    worker = WorkerPrincipal("fixture-worker", 1, True)
    claim = store.claim_review_session(
        command_id="claim-1", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1, lease_seconds=120, now=now,
    )
    item = store.get_next_review_item(
        batch_id=created.batch.batch_id, principal=reviewer,
        session_fence=claim.session_fence, now=now,
    )
    base = dict(
        batch_id=created.batch.batch_id, snapshot_id=item.snapshot_id,
        reviewer=reviewer, session_fence=claim.session_fence, worker=worker,
        worker_lease_expires_at=now + timedelta(seconds=90), now=now,
    )
    with pytest.raises(ReviewDeliveryConflictError, match="delivery_payload_mismatch"):
        store.reserve_review_delivery(
            command_id="reserve-bad", payload_hash="sha256:tampered", **base
        )

    store.reserve_review_delivery(
        command_id="reserve-1", payload_hash=item.payload_hash, **base
    )
    with pytest.raises(ReviewDeliveryConflictError, match="delivery_operation_active"):
        store.reserve_review_delivery(
            command_id="reserve-2", payload_hash=item.payload_hash, **base
        )


def test_delivery_requires_current_session_before_request_and_current_worker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store = _worker_store(root, now)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "session-1", True)
    worker = WorkerPrincipal("fixture-worker", 1, True)
    claim = store.claim_review_session(
        command_id="claim-1", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1, lease_seconds=120, now=now,
    )
    item = store.get_next_review_item(
        batch_id=created.batch.batch_id, principal=reviewer,
        session_fence=claim.session_fence, now=now,
    )
    reserved = store.reserve_review_delivery(
        command_id="reserve-1", batch_id=created.batch.batch_id,
        snapshot_id=item.snapshot_id, payload_hash=item.payload_hash,
        reviewer=reviewer, session_fence=claim.session_fence, worker=worker,
        worker_lease_expires_at=now + timedelta(seconds=90), now=now,
    )

    with pytest.raises(ReviewDeliveryConflictError, match="session_fence_stale"):
        store.mark_review_delivery_request_started(
            command_id="start-stale", delivery_attempt_id=reserved.delivery_attempt_id,
            reviewer=reviewer, session_fence=0, worker=worker,
            now=now + timedelta(seconds=1),
        )
    with pytest.raises(ReviewDeliveryConflictError, match="worker_fence_stale"):
        store.mark_review_delivery_request_started(
            command_id="start-worker-stale",
            delivery_attempt_id=reserved.delivery_attempt_id,
            reviewer=reviewer, session_fence=claim.session_fence,
            worker=WorkerPrincipal("wrong-worker", 1, True),
            now=now + timedelta(seconds=1),
        )
    with pytest.raises(ReviewDeliveryConflictError, match="delivery_phase_conflict"):
        store.finalize_review_delivery(
            command_id="finalize-too-early",
            delivery_attempt_id=reserved.delivery_attempt_id,
            worker=worker, observed_result="accepted",
            remote_reference="simulated-message-1", now=now + timedelta(seconds=1),
        )


def test_accepted_finalization_does_not_depend_on_expired_session_lease(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store = _worker_store(root, now)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "session-1", True)
    worker = WorkerPrincipal("fixture-worker", 1, True)
    claim = store.claim_review_session(
        command_id="claim-1", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1, lease_seconds=2, now=now,
    )
    item = store.get_next_review_item(
        batch_id=created.batch.batch_id, principal=reviewer,
        session_fence=claim.session_fence, now=now,
    )
    reserved = store.reserve_review_delivery(
        command_id="reserve-1", batch_id=created.batch.batch_id,
        snapshot_id=item.snapshot_id, payload_hash=item.payload_hash,
        reviewer=reviewer, session_fence=claim.session_fence, worker=worker,
        worker_lease_expires_at=now + timedelta(seconds=10), now=now,
    )
    store.mark_review_delivery_request_started(
        command_id="start-1", delivery_attempt_id=reserved.delivery_attempt_id,
        reviewer=reviewer, session_fence=claim.session_fence, worker=worker,
        now=now + timedelta(seconds=1),
    )

    finalized = store.finalize_review_delivery(
        command_id="finalize-1", delivery_attempt_id=reserved.delivery_attempt_id,
        worker=worker, observed_result="accepted",
        remote_reference="simulated-message-1", now=now + timedelta(seconds=3),
    )
    assert finalized.outcome == "accepted"
    assert finalized.item_status == "presented"


def test_reclaims_in_review_batch_using_runtime_revision(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store = _worker_store(root, now)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    first_reviewer = ReviewPrincipal("reviewer-1", "binding-1", "session-1", True)
    second_reviewer = ReviewPrincipal("reviewer-1", "binding-1", "session-2", True)
    worker = WorkerPrincipal("fixture-worker", 1, True)
    claim = store.claim_review_session(
        command_id="claim-1", batch_id=created.batch.batch_id,
        principal=first_reviewer, expected_batch_revision=1,
        lease_seconds=2, now=now,
    )
    item = store.get_next_review_item(
        batch_id=created.batch.batch_id, principal=first_reviewer,
        session_fence=claim.session_fence, now=now,
    )
    reserved = store.reserve_review_delivery(
        command_id="reserve-1", batch_id=created.batch.batch_id,
        snapshot_id=item.snapshot_id, payload_hash=item.payload_hash,
        reviewer=first_reviewer, session_fence=claim.session_fence, worker=worker,
        worker_lease_expires_at=now + timedelta(seconds=10), now=now,
    )
    store.mark_review_delivery_request_started(
        command_id="start-1", delivery_attempt_id=reserved.delivery_attempt_id,
        reviewer=first_reviewer, session_fence=claim.session_fence, worker=worker,
        now=now + timedelta(seconds=1),
    )
    store.finalize_review_delivery(
        command_id="finalize-1", delivery_attempt_id=reserved.delivery_attempt_id,
        worker=worker, observed_result="accepted",
        remote_reference="simulated-message-1", now=now + timedelta(seconds=1),
    )

    reclaimed = store.claim_review_session(
        command_id="claim-2", batch_id=created.batch.batch_id,
        principal=second_reviewer, expected_batch_revision=2,
        lease_seconds=120, now=now + timedelta(seconds=2),
    )
    assert reclaimed.batch_status == "in_review"
    assert reclaimed.batch_revision == 2
    assert reclaimed.session_fence == 2


def test_late_exact_claim_replay_survives_runtime_revision_advance(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store = _worker_store(root, now)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "session-1", True)
    worker = WorkerPrincipal("fixture-worker", 1, True)
    claim_args = dict(
        command_id="claim-original", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1, lease_seconds=120, now=now,
    )
    claim = store.claim_review_session(**claim_args)
    item = store.get_next_review_item(
        batch_id=created.batch.batch_id, principal=reviewer,
        session_fence=claim.session_fence, now=now,
    )
    reserved = store.reserve_review_delivery(
        command_id="reserve-1", batch_id=created.batch.batch_id,
        snapshot_id=item.snapshot_id, payload_hash=item.payload_hash,
        reviewer=reviewer, session_fence=claim.session_fence, worker=worker,
        worker_lease_expires_at=now + timedelta(seconds=90), now=now,
    )
    store.mark_review_delivery_request_started(
        command_id="start-1", delivery_attempt_id=reserved.delivery_attempt_id,
        reviewer=reviewer, session_fence=claim.session_fence, worker=worker,
        now=now + timedelta(seconds=1),
    )
    store.finalize_review_delivery(
        command_id="finalize-1", delivery_attempt_id=reserved.delivery_attempt_id,
        worker=worker, observed_result="accepted",
        remote_reference="simulated-message-1", now=now + timedelta(seconds=2),
    )

    replay = _worker_store(root, now).claim_review_session(**claim_args)
    assert replay.status == "replayed"
    assert replay.batch_revision == 1
    assert replay.session_fence == 1


def test_runtime_command_id_is_global_across_batches(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    store = DailyFeedbackBatchStore(root)
    first = _create_batch(store, command_id="create-first", fixture_set=_fixture_set("a"))
    second = store.create_review_batch(
        command_id="create-second", tenant_id="tenant-1", scope_id="scope-1",
        window_start=datetime(2026, 8, 13, tzinfo=UTC),
        window_end=datetime(2026, 8, 14, tzinfo=UTC),
        selection_contract_version="fixture-selection-v1",
        selection_config_fingerprint="sha256:selection-v1",
        reviewer_id="reviewer-1", reviewer_binding_id="binding-1",
        fixture_set=_fixture_set("b"),
    )
    principal = ReviewPrincipal("reviewer-1", "binding-1", "owner", True)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store.claim_review_session(
        command_id="global-runtime-command", batch_id=first.batch.batch_id,
        principal=principal, expected_batch_revision=1, lease_seconds=120, now=now,
    )

    with pytest.raises(IdempotencyConflictError, match="idempotency_conflict"):
        store.claim_review_session(
            command_id="global-runtime-command", batch_id=second.batch.batch_id,
            principal=principal, expected_batch_revision=1,
            lease_seconds=120, now=now,
        )


def test_runtime_command_id_is_global_across_command_types(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store = _worker_store(root, now)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "owner", True)
    worker = WorkerPrincipal("fixture-worker", 1, True)
    claim = store.claim_review_session(
        command_id="globally-unique-command", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1, lease_seconds=120, now=now,
    )
    item = store.get_next_review_item(
        batch_id=created.batch.batch_id, principal=reviewer,
        session_fence=claim.session_fence, now=now,
    )

    with pytest.raises(IdempotencyConflictError, match="idempotency_conflict"):
        store.reserve_review_delivery(
            command_id="globally-unique-command", batch_id=created.batch.batch_id,
            snapshot_id=item.snapshot_id, payload_hash=item.payload_hash,
            reviewer=reviewer, session_fence=claim.session_fence, worker=worker,
            worker_lease_expires_at=now + timedelta(seconds=90), now=now,
        )


def test_command_id_is_global_between_batch_creation_and_runtime(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    store = DailyFeedbackBatchStore(root)
    created = _create_batch(
        store, command_id="shared-command", fixture_set=_fixture_set("a")
    )
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "owner", True)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

    with pytest.raises(IdempotencyConflictError, match="idempotency_conflict"):
        store.claim_review_session(
            command_id="shared-command", batch_id=created.batch.batch_id,
            principal=reviewer, expected_batch_revision=1,
            lease_seconds=120, now=now,
        )

    store.claim_review_session(
        command_id="runtime-first", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1,
        lease_seconds=120, now=now,
    )
    with pytest.raises(IdempotencyConflictError, match="idempotency_conflict"):
        store.create_review_batch(
            command_id="runtime-first", tenant_id="tenant-1", scope_id="scope-1",
            window_start=datetime(2026, 8, 13, tzinfo=UTC),
            window_end=datetime(2026, 8, 14, tzinfo=UTC),
            selection_contract_version="fixture-selection-v1",
            selection_config_fingerprint="fixture-config-v1",
            reviewer_id="reviewer-1", reviewer_binding_id="binding-1",
            fixture_set=_fixture_set("b"),
        )


@pytest.mark.parametrize(
    "mutation", ["swap", "delete", "duplicate", "position", "status", "revision"]
)
def test_runtime_item_binding_tampering_fails_closed(
    tmp_path: Path, mutation: str
) -> None:
    root = tmp_path / "feedback"
    store = DailyFeedbackBatchStore(root)
    created = _create_batch(store, fixture_set=_fixture_set("a", "b"))
    principal = ReviewPrincipal("reviewer-1", "binding-1", "owner", True)
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    claim = store.claim_review_session(
        command_id="claim-1", batch_id=created.batch.batch_id,
        principal=principal, expected_batch_revision=1, lease_seconds=120, now=now,
    )
    runtime_path = root / "runtime" / f"{created.batch.batch_id}.json"
    runtime = json.loads(runtime_path.read_text())
    if mutation == "swap":
        runtime["items"][0]["snapshot_id"], runtime["items"][1]["snapshot_id"] = (
            runtime["items"][1]["snapshot_id"], runtime["items"][0]["snapshot_id"]
        )
    elif mutation == "delete":
        runtime["items"].pop()
    elif mutation == "duplicate":
        runtime["items"][1] = dict(runtime["items"][0])
    elif mutation == "position":
        runtime["items"][0]["position"] = 2
    elif mutation == "status":
        runtime["items"][0]["status"] = "invented"
    else:
        runtime["items"][0]["revision"] = 99
    runtime_path.write_text(json.dumps(runtime) + "\n")
    runtime_path.chmod(0o600)

    with pytest.raises(StorageConflictError, match="daily_feedback_runtime_invalid"):
        store.get_next_review_item(
            batch_id=created.batch.batch_id, principal=principal,
            session_fence=claim.session_fence, now=now,
        )


def test_reclaimed_worker_generation_fences_old_generation(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store = _worker_store(root, now, generation=1)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "session-1", True)
    old_worker = WorkerPrincipal("fixture-worker", 1, True)
    claim = store.claim_review_session(
        command_id="claim-1", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1, lease_seconds=120, now=now,
    )
    item = store.get_next_review_item(
        batch_id=created.batch.batch_id, principal=reviewer,
        session_fence=claim.session_fence, now=now,
    )
    reserved = store.reserve_review_delivery(
        command_id="reserve-1", batch_id=created.batch.batch_id,
        snapshot_id=item.snapshot_id, payload_hash=item.payload_hash,
        reviewer=reviewer, session_fence=claim.session_fence, worker=old_worker,
        worker_lease_expires_at=now + timedelta(seconds=90), now=now,
    )
    reclaimed_store = _worker_store(root, now, generation=2)

    with pytest.raises(ReviewDeliveryConflictError, match="worker_fence_stale"):
        reclaimed_store.mark_review_delivery_request_started(
            command_id="start-old-worker",
            delivery_attempt_id=reserved.delivery_attempt_id,
            reviewer=reviewer, session_fence=claim.session_fence,
            worker=old_worker, now=now + timedelta(seconds=1),
        )


def test_empty_reviewer_token_is_rejected_at_app_construction(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="daily_feedback_reviewer_token_required"):
        create_daily_feedback_fixture_app(
            store=DailyFeedbackBatchStore(tmp_path / "feedback"),
            operator_grant=FixtureOperatorGrant(
                token="operator", tenant_id="tenant-1", scope_id="scope-1",
                reviewer_id="reviewer-1", reviewer_binding_id="binding-1",
                fixture_set_ids=frozenset({"fixtures-v1"}), active=True,
            ),
            reviewer_grant=FixtureReviewerGrant(
                token="", reviewer_id="reviewer-1",
                reviewer_binding_id="binding-1", session_owner="owner", active=True,
            ),
            fixture_sets={"fixtures-v1": _fixture_set("a")},
        )


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


def test_review_session_claim_and_next_item_over_real_http(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    store = DailyFeedbackBatchStore(root)
    created = _create_batch(store, fixture_set=_fixture_set("a", "b"))
    app = create_daily_feedback_fixture_app(
        store=store,
        operator_grant=FixtureOperatorGrant(
            token="controlled-operator-token",
            tenant_id="tenant-1",
            scope_id="scope-1",
            reviewer_id="reviewer-1",
            reviewer_binding_id="binding-1",
            fixture_set_ids=frozenset({"fixtures-v1"}),
            active=True,
        ),
        reviewer_grant=FixtureReviewerGrant(
            token="controlled-reviewer-token",
            reviewer_id="reviewer-1",
            reviewer_binding_id="binding-1",
            session_owner="http-session-owner",
            active=True,
        ),
        fixture_sets={"fixtures-v1": _fixture_set("a", "b")},
    )

    with _real_http_server(app) as base_url:
        claim = httpx.post(
            f"{base_url}/internal/daily-feedback/review-sessions",
            headers={"Authorization": "Bearer controlled-reviewer-token"},
            json={
                "command_id": "claim-http-1",
                "batch_id": created.batch.batch_id,
                "expected_batch_revision": 1,
                "lease_seconds": 120,
            },
            timeout=5,
        )
        item = httpx.get(
            f"{base_url}/internal/daily-feedback/review-sessions/{created.batch.batch_id}/next-item",
            headers={"Authorization": "Bearer controlled-reviewer-token"},
            params={"session_fence": claim.json()["session_fence"]},
            timeout=5,
        )

    assert claim.status_code == 201
    assert claim.json()["batch_status"] == "ready"
    assert claim.json()["session_owner"] == "http-session-owner"
    assert claim.json()["session_fence"] == 1
    assert item.status_code == 200
    assert item.json()["fixture_id"] == "a"
    assert item.json()["status"] == "pending"
    assert item.json()["position"] == 1
    assert item.json()["total"] == 2
    assert item.json()["presentation_snapshot"]["sanitized"] is True


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
