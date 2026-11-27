"""El calentamiento y el espaciado del masivo (F2 del mapa del 2026-11-27, decisiones D7-D10).

Mapa: ``OS/productos/soporte-infoproductores/documentacion/2026-11-27-mapa-frenos-meta-y-calentamiento.md``,
«F2 · Calentamiento del masivo». El masivo: ``2026-11-25-mapa-masivo-por-el-ae.md``.

Detras de ``BULK_SEND_WARMUP_ENABLED`` (apagado por defecto), que solo tiene efecto con
``BULK_SEND_ENABLED``. Apagado no existe nada de esto: ``build_bulk_send`` no lo arma, el motor y la pagina
no lo miran, y ``/ready`` dice ``bulk_send_warmup: disabled`` donde ya estan las claves del masivo.

Que decide (la practica de Brayan; Meta no publica un calendario):

* **D7 · Escalones.** 500, 1.000 y 5.000 personas por numero (``BULK_SEND_WARMUP_STEPS``). El ultimo es el
  techo hasta que se reconfigure.
* **D8 · Tanda grande.** Un masivo de mas personas que la tanda piloto (20). El escalon se mide contra el
  total del masivo (las personas que van), no contra cada lote.
* **D9 · Subir de escalon.** Solo si la tanda grande anterior del numero termino hace al menos 24 h, no la
  corto un freno de Meta y tuvo respuesta suficiente (``BULK_SEND_WARMUP_MIN_REPLY_RATE``, 5 % de las
  entregadas). Si no alcanzo, repite; si la corto un freno de Meta, baja uno.
* **D10 · Espaciado.** Una tanda grande por dia por numero (el dia de la zona de la instancia) y una
  separacion minima de ``BULK_SEND_WARMUP_MIN_GAP_DAYS`` dias (1) entre tandas grandes. Un masivo mas grande
  que el tope del dia no se aprueba ni arranca: la pagina y el AE dicen el tope y desde cuando.

Las decisiones nuevas de esta pieza (con su default):

* **D-F2.1 · El escalon de una tanda se deduce de su tamano.** Es el menor escalon que la contiene (600
  personas -> el de 1.000). Asi, una tanda chica en un escalon alto no lo «usa»: con 400 personas en el de
  1.000, la proxima sigue en 1.000 (sube al siguiente del de 500, que es el mismo). No hace falta guardar
  el escalon en la base: sale de lo que la base ya guarda (``bulk_sends``).
* **D-F2.2 · Solo cuenta la ultima tanda grande terminada.** El escalon de hoy sale de la ultima (por la
  hora en que termino o la frenaron). Una tanda grande que nunca arranco (frenada antes de mandar, por la
  regla de las 24 h o por este mismo calentamiento) no es una tanda. Sin ninguna, el primer escalon.
* **D-F2.3 · Una tanda que no termino no sube.** Frenada por algo que no es Meta (la API de Chatwoot, la
  hora de corte, a mano) repite el escalon: no se sabe como le habria ido entera.
* **D-F2.4 · Las respuestas se miden con el reporte del masivo.** ``bulk_send_report.build_report`` ya
  cuenta, por envio, las entregadas (entregado + leido) y las respuestas (un mensaje publico de la persona
  posterior a la plantilla), leidas de Chatwoot por conversacion (``BulkSendRecipientsReport``). Se reusa
  tal cual: tasa = respuestas / entregadas, medida una vez pasadas las 24 h del fin de la tanda (antes no
  decide: el escalon repite igual). No hace falta migracion. Como leer las conversaciones de una tanda de
  5.000 tarda, la lectura la hacen el motor (al arrancar) y un bucle de fondo cada 15 minutos, una sola
  vez por tanda (si falla, se reintenta a los 30 minutos); la pagina y el AE usan la lectura guardada. Sin
  lectura la tanda cuenta como sin respuesta suficiente: repite, nunca sube.
* **D-F2.5 · El historial son los ultimos 50 masivos de la instancia** (``read_bulk_sends_v1``, el mismo
  tope del motor). Una tanda grande mas vieja que eso no se ve y cuenta como sin historial: el primer
  escalon. Es el lado seguro.
* **D-F2.6 · Una tanda grande viva o ya confirmada bloquea aprobar otra.** Mientras otra tanda grande del
  numero esta saliendo, o confirmada y sin salir, la pagina no aprueba una segunda: su escalon depende de
  como le vaya a la primera. Al arrancar, el motor solo mira lo que ya arranco (la otra confirmada no lo
  frena: si sale primero, la segunda la ve terminada y la frena el espaciado).
* **D-F2.7 · El dia de una aprobacion es el de la hora pedida.** La pagina y el AE evaluan el masivo en
  ``max(ahora, hora pedida)``; el motor lo vuelve a evaluar al arrancar, con su reloj, y si no entra lo
  frena (``calentamiento:<motivo>``) con el aviso ``SYS-007`` de siempre. No es un freno de Meta: no cuenta
  para la regla de las 24 h.
* **D-F2.8 · Configuracion invalida no arranca el bridge.** Escalones que no suben, que no superan la tanda
  piloto o que pasan el tope de una lista (20.000), una tasa fuera de 0 a 1 o una separacion fuera de 1 a
  30 dias levantan ``WarmupConfigError`` al armar el masivo.

``warmup_today`` y ``check_size`` son puros: no leen la base, ni Chatwoot, ni el reloj. ``BulkSendWarmup``
es lo que los alimenta en el bridge. Nada de esto manda un mensaje ni escribe un telefono en un log.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from bridge.bulk_send_brakes import DEFAULT_PILOT_POLICY, Verdict, is_meta_brake

logger = logging.getLogger(__name__)

FLAG = "BULK_SEND_WARMUP_ENABLED"
STEPS_VARIABLE = "BULK_SEND_WARMUP_STEPS"
MIN_REPLY_RATE_VARIABLE = "BULK_SEND_WARMUP_MIN_REPLY_RATE"
MIN_GAP_DAYS_VARIABLE = "BULK_SEND_WARMUP_MIN_GAP_DAYS"
ENV_VARIABLES = (FLAG, STEPS_VARIABLE, MIN_REPLY_RATE_VARIABLE, MIN_GAP_DAYS_VARIABLE)

DEFAULT_STEPS = (500, 1000, 5000)  # D7
DEFAULT_MIN_REPLY_RATE = 0.05  # D9
DEFAULT_MIN_GAP_DAYS = 1  # D10
SETTLE = timedelta(hours=24)  # D9: lo que tiene que pasar desde el fin de la anterior para subir
MAX_STEP = 20_000  # el tope de una lista (bulk_sends.recipient_cap, 20261125000200)
MAX_STEPS = 10
MAX_GAP_DAYS = 30

REASON_PREFIX = "calentamiento"
# Los motivos del escalon (por que el tope del dia es ese).
STEP_NO_HISTORY = "sin_historial"
STEP_UP = "subio"
STEP_CEILING = "techo"
STEP_REPEAT_LOW_REPLY = "respuesta_baja"
STEP_REPEAT_NO_READING = "respuestas_sin_leer"
STEP_REPEAT_TOO_SOON = "menos_de_24h"
STEP_REPEAT_NOT_FINISHED = "no_termino"
STEP_DOWN_META = "freno_meta"
# Los motivos por los que el dia no admite una tanda grande.
BLOCK_LIVE = "tanda_grande_en_curso"
BLOCK_PENDING = "tanda_grande_confirmada"
BLOCK_GAP = "espaciado"

# F1 (meta-enforcement-brakes-v1): la plantilla retirada por recategorizacion, la racha de
# recategorizaciones y la cuenta restringida tambien son frenos de Meta para el escalon (D9: baja uno),
# aunque ``is_meta_brake`` no los cuente para la regla de 24 h del portfolio. Copiados de
# ``meta_enforcement`` para no traer FastAPI a este modulo; un test los ata.
META_ENFORCEMENT_REASONS = frozenset({
    "template_recategorized", "meta_account_restricted", "meta_recategorization_streak"})

_LIVE_STATES = frozenset({"enviando"})
_PENDING_STATES = frozenset({"confirmado", "preparando"})
_STEP_PATTERN = re.compile(r"[0-9]{1,6}")


class WarmupConfigError(ValueError):
    """La configuracion del calentamiento no es valida: el bridge no arranca el masivo."""


# ---------------------------------------------------------------------------
# La configuracion.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WarmupPolicy:
    """Lo que gobierna el calentamiento. ``pilot_size`` es la tanda piloto del motor (D8)."""

    steps: tuple[int, ...] = DEFAULT_STEPS
    min_reply_rate: float = DEFAULT_MIN_REPLY_RATE
    min_gap_days: int = DEFAULT_MIN_GAP_DAYS
    pilot_size: int = DEFAULT_PILOT_POLICY.size
    settle: timedelta = SETTLE

    def __post_init__(self) -> None:
        steps = self.steps
        if (not isinstance(steps, tuple) or not 1 <= len(steps) <= MAX_STEPS
                or not all(isinstance(s, int) and not isinstance(s, bool) for s in steps)):
            raise WarmupConfigError(f"{STEPS_VARIABLE} must be 1 to {MAX_STEPS} whole numbers")
        if any(b <= a for a, b in zip(steps, steps[1:], strict=False)):
            raise WarmupConfigError(f"{STEPS_VARIABLE} must go up")
        if steps[0] <= self.pilot_size or steps[-1] > MAX_STEP:
            raise WarmupConfigError(
                f"{STEPS_VARIABLE} must be above the pilot ({self.pilot_size}) and at most {MAX_STEP}")
        rate = self.min_reply_rate
        if (not isinstance(rate, (int, float)) or isinstance(rate, bool) or math.isnan(float(rate))
                or not 0.0 <= float(rate) <= 1.0):
            raise WarmupConfigError(f"{MIN_REPLY_RATE_VARIABLE} must be between 0 and 1")
        gap = self.min_gap_days
        if not isinstance(gap, int) or isinstance(gap, bool) or not 1 <= gap <= MAX_GAP_DAYS:
            raise WarmupConfigError(f"{MIN_GAP_DAYS_VARIABLE} must be a whole number of days from 1 to {MAX_GAP_DAYS}")
        if self.settle < timedelta(0):
            raise WarmupConfigError("the warmup settle time cannot be negative")

    def step_of(self, size: int) -> int:
        """El indice del menor escalon que contiene ``size`` (D-F2.1); mas grande que el techo, el techo."""
        for index, step in enumerate(self.steps):
            if size <= step:
                return index
        return len(self.steps) - 1


def parse_steps(raw: str) -> tuple[int, ...]:
    """``500,1000,5000`` -> ``(500, 1000, 5000)``. Falla con cualquier otra cosa (D-F2.8)."""
    parts = [p.strip().replace(".", "") for p in raw.split(",")]
    if not parts or not all(_STEP_PATTERN.fullmatch(p) for p in parts):
        raise WarmupConfigError(f"{STEPS_VARIABLE} must be whole numbers separated by commas")
    return tuple(int(p) for p in parts)


def policy_from_env(environ: Mapping[str, str], *, pilot_size: int = DEFAULT_PILOT_POLICY.size) -> WarmupPolicy | None:
    """La politica, o ``None`` con ``BULK_SEND_WARMUP_ENABLED`` apagado (todo lo que no sea ``true``)."""
    if environ.get(FLAG, "false").strip().lower() != "true":
        return None
    raw_steps = environ.get(STEPS_VARIABLE, "").strip()
    raw_rate = environ.get(MIN_REPLY_RATE_VARIABLE, "").strip()
    raw_gap = environ.get(MIN_GAP_DAYS_VARIABLE, "").strip()
    steps = parse_steps(raw_steps) if raw_steps else DEFAULT_STEPS
    try:
        rate = float(raw_rate) if raw_rate else DEFAULT_MIN_REPLY_RATE
    except ValueError as error:
        raise WarmupConfigError(f"{MIN_REPLY_RATE_VARIABLE} must be between 0 and 1") from error
    if raw_gap and not raw_gap.isdigit():
        raise WarmupConfigError(f"{MIN_GAP_DAYS_VARIABLE} must be a whole number of days from 1 to {MAX_GAP_DAYS}")
    gap = int(raw_gap) if raw_gap else DEFAULT_MIN_GAP_DAYS
    return WarmupPolicy(steps=steps, min_reply_rate=rate, min_gap_days=gap, pilot_size=pilot_size)


# ---------------------------------------------------------------------------
# El historial y la decision. Puros.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LargeSend:
    """Un masivo del numero, como lo usa el calentamiento. ``size``: las personas que van (las confirmadas);
    ``ended_at``: cuando termino o lo frenaron, si arranco; ``delivered`` y ``replies``: del reporte (D-F2.4),
    ``None`` si no se leyeron."""

    send_id: str
    size: int
    state: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    brake_reason: str | None = None
    delivered: int | None = None
    replies: int | None = None

    @property
    def ref(self) -> str:
        return self.send_id.replace("-", "")[:12]

    @property
    def meta_brake(self) -> bool:
        return self.state == "frenado" and (
            is_meta_brake(self.brake_reason) or self.brake_reason in META_ENFORCEMENT_REASONS)

    @property
    def reply_rate(self) -> float | None:
        if self.delivered is None or self.replies is None:
            return None
        return self.replies / self.delivered if self.delivered > 0 else 0.0


@dataclass(frozen=True)
class WarmupToday:
    """El calentamiento de un dia para un numero.

    ``cap``: el tope de una tanda grande ese dia. ``large_allowed``: si ese dia se puede arrancar una tanda
    grande. ``reason``: el motivo del bloqueo si no se puede, o el del escalon. ``next_at``: desde cuando se
    puede la proxima tanda grande (si hoy no) o desde cuando podria subir al ``next_cap``.
    """

    cap: int
    step: int
    steps: tuple[int, ...]
    large_allowed: bool
    reason: str
    detail: str
    step_reason: str
    next_at: datetime | None = None
    next_cap: int | None = None
    last_ref: str | None = None


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("%s tiene que ser un datetime con zona" % name)


def local_moment(value: datetime, zone: ZoneInfo) -> str:
    """``28/11 00:00`` en la zona de la instancia."""
    return value.astimezone(zone).strftime("%d/%m %H:%M")


def _start_of(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=zone)


def _thousands(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def warmup_today(history: Iterable[LargeSend], *, at: datetime, zone: ZoneInfo, policy: WarmupPolicy,
                 exclude_send_id: str | None = None, count_pending: bool = False) -> WarmupToday:
    """El tope del dia de ``at`` (en la zona de la instancia) y si ese dia admite una tanda grande.

    ``history``: los masivos del numero (cualquier tamano y estado; aca se filtran las tandas grandes).
    ``exclude_send_id``: el masivo que se esta evaluando, que no cuenta como historial. ``count_pending``: si
    una tanda grande confirmada y sin arrancar bloquea (la aprobacion, D-F2.6) o no (el arranque).
    """
    _aware(at, "at")
    large: list[LargeSend] = []
    for item in history:
        if item.send_id == exclude_send_id or item.size <= policy.pilot_size:
            continue
        for name in ("started_at", "ended_at"):
            value = getattr(item, name)
            if value is not None:
                _aware(value, name)
        large.append(item)

    steps = policy.steps
    top = len(steps) - 1
    finished = [h for h in large if h.started_at is not None and h.ended_at is not None]
    live = [h for h in large if h.state in _LIVE_STATES or (h.started_at is not None and h.ended_at is None)]
    pending = [h for h in large if h.state in _PENDING_STATES and h.started_at is None] if count_pending else []

    next_at: datetime | None = None
    next_cap: int | None = None
    last: LargeSend | None = None
    if not finished:
        step, step_reason = 0, STEP_NO_HISTORY
        step_detail = ("Todavía no salió ninguna tanda grande de este número: el primer escalón es de %s personas."
                       % _thousands(steps[0]))
    else:
        last = max(finished, key=lambda h: h.ended_at)  # type: ignore[arg-type, return-value]
        assert last.ended_at is not None
        used = policy.step_of(last.size)
        rate = last.reply_rate
        settled = at - last.ended_at >= policy.settle
        if last.meta_brake:
            step, step_reason = max(used - 1, 0), STEP_DOWN_META
            step_detail = ("A la tanda anterior (%s personas) la cortó un freno de Meta (%s): el tope baja a %s."
                           % (_thousands(last.size), last.brake_reason, _thousands(steps[max(used - 1, 0)])))
        elif last.state != "terminado":
            step, step_reason = used, STEP_REPEAT_NOT_FINISHED
            step_detail = ("La tanda anterior (%s personas) no terminó (%s): se repite el escalón de %s."
                           % (_thousands(last.size), last.brake_reason or last.state, _thousands(steps[used])))
        elif not settled:
            step, step_reason = used, STEP_REPEAT_TOO_SOON
            step_detail = ("La tanda anterior (%s personas) terminó hace menos de 24 h: hasta que pasen, el tope sigue "
                           "en %s." % (_thousands(last.size), _thousands(steps[used])))
            if used < top:
                next_at, next_cap = last.ended_at + policy.settle, steps[used + 1]
                step_detail += (" Desde el %s podría subir a %s si la respuesta llega al %s %%."
                                % (local_moment(next_at, zone), _thousands(next_cap),
                                   _percent(policy.min_reply_rate)))
        elif rate is None:
            step, step_reason = used, STEP_REPEAT_NO_READING
            step_detail = ("Todavía no se pudo leer cuántos respondieron a la tanda anterior (%s personas): se repite "
                           "el escalón de %s." % (_thousands(last.size), _thousands(steps[used])))
        elif rate < policy.min_reply_rate:
            step, step_reason = used, STEP_REPEAT_LOW_REPLY
            step_detail = ("La tanda anterior (%s personas) tuvo %s respuestas de %s entregadas (%s %%), menos del "
                           "%s %%: se repite el escalón de %s."
                           % (_thousands(last.size), last.replies, last.delivered, _percent(rate),
                              _percent(policy.min_reply_rate), _thousands(steps[used])))
        elif used >= top:
            step, step_reason = top, STEP_CEILING
            step_detail = ("La tanda anterior salió bien y este número ya está en el techo: %s."
                           % _thousands(steps[top]))
        else:
            step, step_reason = used + 1, STEP_UP
            step_detail = ("La tanda anterior (%s personas) salió bien, con %s %% de respuesta: el tope sube a %s."
                           % (_thousands(last.size), _percent(rate), _thousands(steps[used + 1])))

    # El espaciado (D10): una tanda grande por dia y la separacion minima, en dias de la zona de la instancia.
    started = [h.started_at for h in finished + live if h.started_at is not None]
    block, block_detail = None, ""
    if live:
        block = BLOCK_LIVE
        block_detail = "Hay otra tanda grande de este número saliendo: la próxima se decide cuando termine."
        next_at, next_cap = None, None
    elif pending:
        block = BLOCK_PENDING
        block_detail = ("Ya hay otra tanda grande de este número confirmada y sin salir: la próxima se decide cuando "
                        "esa termine.")
        next_at, next_cap = None, None
    elif started:
        earliest_day = max(started).astimezone(zone).date() + timedelta(days=policy.min_gap_days)
        if at.astimezone(zone).date() < earliest_day:
            block = BLOCK_GAP
            gap_at = _start_of(earliest_day, zone)
            block_detail = ("Una tanda grande por día y con %d día(s) de separación: la próxima puede salir desde el %s."
                            % (policy.min_gap_days, local_moment(gap_at, zone)))
            if next_at is None or gap_at > next_at:
                next_at = gap_at
                if next_cap is None:
                    next_cap = steps[step]
    detail = " ".join(x for x in (block_detail, step_detail) if x)
    return WarmupToday(cap=steps[step], step=step, steps=steps, large_allowed=block is None,
                       reason=block or step_reason, detail=detail, step_reason=step_reason,
                       next_at=next_at, next_cap=next_cap, last_ref=last.ref if last else None)


def _percent(rate: float) -> str:
    text = f"{rate * 100:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def check_size(today: WarmupToday, size: int, *, policy: WarmupPolicy, zone: ZoneInfo) -> Verdict:
    """Si un masivo de ``size`` personas entra en el calentamiento del dia. Uno de la tanda piloto o menos
    no es una tanda grande y entra siempre (D8)."""
    if size <= policy.pilot_size:
        return Verdict(True, f"{REASON_PREFIX}:no_es_tanda_grande",
                       "Son %d personas: no es una tanda grande y no cuenta para el calentamiento." % size)
    if not today.large_allowed:
        return Verdict(False, f"{REASON_PREFIX}:{today.reason}", today.detail)
    if size > today.cap:
        text = ("Son %s personas y el tope del día de este número es %s. Partilo en masivos de hasta %s, uno por día."
                % (_thousands(size), _thousands(today.cap), _thousands(today.cap)))
        if today.next_at is not None and today.next_cap is not None and size <= today.next_cap:
            text += " O esperá: desde el %s el tope podría ser %s." % (local_moment(today.next_at, zone),
                                                                     _thousands(today.next_cap))
        return Verdict(False, f"{REASON_PREFIX}:supera_tope_{today.cap}", text + " " + today.detail)
    return Verdict(True, f"{REASON_PREFIX}:entra",
                   "Son %s personas: entra en el tope del día (%s). %s"
                   % (_thousands(size), _thousands(today.cap), today.detail))


# ---------------------------------------------------------------------------
# Del historial de la base al calentamiento.
# ---------------------------------------------------------------------------

def _when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp without zone")
    return parsed


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def size_of(view: Mapping[str, Any]) -> int | None:
    """Las personas que van: las confirmadas o, antes de confirmar, las que quedaron en la lista revisada."""
    confirmed = _count(view.get("confirmed_count"))
    if confirmed is not None:
        return confirmed
    listing = view.get("list")
    if isinstance(listing, Mapping) and listing.get("state") in (None, "revisada"):
        return _count(listing.get("eligible_count"))
    return None


def large_send_of(view: Mapping[str, Any]) -> LargeSend | None:
    """Un masivo de ``read_bulk_sends_v1`` como historial, o ``None`` si no cuenta (borrador, sin tamano o
    frenado antes de arrancar). Levanta ``ValueError`` ante una fila sin la forma de la funcion."""
    send_id, state = view.get("send_id"), view.get("state")
    if not isinstance(send_id, str) or not isinstance(state, str):
        raise ValueError("bulk send view")
    size = size_of(view)
    if state == "borrador" or size is None:
        return None
    started = _when(view.get("started_at"))
    ended = _when(view.get("finished_at")) if state == "terminado" else (
        _when(view.get("braked_at")) if state == "frenado" else None)
    if state == "frenado" and started is None:
        return None  # D-F2.2: frenado antes de mandar, no es una tanda
    reason = view.get("brake_reason") if isinstance(view.get("brake_reason"), str) else None
    return LargeSend(send_id=send_id, size=size, state=state, started_at=started,
                     ended_at=ended if started is not None else None, brake_reason=reason)


@dataclass(frozen=True)
class ReplyReading:
    delivered: int
    replies: int
    read_at: datetime


class BulkSendWarmup:
    """Lo que alimenta al calentamiento en el bridge: el historial de la base, las respuestas del reporte y el
    reloj; y lo que lo usa: el motor (``check``), la pagina y el AE (``view_for``) y ``/ready``.

    ``read_sends``: ``read_bulk_sends_v1`` de la instancia (``SupabaseBulkSendStore.read_sends``). ``report``:
    el reporte de un envio (``BulkSendRecipientsReport.report``), que da ``delivered`` y ``replies``.
    """

    def __init__(self, *, policy: WarmupPolicy, zone: ZoneInfo, instance_key: str,
                 read_sends: Callable[[], Awaitable[Sequence[Mapping[str, Any]]]],
                 report: Callable[[str], Awaitable[Any]] | None, clock: Callable[[], datetime],
                 history_ttl: timedelta = timedelta(seconds=60), reply_ttl: timedelta = timedelta(minutes=30),
                 refresh_every: timedelta = timedelta(minutes=15)) -> None:
        self.policy = policy
        self.zone = zone
        self.instance_key = instance_key
        self._read_sends = read_sends
        self._report = report
        self._clock = clock
        self._history_ttl = history_ttl
        self._reply_ttl = reply_ttl
        self.refresh_every = refresh_every
        self._history: tuple[datetime, list[LargeSend]] | None = None
        self._replies: dict[str, ReplyReading] = {}
        self._failed_at: dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        self._reading = asyncio.Lock()
        self._ready = "sin_calcular"

    def _now(self) -> datetime:
        now = self._clock()
        _aware(now, "clock")
        return now

    async def history(self, *, fresh: bool = False, read_replies: bool = False) -> list[LargeSend]:
        """Los masivos del numero, con las respuestas de la ultima tanda grande terminada.

        ``fresh`` vuelve a leer la base; ``read_replies`` lee las respuestas de Chatwoot si hace falta (lo
        hacen el motor y el bucle de fondo; la pagina y el AE usan la guardada y no esperan esa lectura,
        D-F2.4).
        """
        async with self._lock:
            now = self._now()
            if fresh or self._history is None or now - self._history[0] >= self._history_ttl:
                sends: list[LargeSend] = []
                for view in await self._read_sends():
                    if not isinstance(view, Mapping):
                        raise ValueError("bulk send view")
                    item = large_send_of(view)
                    if item is not None:
                        sends.append(item)
                self._history = (now, sends)
            sends = list(self._history[1])
        finished = [h for h in sends if h.size > self.policy.pilot_size and h.ended_at is not None]
        if not finished:
            return sends
        last = max(finished, key=lambda h: h.ended_at)  # type: ignore[arg-type, return-value]
        if read_replies and self._needs_reading(last, now):
            await self._read_replies(last)
        reading = self._replies.get(last.send_id)
        if reading is None:
            return sends
        return [replace(h, delivered=reading.delivered, replies=reading.replies) if h.send_id == last.send_id else h
                for h in sends]

    def _needs_reading(self, last: LargeSend, now: datetime) -> bool:
        """Se lee una vez pasadas las 24 h del fin (antes la tasa no decide: repite igual) y esa lectura queda;
        si fallo, se reintenta pasado ``reply_ttl``. Asi una tanda de 5.000 se lee de Chatwoot una sola vez."""
        assert last.ended_at is not None
        if self._report is None or last.send_id in self._replies or now < last.ended_at + self.policy.settle:
            return False
        failed = self._failed_at.get(last.send_id)
        return failed is None or now - failed >= self._reply_ttl

    async def _read_replies(self, last: LargeSend) -> None:
        assert self._report is not None
        async with self._reading:  # una sola lectura a la vez; quien llega despues la encuentra hecha
            now = self._now()
            if not self._needs_reading(last, now):
                return
            try:
                built = await self._report(last.send_id)
                reading = ReplyReading(delivered=int(built.delivered), replies=int(built.replies), read_at=now)
            except Exception as error:  # noqa: BLE001 - sin lectura repite el escalon, nunca sube
                logger.warning("bulk_send_warmup_replies_unreadable send=%s error=%s", last.send_id,
                               type(error).__name__)
                self._failed_at[last.send_id] = now
                return
            self._replies = {last.send_id: reading}
            self._failed_at.pop(last.send_id, None)
            logger.info("bulk_send_warmup_replies send=%s delivered=%s replies=%s", last.send_id, reading.delivered,
                        reading.replies)

    async def today(self, *, at: datetime | None = None, exclude_send_id: str | None = None,
                    count_pending: bool = False, fresh: bool = False, read_replies: bool = False) -> WarmupToday:
        sends = await self.history(fresh=fresh, read_replies=read_replies)
        return warmup_today(sends, at=at or self._now(), zone=self.zone, policy=self.policy,
                            exclude_send_id=exclude_send_id, count_pending=count_pending)

    async def check(self, *, send_id: str, size: int, at: datetime | None = None) -> Verdict:
        """El veredicto del motor al arrancar un masivo: base fresca y respuestas leidas (D-F2.7)."""
        now = at or self._now()
        today = await self.today(at=now, exclude_send_id=send_id, count_pending=False, fresh=True, read_replies=True)
        self._ready = self._readiness_of(today)
        return check_size(today, size, policy=self.policy, zone=self.zone)

    async def view_for(self, view: Mapping[str, Any]) -> dict[str, Any]:
        """Lo que ven la pagina y el AE de un masivo: el tope del dia de su hora pedida y si entra. Mismo texto
        para los dos (``detalle``)."""
        now = self._now()
        send_id = view.get("send_id") if isinstance(view.get("send_id"), str) else None
        state = str(view.get("state") or "")
        scheduled = _when(view.get("scheduled_at"))
        at = max(now, scheduled) if scheduled is not None else now
        today = await self.today(at=at, exclude_send_id=send_id, count_pending=state == "borrador")
        out: dict[str, Any] = {
            "dia": at.astimezone(self.zone).date().isoformat(), "zona": self.zone.key,
            "tope_del_dia": today.cap, "escalon": today.step + 1, "escalones": list(today.steps),
            "tanda_grande_ese_dia": today.large_allowed, "motivo": today.reason, "detalle": today.detail,
            "proxima": today.next_at.isoformat() if today.next_at else None, "proximo_tope": today.next_cap,
            "tanda_piloto": self.policy.pilot_size, "este_masivo": None}
        size = size_of(view)
        if state in ("borrador", "confirmado", "preparando") and size is not None:
            verdict = check_size(today, size, policy=self.policy, zone=self.zone)
            out["este_masivo"] = {"personas": size, "entra": verdict.allows, "motivo": verdict.reason,
                                  "detalle": verdict.detail}
        return out

    def _readiness_of(self, today: WarmupToday) -> str:
        if today.large_allowed:
            return f"{self.instance_key}={today.cap}"
        return f"{self.instance_key}=hoy_no:{today.reason}"

    async def refresh(self) -> str:
        """Recalcula el tope de hoy para ``/ready`` (y lee las respuestas si vencieron). No levanta."""
        try:
            today = await self.today(fresh=True, read_replies=True)
        except Exception as error:  # noqa: BLE001 - /ready lo dice; la proxima vuelta reintenta
            logger.warning("bulk_send_warmup_refresh_failed error=%s", type(error).__name__)
            self._ready = f"{self.instance_key}=sin_lectura"
            return self._ready
        self._ready = self._readiness_of(today)
        return self._ready

    def readiness(self) -> str:
        return self._ready


__all__ = [
    "BLOCK_GAP", "BLOCK_LIVE", "BLOCK_PENDING", "DEFAULT_MIN_GAP_DAYS", "DEFAULT_MIN_REPLY_RATE", "DEFAULT_STEPS",
    "ENV_VARIABLES", "FLAG", "BulkSendWarmup", "LargeSend", "WarmupConfigError", "WarmupPolicy", "WarmupToday",
    "check_size", "large_send_of", "parse_steps", "policy_from_env", "size_of", "warmup_today",
]
