"""Durable projection worker for human-handoff alerts (HND-001) into Slack.

Cada derivacion a humano que la base registra en ``human_handoff_requests`` tiene
que terminar como una tarjeta en el canal de Slack del equipo. Hasta el
2026-09-25 ninguna lo hacia: 24 derivaciones desde el 23/08 quedaron proyectadas
en Chatwoot (nota privada + asignacion) y nadie fuera de Chatwoot se entero.

El mecanismo es el mismo que ya funciona para las correlaciones de Hotmart
(``slack_projection.py``): una cola durable en Supabase que la RPC de claim
alimenta sola desde ``human_handoff_requests``, un lease por fila, y el
productor del conector central admite la notificacion. Un aviso en linea en el
momento de la derivacion habria sido mas corto de escribir y se habria perdido
en silencio cada vez que el conector no contestara.
"""

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
class SlackHandoffNotificationClaim:
    handoff_request_id: str
    external_conversation_id: int
    primary_reason_code: str
    detail_reason_code: str | None
    occurred_at: datetime
    claim_token: str
    lease_generation: int


class SlackHandoffProjectionStore(Protocol):
    async def claim_slack_handoff_notifications(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[SlackHandoffNotificationClaim]: ...

    async def complete_slack_handoff_notification(
        self,
        *,
        handoff_request_id: str,
        claim_token: str,
        lease_generation: int,
        notification_id: str,
    ) -> None: ...

    async def release_slack_handoff_notification(
        self,
        *,
        handoff_request_id: str,
        claim_token: str,
        lease_generation: int,
        failure_code: str,
    ) -> None: ...


class SlackHandoffProjectionWorker:
    """Project every durable human handoff into the central Slack connector.

    Diferencia deliberada con ``SlackCorrelationProjectionWorker``: este worker
    no se frena (``halted``) cuando el conector rechaza una tarjeta. Una
    tarjeta rechazada queda ``terminal_failed`` en la base con su motivo, y la
    derivacion siguiente sigue avisando. Frenar toda la cola por una fila
    dejaria a las personas que se derivan despues esperando a nadie, y en
    silencio, que es exactamente el hueco que este worker cierra.

    Tampoco alimenta ``/ready``: el healthcheck del bridge mata el proceso
    cuando ``/ready`` devuelve 503 (47 minutos caido el 2026-09-25), y este
    worker falla de forma esperable mientras la migracion que crea su RPC no
    este aplicada. Su estado se lee en los logs y en la tabla.
    """

    def __init__(
        self,
        *,
        store: SlackHandoffProjectionStore,
        producer: NotificationProducer,
        worker_id: str,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 1,
        lease_seconds: int = 60,
    ) -> None:
        if (
            not isinstance(worker_id, str)
            or not worker_id.strip()
            or len(worker_id) > 200
        ):
            raise ValueError("invalid_slack_handoff_projection_worker_id")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0
        ):
            raise ValueError("invalid_slack_handoff_projection_poll_interval")
        if isinstance(batch_size, bool) or batch_size != 1:
            raise ValueError("invalid_slack_handoff_projection_batch_size")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 30
            or lease_seconds > 900
        ):
            raise ValueError("invalid_slack_handoff_projection_lease_seconds")
        self._store = store
        self._notifier = SlackOperationalNotifier(producer=producer)
        self._worker_id = worker_id
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._task: asyncio.Task[None] | None = None
        self._healthy = False

    @property
    def healthy(self) -> bool:
        return (
            self._healthy
            and self._task is not None
            and not self._task.done()
        )

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="slack-handoff-projection"
            )

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
        claims = await self._store.claim_slack_handoff_notifications(
            worker_id=self._worker_id,
            limit=self._batch_size,
            lease_seconds=self._lease_seconds,
        )
        processed = 0
        for claim in claims:
            try:
                command = await self._notifier.notify_new_handoff(
                    handoff_request_id=claim.handoff_request_id,
                    external_conversation_id=claim.external_conversation_id,
                    primary_reason_code=claim.primary_reason_code,
                    detail_reason_code=claim.detail_reason_code,
                    occurred_at=claim.occurred_at,
                )
            except Exception as exc:
                failure_code = _connector_failure_code(exc)
                logger.error(
                    "slack_handoff_projection_admission_failed "
                    "handoff_request_id=%s conversation=%s failure_code=%s "
                    "error_type=%s",
                    claim.handoff_request_id,
                    claim.external_conversation_id,
                    failure_code,
                    type(exc).__name__,
                )
                # Si el release falla, la excepcion sube: el lease vence solo y
                # la fila vuelve a reclamarse en el proximo ciclo.
                await self._store.release_slack_handoff_notification(
                    handoff_request_id=claim.handoff_request_id,
                    claim_token=claim.claim_token,
                    lease_generation=claim.lease_generation,
                    failure_code=failure_code,
                )
                continue
            try:
                await self._store.complete_slack_handoff_notification(
                    handoff_request_id=claim.handoff_request_id,
                    claim_token=claim.claim_token,
                    lease_generation=claim.lease_generation,
                    notification_id=command.event_id,
                )
            except Exception as exc:
                logger.warning(
                    "slack_handoff_projection_finalize_uncertain "
                    "handoff_request_id=%s error_type=%s",
                    claim.handoff_request_id,
                    type(exc).__name__,
                )
                raise
            logger.info(
                "slack_handoff_projection_admitted handoff_request_id=%s "
                "conversation=%s notification_id=%s",
                claim.handoff_request_id,
                claim.external_conversation_id,
                command.event_id,
            )
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
                    "slack_handoff_projection_poll_failed error_type=%s",
                    type(exc).__name__,
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
