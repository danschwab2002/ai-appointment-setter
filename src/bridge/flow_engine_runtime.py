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
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping

from bridge.supabase import FlowEngineRoute, SupabaseClient, SupabaseError

logger = logging.getLogger(__name__)

ROUTES_REFRESH_SECONDS = 60.0
FORCE_LEGACY_AFTER_FAILURES = 3
MAX_TRACKED_ADMISSION_FAILURES = 1000
READ_PENDING = "pending"
READ_OK = "ok"
READ_FAILING = "failing"


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
        self._read_at: float | None = None
        self._state = READ_PENDING

    @property
    def state(self) -> str:
        return self._state

    def count(self, executor: str) -> int:
        return sum(1 for route in self._routes.values() if route.executor == executor)

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
            self._state = READ_FAILING
            logger.warning(
                "flow_engine_read_failed read=routes error=%s", type(exc).__name__
            )
            return
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
    ) -> None:
        self.instance: dict[str, object] = dict(instance)
        self.routes = FlowEngineRoutes(supabase=supabase, instance=instance, clock=clock)
        self.admission_failures = FlowEngineAdmissionFailures()
