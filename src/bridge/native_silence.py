"""El barrido nativo del seguimiento a 24 h con cupon (P22-3 de M16, E-269).

Diseno: ``documentacion/2026-11-01-m16-diseno-seguimiento-y-ficha-nativos.md``
(1.2 (c)) del vault; contrato: ``docs/contracts/flow-engine-v2.md`` §26.

P22-1 dejo escribir el seguimiento como un flujo nativo (``dispara.silencio``
con ``lo_ultimo: agente``) y P22-2 le dio su reserva en la base
(``claim_flow_engine_silence_v1``) y la barrera del legado
(``blocked_native_flow``): con un silencio nativo prendido o en cohorte, el
barrido del legado ya no le manda a esas conversaciones. Este modulo es quien
manda en su lugar.

Es el mismo barrido del legado (``followup_discount.ConversationFollowupSweeper``:
el criterio puro, la relectura, el filtro del saludo, la identidad 52/521, la
emision del link y el envio), parametrizado con la definicion publicada en vez
de las variables ``CONVERSATION_FOLLOWUP_*``:

* la plantilla y el idioma de ``pasos[0]``, el cupon de ``boton.cupon``;
* ``desde``, ``hasta`` y ``conversaciones`` del silencio;
* el horario (una franja con minutos, ``flow_schedule``);
* la reserva nativa, con el flujo, la version activa y su sha256.

El producto (``{{2}}``) sale del manifiesto (``hotmart.product_name``), el mismo
que exige hoy ``CONVERSATION_FOLLOWUP_PRODUCT_NAME``. Solo corre un silencio del
producto principal de la instancia (D-P22-4, por instancia).

``NativeSilenceSweepers`` lee los silencios de la instancia que corren
(``read_flow_engine_native_silences_v1``, 20261102000300) cada 60 s y tiene un
barredor por flujo: lo arma cuando aparece, lo rearma cuando cambia la version,
el estado o la cohorte, y lo para cuando el flujo deja de correr. En cohorte, el
barredor solo relee y reserva las conversaciones de los contactos de la
cohorte. Lo prende ``FLOW_ENGINE_ENABLED``, no ``CONVERSATION_FOLLOWUP_ENABLED``.

Sin un silencio nativo publicado no arma ningun barredor y no manda nada: lo
unico que agrega es esa lectura.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from bridge.flow_definition import parse_duration
from bridge.flow_engine import NATIVE_SILENCE_TRIGGER, native_not_supported, native_trigger_key
from bridge.flow_schedule import Franja, franja_of
from bridge.followup_discount import (
    COUPON_CODE_RE,
    DEFAULT_MAX_INBOUND_AGE_SECONDS,
    DEFAULT_MIN_INBOUND_AGE_SECONDS,
    ConversationFollowupSweeper,
)
from bridge.supabase import FlowEngineNativeSilence, NativeSilenceRun, SupabaseError

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 60.0
# Cuanto espera el refresco a que termine el barrido en curso antes de parar un
# barredor (otra version, pausado, apagado): un barrido manda a lo sumo
# CONVERSATION_FOLLOWUP_MAX_SENDS_PER_SCAN, cada uno con su relectura y su envio.
DRAIN_SECONDS = 120.0
READ_PENDING = "pending"
READ_OK = "ok"
READ_FAILING = "failing"
# Las conversaciones del silencio, en los estados de Chatwoot que lista el barrido.
CHATWOOT_STATUS_BY_STATE: Mapping[str, str] = {"abierta": "open", "resuelta": "resolved"}
# La ventana del silencio que ejecuta el barrido nativo: la del legado. Menos de
# 24 h es mandar dentro de la ventana de servicio y contra la regla de 24 h de
# silencio (decision de Dan del 2026-09-28); mas de 72 h es escribirle al
# historico el dia que se prende (la misma decision). El validador lo frena al
# publicar (``engine_not_supported:silencio_ventana``); esto es la segunda capa.
MIN_SILENCE_SECONDS = DEFAULT_MIN_INBOUND_AGE_SECONDS
MAX_SILENCE_SECONDS = DEFAULT_MAX_INBOUND_AGE_SECONDS

# Por que un silencio que corre no tiene barredor (lo publica /ready).
REFUSED_OTHER_PRODUCT = "otro_producto"
REFUSED_NOT_EXECUTABLE = "no_ejecutable"
REFUSED_STEP = "paso"
REFUSED_WINDOW = "ventana"
REFUSED_CONVERSATIONS = "conversaciones"
REFUSED_SCHEDULE = "horario"


class NativeSilenceRefused(ValueError):
    """La definicion de un silencio que corre no la ejecuta este barrido."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class NativeSilenceSpec:
    """Lo que el barredor de un silencio nativo saca de su definicion publicada."""

    run: NativeSilenceRun
    flow_key: str
    state: str
    template_name: str
    template_language: str | None
    coupon_code: str
    min_age_seconds: int
    max_age_seconds: int
    conversation_statuses: tuple[str, ...]
    window: Franja
    cohort_conversation_ids: frozenset[int] | None


