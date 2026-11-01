"""El bridge del motor de flujos (contrato flow-engine-v1, seccion 8; la T2.4).

Lo arma ``create_app`` solo con ``FLOW_ENGINE_ENABLED=true``. Apagado, nada de
este modulo existe en el proceso y el bridge hace exactamente lo de hoy.

- ``FlowEngineRoutes``: las rutas de la instancia, leidas con
  ``read_flow_engine_routes_v1`` a lo sumo cada 60 s, en el lote del
  ``ResolutionWorker`` (nunca en ``/ready``). Si la lectura falla, la cache
  queda ``failing`` y el carrito va por el envoltorio de hoy: es seguro,
  porque la base delega igual en el motor (contrato, seccion 7).
- ``FlowEngineAdmissionFailures``: las admisiones del motor que no
  contestaron, por evento. Con tres seguidas del mismo evento, la siguiente va
  con ``p_force_legacy`` (principio 1.4 del diseno: ningun disparo termina
  ``failed`` por culpa del motor). Se borra en la primera respuesta y guarda a
  lo sumo 1.000 eventos.
- ``DivergenceNotifier``: MOT-001, el aviso de una divergencia (el motor no
  pudo y lo planifico el legado). Al arrancar y cada 60 s lee las que no se
  avisaron, avisa una por una por el productor del conector de Slack y marca
  ``notified_at`` solo despues de que el conector la admitio. Una sola via: el
  aviso sale de esta lectura, no del ``divergence_first`` de la admision (D8).
  Sin conector, no avisa y publica ``flow_engine_alerts: sin_slack`` (D10).

Del motor nativo (flow-engine-v2, el PR 3 del hito 4, E-065):

- ``FlowEngineNativeFlows``: los flujos nativos de la instancia, leidos con
  ``read_flow_engine_native_flows_v1`` a lo sumo cada 60 s en el lote. Es lo
  que decide si un evento va a la admision nativa: sin un flujo nativo
  prendido o en cohorte para ese producto y ese disparador, el lote no hace
  ninguna llamada nueva por evento. Si la lectura falla, queda la ultima buena
  (una compra no se pierde por una lectura caida); sin ninguna buena, nada.
- ``StartRejectionNotifier``: MOT-002, el aviso de que el motor no pudo
  arrancar un flujo (``flow_engine_start_rejections``), una vez por flujo,
  fase, motivo y escalon (1, 10, 100), por el mismo productor que MOT-001.
  Marca lo avisado solo despues de que el conector lo admitio.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from bridge.slack_notifications import NotificationProducer
from bridge.supabase import (
    FLOW_ENGINE_NATIVE_ON_STATES,
    FlowEngineDivergence,
    FlowEngineNativeFlow,
    FlowEngineRoute,
    FlowEngineStartRejection,
    SupabaseClient,
    SupabaseError,
)
from slack_correlation.catalog import NotificationCommand
from slack_correlation.producer import ConnectorSemanticConflict

logger = logging.getLogger(__name__)

ROUTES_REFRESH_SECONDS = 60.0
FORCE_LEGACY_AFTER_FAILURES = 3
MAX_TRACKED_ADMISSION_FAILURES = 1000
READ_PENDING = "pending"
READ_OK = "ok"
READ_FAILING = "failing"
ALERTS_SLACK = "slack"
ALERTS_WITHOUT_SLACK = "sin_slack"
DIVERGENCES_POLL_SECONDS = 60.0
DIVERGENCES_PAGE = 20
DIVERGENCE_EVENT_CODE = "MOT-001"
_DIVERGENCE_DEDUPE_NAMESPACE = b"flow-engine-divergence-v1\0"
START_REJECTION_EVENT_CODE = "MOT-002"
START_REJECTIONS_PAGE = 20
_START_REJECTION_DEDUPE_NAMESPACE = b"flow-engine-start-rejection-v1\0"


class FlowEngineRoutes:
    """The routes of the instance, cached by (legacy_scope_key, legacy_kind)."""

    def __init__(
        self,
        *,
        supabase: SupabaseClient,
        instance: Mapping[str, object],
        refresh_seconds: float = ROUTES_REFRESH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._supabase = supabase
        self._instance = dict(instance)
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._routes: dict[tuple[str, str], FlowEngineRoute] = {}
        # Todas las de la ultima lectura buena, tambien las sin scope del
        # legado (el cupon y la reactivacion): las cuenta /ready.
        self._all: tuple[FlowEngineRoute, ...] = ()
        self._read_at: float | None = None
        self._state = READ_PENDING

    @property
    def state(self) -> str:
        return self._state

    def count(self, executor: str) -> int:
        return sum(1 for route in self._all if route.executor == executor)

    async def refresh_if_due(self) -> None:
        """Read the routes again if the last read is 60 s old or never was."""
        now = self._clock()
        if self._read_at is not None and now - self._read_at < self._refresh_seconds:
            return
        self._read_at = now
        try:
            routes = await self._supabase.read_flow_engine_routes(self._instance)
        except SupabaseError as exc:
            self._routes = {}
            self._all = ()
            self._state = READ_FAILING
            logger.warning(
                "flow_engine_read_failed read=routes error=%s", type(exc).__name__
            )
            return
        self._all = tuple(routes)
        self._routes = {
            (route.legacy_scope_key, route.legacy_kind): route
            for route in routes
            if route.legacy_scope_key is not None
        }
        self._state = READ_OK

    def executor(self, scope_key: str, legacy_kind: str) -> str | None:
        """Who runs that legacy flow, or None without a good read or a route."""
        if self._state != READ_OK:
            return None
        route = self._routes.get((scope_key, legacy_kind))
        return route.executor if route is not None else None


class FlowEngineNativeFlows:
    """The native flows of the instance, read at most every 60 s."""

    def __init__(
        self,
        *,
        supabase: SupabaseClient,
        instance: Mapping[str, object],
        refresh_seconds: float = ROUTES_REFRESH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._supabase = supabase
        self._instance = dict(instance)
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._flows: tuple[FlowEngineNativeFlow, ...] = ()
        self._read_at: float | None = None
        self._state = READ_PENDING

    @property
    def state(self) -> str:
        return self._state

    @property
    def flows(self) -> tuple[FlowEngineNativeFlow, ...]:
        return self._flows

    async def refresh_if_due(self) -> None:
        now = self._clock()
        if self._read_at is not None and now - self._read_at < self._refresh_seconds:
            return
        self._read_at = now
        try:
            flows = await self._supabase.read_flow_engine_native_flows(self._instance)
        except SupabaseError as exc:
            # La ultima lectura buena sigue valiendo: un flujo nativo no se
            # apaga por una lectura caida, y la base decide igual al admitir.
            self._state = READ_FAILING
            logger.warning(
                "flow_engine_read_failed read=native_flows error=%s", type(exc).__name__
            )
            return
        self._flows = tuple(flows)
        self._state = READ_OK

    def any_on(self) -> bool:
        return any(flow.state in FLOW_ENGINE_NATIVE_ON_STATES for flow in self._flows)

    def wants(self, product_ref: str, trigger: str) -> bool:
        """Whether a flow of that product and trigger is on or in its cohort."""
        return any(
            flow.product_ref == product_ref
            and flow.trigger == trigger
            and flow.state in FLOW_ENGINE_NATIVE_ON_STATES
            for flow in self._flows
        )

    def count(self, state: str) -> int:
        return sum(1 for flow in self._flows if flow.state == state)


class FlowEngineAdmissionFailures:
    """Consecutive unanswered engine admissions, by webhook event."""

    def __init__(self, *, limit: int = MAX_TRACKED_ADMISSION_FAILURES) -> None:
        self._limit = limit
        self._counts: OrderedDict[str, int] = OrderedDict()

    def should_force_legacy(self, event_id: str) -> bool:
        return self._counts.get(event_id, 0) >= FORCE_LEGACY_AFTER_FAILURES

    def record_failure(self, event_id: str) -> int:
        count = self._counts.pop(event_id, 0) + 1
        self._counts[event_id] = count
        while len(self._counts) > self._limit:
            self._counts.popitem(last=False)
        return count

    def clear(self, event_id: str) -> None:
        self._counts.pop(event_id, None)

    def __len__(self) -> int:
        return len(self._counts)


class FlowEngineRuntime:
    """What the bridge keeps of the engine while the flag is on."""

    def __init__(
        self,
        *,
        supabase: SupabaseClient,
        instance: Mapping[str, object],
        clock: Callable[[], float] = time.monotonic,
        cart_touches_enabled: bool = True,
        native_intent_enabled: bool = False,
    ) -> None:
        self.instance: dict[str, object] = dict(instance)
        # E-165: si el bridge corre A1 con cupon y A2/Z. Sin ellos el motor arranca igual (por sus
        # flujos nativos), y el carrito de una ruta del legado en motor va forzado al legado.
        self.cart_touches_enabled = cart_touches_enabled
        # E-252 (flow-engine-v2 §25): si el formulario entra por la RPC que lo rutea al nativo.
        # Solo lo publica /ready (on/off), para la verificacion del corte.
        self.native_intent_enabled = native_intent_enabled
        self.routes = FlowEngineRoutes(supabase=supabase, instance=instance, clock=clock)
        self.native_flows = FlowEngineNativeFlows(
            supabase=supabase, instance=instance, clock=clock
        )
        self.admission_failures = FlowEngineAdmissionFailures()
        # flow-engine-v2 2.7.0 (E-161): la admision nativa del carrito cuenta aparte, para no
        # forzar al legado la admision del motor del mismo evento.
        self.native_admission_failures = FlowEngineAdmissionFailures()
        # Los escribe el aviso de las divergencias (MOT-001) con su ultima
        # lectura; hasta la primera, unknown.
        self.divergences_unnotified: int | None = None
        # MOT-002: los rechazos del motor sin avisar; y las compras que la
        # admision nativa no contesto. Hasta la primera lectura, unknown.
        self.start_rejections_unnotified: int | None = None
        self.native_queue_pending: int | None = None
        self.alerts = ALERTS_WITHOUT_SLACK

    def readiness(self) -> dict[str, str]:
        """The flow_engine keys of /ready, from what is already in memory.

        It makes no call to the base and none of them gives 503 (decisions D1
        and D6 of the T2.4): a failing optional read is published and seen.
        """
        unnotified = self.divergences_unnotified
        rejections = self.start_rejections_unnotified
        pending = self.native_queue_pending
        return {
            "flow_engine": "enabled",
            "flow_engine_routes_motor": str(self.routes.count("motor")),
            "flow_engine_routes_legado": str(self.routes.count("legado")),
            "flow_engine_read": self.routes.state,
            "flow_engine_divergences_unnotified": (
                str(unnotified) if unnotified is not None else "unknown"
            ),
            "flow_engine_alerts": self.alerts,
            "flow_engine_native_read": self.native_flows.state,
            "flow_engine_native_on": str(
                self.native_flows.count("prendido") + self.native_flows.count("cohorte")
            ),
            "flow_engine_start_rejections_unnotified": (
                str(rejections) if rejections is not None else "unknown"
            ),
            "flow_engine_native_queue_pending": (
                str(pending) if pending is not None else "unknown"
            ),
            "flow_engine_cart_touches": "enabled" if self.cart_touches_enabled else "disabled",
            "flow_engine_native_intent": "on" if self.native_intent_enabled else "off",
        }


def divergence_command(divergence: FlowEngineDivergence) -> NotificationCommand:
    """The MOT-001 of one divergence: no event or contact id goes in it."""
    return NotificationCommand(
        event_id=divergence.divergence_id,
        event_code=DIVERGENCE_EVENT_CODE,
        dedupe_key=hashlib.sha256(
            _DIVERGENCE_DEDUPE_NAMESPACE + divergence.divergence_id.encode("ascii")
        ).hexdigest(),
        occurred_at=datetime.fromisoformat(divergence.first_at).astimezone(UTC),
        # La base ya escribe la clase adelante (transitorio:forzado_al_legado);
        # si no la trae, se le agrega.
        reason_code=(
            divergence.reason
            if divergence.reason.startswith(f"{divergence.kind}:")
            else f"{divergence.kind}:{divergence.reason}"
        )[:80],
        component="flow_engine",
        state="pending",
        count=min(divergence.count, 1_000_000),
    )


class DivergenceNotifier:
    """MOT-001: tell the operations channel each divergence, once."""

    def __init__(
        self,
        *,
        supabase: SupabaseClient,
        runtime: FlowEngineRuntime,
        producer: NotificationProducer | None,
        poll_interval_seconds: float = DIVERGENCES_POLL_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._supabase = supabase
        self._runtime = runtime
        self._producer = producer
        self._poll_interval = poll_interval_seconds
        self._clock = clock
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        runtime.alerts = ALERTS_SLACK if producer is not None else ALERTS_WITHOUT_SLACK

    async def run_once(self) -> None:
        try:
            read = await self._supabase.read_flow_engine_divergences(
                self._runtime.instance, only_unnotified=True, limit=DIVERGENCES_PAGE
            )
        except SupabaseError as exc:
            self._runtime.divergences_unnotified = None
            logger.warning(
                "flow_engine_read_failed read=divergences error=%s", type(exc).__name__
            )
            return
        unnotified = read.unnotified
        self._runtime.divergences_unnotified = unnotified
        if self._producer is None:
            return
        for divergence in read.divergences:
            try:
                await self._producer.admit(divergence_command(divergence))
            except ConnectorSemanticConflict:
                # El conector ya tiene un aviso con ese id y otro contenido: lo
                # admitio una vuelta anterior cuya respuesta o cuya marca no
                # llego, y despues la fila sumo count. Ya esta avisada: se
                # marca, si no la divergencia volveria en cada vuelta y la
                # pagina de 20 se trabaria con ella.
                logger.info(
                    "flow_engine_divergence_already_admitted divergence_id=%s",
                    divergence.divergence_id,
                )
            except Exception as exc:
                # Slack or the connector did not take it: no mark, and the
                # next round reads it again.
                logger.warning(
                    "flow_engine_divergence_alert_failed divergence_id=%s error=%s",
                    divergence.divergence_id,
                    type(exc).__name__,
                )
                continue
            try:
                marked = await self._supabase.mark_flow_engine_divergence_notified(
                    divergence.divergence_id, notified_at=self._clock().isoformat()
                )
            except SupabaseError as exc:
                logger.warning(
                    "flow_engine_divergence_mark_failed divergence_id=%s error=%s",
                    divergence.divergence_id,
                    type(exc).__name__,
                )
                continue
            if marked:
                unnotified = max(0, unnotified - 1)
                self._runtime.divergences_unnotified = unnotified
            logger.info(
                "flow_engine_divergence_alerted divergence_id=%s kind=%s marked=%s",
                divergence.divergence_id,
                divergence.kind,
                marked,
            )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self, *, timeout: float = 10.0) -> None:
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
                await self.run_once()
            except Exception:
                logger.exception("flow_engine_divergence_notifier_unexpected_error")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass


def start_rejection_command(rejection: FlowEngineStartRejection) -> NotificationCommand:
    """The MOT-002 of one flow, phase, reason and step: no event or contact id.

    The flow goes in ``component`` (``flow_engine:<flow_key>``); the event id is
    a UUID made from the dedupe key, so the same step of the same rejection is
    always the same notice.
    """
    digest = hashlib.sha256(
        _START_REJECTION_DEDUPE_NAMESPACE
        + "\0".join(
            (rejection.flow_id, rejection.phase, rejection.reason, str(rejection.step))
        ).encode("ascii")
    ).hexdigest()
    return NotificationCommand(
        event_id=str(uuid.UUID(hex=digest[:32])),
        event_code=START_REJECTION_EVENT_CODE,
        dedupe_key=digest,
        occurred_at=datetime.fromisoformat(rejection.last_at).astimezone(UTC),
        reason_code=rejection.reason[:80],
        component=f"flow_engine:{rejection.flow_key}"[:80],
        state="pending",
        count=min(rejection.count, 1_000_000),
    )


class StartRejectionNotifier:
    """MOT-002: tell the operations channel when the engine could not start a flow."""

    def __init__(
        self,
        *,
        supabase: SupabaseClient,
        runtime: FlowEngineRuntime,
        producer: NotificationProducer | None,
        poll_interval_seconds: float = DIVERGENCES_POLL_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._supabase = supabase
        self._runtime = runtime
        self._producer = producer
        self._poll_interval = poll_interval_seconds
        self._clock = clock
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> None:
        try:
            read = await self._supabase.read_flow_engine_start_rejections(
                self._runtime.instance, only_unnotified=True, limit=START_REJECTIONS_PAGE
            )
        except SupabaseError as exc:
            self._runtime.start_rejections_unnotified = None
            logger.warning(
                "flow_engine_read_failed read=start_rejections error=%s", type(exc).__name__
            )
            return
        unnotified = read.unnotified
        self._runtime.start_rejections_unnotified = unnotified
        if self._producer is None:
            return
        for rejection in read.rejections:
            try:
                await self._producer.admit(start_rejection_command(rejection))
            except ConnectorSemanticConflict:
                # Same id, other content: an earlier round was admitted and its
                # mark did not land. It is told: mark it.
                logger.info(
                    "flow_engine_start_rejection_already_admitted flow_id=%s step=%s",
                    rejection.flow_id,
                    rejection.step,
                )
            except Exception as exc:
                logger.warning(
                    "flow_engine_start_rejection_alert_failed flow_id=%s step=%s error=%s",
                    rejection.flow_id,
                    rejection.step,
                    type(exc).__name__,
                )
                continue
            try:
                marked = await self._supabase.mark_flow_engine_start_rejection_notified(
                    flow_id=rejection.flow_id,
                    phase=rejection.phase,
                    reason=rejection.reason,
                    notified_count=rejection.count,
                    notified_at=self._clock().isoformat(),
                )
            except SupabaseError as exc:
                logger.warning(
                    "flow_engine_start_rejection_mark_failed flow_id=%s error=%s",
                    rejection.flow_id,
                    type(exc).__name__,
                )
                continue
            if marked:
                unnotified = max(0, unnotified - 1)
                self._runtime.start_rejections_unnotified = unnotified
            logger.info(
                "flow_engine_start_rejection_alerted flow_id=%s phase=%s reason=%s "
                "count=%s step=%s marked=%s",
                rejection.flow_id,
                rejection.phase,
                rejection.reason,
                rejection.count,
                rejection.step,
                marked,
            )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self, *, timeout: float = 10.0) -> None:
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
                await self.run_once()
            except Exception:
                logger.exception("flow_engine_start_rejection_notifier_unexpected_error")
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass
