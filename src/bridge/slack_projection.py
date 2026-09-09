"""Durable projection worker for unresolved-correlation Slack alerts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
import math
from typing import Protocol

from bridge.slack_notifications import NotificationProducer, SlackOperationalNotifier
from slack_correlation.producer import (
    ConnectorAdmissionUnknown,
    ConnectorRejected,
    ConnectorSemanticConflict,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlackCorrelationNotificationClaim:
    source_event_id: str
    outcome: str
    reason_code: str
    candidate_count: int
    occurred_at: datetime
    claim_token: str
    lease_generation: int


class SlackCorrelationProjectionStore(Protocol):
    async def claim_slack_correlation_notifications(
        self,
        *,
        tenant_ref: str,
        funnel_ref: str,
        worker_id: str,
        limit: int,
        lease_seconds: int,
        binding_version: int | None,
    ) -> list[SlackCorrelationNotificationClaim]: ...

    async def complete_slack_correlation_notification(
        self,
        *,
        source_event_id: str,
        claim_token: str,
        lease_generation: int,
        notification_id: str,
    ) -> None: ...

    async def release_slack_correlation_notification(
        self,
        *,
        source_event_id: str,
        claim_token: str,
        lease_generation: int,
        failure_code: str,
    ) -> None: ...


class SlackCorrelationProjectionWorker:
    """Project durable unresolved correlations into the central connector."""

    def __init__(
        self,
        *,
        store: SlackCorrelationProjectionStore,
        producer: NotificationProducer,
        tenant_ref: str,
        funnel_ref: str,
        worker_id: str,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 1,
        lease_seconds: int = 60,
        binding_version: int | None = None,
    ) -> None:
        if (
            not isinstance(tenant_ref, str)
            or not tenant_ref.strip()
            or not isinstance(funnel_ref, str)
            or not funnel_ref.strip()
        ):
            raise ValueError("invalid_slack_projection_scope")
        if (
            not isinstance(worker_id, str)
            or not worker_id.strip()
            or len(worker_id) > 200
        ):
            raise ValueError("invalid_slack_projection_worker_id")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0
        ):
            raise ValueError("invalid_slack_projection_poll_interval")
        if isinstance(batch_size, bool) or batch_size != 1:
            raise ValueError("invalid_slack_projection_batch_size")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 30
            or lease_seconds > 900
        ):
            raise ValueError("invalid_slack_projection_lease_seconds")
        if (
            isinstance(binding_version, bool)
            or (binding_version is not None and binding_version < 1)
        ):
            raise ValueError("invalid_slack_projection_binding_version")
        self._store = store
        self._notifier = SlackOperationalNotifier(producer=producer)
        self._tenant_ref = tenant_ref
        self._funnel_ref = funnel_ref
        self._worker_id = worker_id
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._binding_version = binding_version
        self._task: asyncio.Task[None] | None = None
        self._halted = False
        self._healthy = False

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def healthy(self) -> bool:
        return (
            self._healthy
            and self._task is not None
            and not self._task.done()
        )

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="slack-correlation-projection")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def run_once(self) -> int:
        if self._halted:
            return 0
        claims = await self._store.claim_slack_correlation_notifications(
            tenant_ref=self._tenant_ref,
            funnel_ref=self._funnel_ref,
            worker_id=self._worker_id,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
            binding_version=self._binding_version,
        )
        processed = 0
        for claim in claims:
            try:
                command = await self._notifier.notify_unresolved_correlation(
                    source_event_id=claim.source_event_id,
                    outcome=claim.outcome,
                    reason_code=claim.reason_code,
                    candidate_count=claim.candidate_count,
                    occurred_at=claim.occurred_at,
                )
            except Exception as exc:
                failure_code = _connector_failure_code(exc)
                terminal = isinstance(
                    exc, (ConnectorSemanticConflict, ConnectorRejected)
                ) or not isinstance(exc, ConnectorAdmissionUnknown)
                if terminal:
                    self._halted = True
                try:
                    await self._store.release_slack_correlation_notification(
                        source_event_id=claim.source_event_id,
                        claim_token=claim.claim_token,
                        lease_generation=claim.lease_generation,
                        failure_code=failure_code,
                    )
                except Exception as release_exc:
                    logger.error(
                        "slack_projection_release_failed terminal=%s error_type=%s",
                        terminal,
                        type(release_exc).__name__,
                    )
                    if terminal:
                        return processed
                    raise
                if terminal:
                    logger.error(
                        "slack_projection_halted stage=admission error_type=%s",
                        type(exc).__name__,
                    )
                    return processed
                logger.warning(
                    "slack_projection_retry_scheduled error_type=%s",
                    type(exc).__name__,
                )
                continue
            try:
                await self._store.complete_slack_correlation_notification(
                    source_event_id=claim.source_event_id,
                    claim_token=claim.claim_token,
                    lease_generation=claim.lease_generation,
                    notification_id=command.event_id,
                )
            except Exception as exc:
                logger.warning(
                    "slack_projection_finalize_uncertain error_type=%s",
                    type(exc).__name__,
                )
                raise
            processed += 1
        return processed

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
                self._healthy = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._healthy = False
                logger.warning(
                    "slack_projection_poll_failed error_type=%s", type(exc).__name__
                )
            await asyncio.sleep(self._poll_interval_seconds)


def _connector_failure_code(exc: Exception) -> str:
    if isinstance(exc, ConnectorAdmissionUnknown):
        return "connector_admission_unknown"
    if isinstance(exc, ConnectorSemanticConflict):
        return "connector_semantic_conflict"
    if isinstance(exc, ConnectorRejected):
        return "connector_rejected"
    return "connector_unexpected_error"
