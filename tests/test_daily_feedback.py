from datetime import UTC, datetime, timedelta, timezone
import hashlib
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
    FixtureReconciliationGrant,
    ReviewPrincipal,
    ReviewAuthorizationError,
    ReviewDeliveryConflictError,
    ReconciliationPrincipal,
    ReconciliationServiceGrant,
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


def _reconciliation_store(
    root: Path, now: datetime
) -> DailyFeedbackBatchStore:
    return DailyFeedbackBatchStore(
        root,
        worker_grants={
            "fixture-worker": WorkerLeaseGrant(
                worker_owner="fixture-worker",
                worker_lease_generation=1,
                lease_expires_at=now + timedelta(minutes=5),
                active=True,
            )
        },
        reconciliation_grants={
            "fixture-reconciler": ReconciliationServiceGrant(
                reconciliation_owner="fixture-reconciler",
                active=True,
            ),
            "fixture-reconciler-rival": ReconciliationServiceGrant(
                reconciliation_owner="fixture-reconciler-rival",
                active=True,
            ),
        },
    )


def _started_delivery(
    root: Path, now: datetime
) -> tuple[
    DailyFeedbackBatchStore,
    CreateBatchResult,
    ReviewPrincipal,
    WorkerPrincipal,
    str,
]:
    store = _worker_store(root, now)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "session-1", True)
    worker = WorkerPrincipal("fixture-worker", 1, True)
    claim = store.claim_review_session(
        command_id="claim-1", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1,
        lease_seconds=120, now=now,
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
    store.mark_review_delivery_request_started(
        command_id="start-1", delivery_attempt_id=reserved.delivery_attempt_id,
        reviewer=reviewer, session_fence=claim.session_fence,
        worker=worker, now=now + timedelta(seconds=1),
    )
    return store, created, reviewer, worker, reserved.delivery_attempt_id


def _reserved_delivery(
    root: Path, now: datetime
) -> tuple[
    DailyFeedbackBatchStore,
    CreateBatchResult,
    ReviewPrincipal,
    WorkerPrincipal,
    int,
    str,
]:
    store = _worker_store(root, now)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "session-1", True)
    worker = WorkerPrincipal("fixture-worker", 1, True)
    claim = store.claim_review_session(
        command_id="claim-1", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1,
        lease_seconds=120, now=now,
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
    return (
        store, created, reviewer, worker, claim.session_fence,
        reserved.delivery_attempt_id,
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


def test_reserved_delivery_can_cancel_before_request_without_presentation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, reviewer, worker, fence, attempt_id = _reserved_delivery(
        root, now
    )

    cancelled = store.cancel_review_delivery_before_request(
        command_id="cancel-before-request",
        delivery_attempt_id=attempt_id,
        worker=worker,
        reason_code="reviewer_authority_revoked",
        now=now + timedelta(seconds=1),
    )

    assert cancelled.phase == "finalized"
    assert cancelled.outcome == "cancelled_before_request"
    assert cancelled.item_status == "pending"
    assert cancelled.batch_status == "ready"
    runtime = json.loads(
        (root / "runtime" / f"{created.batch.batch_id}.json").read_text()
    )
    assert runtime["delivery_attempts"][attempt_id]["cancellation_reason"] == (
        "reviewer_authority_revoked"
    )
    with pytest.raises(ReviewDeliveryConflictError, match="delivery_phase_conflict"):
        store.mark_review_delivery_request_started(
            command_id="start-after-cancel", delivery_attempt_id=attempt_id,
            reviewer=reviewer, session_fence=fence, worker=worker,
            now=now + timedelta(seconds=2),
        )


def test_stateful_connector_invokes_once_only_after_request_started(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, reviewer, worker, fence, attempt_id = _reserved_delivery(
        root, now
    )
    with pytest.raises(ReviewDeliveryConflictError, match="delivery_phase_conflict"):
        store.invoke_simulated_delivery_connector(
            command_id="invoke-before-start", delivery_attempt_id=attempt_id,
            worker=worker, configured_result="accepted",
            now=now + timedelta(seconds=1),
        )
    store.mark_review_delivery_request_started(
        command_id="start", delivery_attempt_id=attempt_id,
        reviewer=reviewer, session_fence=fence, worker=worker,
        now=now + timedelta(seconds=1),
    )

    first = store.invoke_simulated_delivery_connector(
        command_id="invoke", delivery_attempt_id=attempt_id,
        worker=worker, configured_result="accepted",
        now=now + timedelta(seconds=2),
    )
    replay = _worker_store(root, now).invoke_simulated_delivery_connector(
        command_id="invoke", delivery_attempt_id=attempt_id,
        worker=worker, configured_result="accepted",
        now=now + timedelta(seconds=3),
    )

    assert first.status == "applied"
    assert replay.status == "replayed"
    assert replay.remote_reference == first.remote_reference
    runtime = json.loads(
        (root / "runtime" / f"{created.batch.batch_id}.json").read_text()
    )
    effect = runtime["simulated_connector_effects"][attempt_id]
    assert effect["invocation_count"] == 1
    assert effect["configured_result"] == "accepted"


def test_connector_rejection_finalizes_without_presenting_and_cannot_be_forged(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, reviewer, worker, fence, attempt_id = _reserved_delivery(
        root, now
    )
    store.mark_review_delivery_request_started(
        command_id="start", delivery_attempt_id=attempt_id,
        reviewer=reviewer, session_fence=fence, worker=worker,
        now=now + timedelta(seconds=1),
    )
    effect = store.invoke_simulated_delivery_connector(
        command_id="invoke-rejected", delivery_attempt_id=attempt_id,
        worker=worker, configured_result="rejected",
        now=now + timedelta(seconds=2),
    )
    with pytest.raises(ReviewDeliveryConflictError, match="connector_result_mismatch"):
        store.finalize_review_delivery(
            command_id="forged-accept", delivery_attempt_id=attempt_id,
            worker=worker, observed_result="accepted",
            remote_reference=effect.remote_reference,
            now=now + timedelta(seconds=3),
        )

    rejected = store.finalize_review_delivery(
        command_id="finalize-rejected", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="rejected",
        remote_reference=effect.remote_reference,
        now=now + timedelta(seconds=3),
    )
    assert rejected.outcome == "rejected"
    assert rejected.item_status == "pending"
    assert rejected.batch_status == "ready"
    runtime = json.loads(
        (root / "runtime" / f"{created.batch.batch_id}.json").read_text()
    )
    assert runtime["simulated_connector_effects"][attempt_id]["invocation_count"] == 1


def test_not_applied_observation_enables_exactly_one_fenced_retry_attempt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    effect = store.invoke_simulated_delivery_connector(
        command_id="invoke-unknown", delivery_attempt_id=attempt_id,
        worker=worker, configured_result="delivery_unknown",
        configured_reconciliation_result="not_applied",
        now=now + timedelta(seconds=2),
    )
    store.finalize_review_delivery(
        command_id="finalize-unknown", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference=effect.remote_reference,
        reconciliation_deadline=now + timedelta(minutes=15),
        now=now + timedelta(seconds=3),
    )
    observation = store.submit_late_delivery_observation(
        command_id="observe-not-applied", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="not_applied",
        remote_reference=json.loads(
            (root / "runtime" / f"{created.batch.batch_id}.json").read_text()
        )["simulated_connector_effects"][attempt_id]["reconciliation_reference"],
        observed_at=now + timedelta(minutes=2),
        submitted_at=now + timedelta(minutes=3),
    )
    retry_store = DailyFeedbackBatchStore(
        root,
        worker_grants={
            "retry-worker": WorkerLeaseGrant(
                worker_owner="retry-worker", worker_lease_generation=2,
                lease_expires_at=now + timedelta(minutes=10), active=True,
            )
        },
        reconciliation_grants={
            "fixture-reconciler": ReconciliationServiceGrant(
                reconciliation_owner="fixture-reconciler", active=True
            )
        },
    )
    reconciler = ReconciliationPrincipal("fixture-reconciler", True)
    claim = retry_store.claim_delivery_reconciliation(
        command_id="claim-reconcile", delivery_attempt_id=attempt_id,
        reconciler=reconciler, lease_seconds=120,
        now=now + timedelta(minutes=3),
    )
    retry_reviewer = ReviewPrincipal(
        "reviewer-1", "binding-1", "retry-session", True
    )
    retry_session = retry_store.claim_review_session(
        command_id="claim-retry-session", batch_id=created.batch.batch_id,
        principal=retry_reviewer, expected_batch_revision=1,
        lease_seconds=120, now=now + timedelta(minutes=3),
    )
    retry_worker = WorkerPrincipal("retry-worker", 2, True)
    retry = retry_store.reconcile_review_delivery_not_applied(
        command_id="reconcile-not-applied", delivery_attempt_id=attempt_id,
        reconciler=reconciler,
        reconciliation_generation=claim.reconciliation_generation,
        observation_fingerprint=observation.observation_fingerprint,
        reviewer=retry_reviewer,
        session_fence=retry_session.session_fence,
        retry_worker=retry_worker,
        retry_worker_lease_expires_at=now + timedelta(minutes=5),
        now=now + timedelta(minutes=3, seconds=1),
    )

    assert retry.status == "applied"
    assert retry.attempt_number == 2
    assert retry.phase == "reserved"
    assert retry.previous_delivery_attempt_id == attempt_id
    runtime = json.loads(
        (root / "runtime" / f"{created.batch.batch_id}.json").read_text()
    )
    previous = runtime["delivery_attempts"][attempt_id]
    successor = runtime["delivery_attempts"][retry.delivery_attempt_id]
    assert previous["outcome"] == "not_applied"
    assert successor["semantic_delivery_key"] == previous["semantic_delivery_key"]
    assert successor["attempt_number"] == 2
    assert retry.delivery_attempt_id not in runtime["simulated_connector_effects"]

    replay = retry_store.reconcile_review_delivery_not_applied(
        command_id="reconcile-not-applied", delivery_attempt_id=attempt_id,
        reconciler=reconciler,
        reconciliation_generation=claim.reconciliation_generation,
        observation_fingerprint=observation.observation_fingerprint,
        reviewer=retry_reviewer,
        session_fence=retry_session.session_fence,
        retry_worker=retry_worker,
        retry_worker_lease_expires_at=now + timedelta(minutes=5),
        now=now + timedelta(minutes=3, seconds=2),
    )
    assert replay.status == "replayed"
    assert replay.delivery_attempt_id == retry.delivery_attempt_id
    retry_store.mark_review_delivery_request_started(
        command_id="start-retry", delivery_attempt_id=retry.delivery_attempt_id,
        reviewer=retry_reviewer,
        session_fence=retry_session.session_fence, worker=retry_worker,
        now=now + timedelta(minutes=3, seconds=3),
    )
    second_effect = retry_store.invoke_simulated_delivery_connector(
        command_id="invoke-retry", delivery_attempt_id=retry.delivery_attempt_id,
        worker=retry_worker, configured_result="accepted",
        now=now + timedelta(minutes=3, seconds=4),
    )
    runtime = json.loads(
        (root / "runtime" / f"{created.batch.batch_id}.json").read_text()
    )
    assert len(runtime["delivery_attempts"]) == 2
    assert runtime["simulated_connector_effects"][attempt_id]["invocation_count"] == 1
    assert runtime["simulated_connector_effects"][retry.delivery_attempt_id][
        "invocation_count"
    ] == 1
    assert second_effect.remote_reference != effect.remote_reference
    pristine = json.loads(json.dumps(runtime))
    runtime_path = root / "runtime" / f"{created.batch.batch_id}.json"
    mutations = (
        ("retry", "previous_delivery_attempt_id", "attempt_forged"),
        ("retry", "attempt_number", 1),
        ("retry", "semantic_delivery_key", "delivery_forged"),
        ("effect", "invocation_count", 2),
        ("effect", "configured_result", "rejected"),
        ("effect", "remote_reference", "simulated_forged"),
    )
    for target, field, value in mutations:
        tampered = json.loads(json.dumps(pristine))
        if target == "retry":
            tampered["delivery_attempts"][retry.delivery_attempt_id][field] = value
        else:
            tampered["simulated_connector_effects"][retry.delivery_attempt_id][
                field
            ] = value
        runtime_path.write_text(json.dumps(tampered) + "\n")
        runtime_path.chmod(0o600)
        with pytest.raises(StorageConflictError, match="daily_feedback_runtime_invalid"):
            retry_store.get_next_review_item(
                batch_id=created.batch.batch_id, principal=retry_reviewer,
                session_fence=retry_session.session_fence,
                now=now + timedelta(minutes=3, seconds=5),
            )
    tampered = json.loads(json.dumps(pristine))
    predecessor_effect = tampered["simulated_connector_effects"][attempt_id]
    predecessor_effect["configured_reconciliation_result"] = "accepted"
    reconciliation_canonical = json.dumps(
        {"attempt_id": attempt_id, "result": "accepted"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    predecessor_effect["reconciliation_reference"] = (
        "simulated_reconciliation_"
        + hashlib.sha256(reconciliation_canonical).hexdigest()
    )
    runtime_path.write_text(json.dumps(tampered) + "\n")
    runtime_path.chmod(0o600)
    with pytest.raises(StorageConflictError, match="daily_feedback_runtime_invalid"):
        retry_store.get_next_review_item(
            batch_id=created.batch.batch_id, principal=retry_reviewer,
            session_fence=retry_session.session_fence,
            now=now + timedelta(minutes=3, seconds=5),
        )
    runtime_path.write_text(json.dumps(pristine) + "\n")
    runtime_path.chmod(0o600)


def test_conflicting_late_evidence_blocks_not_applied_retry_without_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    effect = store.invoke_simulated_delivery_connector(
        command_id="invoke-unknown-conflict", delivery_attempt_id=attempt_id,
        worker=worker, configured_result="delivery_unknown",
        configured_reconciliation_result="not_applied",
        now=now + timedelta(seconds=1),
    )
    store.finalize_review_delivery(
        command_id="unknown", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference=effect.remote_reference,
        reconciliation_deadline=now + timedelta(minutes=15),
        now=now + timedelta(seconds=2),
    )
    not_applied = store.submit_late_delivery_observation(
        command_id="not-applied", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="not_applied",
        remote_reference=json.loads(
            (root / "runtime" / f"{created.batch.batch_id}.json").read_text()
        )["simulated_connector_effects"][attempt_id]["reconciliation_reference"],
        observed_at=now + timedelta(minutes=1),
        submitted_at=now + timedelta(minutes=2),
    )
    store.submit_late_delivery_observation(
        command_id="accepted", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="accepted", remote_reference="message-1",
        observed_at=now + timedelta(minutes=1),
        submitted_at=now + timedelta(minutes=2),
    )
    reconciler = ReconciliationPrincipal("fixture-reconciler", True)
    retry_store = DailyFeedbackBatchStore(
        root,
        worker_grants={
            "retry-worker": WorkerLeaseGrant(
                "retry-worker", 2, now + timedelta(minutes=10), True
            )
        },
        reconciliation_grants={
            "fixture-reconciler": ReconciliationServiceGrant(
                "fixture-reconciler", True
            )
        },
    )
    claim = retry_store.claim_delivery_reconciliation(
        command_id="claim-r", delivery_attempt_id=attempt_id,
        reconciler=reconciler, lease_seconds=120, now=now + timedelta(minutes=2),
    )
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "retry", True)
    session = retry_store.claim_review_session(
        command_id="claim-s", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1, lease_seconds=120,
        now=now + timedelta(minutes=2),
    )
    runtime_path = root / "runtime" / f"{created.batch.batch_id}.json"
    before = runtime_path.read_bytes()
    with pytest.raises(ReviewDeliveryConflictError, match="delivery_result_conflict"):
        retry_store.reconcile_review_delivery_not_applied(
            command_id="retry", delivery_attempt_id=attempt_id,
            reconciler=reconciler,
            reconciliation_generation=claim.reconciliation_generation,
            observation_fingerprint=not_applied.observation_fingerprint,
            reviewer=reviewer, session_fence=session.session_fence,
            retry_worker=WorkerPrincipal("retry-worker", 2, True),
            retry_worker_lease_expires_at=now + timedelta(minutes=5),
            now=now + timedelta(minutes=2, seconds=1),
        )
    assert runtime_path.read_bytes() == before

def test_worker_asserted_not_applied_without_connector_proof_cannot_retry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    store.finalize_review_delivery(
        command_id="unknown", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference="ambiguous", reconciliation_deadline=now + timedelta(minutes=15),
        now=now + timedelta(seconds=1),
    )
    observation = store.submit_late_delivery_observation(
        command_id="asserted-none", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="not_applied", remote_reference="asserted-proof",
        observed_at=now + timedelta(minutes=1),
        submitted_at=now + timedelta(minutes=2),
    )
    retry_store = DailyFeedbackBatchStore(
        root,
        worker_grants={
            "retry-worker": WorkerLeaseGrant(
                "retry-worker", 2, now + timedelta(minutes=10), True
            )
        },
        reconciliation_grants={
            "fixture-reconciler": ReconciliationServiceGrant(
                "fixture-reconciler", True
            )
        },
    )
    reconciler = ReconciliationPrincipal("fixture-reconciler", True)
    claim = retry_store.claim_delivery_reconciliation(
        command_id="claim-r-asserted", delivery_attempt_id=attempt_id,
        reconciler=reconciler, lease_seconds=120, now=now + timedelta(minutes=2),
    )
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "retry", True)
    session = retry_store.claim_review_session(
        command_id="claim-s-asserted", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1, lease_seconds=120,
        now=now + timedelta(minutes=2),
    )
    runtime_path = root / "runtime" / f"{created.batch.batch_id}.json"
    before = runtime_path.read_bytes()
    with pytest.raises(ReviewDeliveryConflictError, match="delivery_result_conflict"):
        retry_store.reconcile_review_delivery_not_applied(
            command_id="retry-asserted", delivery_attempt_id=attempt_id,
            reconciler=reconciler,
            reconciliation_generation=claim.reconciliation_generation,
            observation_fingerprint=observation.observation_fingerprint,
            reviewer=reviewer, session_fence=session.session_fence,
            retry_worker=WorkerPrincipal("retry-worker", 2, True),
            retry_worker_lease_expires_at=now + timedelta(minutes=5),
            now=now + timedelta(minutes=2, seconds=1),
        )
    assert runtime_path.read_bytes() == before


def test_not_applied_retry_requires_new_session_and_worker_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, reviewer, worker, attempt_id = _started_delivery(root, now)
    effect = store.invoke_simulated_delivery_connector(
        command_id="invoke-unknown-stale", delivery_attempt_id=attempt_id,
        worker=worker, configured_result="delivery_unknown",
        configured_reconciliation_result="not_applied",
        now=now + timedelta(seconds=2),
    )
    store.finalize_review_delivery(
        command_id="unknown-stale", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference=effect.remote_reference,
        reconciliation_deadline=now + timedelta(minutes=10),
        now=now + timedelta(seconds=3),
    )
    runtime_path = root / "runtime" / f"{created.batch.batch_id}.json"
    runtime = json.loads(runtime_path.read_text())
    proof = runtime["simulated_connector_effects"][attempt_id][
        "reconciliation_reference"
    ]
    observation = store.submit_late_delivery_observation(
        command_id="observe-stale", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="not_applied", remote_reference=proof,
        observed_at=now + timedelta(seconds=4),
        submitted_at=now + timedelta(seconds=5),
    )
    retry_store = DailyFeedbackBatchStore(
        root,
        worker_grants={
            "fixture-worker": WorkerLeaseGrant(
                "fixture-worker", 1, now + timedelta(minutes=5), True
            )
        },
        reconciliation_grants={
            "fixture-reconciler": ReconciliationServiceGrant(
                "fixture-reconciler", True
            )
        },
    )
    reconciler = ReconciliationPrincipal("fixture-reconciler", True)
    claim = retry_store.claim_delivery_reconciliation(
        command_id="claim-stale", delivery_attempt_id=attempt_id,
        reconciler=reconciler, lease_seconds=60, now=now + timedelta(seconds=6),
    )
    before = runtime_path.read_bytes()
    with pytest.raises(ReviewDeliveryConflictError, match="delivery_retry_authority_stale"):
        retry_store.reconcile_review_delivery_not_applied(
            command_id="retry-stale", delivery_attempt_id=attempt_id,
            reconciler=reconciler,
            reconciliation_generation=claim.reconciliation_generation,
            observation_fingerprint=observation.observation_fingerprint,
            reviewer=reviewer, session_fence=1, retry_worker=worker,
            retry_worker_lease_expires_at=now + timedelta(seconds=60),
            now=now + timedelta(seconds=7),
        )
    assert runtime_path.read_bytes() == before


def test_finalized_attempt_must_match_stateful_connector_ledger(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, reviewer, worker, attempt_id = _started_delivery(root, now)
    effect = store.invoke_simulated_delivery_connector(
        command_id="invoke-accepted-binding", delivery_attempt_id=attempt_id,
        worker=worker, configured_result="accepted",
        now=now + timedelta(seconds=2),
    )
    store.finalize_review_delivery(
        command_id="finalize-accepted-binding", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="accepted",
        remote_reference=effect.remote_reference,
        now=now + timedelta(seconds=3),
    )
    runtime_path = root / "runtime" / f"{created.batch.batch_id}.json"
    runtime = json.loads(runtime_path.read_text())
    ledger = runtime["simulated_connector_effects"][attempt_id]
    ledger["configured_result"] = "rejected"
    canonical = json.dumps(
        {"attempt_id": attempt_id, "result": "rejected"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    ledger["remote_reference"] = (
        "simulated_" + hashlib.sha256(canonical).hexdigest()
    )
    runtime_path.write_text(json.dumps(runtime) + "\n")
    runtime_path.chmod(0o600)

    with pytest.raises(StorageConflictError, match="daily_feedback_runtime_invalid"):
        store.get_next_review_item(
            batch_id=created.batch.batch_id, principal=reviewer,
            session_fence=1, now=now + timedelta(seconds=4),
        )


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


def test_unknown_finalization_preserves_item_and_sets_reconciliation_deadline(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    deadline = now + timedelta(minutes=15)

    result = store.finalize_review_delivery(
        command_id="finalize-unknown",
        delivery_attempt_id=attempt_id,
        worker=worker,
        observed_result="delivery_unknown",
        remote_reference="simulated-ambiguous-1",
        reconciliation_deadline=deadline,
        now=now + timedelta(seconds=2),
    )

    assert result.outcome == "delivery_unknown"
    assert result.phase == "finalized"
    assert result.item_status == "pending"
    assert result.batch_status == "ready"
    runtime = json.loads(
        (root / "runtime" / f"{created.batch.batch_id}.json").read_text()
    )
    attempt = runtime["delivery_attempts"][attempt_id]
    assert attempt["reconciliation_deadline"] == deadline.isoformat()
    with pytest.raises(ReviewDeliveryConflictError, match="delivery_phase_conflict"):
        store.finalize_review_delivery(
            command_id="finalize-conflicting",
            delivery_attempt_id=attempt_id,
            worker=worker,
            observed_result="accepted",
            remote_reference="simulated-message-1",
            now=now + timedelta(seconds=3),
        )


def test_late_observation_is_append_only_and_bound_to_historical_worker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    store.finalize_review_delivery(
        command_id="finalize-unknown", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference="simulated-ambiguous-1",
        reconciliation_deadline=now + timedelta(minutes=15),
        now=now + timedelta(seconds=2),
    )

    observation = store.submit_late_delivery_observation(
        command_id="observe-accepted",
        delivery_attempt_id=attempt_id,
        worker=worker,
        observed_result="accepted",
        remote_reference="simulated-message-1",
        observed_at=now + timedelta(seconds=3),
        submitted_at=now + timedelta(minutes=6),
    )

    assert observation.status == "applied"
    assert observation.observed_result == "accepted"
    assert observation.observation_fingerprint.startswith("sha256:")
    runtime = json.loads(
        (root / "runtime" / f"{created.batch.batch_id}.json").read_text()
    )
    attempt = runtime["delivery_attempts"][attempt_id]
    assert attempt["outcome"] == "delivery_unknown"
    assert runtime["items"][0]["status"] == "pending"
    assert len(runtime["delivery_observations"]) == 1

    with pytest.raises(ReviewDeliveryConflictError, match="worker_fence_stale"):
        store.submit_late_delivery_observation(
            command_id="observe-wrong-worker",
            delivery_attempt_id=attempt_id,
            worker=WorkerPrincipal("fixture-worker", 2, True),
            observed_result="accepted",
            remote_reference="simulated-message-1",
            observed_at=now + timedelta(seconds=3),
            submitted_at=now + timedelta(minutes=6),
        )


def test_late_observation_requires_active_server_side_historical_grant(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, _created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    store.finalize_review_delivery(
        command_id="unknown", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference="ambiguous",
        reconciliation_deadline=now + timedelta(minutes=15),
        now=now + timedelta(seconds=2),
    )
    unauthorized = DailyFeedbackBatchStore(root)
    with pytest.raises(ReviewDeliveryConflictError, match="worker_fence_stale"):
        unauthorized.submit_late_delivery_observation(
            command_id="forged-late", delivery_attempt_id=attempt_id,
            worker=worker, observed_result="accepted", remote_reference="message-1",
            observed_at=now + timedelta(minutes=2),
            submitted_at=now + timedelta(minutes=6),
        )


def test_tampered_late_observation_content_fails_integrity_validation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, reviewer, worker, attempt_id = _started_delivery(root, now)
    store.finalize_review_delivery(
        command_id="unknown", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference="ambiguous",
        reconciliation_deadline=now + timedelta(minutes=15),
        now=now + timedelta(seconds=2),
    )
    observation = store.submit_late_delivery_observation(
        command_id="observe", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="accepted", remote_reference="message-1",
        observed_at=now + timedelta(minutes=2),
        submitted_at=now + timedelta(minutes=6),
    )
    runtime_path = root / "runtime" / f"{created.batch.batch_id}.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["delivery_observations"][observation.observation_fingerprint][
        "remote_reference"
    ] = "tampered-message"
    runtime_path.write_text(json.dumps(runtime) + "\n")
    runtime_path.chmod(0o600)

    with pytest.raises(StorageConflictError, match="daily_feedback_runtime_invalid"):
        store.get_next_review_item(
            batch_id=created.batch.batch_id, principal=reviewer,
            session_fence=1, now=now,
        )


def test_reconciler_claim_consumes_compatible_observation_exactly_once(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store = _reconciliation_store(root, now)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "session-1", True)
    worker = WorkerPrincipal("fixture-worker", 1, True)
    claim = store.claim_review_session(
        command_id="claim-1", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1,
        lease_seconds=120, now=now,
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
    store.mark_review_delivery_request_started(
        command_id="start-1", delivery_attempt_id=reserved.delivery_attempt_id,
        reviewer=reviewer, session_fence=claim.session_fence,
        worker=worker, now=now + timedelta(seconds=1),
    )
    store.finalize_review_delivery(
        command_id="finalize-unknown",
        delivery_attempt_id=reserved.delivery_attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference="simulated-ambiguous-1",
        reconciliation_deadline=now + timedelta(minutes=15),
        now=now + timedelta(seconds=2),
    )
    observation = store.submit_late_delivery_observation(
        command_id="observe-accepted",
        delivery_attempt_id=reserved.delivery_attempt_id,
        worker=worker, observed_result="accepted",
        remote_reference="simulated-message-1",
        observed_at=now + timedelta(seconds=3),
        submitted_at=now + timedelta(minutes=6),
    )
    reconciler = ReconciliationPrincipal("fixture-reconciler", True)

    reconciliation_claim = store.claim_delivery_reconciliation(
        command_id="claim-reconciliation",
        delivery_attempt_id=reserved.delivery_attempt_id,
        reconciler=reconciler,
        lease_seconds=120,
        now=now + timedelta(minutes=6),
    )
    assert reconciliation_claim.reconciliation_generation == 1
    with pytest.raises(ReviewDeliveryConflictError, match="reconciliation_lease_active"):
        store.claim_delivery_reconciliation(
            command_id="claim-reconciliation-rival",
            delivery_attempt_id=reserved.delivery_attempt_id,
            reconciler=ReconciliationPrincipal("fixture-reconciler-rival", True),
            lease_seconds=120,
            now=now + timedelta(minutes=6),
        )

    reconciled = store.reconcile_review_delivery(
        command_id="reconcile-found",
        delivery_attempt_id=reserved.delivery_attempt_id,
        reconciler=reconciler,
        reconciliation_generation=reconciliation_claim.reconciliation_generation,
        resolution="found",
        observation_fingerprint=observation.observation_fingerprint,
        now=now + timedelta(minutes=6, seconds=1),
    )

    assert reconciled.outcome == "accepted"
    assert reconciled.item_status == "presented"
    assert reconciled.batch_status == "in_review"
    replay = store.reconcile_review_delivery(
        command_id="reconcile-found",
        delivery_attempt_id=reserved.delivery_attempt_id,
        reconciler=reconciler,
        reconciliation_generation=reconciliation_claim.reconciliation_generation,
        resolution="found",
        observation_fingerprint=observation.observation_fingerprint,
        now=now + timedelta(minutes=6, seconds=1),
    )
    assert replay.status == "replayed"


def test_reconciliation_rejects_conflicting_final_observations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, _created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    store.finalize_review_delivery(
        command_id="unknown", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference="ambiguous",
        reconciliation_deadline=now + timedelta(minutes=15),
        now=now + timedelta(seconds=2),
    )
    first = store.submit_late_delivery_observation(
        command_id="observe-1", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="accepted",
        remote_reference="simulated-message-1",
        observed_at=now + timedelta(seconds=3),
        submitted_at=now + timedelta(minutes=6),
    )
    store.submit_late_delivery_observation(
        command_id="observe-2", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="accepted",
        remote_reference="simulated-message-2",
        observed_at=now + timedelta(seconds=4),
        submitted_at=now + timedelta(minutes=6),
    )
    store = _reconciliation_store(root, now)
    reconciler = ReconciliationPrincipal("fixture-reconciler", True)
    lease = store.claim_delivery_reconciliation(
        command_id="claim-reconcile", delivery_attempt_id=attempt_id,
        reconciler=reconciler, lease_seconds=120,
        now=now + timedelta(minutes=6),
    )

    with pytest.raises(ReviewDeliveryConflictError, match="delivery_result_conflict"):
        store.reconcile_review_delivery(
            command_id="reconcile", delivery_attempt_id=attempt_id,
            reconciler=reconciler,
            reconciliation_generation=lease.reconciliation_generation,
            resolution="found", observation_fingerprint=first.observation_fingerprint,
            now=now + timedelta(minutes=6, seconds=1),
        )


def test_unresolved_reconciliation_waits_then_blocks_at_deadline(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    deadline = now + timedelta(minutes=15)
    store.finalize_review_delivery(
        command_id="unknown", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference="ambiguous", reconciliation_deadline=deadline,
        now=now + timedelta(seconds=2),
    )
    store = _reconciliation_store(root, now)
    reconciler = ReconciliationPrincipal("fixture-reconciler", True)
    first_lease = store.claim_delivery_reconciliation(
        command_id="claim-before", delivery_attempt_id=attempt_id,
        reconciler=reconciler, lease_seconds=60,
        now=now + timedelta(minutes=10),
    )
    pending = store.reconcile_review_delivery(
        command_id="unresolved-before", delivery_attempt_id=attempt_id,
        reconciler=reconciler,
        reconciliation_generation=first_lease.reconciliation_generation,
        resolution="unresolved", observation_fingerprint=None,
        now=now + timedelta(minutes=10, seconds=1),
    )
    assert pending.outcome == "delivery_unknown"
    assert pending.batch_status == "ready"

    second_lease = store.claim_delivery_reconciliation(
        command_id="claim-after", delivery_attempt_id=attempt_id,
        reconciler=reconciler, lease_seconds=60,
        now=now + timedelta(minutes=16),
    )
    blocked = store.reconcile_review_delivery(
        command_id="unresolved-after", delivery_attempt_id=attempt_id,
        reconciler=reconciler,
        reconciliation_generation=second_lease.reconciliation_generation,
        resolution="unresolved", observation_fingerprint=None,
        now=now + timedelta(minutes=16, seconds=1),
    )
    assert blocked.outcome == "delivery_unknown"
    assert blocked.item_status == "pending"
    assert blocked.batch_status == "blocked"
    runtime = json.loads(
        (root / "runtime" / f"{created.batch.batch_id}.json").read_text()
    )
    assert runtime["batch_status"] == "blocked"


def test_found_after_deadline_block_fails_without_corrupting_runtime(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    store.finalize_review_delivery(
        command_id="unknown", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown", remote_reference="ambiguous",
        reconciliation_deadline=now + timedelta(minutes=7),
        now=now + timedelta(seconds=2),
    )
    observation = store.submit_late_delivery_observation(
        command_id="observe", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="accepted", remote_reference="message-1",
        observed_at=now + timedelta(minutes=6),
        submitted_at=now + timedelta(minutes=6),
    )
    store = _reconciliation_store(root, now)
    reconciler = ReconciliationPrincipal("fixture-reconciler", True)
    claim = store.claim_delivery_reconciliation(
        command_id="claim", delivery_attempt_id=attempt_id,
        reconciler=reconciler, lease_seconds=120,
        now=now + timedelta(minutes=6),
    )
    store.reconcile_review_delivery(
        command_id="block", delivery_attempt_id=attempt_id,
        reconciler=reconciler,
        reconciliation_generation=claim.reconciliation_generation,
        resolution="unresolved", observation_fingerprint=None,
        now=now + timedelta(minutes=7),
    )
    runtime_path = root / "runtime" / f"{created.batch.batch_id}.json"
    before = runtime_path.read_bytes()
    with pytest.raises(ReviewDeliveryConflictError, match="delivery_result_conflict"):
        store.reconcile_review_delivery(
            command_id="found-after-block", delivery_attempt_id=attempt_id,
            reconciler=reconciler,
            reconciliation_generation=claim.reconciliation_generation,
            resolution="found",
            observation_fingerprint=observation.observation_fingerprint,
            now=now + timedelta(minutes=7, seconds=1),
        )
    assert runtime_path.read_bytes() == before
    with pytest.raises(ReviewDeliveryConflictError, match="delivery_result_conflict"):
        store.claim_delivery_reconciliation(
            command_id="claim-after-block", delivery_attempt_id=attempt_id,
            reconciler=reconciler, lease_seconds=120,
            now=now + timedelta(minutes=7, seconds=1),
        )
    assert runtime_path.read_bytes() == before


def test_reconciliation_takeover_fences_expired_generation(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, _created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    store.finalize_review_delivery(
        command_id="unknown", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference="ambiguous",
        reconciliation_deadline=now + timedelta(minutes=15),
        now=now + timedelta(seconds=2),
    )
    store = _reconciliation_store(root, now)
    first = ReconciliationPrincipal("fixture-reconciler", True)
    second = ReconciliationPrincipal("fixture-reconciler-rival", True)
    first_claim = store.claim_delivery_reconciliation(
        command_id="claim-first", delivery_attempt_id=attempt_id,
        reconciler=first, lease_seconds=10, now=now + timedelta(minutes=6),
    )
    second_claim = store.claim_delivery_reconciliation(
        command_id="claim-second", delivery_attempt_id=attempt_id,
        reconciler=second, lease_seconds=60,
        now=now + timedelta(minutes=6, seconds=11),
    )
    assert second_claim.reconciliation_generation == (
        first_claim.reconciliation_generation + 1
    )

    with pytest.raises(ReviewDeliveryConflictError, match="reconciliation_fence_stale"):
        store.reconcile_review_delivery(
            command_id="stale-unresolved", delivery_attempt_id=attempt_id,
            reconciler=first,
            reconciliation_generation=first_claim.reconciliation_generation,
            resolution="unresolved", observation_fingerprint=None,
            now=now + timedelta(minutes=6, seconds=12),
        )
    current = store.reconcile_review_delivery(
        command_id="current-unresolved", delivery_attempt_id=attempt_id,
        reconciler=second,
        reconciliation_generation=second_claim.reconciliation_generation,
        resolution="unresolved", observation_fingerprint=None,
        now=now + timedelta(minutes=6, seconds=12),
    )
    assert current.outcome == "delivery_unknown"


def test_new_same_owner_reconciliation_claim_fences_prior_claim(
    tmp_path: Path,
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, _created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    store.finalize_review_delivery(
        command_id="unknown", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference="ambiguous",
        reconciliation_deadline=now + timedelta(minutes=15),
        now=now + timedelta(seconds=2),
    )
    store = _reconciliation_store(root, now)
    reconciler = ReconciliationPrincipal("fixture-reconciler", True)
    first = store.claim_delivery_reconciliation(
        command_id="claim-first", delivery_attempt_id=attempt_id,
        reconciler=reconciler, lease_seconds=120,
        now=now + timedelta(minutes=6),
    )
    second = store.claim_delivery_reconciliation(
        command_id="claim-second", delivery_attempt_id=attempt_id,
        reconciler=reconciler, lease_seconds=120,
        now=now + timedelta(minutes=6, seconds=1),
    )
    assert second.reconciliation_generation == first.reconciliation_generation + 1
    with pytest.raises(ReviewDeliveryConflictError, match="reconciliation_fence_stale"):
        store.reconcile_review_delivery(
            command_id="stale", delivery_attempt_id=attempt_id,
            reconciler=reconciler,
            reconciliation_generation=first.reconciliation_generation,
            resolution="unresolved", observation_fingerprint=None,
            now=now + timedelta(minutes=6, seconds=2),
        )


def test_reconciliation_rejects_non_utc_clock_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store, _created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    store.finalize_review_delivery(
        command_id="unknown", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference="ambiguous",
        reconciliation_deadline=now + timedelta(minutes=15),
        now=now + timedelta(seconds=2),
    )
    store = _reconciliation_store(root, now)
    before = (root / "runtime" / next((root / "runtime").iterdir()).name).read_bytes()
    with pytest.raises(ReviewDeliveryConflictError, match="invalid_reconciliation_clock"):
        store.claim_delivery_reconciliation(
            command_id="naive", delivery_attempt_id=attempt_id,
            reconciler=ReconciliationPrincipal("fixture-reconciler", True),
            lease_seconds=60, now=datetime(2026, 8, 13, 12, 6),
        )
    after = (root / "runtime" / next((root / "runtime").iterdir()).name).read_bytes()
    assert after == before


@pytest.mark.parametrize("mutation", ["blocked_without_attempt", "orphan_observation"])
def test_reconciliation_runtime_tampering_fails_closed(
    tmp_path: Path, mutation: str
) -> None:
    root = tmp_path / "feedback"
    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    store = _reconciliation_store(root, now)
    created = _create_batch(store, fixture_set=_fixture_set("a"))
    reviewer = ReviewPrincipal("reviewer-1", "binding-1", "owner", True)
    claim = store.claim_review_session(
        command_id="claim", batch_id=created.batch.batch_id,
        principal=reviewer, expected_batch_revision=1,
        lease_seconds=120, now=now,
    )
    runtime_path = root / "runtime" / f"{created.batch.batch_id}.json"
    runtime = json.loads(runtime_path.read_text())
    if mutation == "blocked_without_attempt":
        runtime["batch_status"] = "blocked"
        runtime["batch_revision"] = 2
    else:
        runtime["delivery_observations"] = {
            "sha256:orphan": {
                "delivery_attempt_id": "attempt_missing",
                "observation_fingerprint": "sha256:orphan",
                "observed_result": "accepted",
                "remote_reference": "message",
                "observed_at": now.isoformat(),
                "submitted_at": now.isoformat(),
            }
        }
    runtime_path.write_text(json.dumps(runtime) + "\n")
    runtime_path.chmod(0o600)

    with pytest.raises(StorageConflictError, match="daily_feedback_runtime_invalid"):
        store.get_next_review_item(
            batch_id=created.batch.batch_id, principal=reviewer,
            session_fence=claim.session_fence, now=now,
        )


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


def test_reconciliation_claim_and_unresolved_over_real_http(tmp_path: Path) -> None:
    root = tmp_path / "feedback"
    now = datetime.now(UTC)
    store, _created, _reviewer, worker, attempt_id = _started_delivery(root, now)
    store.finalize_review_delivery(
        command_id="unknown-http", delivery_attempt_id=attempt_id,
        worker=worker, observed_result="delivery_unknown",
        remote_reference="ambiguous-http",
        reconciliation_deadline=now + timedelta(minutes=15),
        now=now + timedelta(seconds=2),
    )
    store = DailyFeedbackBatchStore(
        root,
        reconciliation_grants={
            "http-reconciler": ReconciliationServiceGrant(
                reconciliation_owner="http-reconciler", active=True
            )
        },
    )
    app = create_daily_feedback_fixture_app(
        store=store,
        operator_grant=FixtureOperatorGrant(
            token="controlled-operator-token", tenant_id="tenant-1",
            scope_id="scope-1", reviewer_id="reviewer-1",
            reviewer_binding_id="binding-1",
            fixture_set_ids=frozenset({"fixtures-v1"}), active=True,
        ),
        reviewer_grant=FixtureReviewerGrant(
            token="controlled-reviewer-token", reviewer_id="reviewer-1",
            reviewer_binding_id="binding-1", session_owner="owner", active=True,
        ),
        reconciliation_grant=FixtureReconciliationGrant(
            token="controlled-reconciliation-token",
            reconciliation_owner="http-reconciler", active=True,
        ),
        fixture_sets={"fixtures-v1": _fixture_set("a")},
    )

    with _real_http_server(app) as base_url:
        denied = httpx.post(
            f"{base_url}/internal/daily-feedback/deliveries/reconciliation-claims",
            headers={"Authorization": "Bearer controlled-reviewer-token"},
            json={"command_id": "denied", "delivery_attempt_id": attempt_id,
                  "lease_seconds": 120}, timeout=5,
        )
        claim = httpx.post(
            f"{base_url}/internal/daily-feedback/deliveries/reconciliation-claims",
            headers={"Authorization": "Bearer controlled-reconciliation-token"},
            json={"command_id": "claim-http-reconcile",
                  "delivery_attempt_id": attempt_id, "lease_seconds": 120},
            timeout=5,
        )
        reconcile = httpx.post(
            f"{base_url}/internal/daily-feedback/deliveries/reconcile",
            headers={"Authorization": "Bearer controlled-reconciliation-token"},
            json={"command_id": "reconcile-http", "delivery_attempt_id": attempt_id,
                  "reconciliation_generation": claim.json()["reconciliation_generation"],
                  "resolution": "unresolved", "observation_fingerprint": None},
            timeout=5,
        )

    assert denied.status_code == 401
    assert claim.status_code == 200
    assert claim.json()["reconciliation_owner"] == "http-reconciler"
    assert reconcile.status_code == 200
    assert reconcile.json()["outcome"] == "delivery_unknown"
    assert reconcile.json()["batch_status"] == "ready"


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
