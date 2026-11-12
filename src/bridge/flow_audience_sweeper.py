"""El barredor del disparo por audiencia (E-484, H6, P19): ocurrencias, freno por salud y ritmo.

Contrato: ``docs/contracts/flow-audience-trigger-v1.md``; la base:
``20261112100100_flow_audience_triggers_v1.sql`` (materializar, frenar, soltar, leer).

Cada pasada (cada 60 s, del molde de ``native_silence.NativeSilenceSweepers``):

1. Lee los flujos de audiencia que corren (prendidos o en cohorte) de la instancia.
2. Por cada uno, si vencio una ocurrencia de ``fecha`` o ``cada``
   (``flow_audience.due_occurrence``), arma la audiencia con la fuente
   (``AudienceSource``: Chatwoot, estado de cliente, formularios), la filtra
   (``flow_audience.select_members``) y la materializa en la base. La base es
   idempotente por (flujo, version, ocurrencia): un reinicio no la vuelve a armar.
3. Antes de soltar a nadie mira la salud de Meta de la instancia (D8): rojo o
   amarillo, una lectura que no se puede leer o vieja (mas de 26 h) frenan la
   ocurrencia y avisan; una frenada no se suelta nunca mas.
4. Suelta a lo sumo ``ritmo.por_hora`` por hora (la base lleva la cuenta) y le pasa
   cada persona al motor (``RunOpener``) para que abra su recorrido nativo.

Sin ``FLOW_AUDIENCE_TRIGGERS_ENABLED`` no lee ni manda nada. Sin un ``RunOpener``
(el motor nativo todavia no admite un recorrido de audiencia, el punto de choque con
E-483) no suelta a nadie: la ocurrencia queda confirmada esperando y ``/ready`` lo
dice. ``manual`` no la arma este barredor: la arma una persona desde la pagina, y la
suelta este barredor una vez confirmada con el conteo exacto (D7).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
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
REFRESH_SECONDS = 60.0
# Una lectura de salud mas vieja que esto no sirve: el timer de la salud corre una vez por dia.
HEALTH_MAX_AGE = timedelta(hours=26)
# DEFAULT_REVISAR_DAN (D8): rojo y amarillo frenan una difusion; los flujos por evento no se frenan.
BRAKING_QUALITIES = frozenset({"RED", "YELLOW"})
RELEASE_BATCH = 50

BRAKE_NO_READING = "meta_salud:sin_lectura"
BRAKE_STALE = "meta_salud:vieja"

STATE_OFF = "apagado"
STATE_OK = "ok"
STATE_NO_ENGINE = "sin_motor"
STATE_FAILING = "failing"


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
    """El veredicto de una lectura ``{"calidad": "GREEN|YELLOW|RED", "leido_en": iso}``. Fail-closed."""
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
    return HealthVerdict(True)


def file_health_reader(path: Path) -> Callable[[], Mapping[str, Any] | None]:
    """La lectura de la salud que deja el timer de la instancia en un JSON; None si no se puede leer."""
    def read() -> Mapping[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None
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


@dataclass(frozen=True)
class OccurrenceRow:
    """Una ocurrencia como la lee ``read_flow_audience_occurrences_v1``."""

    occurrence_id: str
    flow_id: str
    state: str
    form: str


class AudienceStore(Protocol):
    async def read_audience_flows(self) -> Sequence[AudienceFlow]: ...

    async def read_occurrences(self) -> Sequence[OccurrenceRow]: ...

    async def materialize(self, flow: AudienceFlow, occurrence: Occurrence, members: Sequence[Candidate],
                          fingerprint: str) -> Mapping[str, Any]: ...

    async def brake(self, occurrence_id: str, reason: str) -> Mapping[str, Any]: ...

    async def release(self, occurrence_id: str, *, now: datetime, limit: int) -> Mapping[str, Any]: ...


class AudienceSource(Protocol):
    async def candidates(self, spec: AudienceSpec) -> Iterable[Candidate]: ...


class RunOpener(Protocol):
    async def open(self, flow_id: str, contact_id: str, occurrence_id: str) -> None: ...


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
    refused: dict[str, str] | None = None
    waiting_for_engine: int = 0


class AudienceSweeper:
    def __init__(self, *, store: AudienceStore, source: AudienceSource, health: Callable[[], Mapping[str, Any] | None],
                 opener: RunOpener | None, notifier: BrakeNotifier | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._store = store
        self._source = source
        self._health = health
        self._opener = opener
        self._notifier = notifier
        self._clock = clock
        self._state = STATE_OK if opener is not None else STATE_NO_ENGINE
        self._last: SweepReport | None = None

    def readiness(self) -> dict[str, object]:
        report = self._last
        return {
            "state": self._state,
            "last_materialized": 0 if report is None else report.materialized,
            "last_braked": 0 if report is None else report.braked,
            "last_released": 0 if report is None else report.released,
            "waiting_for_engine": 0 if report is None else report.waiting_for_engine,
            "refused": {} if report is None or report.refused is None else dict(report.refused),
        }

    async def sweep_once(self) -> SweepReport:
        now = self._clock()
        report = SweepReport(refused={})
        flows = {flow.flow_id: flow for flow in await self._store.read_audience_flows()}
        verdict = health_verdict(self._health(), now=now)

        for flow in flows.values():
            try:
                spec = audience_spec(flow.definition)
            except AudienceSpecInvalid as error:
                assert report.refused is not None
                report.refused[flow.flow_key] = str(error)
                continue
            occurrence = due_occurrence(spec, now=now)
            if occurrence is None:
                continue
            members, omitted = select_members(spec, await self._source.candidates(spec), now=now)
            result = await self._store.materialize(flow, occurrence, members, spec.fingerprint)
            if result.get("ok") and result.get("created"):
                report.materialized += 1
                logger.info("flow_audience_materialized", extra={
                    "flow_key": flow.flow_key, "occurrence": occurrence.key,
                    "members": result.get("member_count"), "omitted_bridge": dict(omitted),
                    "omitted_base": result.get("omitted_count")})

        for row in await self._store.read_occurrences():
            if row.state not in ("confirmada", "enviando"):
                continue
            flow = flows.get(row.flow_id)
            if not verdict.allows:
                assert verdict.brake_reason is not None
                braked = await self._store.brake(row.occurrence_id, verdict.brake_reason)
                if braked.get("ok"):
                    report.braked += 1
                    if self._notifier is not None:
                        await self._notifier.braked(flow.flow_key if flow else row.flow_id, row.occurrence_id,
                                                    verdict.brake_reason)
                continue
            if self._opener is None:
                report.waiting_for_engine += 1
                continue
            released = await self._store.release(row.occurrence_id, now=now, limit=RELEASE_BATCH)
            for member in released.get("released", ()) if released.get("ok") else ():
                await self._opener.open(row.flow_id, str(member["contact_id"]), row.occurrence_id)
                report.released += 1

        self._last = report
        return report
