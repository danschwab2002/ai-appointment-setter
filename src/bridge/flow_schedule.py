"""El horario de un flujo: la franja, si esta abierta y su proxima apertura.

Es la gemela de la ventana de ``reevaluate_followup_action`` en su version
vigente (``supabase/migrations/20260805000300_per_case_conversation_anchor.sql``,
:219-254; ninguna migracion posterior la redefine):

* **abierta**: ``to_char(p_now at time zone tz, 'HH24:MI')`` entre ``start`` y
  ``end``, los dos incluidos. Los segundos no cuentan, asi que el minuto de fin
  entra entero: con fin 21:00, las 21:00:59 estan abiertas, como dice el
  contrato (flow-definition-v1.md, «El horario y los topes»);
* **la proxima apertura**: el minimo de ``((p_now at time zone tz)::date + d)
  + start``, pasado a instante con ``at time zone tz``, con ``d`` de 0 a 7 (ocho
  fechas), que sea estrictamente posterior a ``p_now``. La base la calcula solo
  con la franja cerrada (para diferir o escalar); aca se calcula siempre.

Una franja es una sola ventana, la misma todos los dias: es lo que escribe el
esquema (``horario`` con zona, desde y hasta, o ``siempre``) y lo que compila
``flow_policy.compile_policy``. Una fila de ``followup_policy_versions`` con
otra forma (mas de una ventana, dias que no son los siete, una hora que la base
no lee) no es una franja: ``ValueError``, nunca una aproximacion.

**La hora local que no existe o que pasa dos veces** (el cambio de hora).
Medido en PGlite 0.5.4 (PostgreSQL 18.3) el 2026-10-16, con
``(fecha + hora) at time zone zona``: de las dos lecturas posibles de una hora
local, la base toma la que cae mas tarde en UTC. En Europe/Madrid, el
2026-03-29 las 02:30 no existen y dan 01:30Z, con el offset de antes del salto
(+01:00, lo que ``zoneinfo`` llama ``fold=0``); el 2026-10-25 las 02:30 pasan
dos veces y dan 01:30Z, la segunda, con el offset de despues (+01:00,
``fold=1``). Se contrasto en 22 horas de 11 zonas (Nueva York, Santiago, Lord
Howe con su media hora, Moscu, Apia, Dublin, CDMX y Etc/GMT+5): en todas, la
base da el maximo en UTC de ``fold=0`` y ``fold=1``, y asi se replica aca.
``fold=0`` solo se equivoca en la hora que pasa dos veces: difiere a la
primera, una hora antes que la base. Lo fijan los casos de Madrid de
``tests/fixtures/flow_engine/window_cases.json``, en pytest y en PGlite.

La otra direccion, de instante a hora local (``p_now at time zone tz``), no
tiene ambiguedad: ``astimezone``.

Es puro: no lee el entorno, no hace red ni toca bases. Las zonas salen de la
base de zonas del sistema (``zoneinfo``), como en ``flow_definition``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .flow_definition import ALWAYS, is_iana_zone, parse_hour

DAY_MINUTES = 24 * 60
LAST_MINUTE = DAY_MINUTES - 1
# Los dias de la semana de la base, isodow: del lunes (1) al domingo (7).
ISO_WEEK_DAYS = (1, 2, 3, 4, 5, 6, 7)
# Las ocho fechas que mira la base para la proxima apertura (generate_series(0, 7)).
NEXT_OPEN_DAYS = range(0, 8)
# La zona de un flujo que nunca tuvo un horario objeto (flow_policy, §2.5).
DEFAULT_ZONE = "UTC"


@dataclass(frozen=True)
class Franja:
    """Una ventana de horas, la misma todos los dias, en una zona.

    ``start_minute`` y ``end_minute`` son minutos desde la medianoche local, los
    dos incluidos, y el minuto de fin entra entero. ``always`` es el horario
    ``siempre``: abierta a toda hora, con la franja de 00:00 a 23:59 que compila
    la politica y la zona fija del flujo, o ninguna. ``zone`` es None solo con
    ``always``.

    Como la base, no exige ``start <= end``: con el inicio despues del fin la
    franja no abre nunca (el validador ya lo impide al publicar, regla 8).
    """

    always: bool
    zone: str | None
    start_minute: int
    end_minute: int

    def __post_init__(self) -> None:
        if not isinstance(self.always, bool):
            raise ValueError("franja_always_invalid")
        if self.zone is None:
            if not self.always:
                raise ValueError("franja_zone_missing")
        elif not is_iana_zone(self.zone):
            raise ValueError("franja_zone_invalid")
        for minute in (self.start_minute, self.end_minute):
            if not isinstance(minute, int) or isinstance(minute, bool) or not 0 <= minute <= LAST_MINUTE:
                raise ValueError("franja_minute_invalid")
        if self.always and (self.start_minute, self.end_minute) != (0, LAST_MINUTE):
            raise ValueError("franja_always_not_whole_day")


def hour_text(minute: int) -> str:
    """Los minutos desde la medianoche como ``HH:MM``, la forma de la base."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def franja_of(horario: object, *, fixed_zone: str | None = None) -> Franja:
    """La franja del ``horario`` de una definicion: un objeto o ``siempre``.

    Con ``siempre``, la zona es ``fixed_zone`` (la zona fija del flujo,
    ``WorldFlow.fixed_zone``), o ninguna. Con un objeto, la zona es la suya; si
    viene ``fixed_zone`` y no coincide, el mundo esta roto (la zona no cambia,
    invariante 4 del almacen) y es ``ValueError``.
    """
    if fixed_zone is not None and not is_iana_zone(fixed_zone):
        raise ValueError("franja_fixed_zone_invalid")
    if horario == ALWAYS:
        return Franja(always=True, zone=fixed_zone, start_minute=0, end_minute=LAST_MINUTE)
    if not isinstance(horario, Mapping) or set(horario) != {"zona", "desde", "hasta"}:
        raise ValueError("franja_horario_invalid")
    zone, start, end = horario["zona"], parse_hour(horario["desde"]), parse_hour(horario["hasta"])
    if not is_iana_zone(zone):
        raise ValueError("franja_zone_invalid")
    if start is None or end is None:
        raise ValueError("franja_hour_invalid")
    if fixed_zone is not None and fixed_zone != zone:
        raise ValueError("franja_zone_differs_from_fixed_zone")
    return Franja(always=False, zone=zone, start_minute=start, end_minute=end)


