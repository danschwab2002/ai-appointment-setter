"""Deferred worker that consumes Hotmart webhook events from Supabase.

Polls ``webhook_events`` for rows in ``received`` status, passes each one
through :func:`bridge.resolution.resolve_event` to resolve identity, then
sends the resulting SituationReport to the agent for a recovery decision.
Handles errors without crashing the loop.  Designed to run as a single
background asyncio task inside the FastAPI application.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from bridge.messaging import MessageSender
from bridge.recovery_agent import (
    RecoveryAgentClient,
    required_recovery_decision,
)
from bridge.resolution import ResolutionError, resolve_event
from bridge.supabase import SupabaseClient, SupabaseError

logger = logging.getLogger(__name__)


class ResolutionWorker:
    """Consume pending webhook events, resolve identity, and request a
    recovery proposal from the agent.

    The worker is intentionally single-threaded: it processes one event at a
    time to avoid race conditions on contact creation and recovery_case
    creation when two events arrive for the same buyer.
    """

    def __init__(
        self,
        *,
        supabase: SupabaseClient,
        poll_interval_seconds: float = 5.0,
        batch_size: int = 10,
        recovery_agent: RecoveryAgentClient | None = None,
        message_sender: MessageSender | None = None,
    ) -> None:
        self._supabase = supabase
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._recovery_agent = recovery_agent
        self._message_sender = message_sender
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background poll loop."""
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self, *, timeout: float = 10.0) -> None:
        """Signal the loop to stop and wait for it to finish."""
        self._stopped.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._process_batch()
            except SupabaseError:
                # Supabase is unavailable — wait and retry.
                pass
            except Exception:
                # Unexpected error — log without PII and keep running.
                logger.exception("resolution_worker_unexpected_error")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self._poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def _process_batch(self) -> None:
        events = await self._supabase.fetch_pending_events(
            limit=self._batch_size
        )
        for event in events:
            if self._stopped.is_set():
                break
            await self._process_one(event)

    async def _process_one(self, event: dict[str, Any]) -> None:
        event_id = event.get("id")
        if not isinstance(event_id, str):
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            await self._supabase.update_event_status(
                event_id=event_id, status="failed", error="invalid_payload"
            )
            return
        try:
            report = await resolve_event(
                webhook_event_id=event_id,
                payload=payload,
                supabase=self._supabase,
            )
        except ResolutionError:
            # resolve_event already marked the event as 'failed'.
            return
        except SupabaseError:
            # Supabase failed mid-resolution — mark as failed for retry.
            try:
                await self._supabase.update_event_status(
                    event_id=event_id,
                    status="failed",
                    error="supabase_mid_resolution_error",
                )
            except SupabaseError:
                pass
            return

        # If a recovery agent is configured, send the SituationReport.
        if self._recovery_agent is None:
            logger.warning(
                "recovery_agent_not_configured event_id=%s",
                report.event_id,
            )
            return

        if self._recovery_agent is not None:
            situation_report = report.to_dict()
            try:
                proposal = await self._recovery_agent.request_proposal(
                    event_id=report.event_id,
                    situation_report=situation_report,
                )
            except Exception:
                # Agent call failed — the event is already 'processed'.
                # The proposal can be retried later.
                logger.exception(
                    "recovery_agent_request_failed event_id=%s",
                    report.event_id,
                )
                return

            if proposal is None:
                logger.warning(
                    "recovery_proposal_unavailable event_id=%s",
                    report.event_id,
                )
                return

            logger.info(
                "recovery_proposal_received event_id=%s action=%s",
                report.event_id,
                proposal.get("action"),
            )

            action = proposal.get("action")
            if action != "send_first_touch":
                logger.info(
                    "recovery_proposal_no_send event_id=%s action=%s",
                    report.event_id,
                    action,
                )
                return

            required_decision = required_recovery_decision(situation_report)
            if (
                required_decision != {
                    "action": "send_first_touch",
                    "reason_code": "first_touch",
                }
                or proposal.get("reason_code") != "first_touch"
            ):
                logger.error(
                    "first_touch_decision_guard_blocked "
                    "event_id=%s required_action=%s",
                    report.event_id,
                    required_decision["action"],
                )
                return

            if self._message_sender is None:
                logger.warning(
                    "first_touch_sender_not_configured event_id=%s",
                    report.event_id,
                )
                return

            if not report.phone_available or report.buyer_phone is None:
                logger.warning(
                    "first_touch_phone_unavailable event_id=%s",
                    report.event_id,
                )
                return

            message = proposal.get("message")
            if not isinstance(message, str):
                logger.error(
                    "send_first_touch_without_message event_id=%s",
                    report.event_id,
                )
                return
            try:
                send_result = await self._message_sender.send_first_touch(
                    phone=report.buyer_phone,
                    buyer_name=report.buyer_name,
                    buyer_email=report.buyer_email,
                    content=message,
                    delivery_id=report.event_id,
                )
            except Exception:
                logger.exception(
                    "first_touch_send_failed event_id=%s",
                    report.event_id,
                )
                return
            if send_result.status == "sent":
                logger.info(
                    "first_touch_sent event_id=%s conversation=%s",
                    report.event_id,
                    send_result.conversation_id,
                )
            else:
                logger.warning(
                    "first_touch_%s event_id=%s reason=%s",
                    send_result.status,
                    report.event_id,
                    send_result.reason,
                )
