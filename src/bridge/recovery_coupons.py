"""Worker del cupon personal del carrito abandonado (A1 de ATT1).

Cada ciclo reclama con lease (``claim_due_recovery_coupons_v1``) los cupones
que hay que crear o verificar, y por cada uno habla con Hotmart y deja el
resultado con ``settle_recovery_coupon_v1``. La base siembra la fila, impone
las transiciones y el tope de dos codigos por caso; este worker solo ejecuta.

Estados que recibe:

- ``reserved``: pide el token, marca ``request_started`` (la base conserva el
  lease), crea el cupon y lo deja ``created`` con la verificacion a los 7 min.
- ``create_unknown``: no se sabe si Hotmart lo creo. Primero lo busca; si esta,
  ``created``; si no, lo crea como un ``reserved``.
- ``created``: lo lee; ``valid`` y ``active`` -> ``valid``.

Un cupon recien creado tarda ~5,5 min en aplicar en el checkout aunque la API
diga ``valid`` (medido el 18/10): por eso la primera verificacion va 7 min
despues de crearlo.

Lo que se loguea: el id de la fila, el estado y el codigo de error. Nunca el
codigo del cupon, el token ni las credenciales.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from bridge.hotmart_api import (
    HotmartApiClient,
    HotmartApiError,
    HotmartCoupon,
    HotmartPermanentError,
    HotmartTransientError,
    hotmart_coupon_window,
)
from bridge.supabase import (
    RecoveryCouponClaim,
    SupabaseClient,
    SupabaseError,
)

logger = logging.getLogger(__name__)

VERIFY_AFTER_CREATE = timedelta(minutes=7)
RETRY_AFTER = timedelta(minutes=1)
DEFAULT_LEAD = timedelta(minutes=15)
DEFAULT_VALIDITY = timedelta(hours=24)
DEFAULT_DISCOUNT = 0.10


async def _await_despite_cancellation(coro: Any) -> Any:
    """Terminar un await aunque la cancelacion se repita (como bridge.worker)."""
    task = asyncio.create_task(coro)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


class RecoveryCouponWorker:
    """Crea en Hotmart el cupon personal de cada carrito y lo verifica."""

    def __init__(
        self,
        *,
        supabase: SupabaseClient,
        hotmart: HotmartApiClient,
        code_prefix: str,
        worker_id: str | None = None,
        poll_interval_seconds: float = 30.0,
        batch_size: int = 10,
        lease_seconds: int = 120,
        lead: timedelta = DEFAULT_LEAD,
        validity: timedelta = DEFAULT_VALIDITY,
        discount: float = DEFAULT_DISCOUNT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._supabase = supabase
        self._hotmart = hotmart
        self._code_prefix = code_prefix
        self._worker_id = worker_id or f"recovery-coupon:{uuid.uuid4().hex[:12]}"
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._lead = lead
        self._validity = validity
        self._discount = discount
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self, *, timeout: float = 10.0) -> None:
        await _await_despite_cancellation(self._stop_bounded(timeout=timeout))

    async def _stop_bounded(self, *, timeout: float) -> None:
        self._stopped.set()
        task = self._task
        if task is None:
            return
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=max(timeout, 1.0))
        if not done:
            logger.warning("recovery_coupon_worker_shutdown_timed_out")
            return
        self._task = None
        if task.cancelled():
            return
        task.result()

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.run_once()
            except SupabaseError:
                logger.warning("recovery_coupon_claim_failed")
            except Exception:
                logger.exception("recovery_coupon_worker_unexpected_error")
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self._poll_interval
                )
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> int:
        now = self._clock()
        claims = await self._supabase.claim_due_recovery_coupons(
            now=now.isoformat(),
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
            code_prefix=self._code_prefix,
            lead_seconds=int(self._lead.total_seconds()),
            validity_seconds=int(self._validity.total_seconds()),
            discount=self._discount,
            limit=self._batch_size,
        )
        processed = 0
        for claim in claims:
            if self._stopped.is_set():
                break
            try:
                await self._process(claim)
            except SupabaseError as exc:
                logger.warning(
                    "recovery_coupon_settle_failed coupon_id=%s reason=%s",
                    claim.coupon_id,
                    getattr(exc, "reason", None) or type(exc).__name__,
                )
                continue
            processed += 1
        return processed

    async def _process(self, claim: RecoveryCouponClaim) -> None:
        if claim.status == "reserved":
            await self._create(claim)
        elif claim.status == "create_unknown":
            await self._resolve_unknown(claim)
        elif claim.status == "created":
            await self._verify(claim)

    async def _settle(
        self,
        claim: RecoveryCouponClaim,
        status: str,
        *,
        hotmart_coupon_id: int | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        failure_code: str | None = None,
        retry_after: timedelta | None = None,
    ) -> None:
        now = self._clock()
        await self._supabase.settle_recovery_coupon(
            coupon_id=claim.coupon_id,
            worker_id=self._worker_id,
            status=status,
            now=now.isoformat(),
            hotmart_coupon_id=hotmart_coupon_id,
            hotmart_start_ms=start_ms,
            hotmart_end_ms=end_ms,
            failure_code=failure_code,
            next_attempt_at=(
                None if retry_after is None else (now + retry_after).isoformat()
            ),
        )
        logger.info(
            "recovery_coupon_settled coupon_id=%s status=%s failure_code=%s "
            "attempt=%s",
            claim.coupon_id,
            status,
            failure_code,
            claim.attempt_count,
        )

    async def _create(self, claim: RecoveryCouponClaim) -> None:
        try:
            start_ms, end_ms = hotmart_coupon_window(self._clock(), claim.expires_at)
        except ValueError:
            await self._settle(
                claim, "create_failed", failure_code="coupon_window_expired"
            )
            return
        try:
            # El token antes de request_started: si Hotmart no lo da, no se
            # llamo a crear nada y la fila vuelve sola al vencer el lease.
            await self._hotmart.get_token()
        except HotmartApiError as exc:
            logger.warning(
                "recovery_coupon_hotmart_token_failed coupon_id=%s error_code=%s",
                claim.coupon_id,
                exc.code,
            )
            return
        await self._settle(claim, "request_started")
        try:
            created = await self._hotmart.create_coupon(
                claim.external_product_id,
                claim.code,
                self._discount,
                start_ms,
                end_ms,
            )
        except HotmartTransientError as exc:
            await self._settle(
                claim,
                "create_unknown",
                failure_code=exc.code,
                retry_after=RETRY_AFTER,
            )
            return
        except HotmartPermanentError as exc:
            await self._settle(claim, "create_failed", failure_code=exc.code)
            return
        await self._settle(
            claim,
            "created",
            hotmart_coupon_id=created.coupon_id,
            start_ms=start_ms,
            end_ms=end_ms,
            retry_after=VERIFY_AFTER_CREATE,
        )

    async def _resolve_unknown(self, claim: RecoveryCouponClaim) -> None:
        try:
            found = await self._hotmart.find_coupon(
                claim.external_product_id, claim.code
            )
        except HotmartTransientError as exc:
            await self._settle(
                claim,
                "create_unknown",
                failure_code=exc.code,
                retry_after=RETRY_AFTER,
            )
            return
        except HotmartPermanentError as exc:
            await self._settle(claim, "create_failed", failure_code=exc.code)
            return
        if found is None:
            await self._create(claim)
            return
        try:
            start_ms, end_ms = self._window_of_existing(found, claim)
        except ValueError:
            await self._settle(
                claim, "create_failed", failure_code="coupon_window_expired"
            )
            return
        # Existe en Hotmart: no se duplica. La verificacion espera los 7 min
        # desde ahora, porque no se sabe cuando lo creo.
        await self._settle(
            claim,
            "created",
            hotmart_coupon_id=found.coupon_id,
            start_ms=start_ms,
            end_ms=end_ms,
            retry_after=VERIFY_AFTER_CREATE,
        )

    def _window_of_existing(
        self, found: HotmartCoupon, claim: RecoveryCouponClaim
    ) -> tuple[int, int]:
        computed_start, end_ms = hotmart_coupon_window(
            self._clock(), claim.expires_at
        )
        start_ms = found.start_ms
        if start_ms is None or start_ms >= end_ms:
            start_ms = computed_start
        return start_ms, end_ms

    async def _verify(self, claim: RecoveryCouponClaim) -> None:
        try:
            found = await self._hotmart.find_coupon(
                claim.external_product_id, claim.code
            )
        except HotmartTransientError as exc:
            logger.warning(
                "recovery_coupon_verify_failed coupon_id=%s error_code=%s",
                claim.coupon_id,
                exc.code,
            )
            await self._settle(claim, "created", retry_after=RETRY_AFTER)
            return
        except HotmartPermanentError as exc:
            await self._settle(claim, "create_failed", failure_code=exc.code)
            return
        if found is None:
            await self._settle(
                claim, "create_failed", failure_code="hotmart_coupon_missing"
            )
            return
        if found.status == "valid" and found.active is True:
            await self._settle(claim, "valid")
            return
        if found.status == "overdue":
            # Vencido en Hotmart: no va a volver a valer.
            await self._settle(
                claim, "create_failed", failure_code="hotmart_coupon_overdue"
            )
            return
        if found.status == "valid" and found.active is False:
            await self._settle(
                claim, "create_failed", failure_code="hotmart_coupon_inactive"
            )
            return
        # soon (o un estado que todavia no se conoce): se vuelve a mirar.
        await self._settle(claim, "created", retry_after=RETRY_AFTER)
