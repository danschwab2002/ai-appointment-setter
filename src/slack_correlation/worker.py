"""Single-replica durable outbound worker for Slack notifications."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from slack_correlation.catalog import render_message
from slack_correlation.client import (
    SlackMessageReference,
    SlackProtocolError,
    SlackRejectedError,
)
from slack_correlation.store import NotificationClaim, NotificationStore

logger = logging.getLogger(__name__)


class SlackMessageSender(Protocol):
    async def post_message(
        self,
        *,
        channel_id: str,
        message: dict[str, Any],
    ) -> SlackMessageReference: ...


class NotificationWorker:
    def __init__(
        self,
        *,
        store: NotificationStore,
        slack_client: SlackMessageSender,
        tenant_channels: dict[str, str] | None = None,
        tenant_labels: dict[str, str],
        worker_id: str,
        team_id: str | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._store = store
        self._slack = slack_client
        self._tenant_channels = dict(tenant_channels or {})
        self._tenant_labels = dict(tenant_labels)
        self._worker_id = worker_id
        self._team_id = team_id
        self._poll_interval = poll_interval_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._halted = False
        self._halt_reason: str | None = None

    @property
    def healthy(self) -> bool:
        return not self._halted

    def resume_after_verified_connectivity(self) -> None:
        if self._halt_reason not in {None, "connectivity", "delivery_unknown"}:
            return
        try:
            unresolved = self._store.state_inventory()["delivery_unknown"]
        except Exception:
            self.halt("storage")
            return
        if unresolved:
            self.halt("delivery_unknown")
            return
        self._halted = False
        self._halt_reason = None

    def halt(self, reason: str = "connectivity") -> None:
        if (
            reason == "connectivity"
            and self._halted
            and self._halt_reason not in {None, "connectivity"}
        ):
            return
        self._halted = True
        self._halt_reason = reason

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("slack_worker_already_started")
        await asyncio.to_thread(self._store.recover_incomplete)
        inventory = await asyncio.to_thread(self._store.state_inventory)
        if inventory["delivery_unknown"]:
            self.halt("delivery_unknown")
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="slack-notification-worker")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("slack_notification_worker_error")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._poll_interval
                )
            except TimeoutError:
                pass

    async def run_once(self) -> bool:
        if self._halted:
            return False
        try:
            inventory = await asyncio.to_thread(self._store.state_inventory)
        except Exception:
            self.halt("storage")
            raise
        if inventory["delivery_unknown"]:
            self.halt("delivery_unknown")
            return False
        claim = await asyncio.to_thread(
            self._store.claim_next, worker_id=self._worker_id
        )
        if claim is None:
            return False
        try:
            tenant_label = self._tenant_labels[claim.tenant_ref]
            configured_channel = self._tenant_channels.get(claim.tenant_ref)
            if configured_channel is None:
                raise RuntimeError("tenant_channel_unconfigured")
            if claim.team_id != self._team_id:
                raise RuntimeError("tenant_team_binding_mismatch")
            if claim.channel_id is not None and claim.channel_id != configured_channel:
                raise RuntimeError("tenant_channel_binding_mismatch")
            channel_id = claim.channel_id or configured_channel
            thread_ts = await asyncio.to_thread(self._store.resolve_thread_ts, claim)
            message = render_message(
                claim.command,
                tenant_label=tenant_label,
                thread_ts=thread_ts,
            )
        except Exception:
            self.halt("internal")
            await asyncio.to_thread(self._store.release_claim, claim)
            raise

        try:
            await asyncio.to_thread(self._store.mark_request_started, claim)
        except Exception:
            self.halt("storage")
            try:
                await asyncio.to_thread(self._store.release_claim, claim)
            except Exception:
                logger.exception("slack_notification_claim_release_failed")
            raise
        try:
            reference = await self._slack.post_message(
                channel_id=channel_id,
                message=message,
            )
        except asyncio.CancelledError:
            self.halt("delivery_unknown")
            await self._finalize_unknown(claim)
            raise
        except SlackRejectedError:
            self.halt("slack_rejected")
            await asyncio.to_thread(
                self._store.finalize_rejected,
                claim,
                failure_code="slack_rejected",
            )
            return True
        except SlackProtocolError:
            self.halt("delivery_unknown")
            await self._finalize_unknown(claim)
            return True

        try:
            await asyncio.to_thread(
                self._store.finalize_accepted,
                claim,
                channel_id=reference.channel_id,
                message_ts=reference.message_ts,
                thread_ts=thread_ts,
                team_id=self._team_id,
            )
        except Exception:
            self.halt("delivery_unknown")
            await self._finalize_unknown(claim)
            raise
        return True

    async def _finalize_unknown(self, claim: NotificationClaim) -> None:
        task = asyncio.create_task(
            asyncio.to_thread(
                self._store.finalize_delivery_unknown,
                claim,
                failure_code="slack_delivery_unknown",
            )
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
