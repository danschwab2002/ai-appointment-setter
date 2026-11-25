"""Los frenos del masivo manejado por el Automation Expert (encargo 8, pieza 10).

Diseno: ``coordinacion/etapa-1`` del vault, ``documentacion/2026-11-25-mapa-masivo-por-el-ae.md``,
secciones «9. El motor de envio», «10. Los frenos» y las decisiones 2, 5, 6, 12 y 13.

Portado del prototipo que manda los masivos del 22/11 y del 29/11 por Chatwoot
(``scripts-2026-11-25/vivo-29-11/masivo.py``: ``evaluar_freno``, ``ajustar_ritmo``,
``comparar_salud``, ``freno_salud_al_arrancar``, ``nivel_limite`` y ``salida_de``), con
las pruebas de su ``--selftest`` y ``selftest_2511`` en ``tests/test_bulk_send_brakes.py``.

Lo que decide cada funcion:

* ``meta_code_brake``: los codigos de Meta que cortan. 368, 131031, 131042, 131048 y
  130429 al primero; 130472, 131056 y los de plantilla (132000-132016) al tercero.
* ``delivery_brake``: con 40 resultados finales o mas, mas del 15 % fallidos o mas del
  35 % con 131049.
* ``chatwoot_api_brake``: mas del 20 % de errores en las ultimas 50 llamadas a la API
  de Chatwoot (con 20 llamadas como minimo, como el prototipo).
* ``evaluate_batch``: los tres de arriba, en ese orden, sobre el estado de un lote.
* ``start_health_verdict`` y ``portfolio_start_verdict``: si arranca segun la salud
  (decision 5): verde arranca; amarillo solo con la segunda marca del aprobador; rojo,
  ``BLOCKED``, un numero ``FLAGGED``/``RESTRICTED``/``BANNED`` o una lectura que no se
  entiende no arranca.
* ``compare_health`` y ``compare_portfolio``: en el medio frena solo si la salud
  empeora contra la lectura del arranque, en cualquier numero del portfolio (decision 6).
* ``adjust_rate`` y ``retained_in_window``: el ritmo adaptativo (decision 2).
* ``pilot_verdict``: la tanda piloto (decision 12).
* ``start_after_meta_brake_verdict``: la regla de las 24 h (decision 13).

Este modulo es puro: no lee la base, ni Chatwoot, ni Meta, ni el reloj. Todo entra por
argumento. Las lecturas de salud llegan ya normalizadas, con la forma de ``normalizar``
de ``despliegue/salud-meta/salud_meta.py`` del repo de la instancia (``calidad``,
``limite``, ``estado_numero``, ``puede_enviar``; ``error`` o ``sin_numero`` cuando no se
pudo leer). Nadie lo usa todavia: lo llama el motor de envio (encargo 9).

Los valores marcados ``DEFAULT_REVISAR_DAN`` son defaults de esta pieza que el mapa no
fija con numero y tiene que revisar Dan.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Los codigos de Meta que cortan (masivo.py:121-124). El numero es cuantos hacen falta.
# ---------------------------------------------------------------------------

META_CODE_THRESHOLDS: Mapping[str, int] = {
    # Al primero: la cuenta, el numero o el ecosistema dicen que se pare.
    "368": 1,  # cuenta restringida temporalmente por violar politicas
    "131031": 1,  # cuenta bloqueada
    "131042": 1,  # problema con el medio de pago de la cuenta
    "131048": 1,  # limite por spam: demasiados mensajes marcados o bloqueados
    "130429": 1,  # se paso el throughput del numero
    # Al tercero: pueden ser de una persona puntual.
    "130472": 3,  # numero del usuario en un experimento de Meta
    "131056": 3,  # demasiados mensajes al mismo numero en poco tiempo
    "132000": 3,  # la cantidad de variables no coincide con la plantilla
    "132001": 3,  # la plantilla no existe en ese idioma
    "132005": 3,  # el texto con las variables es demasiado largo
    "132007": 3,  # el contenido viola la politica de la plantilla
    "132012": 3,  # el formato de las variables no coincide
    "132015": 3,  # plantilla pausada por baja calidad
    "132016": 3,  # plantilla deshabilitada
}

CODE_131049 = "131049"

# Los status de ``messages.status`` de Chatwoot: 0 sent, 1 delivered, 2 read, 3 failed.
STATUS_SENT = 0
STATUS_DELIVERED = 1
STATUS_READ = 2
STATUS_FAILED = 3
_STATUS_NAMES = {"sent": STATUS_SENT, "delivered": STATUS_DELIVERED, "read": STATUS_READ, "failed": STATUS_FAILED}

# Prefijos de los motivos. Un motivo es ``<prefijo>:<detalle>``, sin telefonos ni nombres.
REASON_META_CODE = "meta_codigo"
REASON_DELIVERY = "entrega"
REASON_CHATWOOT_API = "chatwoot_api"
REASON_HEALTH = "salud"
REASON_PILOT = "piloto"
REASON_24H = "regla_24h"


@dataclass(frozen=True)
class Brake:
    """Un freno: ``reason`` es el codigo estable (va al registro y a ``SYS-007``); ``detail``, la
    explicacion en castellano para el aviso."""

    reason: str
    detail: str


@dataclass(frozen=True)
class BrakeThresholds:
    """Los umbrales de ``evaluate_batch`` (masivo.py:1285-1288 y :895)."""

    min_sample: int = 40
    failed_ratio: float = 0.15
    ratio_131049: float = 0.35
    chatwoot_window: int = 50
    chatwoot_min_calls: int = 20
    chatwoot_failed_ratio: float = 0.20


DEFAULT_THRESHOLDS = BrakeThresholds()


def _codes(codes: Mapping[Any, int] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for code, n in (codes or {}).items():
        key = str(code).strip()
        out[key] = out.get(key, 0) + int(n or 0)
    return out


def _statuses(statuses: Mapping[Any, int] | None) -> dict[int, int]:
    """Acepta las claves enteras de Chatwoot (0-3) o sus nombres (``sent``, ``failed``...)."""
    out: dict[int, int] = {}
    for status, n in (statuses or {}).items():
        if isinstance(status, str) and status.strip().lower() in _STATUS_NAMES:
            key = _STATUS_NAMES[status.strip().lower()]
        else:
            try:
                key = int(status)
            except (TypeError, ValueError):
                continue
        out[key] = out.get(key, 0) + int(n or 0)
    return out


def meta_code_brake(codes: Mapping[Any, int] | None) -> Brake | None:
    """El primer codigo de ``META_CODE_THRESHOLDS`` que llego a su tope, o ``None``."""
    seen = _codes(codes)
    for code, threshold in META_CODE_THRESHOLDS.items():
        n = seen.get(code, 0)
        if n >= threshold:
            return Brake(
                "%s:%s_x%d" % (REASON_META_CODE, code, n),
                "Meta contesto %d vez/veces el codigo %s, que corta el envio" % (n, code),
            )
    return None


def delivery_brake(
    statuses: Mapping[Any, int] | None,
    codes: Mapping[Any, int] | None,
    thresholds: BrakeThresholds = DEFAULT_THRESHOLDS,
) -> Brake | None:
    """Con ``min_sample`` resultados finales o mas (entregados + leidos + fallidos), frena con mas
    de ``failed_ratio`` fallidos o mas de ``ratio_131049`` con 131049. Lo que sigue en «sent» no
    cuenta: no es un resultado."""
    st, seen = _statuses(statuses), _codes(codes)
    failed = st.get(STATUS_FAILED, 0)
    finals = st.get(STATUS_DELIVERED, 0) + st.get(STATUS_READ, 0) + failed
    if finals < thresholds.min_sample:
        return None
    if failed / finals > thresholds.failed_ratio:
        return Brake(
            "%s:fallidos_%d_de_%d" % (REASON_DELIVERY, failed, finals),
            "%d de %d mensajes con resultado fallaron (mas del %d %%)"
            % (failed, finals, round(thresholds.failed_ratio * 100)),
        )
    n_131049 = seen.get(CODE_131049, 0)
    if n_131049 / finals > thresholds.ratio_131049:
        return Brake(
            "%s:131049_%d_de_%d" % (REASON_DELIVERY, n_131049, finals),
            "%d de %d mensajes con resultado los freno Meta con 131049 (mas del %d %%)"
            % (n_131049, finals, round(thresholds.ratio_131049 * 100)),
        )
    return None


def chatwoot_api_brake(
    call_failures: Sequence[bool], thresholds: BrakeThresholds = DEFAULT_THRESHOLDS
) -> Brake | None:
    """``call_failures``: una entrada por llamada a la API de Chatwoot, en orden, ``True`` si fallo.
    Mira las ultimas ``chatwoot_window``; con menos de ``chatwoot_min_calls`` no decide. Un veto
    (la persona no se manda) no es una falla: no va en la lista como ``True``."""
    window = [bool(x) for x in list(call_failures)[-thresholds.chatwoot_window:]]
    if len(window) < thresholds.chatwoot_min_calls:
        return None
    failed = sum(window)
    if failed / len(window) > thresholds.chatwoot_failed_ratio:
        return Brake(
            "%s:%d_de_%d" % (REASON_CHATWOOT_API, failed, len(window)),
            "fallaron %d de las ultimas %d llamadas a Chatwoot (mas del %d %%)"
            % (failed, len(window), round(thresholds.chatwoot_failed_ratio * 100)),
        )
    return None


def evaluate_batch(
    statuses: Mapping[Any, int] | None,
    codes: Mapping[Any, int] | None,
    call_failures: Sequence[bool],
    thresholds: BrakeThresholds = DEFAULT_THRESHOLDS,
) -> Brake | None:
    """El freno de un lote: codigos de Meta, despues los porcentajes, despues la API de Chatwoot."""
    return (
        meta_code_brake(codes)
        or delivery_brake(statuses, codes, thresholds)
        or chatwoot_api_brake(call_failures, thresholds)
    )


# ---------------------------------------------------------------------------
# La salud de Meta (decisiones 5 y 6). Lecturas con la forma de ``salud_meta.normalizar``.
# ---------------------------------------------------------------------------

QUALITY_ORDER: Mapping[str, int] = {"GREEN": 0, "YELLOW": 1, "RED": 2}
# ``status`` del numero en la Graph API que cuentan como malos.
BAD_NUMBER_STATES = frozenset({"FLAGGED", "RESTRICTED", "BANNED"})
SEND_BLOCKED = "BLOCKED"


def messaging_limit_level(limit: Any) -> float | None:
    """``TIER_250`` -> 250, ``TIER_1K`` -> 1000, ``TIER_100K`` -> 100000, ``TIER_UNLIMITED`` -> inf;
    ``None`` si no se entiende (``"?"``, vacio)."""
    text = str(limit or "").upper()
    if "UNLIMITED" in text:
        return math.inf
    match = re.search(r"(\d+)\s*([KM]?)\b", text)
    if not match:
        return None
    return float(int(match.group(1)) * {"": 1, "K": 1000, "M": 1000000}[match.group(2)])


def _number_state(reading: Mapping[str, Any]) -> Any:
    # ``estado_numero`` es el campo de ``normalizar``; ``estado`` es el del prototipo.
    return reading.get("estado_numero", reading.get("estado"))


def _legible(reading: Any) -> bool:
    return (
        isinstance(reading, Mapping)
        and "error" not in reading
        and "sin_numero" not in reading
        and reading.get("calidad") in QUALITY_ORDER
    )


@dataclass(frozen=True)
class Verdict:
    """El veredicto de una funcion de decision: ``allows`` (arranca / sigue), el codigo del motivo y
    su explicacion en castellano. Con ``allows`` en ``True`` el motivo puede traer un aviso."""

    allows: bool
    reason: str | None = None
    detail: str | None = None


def start_health_verdict(reading: Mapping[str, Any] | None, *, yellow_confirmed: bool) -> Verdict:
    """Si el numero que manda puede arrancar (decision 5). Fail-closed: sin una lectura que se
    entienda no arranca. ``yellow_confirmed`` es la segunda marca del aprobador («el numero esta
    en amarillo, lo mando igual»); no habilita el rojo."""
    if not _legible(reading):
        return Verdict(False, "%s:ilegible_al_arrancar" % REASON_HEALTH,
                       "no se pudo leer la calidad del numero: sin lectura no arranca")
    assert reading is not None
    if reading.get("puede_enviar") == SEND_BLOCKED:
        return Verdict(False, "%s:cuenta_blocked_al_arrancar" % REASON_HEALTH,
                       "Meta dice que la cuenta no puede mandar (BLOCKED)")
    state = _number_state(reading)
    if state in BAD_NUMBER_STATES:
        return Verdict(False, "%s:numero_%s_al_arrancar" % (REASON_HEALTH, str(state).lower()),
                       "el numero esta en %s" % state)
    quality = reading["calidad"]
    if quality == "RED":
        return Verdict(False, "%s:rojo_al_arrancar" % REASON_HEALTH, "el numero esta en rojo: no arranca")
    if quality == "YELLOW":
        if not yellow_confirmed:
            return Verdict(False, "%s:amarillo_sin_confirmar" % REASON_HEALTH,
                           "el numero esta en amarillo: hace falta la segunda marca del aprobador")
        return Verdict(True, "%s:amarillo_confirmado" % REASON_HEALTH,
                       "el numero esta en amarillo y el aprobador lo confirmo")
    return Verdict(True)


def portfolio_start_verdict(
    sending: str, readings: Mapping[str, Mapping[str, Any] | None], *, yellow_confirmed: bool
) -> Verdict:
    """Si arranca, mirando todos los numeros del portfolio (decision 6). ``readings``: clave de la
    instancia (``maribel``, ``att1``...) -> lectura. El que manda pasa por ``start_health_verdict``;
    de los demas hace falta una lectura que se entienda (es la base contra la que se compara en el
    medio) y que su cuenta no este ``BLOCKED``. El rojo o el amarillo de otro numero no frenan el
    arranque: frenan si empeoran."""
    if sending not in readings:
        return Verdict(False, "%s:%s:sin_lectura" % (REASON_HEALTH, sending),
                       "no hay lectura del numero que manda (%s)" % sending)
    own = start_health_verdict(readings[sending], yellow_confirmed=yellow_confirmed)
    if not own.allows:
        what = (own.reason or "?").split(":", 1)[-1]
        return Verdict(False, "%s:%s:%s" % (REASON_HEALTH, sending, what), own.detail)
    for key in sorted(readings):
        if key == sending:
            continue
        other = readings[key]
        if not _legible(other):
            return Verdict(False, "%s:%s:ilegible_al_arrancar" % (REASON_HEALTH, key),
                           "no se pudo leer la salud de %s, que comparte el portfolio" % key)
        assert other is not None
        if other.get("puede_enviar") == SEND_BLOCKED:
            return Verdict(False, "%s:%s:cuenta_blocked_al_arrancar" % (REASON_HEALTH, key),
                           "Meta tiene en BLOCKED a %s, que comparte el portfolio" % key)
    return own


def compare_health(
    baseline: Mapping[str, Any], current: Mapping[str, Any] | None
) -> tuple[Brake | None, str | None]:
    """``(freno, aviso)`` contra la lectura del arranque. Frena solo si empeora: la calidad
    (GREEN < YELLOW < RED), el limite (tier) baja, la cuenta pasa a ``BLOCKED`` o el numero pasa
    a ``FLAGGED``/``RESTRICTED``/``BANNED``. Una mejora no frena: queda como aviso y se sigue
    comparando contra el arranque. Una lectura que no se entiende tampoco frena (aviso): los
    codigos de Meta del lote siguen frenando por su lado."""
    if not isinstance(current, Mapping):
        return None, "no pude leer la salud (sin lectura); sigo"
    if "error" in current or "sin_numero" in current:
        return None, "no pude leer la salud (%s); sigo" % (current.get("error") or "sin numero")
    notices: list[str] = []
    qb, qc = baseline.get("calidad"), current.get("calidad")
    if qc not in QUALITY_ORDER:
        notices.append("no pude leer la calidad (%s); sigo" % qc)
    elif QUALITY_ORDER[qc] > QUALITY_ORDER.get(qb, 0):
        return Brake("%s:calidad_%s_a_%s" % (REASON_HEALTH, qb, qc),
                     "la calidad del numero empeoro de %s a %s" % (qb, qc)), None
    elif QUALITY_ORDER[qc] < QUALITY_ORDER.get(qb, 0):
        notices.append("la calidad mejoro de %s a %s (sigo; comparo contra el arranque)" % (qb, qc))
    lb, lc = messaging_limit_level(baseline.get("limite")), messaging_limit_level(current.get("limite"))
    if lb is not None and lc is not None and lc < lb:
        return Brake("%s:limite_%s_a_%s" % (REASON_HEALTH, baseline.get("limite"), current.get("limite")),
                     "Meta bajo el limite de mensajeria de %s a %s"
                     % (baseline.get("limite"), current.get("limite"))), None
    if lb is not None and lc is not None and lc > lb:
        notices.append("el limite subio de %s a %s" % (baseline.get("limite"), current.get("limite")))
    if current.get("puede_enviar") == SEND_BLOCKED and baseline.get("puede_enviar") != SEND_BLOCKED:
        return Brake("%s:cuenta_blocked" % REASON_HEALTH, "Meta paso la cuenta a BLOCKED"), None
    sb, sc = _number_state(baseline), _number_state(current)
    if sc in BAD_NUMBER_STATES and sb not in BAD_NUMBER_STATES:
        return Brake("%s:numero_%s" % (REASON_HEALTH, str(sc).lower()), "el numero paso a %s" % sc), None
    return None, ("; ".join(notices) or None)


def compare_portfolio(
    baselines: Mapping[str, Mapping[str, Any]], currents: Mapping[str, Mapping[str, Any] | None]
) -> tuple[Brake | None, str | None]:
    """``compare_health`` sobre cada numero del portfolio con lectura de arranque (decision 6). Si
    cualquiera empeora, frena; el motivo lleva la clave de la instancia. Un numero sin lectura en
    el medio es un aviso."""
    notices: list[str] = []
    for key in sorted(baselines):
        brake, notice = compare_health(baselines[key], currents.get(key))
        if brake:
            return Brake("%s:%s:%s" % (REASON_HEALTH, key, brake.reason.split(":", 1)[1]),
                         "%s: %s" % (key, brake.detail)), None
        if notice:
            notices.append("%s: %s" % (key, notice))
    return None, ("; ".join(notices) or None)


# ---------------------------------------------------------------------------
# El ritmo adaptativo (decision 2; masivo.py:900-918).
# ---------------------------------------------------------------------------

DEFAULT_RATE = 2.5  # por segundo: el techo de la decision 2
HARD_CEILING_RATE = 5.0  # techo duro: ningun pedido lo pasa
SLOW_RATE = 1.0
STEP_UP = 0.5
RETAINED_WINDOW_SECONDS = (180, 480)  # lo mandado hace 3 a 8 minutos


@dataclass(frozen=True)
class RatePolicy:
    """Lo que gobierna ``adjust_rate``. ``min_sample``: con menos mensajes en la ventana no decide."""

    slow: float = SLOW_RATE
    step_up: float = STEP_UP
    retained_ratio: float = 0.30
    min_sample: int = 20


DEFAULT_RATE_POLICY = RatePolicy()


def requested_rate(requested: float | None) -> float:
    """El ritmo pedido, acotado: sin pedido el techo de 2,5/s; nunca mas de 5/s; nunca 0 o menos."""
    if requested is None:
        return DEFAULT_RATE
    rate = float(requested)
    if not rate > 0 or math.isnan(rate):
        raise ValueError("el ritmo tiene que ser mayor que 0")
    return min(rate, HARD_CEILING_RATE)


def retained_in_window(
    sent: Iterable[tuple[datetime, Any]], *, now: datetime,
    window: tuple[int, int] = RETAINED_WINDOW_SECONDS,
) -> tuple[int, int]:
    """``(retenidos, total)`` de los mensajes mandados hace entre ``window[0]`` y ``window[1]``
    segundos. ``sent``: ``(cuando se mando, status de Chatwoot)``. Retenido es el que sigue en
    «sent» (0): Chatwoot crea el saliente con 0 antes de que Meta conteste, asi que lo que se
    cuenta es «todavia sin entregado, leido ni fallido»."""
    _aware(now, "now")
    low, high = now - timedelta(seconds=window[1]), now - timedelta(seconds=window[0])
    retained = total = 0
    for sent_at, status in sent:
        _aware(sent_at, "sent_at")
        if low <= sent_at <= high:
            total += 1
            if _statuses({status: 1}).get(STATUS_SENT):
                retained += 1
    return retained, total


def adjust_rate(
    current: float, requested: float | None, retained: int, total: int,
    policy: RatePolicy = DEFAULT_RATE_POLICY,
) -> tuple[float, str | None]:
    """``(ritmo nuevo, evento)``. Con mas de ``retained_ratio`` retenido baja a ``slow`` (o al
    pedido, si es menor); con la mitad del umbral o menos sube de a ``step_up`` hasta el pedido.
    Entre las dos zonas se queda. Con menos de ``min_sample`` mensajes en la ventana no decide."""
    target = requested_rate(requested)
    current = min(float(current), target)
    slow = min(policy.slow, target)
    if total < policy.min_sample:
        return current, None
    ratio = retained / total
    if ratio > policy.retained_ratio and current > slow:
        return slow, "ritmo_bajado de %.1f/s a %.1f/s · retenidos %d de %d (%d %%) con 3 a 8 min sin entregar" % (
            current, slow, retained, total, round(ratio * 100))
    if ratio <= policy.retained_ratio / 2 and current < target:
        new = min(target, current + policy.step_up)
        return new, "ritmo_subido de %.1f/s a %.1f/s · retenidos %d de %d (%d %%)" % (
            current, new, retained, total, round(ratio * 100))
    return current, None


# ---------------------------------------------------------------------------
# La tanda piloto (decision 12).
# ---------------------------------------------------------------------------

PILOT_SEND = "mandar_piloto"
PILOT_WAIT = "esperar"
PILOT_CONTINUE = "seguir"
PILOT_STOP = "frenar"
PILOT_DONE = "terminado"


@dataclass(frozen=True)
class PilotPolicy:
    """Los primeros ``size`` y ``wait_seconds`` de espera mirando la entrega (decision 12).
    ``max_wait_seconds`` y los dos umbrales son ``DEFAULT_REVISAR_DAN``: si a los 5 minutos mas
    de ``retained_ratio`` del piloto sigue sin resultado, espera hasta 15 minutos y despues frena;
    con mas de ``failed_ratio`` del piloto fallido frena (con 20, el cuarto)."""

    size: int = 20
    wait_seconds: int = 300
    max_wait_seconds: int = 900
    retained_ratio: float = 0.30
    failed_ratio: float = 0.15


DEFAULT_PILOT_POLICY = PilotPolicy()


@dataclass(frozen=True)
class PilotState:
    """El estado del piloto. ``recipients``: personas del envio sin vetar; ``sent``: cuantas del
    piloto salieron; ``finished_at``: cuando salio la ultima del piloto; ``finals`` y ``failed``:
    del piloto, con resultado (entregado + leido + fallido) y fallidas; ``brake``: el freno que dio
    ``evaluate_batch`` o la salud en ese tiempo, si alguno."""

    recipients: int
    sent: int
    now: datetime
    finished_at: datetime | None = None
    finals: int = 0
    failed: int = 0
    brake: Brake | None = None


def pilot_verdict(state: PilotState, policy: PilotPolicy = DEFAULT_PILOT_POLICY) -> tuple[str, str | None]:
    """``(que hacer, motivo)``: ``mandar_piloto`` (faltan del piloto), ``esperar`` (no pasaron los 5
    minutos, o el piloto sigue sin resultado), ``seguir``, ``frenar`` o ``terminado`` (la lista
    entera cabia en el piloto y ya salio). Un freno en el piloto no sigue nunca."""
    _aware(state.now, "now")
    if state.brake is not None:
        return PILOT_STOP, "%s:%s" % (REASON_PILOT, state.brake.reason)
    size = min(policy.size, max(state.recipients, 0))
    if state.sent < size:
        return PILOT_SEND, None
    if size and state.failed / size > policy.failed_ratio:
        return PILOT_STOP, "%s:%s:fallidos_%d_de_%d" % (REASON_PILOT, REASON_DELIVERY, state.failed, size)
    if state.sent >= state.recipients:
        return PILOT_DONE, None
    if state.finished_at is None:
        return PILOT_WAIT, "%s:sin_hora_de_fin" % REASON_PILOT
    _aware(state.finished_at, "finished_at")
    elapsed = (state.now - state.finished_at).total_seconds()
    if elapsed < policy.wait_seconds:
        return PILOT_WAIT, "%s:esperando_%ds" % (REASON_PILOT, round(policy.wait_seconds - elapsed))
    pending = size - min(state.finals, size)
    if size and pending / size > policy.retained_ratio:
        if elapsed < policy.max_wait_seconds:
            return PILOT_WAIT, "%s:sin_resultado_%d_de_%d" % (REASON_PILOT, pending, size)
        return PILOT_STOP, "%s:%s:sin_entrega_%d_de_%d" % (REASON_PILOT, REASON_DELIVERY, pending, size)
    return PILOT_CONTINUE, None


# ---------------------------------------------------------------------------
# La regla de las 24 h (decision 13; decision 5 del LEEME de masivo.py hecha general).
# ---------------------------------------------------------------------------

META_BRAKE_WINDOW = timedelta(hours=24)
# Los frenos que no son de Meta: la API de Chatwoot. Un corte manual o por hora no es un freno.
_NOT_META_PREFIXES = (REASON_CHATWOOT_API + ":",)


def is_meta_brake(reason: str | None) -> bool:
    """Si un freno vino de Meta: un codigo, la entrega (fallidos, 131049, o el piloto sin entregar)
    o la salud, tambien dentro de la tanda piloto. La API de Chatwoot no cuenta."""
    if not reason:
        return False
    text = reason[len(REASON_PILOT) + 1:] if reason.startswith(REASON_PILOT + ":") else reason
    if text.startswith(_NOT_META_PREFIXES):
        return False
    return text.startswith((REASON_META_CODE + ":", REASON_DELIVERY + ":", REASON_HEALTH + ":"))


@dataclass(frozen=True)
class FinishedSend:
    """Un envio del portfolio que termino frenado: cuando, el motivo y su referencia (sin datos
    personales)."""

    send_ref: str
    braked_at: datetime
    reason: str


def start_after_meta_brake_verdict(
    history: Iterable[FinishedSend], *, now: datetime, confirmed_at: datetime | None = None,
    window: timedelta = META_BRAKE_WINDOW,
) -> Verdict:
    """Si un masivo del portfolio puede arrancar segun la regla de las 24 h. Si algun envio freno
    por Meta en las ultimas 24 h, no arranca sin una confirmacion nueva en la pagina, posterior al
    ultimo de esos frenos (``confirmed_at``). Un freno con fecha futura cuenta (fail-closed)."""
    _aware(now, "now")
    relevant = []
    for item in history:
        _aware(item.braked_at, "braked_at")
        if is_meta_brake(item.reason) and item.braked_at > now - window:
            relevant.append(item)
    if not relevant:
        return Verdict(True)
    last = max(relevant, key=lambda x: x.braked_at)
    if confirmed_at is not None:
        _aware(confirmed_at, "confirmed_at")
        if last.braked_at < confirmed_at <= now:
            return Verdict(True, "%s:confirmado_tras_%s" % (REASON_24H, last.send_ref),
                           "se confirmo de nuevo despues del freno de Meta de %s" % last.send_ref)
    hours = max(0.0, (now - last.braked_at).total_seconds() / 3600)
    return Verdict(False, "%s:freno_meta_%s" % (REASON_24H, last.send_ref),
                   "el envio %s freno por Meta hace %.1f h (%s): hace falta una confirmacion nueva"
                   % (last.send_ref, hours, last.reason))


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("%s tiene que ser un datetime con zona" % name)
