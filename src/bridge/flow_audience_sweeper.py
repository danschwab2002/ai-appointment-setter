"""El barredor del disparo por audiencia (E-484, H6, P19): ocurrencias, freno por salud y ritmo.

Contrato: ``docs/contracts/flow-audience-trigger-v1.md``; la base:
``20261112220000_flow_audience_triggers_v1.sql`` (materializar, frenar, soltar, asentar, leer).

Cada pasada (cada 60 s, del molde de ``native_silence.NativeSilenceSweepers``):

1. Lee los flujos de audiencia que corren (prendidos o en cohorte) de la instancia.
2. Por cada uno, si vencio una ocurrencia de ``fecha`` o ``cada``
   (``flow_audience.due_occurrence``), arma la audiencia con la fuente
   (``AudienceSource``: la base y, si el flujo pide etiquetas, Chatwoot), la filtra
   (``flow_audience.select_members``) y la materializa en la base. La base es
   idempotente por (flujo, version, ocurrencia): un reinicio no la vuelve a armar.
3. Antes de soltar a nadie mira la salud de Meta de la instancia (D8): rojo o
   amarillo, una lectura que no se puede leer o vieja (mas de 26 h) frenan la
   ocurrencia y avisan; una frenada no se suelta nunca mas.
4. Suelta a lo sumo ``ritmo.por_hora`` por hora (la base lleva la cuenta), le pasa
   cada persona al motor (``RunOpener``) para que abra su recorrido nativo, y le
   asienta a la base lo que contesto el motor. Si el motor no contesta, no asienta:
   la base la vuelve a dar a los 10 minutos (el motor abre por ``evento_id``
   unico, asi que abrir dos veces devuelve el mismo recorrido).

Sin ``FLOW_AUDIENCE_TRIGGERS_ENABLED`` no se arma. Sin un ``RunOpener`` (el motor
nativo no corre en la instancia) no suelta a nadie: la ocurrencia queda confirmada
esperando y ``/ready`` dice ``sin_motor``. ``manual`` no la arma la pasada: la arma
una persona desde la pagina (``materialize_manual``) y la suelta la pasada una vez
confirmada con el conteo exacto (D7).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from bridge.flow_audience import (
    AudienceSpec,
    AudienceSpecInvalid,
    Candidate,
    Occurrence,
    audience_spec,
    due_occurrence,
    select_members,
)

logger = logging.getLogger(__name__)

FLAG = "FLOW_AUDIENCE_TRIGGERS_ENABLED"
HEALTH_PATH_VARIABLE = "FLOW_AUDIENCE_META_HEALTH_PATH"
HEALTH_KEY_VARIABLE = "FLOW_AUDIENCE_META_HEALTH_KEY"
REFRESH_SECONDS = 60.0
# Una lectura de salud mas vieja que esto no sirve: el timer de la salud corre una vez por dia.
HEALTH_MAX_AGE = timedelta(hours=26)
# DEFAULT_REVISAR_DAN (D8): rojo y amarillo frenan una difusion; los flujos por evento no se frenan.
BRAKING_QUALITIES = frozenset({"RED", "YELLOW"})
# Lo que frena ademas de la calidad: Meta dice que el numero no puede mandar.
BRAKING_SEND_STATES = frozenset({"BLOCKED", "LIMITED"})
RELEASE_BATCH = 50

BRAKE_NO_READING = "meta_salud:sin_lectura"
BRAKE_STALE = "meta_salud:vieja"

STATE_OFF = "apagado"
STATE_OK = "ok"
STATE_NO_ENGINE = "sin_motor"
STATE_FAILING = "failing"

OPEN_STARTED = "arranco"
OPEN_NOT_STARTED = "no_arranca"
OPEN_NO_FLOW = "sin_flujo"


def enabled(environment: Mapping[str, str]) -> bool:
    """El flag, apagado por defecto: solo ``true`` lo prende."""
    return environment.get(FLAG, "").strip().lower() == "true"


# ---------------------------------------------------------------------------
# La salud de Meta (D8).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HealthVerdict:
    """Si la salud deja soltar, y si no, el motivo del freno (``meta_salud:<que>``)."""

    allows: bool
    brake_reason: str | None = None


def health_verdict(reading: Mapping[str, Any] | None, *, now: datetime) -> HealthVerdict:
    """El veredicto de una lectura ``{"calidad": "GREEN|YELLOW|RED", "leido_en": iso}``. Fail-closed.

    Con ``puede_enviar`` (la lectura de ``meta_salud.py``), ``BLOCKED`` o ``LIMITED`` frenan igual.
    """
    if not isinstance(reading, Mapping):
        return HealthVerdict(False, BRAKE_NO_READING)
    quality = reading.get("calidad")
    read_at = reading.get("leido_en")
    if not isinstance(quality, str) or not isinstance(read_at, str):
        return HealthVerdict(False, BRAKE_NO_READING)
    try:
        when = datetime.fromisoformat(read_at.replace("Z", "+00:00"))
    except ValueError:
        return HealthVerdict(False, BRAKE_NO_READING)
    if when.tzinfo is None:
        return HealthVerdict(False, BRAKE_NO_READING)
    if now - when > HEALTH_MAX_AGE:
        return HealthVerdict(False, BRAKE_STALE)
    normalized = quality.strip().upper()
    if normalized in BRAKING_QUALITIES:
        return HealthVerdict(False, f"meta_salud:{normalized.lower()}")
    if normalized != "GREEN":
        return HealthVerdict(False, BRAKE_NO_READING)
    send = reading.get("puede_enviar")
    if isinstance(send, str) and send.strip().upper() in BRAKING_SEND_STATES:
        return HealthVerdict(False, f"meta_salud:{send.strip().lower()}")
    return HealthVerdict(True)


def file_health_reader(path: Path, *, instance_key: str | None = None) -> Callable[[], Mapping[str, Any] | None]:
    """La lectura de la salud que deja el timer de la salud en un JSON; None si no se puede leer.

    Acepta las dos formas: una lectura suelta (``{"calidad", "leido_en", ...}``) o el
    ``estado.json`` de ``meta_salud.py`` (``{"<instancia>": {"calidad", ...}}``), del que
    toma ``instance_key``. Si la lectura no trae ``leido_en``, la hora es la de la
    ultima escritura del archivo (``meta_salud.py`` lo reescribe en cada corrida).
    """
    def read() -> Mapping[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            written = datetime.fromtimestamp(os.stat(path).st_mtime, timezone.utc)
        except (OSError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        if instance_key is not None and "calidad" not in value:
            value = value.get(instance_key)
            if not isinstance(value, dict):
                return None
        if "leido_en" not in value:
            value = {**value, "leido_en": written.isoformat()}
        return value
    return read


# ---------------------------------------------------------------------------
# Lo que el barredor usa de afuera.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AudienceFlow:
    """Un flujo de audiencia que corre: su id, su version activa y su definicion publicada."""

    flow_id: str
    version: int
    flow_key: str
    definition: Mapping[str, Any]
    product_ref: str = ""
    state: str = ""


@dataclass(frozen=True)
class OccurrenceRow:
    """Una ocurrencia como la lee ``read_flow_audience_occurrences_v1``."""

    occurrence_id: str
    flow_id: str
    state: str
    form: str
    flow_version: int = 0


@dataclass(frozen=True)
class ReleasedMember:
    """Una persona que la base solto (``again``: la vuelve a dar porque nadie la asento)."""

    contact_id: str
    phone: str
    again: bool = False


class AudienceStore(Protocol):
    async def read_audience_flows(self) -> Sequence[AudienceFlow]: ...

    async def read_occurrences(self) -> Sequence[OccurrenceRow]: ...

    async def materialize(self, flow: AudienceFlow, occurrence: Occurrence, members: Sequence[Candidate],
                          fingerprint: str, *, materialized_by: str = "barredor") -> Mapping[str, Any]: ...

    async def brake(self, occurrence_id: str, reason: str) -> Mapping[str, Any]: ...

    async def release(self, occurrence_id: str, *, now: datetime, limit: int) -> Mapping[str, Any]: ...

    async def settle(self, occurrence_id: str, contact_id: str, outcome: str) -> Mapping[str, Any]: ...


class AudienceSource(Protocol):
    async def candidates(self, flow: AudienceFlow, spec: AudienceSpec) -> Iterable[Candidate]: ...


class AudienceOpenRetry(Exception):
    """El motor no contesto: no se asienta y la base vuelve a dar a la persona."""


class RunOpener(Protocol):
    async def open(self, flow: AudienceFlow, occurrence: OccurrenceRow, member: ReleasedMember) -> str:
        """``arranco``, ``no_arranca`` o ``sin_flujo``; ``AudienceOpenRetry`` si no contesto."""
        ...


class BrakeNotifier(Protocol):
    async def braked(self, flow_key: str, occurrence_id: str, reason: str) -> None: ...


# ---------------------------------------------------------------------------
# El barredor.
# ---------------------------------------------------------------------------

@dataclass
class SweepReport:
    """Lo que hizo una pasada (para el log y para /ready). Sin telefonos ni nombres."""

    materialized: int = 0
    braked: int = 0
    released: int = 0
    started: int = 0
    retry: int = 0
    refused: dict[str, str] = field(default_factory=dict)
    waiting_for_engine: int = 0


@dataclass(frozen=True)
class ManualResult:
    """Lo que vuelve de armar una lista manual desde la pagina."""

    ok: bool
    reason_code: str | None = None
    occurrence_id: str | None = None
    member_count: int = 0


class AudienceSweeper:
    def __init__(self, *, store: AudienceStore, source: AudienceSource, health: Callable[[], Mapping[str, Any] | None],
                 opener: RunOpener | None, notifier: BrakeNotifier | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 refresh_seconds: float = REFRESH_SECONDS) -> None:
        self._store = store
        self._source = source
        self._health = health
        self._opener = opener
        self._notifier = notifier
        self._clock = clock
        self._refresh = refresh_seconds
        self._state = STATE_OK if opener is not None else STATE_NO_ENGINE
        self._last: SweepReport | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._lock = asyncio.Lock()

    def readiness(self) -> dict[str, object]:
        report = self._last
        return {
            "flow_audience": self._state,
            "flow_audience_last_materialized": 0 if report is None else report.materialized,
            "flow_audience_last_braked": 0 if report is None else report.braked,
            "flow_audience_last_released": 0 if report is None else report.released,
            "flow_audience_last_retry": 0 if report is None else report.retry,
            "flow_audience_waiting_for_engine": 0 if report is None else report.waiting_for_engine,
            "flow_audience_refused": {} if report is None else dict(report.refused),
        }

    async def _materialize(self, flow: AudienceFlow, spec: AudienceSpec, occurrence: Occurrence, *,
                           by: str) -> Mapping[str, Any]:
        members, omitted = select_members(spec, await self._source.candidates(flow, spec), now=self._clock())
        result = await self._store.materialize(flow, occurrence, members, spec.fingerprint, materialized_by=by)
        if result.get("ok") and result.get("created"):
            logger.info("flow_audience_materialized flow_key=%s occurrence=%s members=%s omitted_bridge=%s "
                        "omitted_base=%s", flow.flow_key, occurrence.key, result.get("member_count"),
                        json.dumps(dict(omitted), sort_keys=True), result.get("omitted_count"))
        return result

    async def sweep_once(self) -> SweepReport:
        async with self._lock:
            return await self._sweep()

    async def _sweep(self) -> SweepReport:
        now = self._clock()
        report = SweepReport()
        flows = {flow.flow_id: flow for flow in await self._store.read_audience_flows()}
        verdict = health_verdict(self._health(), now=now)

        for flow in flows.values():
            try:
                spec = audience_spec(flow.definition)
            except AudienceSpecInvalid as error:
                report.refused[flow.flow_key] = str(error)
                continue
            occurrence = due_occurrence(spec, now=now)
            if occurrence is None:
                continue
            try:
                result = await self._materialize(flow, spec, occurrence, by="barredor")
            except Exception as error:  # noqa: BLE001 - un flujo que no se puede armar no frena a los otros
                report.refused[flow.flow_key] = f"materializar:{type(error).__name__}"
                logger.warning("flow_audience_materialize_failed flow_key=%s error=%s", flow.flow_key,
                               type(error).__name__)
                continue
            if result.get("ok") and result.get("created"):
                report.materialized += 1

        for row in await self._store.read_occurrences():
            if row.state not in ("confirmada", "enviando"):
                continue
            flow = flows.get(row.flow_id)
            if not verdict.allows:
                assert verdict.brake_reason is not None
                braked = await self._store.brake(row.occurrence_id, verdict.brake_reason)
                if braked.get("ok"):
                    report.braked += 1
                    logger.warning("flow_audience_braked occurrence=%s reason=%s", row.occurrence_id,
                                   verdict.brake_reason)
                    if self._notifier is not None:
                        try:
                            await self._notifier.braked(flow.flow_key if flow else row.flow_id, row.occurrence_id,
                                                        verdict.brake_reason)
                        except Exception as error:  # noqa: BLE001 - el aviso no deshace el freno
                            logger.warning("flow_audience_brake_notice_failed error=%s", type(error).__name__)
                continue
            if self._opener is None:
                report.waiting_for_engine += 1
                continue
            if flow is None or flow.version != row.flow_version:
                # El flujo se pauso, se apago o cambio de version: no se suelta mas de esta lista.
                report.refused[row.occurrence_id] = "flujo_no_corre"
                continue
            released = await self._store.release(row.occurrence_id, now=now, limit=RELEASE_BATCH)
            if not released.get("ok"):
                continue
            for member in released.get("released", ()):
                try:
                    outcome = await self._opener.open(flow, row, member)
                except AudienceOpenRetry:
                    report.retry += 1
                    continue
                await self._store.settle(row.occurrence_id, member.contact_id, outcome)
                if not member.again:
                    report.released += 1
                if outcome == OPEN_STARTED:
                    report.started += 1

        self._last = report
        return report

    async def materialize_manual(self, flow_id: str, *, slack_user_id: str) -> ManualResult:
        """Arma la lista de un flujo ``manual`` para la pagina: queda esperando la confirmacion (D7).

        La arma el bridge (la fuente lee Chatwoot si hacen falta etiquetas), con quien la
        pidio; confirmarla es otra cosa y la hace la base con la sesion de la pagina.
        """
        async with self._lock:
            flows = {flow.flow_id: flow for flow in await self._store.read_audience_flows()}
            flow = flows.get(flow_id)
            if flow is None:
                return ManualResult(False, "flow_not_running")
            try:
                spec = audience_spec(flow.definition)
            except AudienceSpecInvalid as error:
                return ManualResult(False, f"definicion:{error}")
            if spec.form != "manual":
                return ManualResult(False, "flow_not_manual")
            occurrence = Occurrence(f"manual:{uuid.uuid4()}", self._clock())
            result = await self._materialize(flow, spec, occurrence, by=f"slack:{slack_user_id}")
            if not result.get("ok"):
                return ManualResult(False, str(result.get("reason_code") or "flow_audience_final"))
            return ManualResult(True, None, str(result.get("occurrence_id")), int(result.get("member_count") or 0))

    async def start(self) -> None:
        if self._task is None:
            self._stopped.clear()
            self._task = asyncio.create_task(self._run(), name="flow_audience_sweeper")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - parar no levanta
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.sweep_once()
                if self._state == STATE_FAILING:
                    self._state = STATE_OK if self._opener is not None else STATE_NO_ENGINE
            except Exception as error:  # noqa: BLE001 - el bucle no muere por una pasada
                self._state = STATE_FAILING
                logger.warning("flow_audience_sweep_failed error=%s", type(error).__name__)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._refresh)
            except asyncio.TimeoutError:
                pass