def native_silence_spec(flow: FlowEngineNativeSilence, *, product_ref: str) -> NativeSilenceSpec:
    """La configuracion del barredor de un silencio, o ``NativeSilenceRefused`` con el motivo.

    Lo vuelve a mirar todo, aunque la base y el validador ya lo miraron al
    publicar: el barredor solo corre una definicion que sabe ejecutar entera.
    """
    if flow.product_ref != product_ref:
        raise NativeSilenceRefused(REFUSED_OTHER_PRODUCT)
    definition = flow.definition
    if native_trigger_key(definition) != NATIVE_SILENCE_TRIGGER:
        raise NativeSilenceRefused(REFUSED_NOT_EXECUTABLE)
    codes = native_not_supported(definition)
    if codes:
        raise NativeSilenceRefused(f"{REFUSED_NOT_EXECUTABLE}:{codes[0]}")
    steps = definition.get("pasos")
    step = steps[0] if isinstance(steps, list) and len(steps) == 1 else None
    if not isinstance(step, Mapping):
        raise NativeSilenceRefused(REFUSED_STEP)
    template = step.get("plantilla")
    language = step.get("idioma")
    button = step.get("boton")
    coupon = button.get("cupon") if isinstance(button, Mapping) else None
    if (
        not isinstance(template, str)
        or not template.strip()
        or (language is not None and (not isinstance(language, str) or not language.strip()))
        or not isinstance(coupon, str)
        or COUPON_CODE_RE.fullmatch(coupon) is None
    ):
        raise NativeSilenceRefused(REFUSED_STEP)
    silence = definition["dispara"]["silencio"]
    since, until = parse_duration(silence.get("desde")), parse_duration(silence.get("hasta"))
    if since is None or until is None:
        raise NativeSilenceRefused(REFUSED_WINDOW)
    min_age, max_age = since * 60, until * 60
    if not MIN_SILENCE_SECONDS <= min_age < max_age <= MAX_SILENCE_SECONDS:
        raise NativeSilenceRefused(REFUSED_WINDOW)
    states = silence.get("conversaciones")
    if (
        not isinstance(states, list)
        or not states
        or len(set(map(str, states))) != len(states)
        or any(state not in CHATWOOT_STATUS_BY_STATE for state in states)
    ):
        raise NativeSilenceRefused(REFUSED_CONVERSATIONS)
    try:
        window = franja_of(definition.get("horario"))
    except ValueError as error:
        raise NativeSilenceRefused(REFUSED_SCHEDULE) from error
    return NativeSilenceSpec(
        run=NativeSilenceRun(
            flow_id=flow.flow_id,
            version=flow.active_version,
            definition_sha256=flow.definition_sha256,
        ),
        flow_key=flow.flow_key,
        state=flow.state,
        template_name=template.strip(),
        template_language=language.strip() if isinstance(language, str) else None,
        coupon_code=coupon,
        min_age_seconds=min_age,
        max_age_seconds=max_age,
        conversation_statuses=tuple(CHATWOOT_STATUS_BY_STATE[state] for state in states),
        window=window,
        cohort_conversation_ids=(
            flow.cohort_conversation_ids if flow.state == "cohorte" else None
        ),
    )


def _same_run(left: NativeSilenceSpec, right: NativeSilenceSpec) -> bool:
    """Si dos configuraciones son el mismo barredor salvo la lista de la cohorte.

    Cambiar de prendido a cohorte (o al reves) si lo rearma: cambia a quien le escribe.
    """
    return (
        replace(left, cohort_conversation_ids=None) == replace(right, cohort_conversation_ids=None)
        and (left.cohort_conversation_ids is None) == (right.cohort_conversation_ids is None)
    )