def franja_of_policy(policy: Mapping[str, Any]) -> Franja:
    """Desde una fila de followup_policy_versions: business_windows y timezone.

    Lee lo que lee la base: una sola ventana con ``start`` y ``end`` en
    ``HH:MM`` y ``days`` que contiene los siete ``isodow``. Las claves de mas de
    la ventana y los dias de mas no cambian nada en la base, y aca tampoco.
    Nunca da ``always``: la fila de un ``siempre`` es la ventana de 00:00 a
    23:59, que abre a toda hora igual.
    """
    if not isinstance(policy, Mapping):
        raise ValueError("franja_policy_invalid")
    zone = policy.get("timezone")
    if not is_iana_zone(zone):
        raise ValueError("franja_zone_invalid")
    windows = policy.get("business_windows")
    if not isinstance(windows, list) or len(windows) != 1 or not isinstance(windows[0], Mapping):
        raise ValueError("franja_policy_not_one_window")
    window = windows[0]
    days = window.get("days")
    if not isinstance(days, list) or not all(
        any(isinstance(day, int) and not isinstance(day, bool) and day == iso for day in days)
        for iso in ISO_WEEK_DAYS
    ):
        raise ValueError("franja_policy_not_every_day")
    start, end = parse_hour(window.get("start")), parse_hour(window.get("end"))
    if start is None or end is None:
        raise ValueError("franja_hour_invalid")
    return Franja(always=False, zone=zone, start_minute=start, end_minute=end)


def _require_aware(at: datetime) -> None:
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("instant_without_zone")


def _zone_info(franja: Franja) -> ZoneInfo:
    return ZoneInfo(franja.zone if franja.zone is not None else DEFAULT_ZONE)


def is_open(franja: Franja, at: datetime) -> bool:
    """Compara HH:MM local (sin segundos) contra inicio y fin, como to_char(.., 'HH24:MI')."""
    _require_aware(at)
    if franja.always:
        return True
    local = at.astimezone(_zone_info(franja))
    minute = local.hour * 60 + local.minute
    return franja.start_minute <= minute <= franja.end_minute


def _local_instant(zone: ZoneInfo, day: date, minute: int) -> datetime:
    """La fecha y la hora locales como instante, como ``timestamp at time zone``.

    De las dos lecturas (``fold=0`` y ``fold=1``), la que cae mas tarde en UTC:
    asi resuelve la base la hora que no existe y la que pasa dos veces (ver el
    docstring del modulo). Se comparan en UTC: dentro de la misma zona, Python
    compara sin mirar ``fold``.
    """
    naive = datetime.combine(day, time(minute // 60, minute % 60))
    return max(naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc) for fold in (0, 1))


def next_open(franja: Franja, at: datetime) -> datetime | None:
    """El primer (fecha local + d) + inicio, con d de 0 a 7 (ocho fechas), estrictamente posterior a at.

    En UTC. Con ``always`` es la proxima medianoche de su zona (o de UTC, sin
    zona), como la fila que compila un ``siempre``. Con una sola ventana todos
    los dias siempre hay una; None queda para la forma, como en la base.
    """
    _require_aware(at)
    zone = _zone_info(franja)
    today = at.astimezone(zone).date()
    candidates = [
        _local_instant(zone, today + timedelta(days=offset), franja.start_minute)
        for offset in NEXT_OPEN_DAYS
    ]
    later = [candidate for candidate in candidates if candidate > at]
    return min(later) if later else None


def local_day(zone: str, at: datetime) -> date:
    """El dia local de ``at`` en la zona: el de ``(at at time zone zona)::date``."""
    _require_aware(at)
    if not is_iana_zone(zone):
        raise ValueError("franja_zone_invalid")
    return at.astimezone(ZoneInfo(zone)).date()

