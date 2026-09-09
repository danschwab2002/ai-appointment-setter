import asyncio
from datetime import UTC, datetime
import math

import pytest

from bridge.slack_projection import (
    SlackCorrelationNotificationClaim,
    SlackCorrelationProjectionWorker,
)
from slack_correlation.catalog import NotificationCommand
from slack_correlation.producer import (
    AdmissionReceipt,
    ConnectorAdmissionUnknown,
    ConnectorRejected,
    ConnectorSemanticConflict,
)


class _Store:
    def __init__(self, claims: list[SlackCorrelationNotificationClaim]) -> None:
        self.claims = claims
        self.complete_calls: list[dict[str, object]] = []
        self.release_calls: list[dict[str, object]] = []

    async def claim_slack_correlation_notifications(
        self, **kwargs: object
    ) -> list[SlackCorrelationNotificationClaim]:
        claims, self.claims = self.claims, []
        return claims

    async def complete_slack_correlation_notification(
        self, **kwargs: object
    ) -> None:
        self.complete_calls.append(kwargs)

    async def release_slack_correlation_notification(
        self, **kwargs: object
    ) -> None:
        self.release_calls.append(kwargs)


class _Producer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.commands: list[NotificationCommand] = []

    async def admit(self, command: NotificationCommand) -> AdmissionReceipt:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return AdmissionReceipt("admitted", command.event_id, "pending")


def _claim() -> SlackCorrelationNotificationClaim:
    return SlackCorrelationNotificationClaim(
        source_event_id="11111111-1111-4111-8111-111111111111",
        outcome="ambiguous",
        reason_code="multiple_candidates",
        candidate_count=2,
        occurred_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        claim_token="22222222-2222-4222-8222-222222222222",
        lease_generation=3,
    )


def test_worker_admits_then_completes_the_exact_fenced_projection() -> None:
    store = _Store([_claim()])
    producer = _Producer()
    worker = SlackCorrelationProjectionWorker(
        store=store,
        producer=producer,
        tenant_ref="att1",
        funnel_ref="att1-main",
        worker_id="att1-slack-1",
        poll_interval_seconds=0.01,
        batch_size=1,
        lease_seconds=60,
    )

    processed = asyncio.run(worker.run_once())

    assert processed == 1
    assert len(producer.commands) == 1
    command = producer.commands[0]
    assert command.event_code == "COR-002"
    assert store.complete_calls == [
        {
            "source_event_id": _claim().source_event_id,
            "claim_token": _claim().claim_token,
            "lease_generation": 3,
            "notification_id": command.event_id,
        }
    ]
    assert store.release_calls == []
    assert worker.halted is False


def test_worker_releases_unknown_connector_admission_for_durable_retry() -> None:
    store = _Store([_claim()])
    producer = _Producer(ConnectorAdmissionUnknown("connector_admission_unknown"))
    worker = SlackCorrelationProjectionWorker(
        store=store,
        producer=producer,
        tenant_ref="lancemos",
        funnel_ref="psicologajohanna",
        worker_id="johanna-slack-1",
        poll_interval_seconds=0.01,
        batch_size=1,
        lease_seconds=60,
    )

    processed = asyncio.run(worker.run_once())

    assert processed == 0
    assert store.complete_calls == []
    assert store.release_calls == [
        {
            "source_event_id": _claim().source_event_id,
            "claim_token": _claim().claim_token,
            "lease_generation": 3,
            "failure_code": "connector_admission_unknown",
        }
    ]
    assert worker.halted is False


def test_worker_releases_and_halts_after_terminal_connector_failure() -> None:
    for error, failure_code in (
        (ConnectorSemanticConflict("conflict"), "connector_semantic_conflict"),
        (ConnectorRejected("rejected"), "connector_rejected"),
    ):
        store = _Store([_claim()])
        worker = SlackCorrelationProjectionWorker(
            store=store,
            producer=_Producer(error),
            tenant_ref="lancemos",
            funnel_ref="psicologajohanna",
            worker_id="johanna-slack-1",
        )

        assert asyncio.run(worker.run_once()) == 0
        assert store.release_calls[0]["failure_code"] == failure_code
        assert worker.halted is True


def test_worker_does_not_claim_more_work_after_it_halts() -> None:
    store = _Store([_claim()])
    worker = SlackCorrelationProjectionWorker(
        store=store,
        producer=_Producer(ConnectorSemanticConflict("conflict")),
        tenant_ref="lancemos",
        funnel_ref="psicologajohanna",
        worker_id="johanna-slack-1",
        poll_interval_seconds=0.01,
        batch_size=1,
        lease_seconds=60,
    )

    asyncio.run(worker.run_once())
    store.claims = [_claim()]
    processed = asyncio.run(worker.run_once())

    assert processed == 0
    assert store.claims == [_claim()]