class NativeSilenceSweepers:
    """Un barredor por silencio nativo de la instancia que corre, rearmado cada 60 s.

    ``build`` arma el barredor de una configuracion con lo que el bridge ya
    tiene (Chatwoot, Supabase, las ids canonicas, el producto, el resolvedor de
    la identidad). Si la lectura falla, los barredores que corren siguen: la
    reserva nativa mira igual el control, la version y la cohorte en cada envio,
    asi que un flujo pausado o cambiado no manda aunque la lectura este caida.
    """

    def __init__(
        self,
        *,
        supabase: Any,
        instance: Mapping[str, object],
        product_ref: str,
        build: Callable[[NativeSilenceSpec], ConversationFollowupSweeper],
        refresh_seconds: float = REFRESH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(product_ref, str) or not product_ref or not callable(build) \
                or not refresh_seconds > 0:
            raise ValueError("invalid native silence sweepers configuration")
        self._supabase = supabase
        self._instance = dict(instance)
        self._product_ref = product_ref
        self._build = build
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._state = READ_PENDING
        self._running: dict[str, tuple[NativeSilenceSpec, ConversationFollowupSweeper]] = {}
        self._refused: dict[str, str] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    @property
    def state(self) -> str:
        return self._state

    @property
    def sweepers(self) -> tuple[ConversationFollowupSweeper, ...]:
        return tuple(sweeper for _, sweeper in self._running.values())

    @property
    def refused(self) -> Mapping[str, str]:
        """Los silencios que corren sin barredor: la clave del flujo y el motivo."""
        return dict(self._refused)

    async def refresh_once(self) -> None:
        """Leer los silencios y dejar un barredor por cada uno que se puede ejecutar."""
        try:
            flows = await self._supabase.read_flow_engine_native_silences(self._instance)
        except SupabaseError as exc:
            self._state = READ_FAILING
            logger.warning(
                "flow_engine_read_failed read=native_silences error=%s", type(exc).__name__
            )
            return
        self._state = READ_OK
        wanted: dict[str, NativeSilenceSpec] = {}
        refused: dict[str, str] = {}
        for flow in flows:
            try:
                wanted[flow.flow_id] = native_silence_spec(flow, product_ref=self._product_ref)
            except NativeSilenceRefused as refusal:
                refused[f"{flow.product_ref}/{flow.flow_key}"] = refusal.reason
        for flow_key, reason in refused.items():
            if self._refused.get(flow_key) != reason:
                logger.warning(
                    "flow_engine_native_silence_refused flow=%s reason=%s", flow_key, reason
                )
        refused_before = self._refused
        self._refused = refused
        for flow_id in list(self._running):
            spec, sweeper = self._running[flow_id]
            new = wanted.get(flow_id)
            if new is not None and _same_run(new, spec):
                if new.cohort_conversation_ids != spec.cohort_conversation_ids:
                    # Un miembro nuevo de la cohorte (o una conversacion nueva de un
                    # miembro): se le pasa al barredor sin pararlo.
                    assert new.cohort_conversation_ids is not None
                    sweeper.set_only_conversation_ids(new.cohort_conversation_ids)
                    self._running[flow_id] = (new, sweeper)
                continue
            await sweeper.stop(drain_seconds=DRAIN_SECONDS)
            del self._running[flow_id]
            logger.info(
                "flow_engine_native_silence_stopped flow=%s version=%s",
                spec.flow_key,
                spec.run.version,
            )
        for flow_id, spec in wanted.items():
            if flow_id in self._running:
                continue
            flow_ref = f"{self._product_ref}/{spec.flow_key}"
            try:
                sweeper = self._build(spec)
                await sweeper.start()
            except Exception as error:  # noqa: BLE001 - un flujo no frena a los demas
                repeated = refused_before.get(flow_ref) == "configuracion"
                self._refused[flow_ref] = "configuracion"
                if repeated:
                    continue
                logger.warning(
                    "flow_engine_native_silence_refused flow=%s reason=configuracion error_type=%s",
                    flow_ref,
                    type(error).__name__,
                )
                continue
            self._running[flow_id] = (spec, sweeper)
            logger.info(
                "flow_engine_native_silence_started flow=%s version=%s state=%s cohort=%s",
                spec.flow_key,
                spec.run.version,
                spec.state,
                (
                    len(spec.cohort_conversation_ids)
                    if spec.cohort_conversation_ids is not None
                    else "todas"
                ),
            )

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._stopping.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for _, sweeper in self._running.values():
            await sweeper.stop()
        self._running.clear()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.refresh_once()
            except Exception as exc:  # noqa: BLE001 - el refresco no tumba el bridge
                self._state = READ_FAILING
                logger.warning(
                    "flow_engine_native_silence_refresh_failed error_type=%s",
                    type(exc).__name__,
                )
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._refresh_seconds)
            except TimeoutError:
                pass

    def readiness(self) -> dict[str, str]:
        """Las claves de /ready, de lo que ya esta en memoria: ninguna da 503 ni lee la base.

        Sin datos de nadie: los flujos por su clave, los barridos por su resumen.
        """
        running = sorted(self._running.values(), key=lambda item: item[0].flow_key)
        states = [sweeper.last_scan_state for _, sweeper in running]
        if not states:
            scan = "none"
        elif "error" in states:
            scan = "error"
        elif "never" in states:
            scan = "never"
        else:
            scan = "healthy"
        return {
            "flow_engine_native_silence_read": self._state,
            "flow_engine_native_silence_running": str(len(running)),
            "flow_engine_native_silence_cohort": str(
                sum(1 for spec, _ in running if spec.state == "cohorte")
            ),
            "flow_engine_native_silence_scan": scan,
            "flow_engine_native_silence_last_scan": " | ".join(
                f"{spec.flow_key} v{spec.run.version}: {sweeper.last_scan_summary}"
                for spec, sweeper in running
            ) or "none",
            "flow_engine_native_silence_refused": ", ".join(
                f"{flow_key}={reason}" for flow_key, reason in sorted(self._refused.items())
            ) or "none",
        }