def test_terminal_connector_failure_halts_even_when_release_fails() -> None:
    class ReleaseFailStore(_Store):
        async def release_slack_correlation_notification(self, **kwargs) -> None:
            raise RuntimeError("database unavailable")

    worker = SlackCorrelationProjectionWorker(
        store=ReleaseFailStore([_claim()]),
        producer=_Producer(ConnectorRejected("rejected")),
        tenant_ref="lancemos",
        funnel_ref="psicologajohanna",
        worker_id="johanna-slack-1",
    )

    assert asyncio.run(worker.run_once()) == 0
    assert worker.halted is True


def test_uncertain_completion_remains_recoverable() -> None:
    class CompletionFailStore(_Store):
        async def complete_slack_correlation_notification(self, **kwargs) -> None:
            raise RuntimeError("completion unknown")

    worker = SlackCorrelationProjectionWorker(
        store=CompletionFailStore([_claim()]),
        producer=_Producer(),
        tenant_ref="lancemos",
        funnel_ref="psicologajohanna",
        worker_id="johanna-slack-1",
    )

    with pytest.raises(RuntimeError, match="completion unknown"):
        asyncio.run(worker.run_once())
    assert worker.halted is False


def test_worker_validates_tenant_and_worker_configuration() -> None:
    store = _Store([])
    producer = _Producer()

    try:
        SlackCorrelationProjectionWorker(
            store=store,
            producer=producer,
            tenant_ref="",
            funnel_ref="att1-main",
            worker_id="att1-slack-1",
        )
    except ValueError as exc:
        assert str(exc) == "invalid_slack_projection_scope"
    else:
        raise AssertionError("blank tenant must fail")

    for worker_id in ("", "w" * 201):
        with pytest.raises(ValueError, match="worker_id"):
            SlackCorrelationProjectionWorker(
                store=store,
                producer=producer,
                tenant_ref="att1",
                funnel_ref="att1-main",
                worker_id=worker_id,
            )

    for lease_seconds in (1, 29, 901):
        with pytest.raises(ValueError, match="lease_seconds"):
            SlackCorrelationProjectionWorker(
                store=store,
                producer=producer,
                tenant_ref="att1",
                funnel_ref="att1-main",
                worker_id="worker-1",
                lease_seconds=lease_seconds,
            )


def test_worker_marks_poll_failures_unhealthy_until_a_successful_poll() -> None:
    class FlakyStore(_Store):
        fail = True

        async def claim_slack_correlation_notifications(self, **kwargs):
            if self.fail:
                self.fail = False
                raise RuntimeError("database unavailable")
            return []

    async def exercise() -> None:
        worker = SlackCorrelationProjectionWorker(
            store=FlakyStore([]),
            producer=_Producer(),
            tenant_ref="att1",
            funnel_ref="att1-main",
            worker_id="worker-1",
            poll_interval_seconds=0.01,
            batch_size=1,
            lease_seconds=60,
        )
        await worker.start()
        for _ in range(100):
            if not worker.healthy:
                break
            await asyncio.sleep(0.001)
        assert worker.healthy is False
        for _ in range(100):
            if worker.healthy:
                break
            await asyncio.sleep(0.001)
        assert worker.healthy is True
        await worker.stop()

    asyncio.run(exercise())


def test_worker_rejects_non_finite_poll_intervals() -> None:
    for poll_interval in (math.nan, math.inf, -math.inf):
        try:
            SlackCorrelationProjectionWorker(
                store=_Store([]),
                producer=_Producer(),
                tenant_ref="att1",
                funnel_ref="att1-main",
                worker_id="worker-1",
                poll_interval_seconds=poll_interval,
                batch_size=1,
                lease_seconds=60,
            )
        except ValueError as exc:
            assert str(exc) == "invalid_slack_projection_poll_interval"
        else:
            raise AssertionError("non-finite poll interval must fail")


def test_worker_rejects_multi_row_leases() -> None:
    try:
        SlackCorrelationProjectionWorker(
            store=_Store([]),
            producer=_Producer(),
            tenant_ref="att1",
            funnel_ref="att1-main",
            worker_id="worker-1",
            batch_size=2,
        )
    except ValueError as exc:
        assert str(exc) == "invalid_slack_projection_batch_size"
    else:
        raise AssertionError("multi-row leases must fail")
