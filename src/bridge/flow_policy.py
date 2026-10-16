"""La politica compilada de un flujo del motor: su fila de followup_policy_versions.

Diseno del motor de flujos, §2.5. Por la clave foranea, el sustrato exige una
fila de ``followup_policy_versions`` para cada caso y cada accion (§1.0). El
motor la compila de la definicion publicada, con la clave ``flujo:<flow_id>`` y
la version del flujo. Este modulo es la gemela en Python de
``_flow_engine_compile_policy`` (T2); las dos se atan con
``tests/fixtures/flow_engine/compile_cases.json``.

Los campos:

* ``timezone`` y ``business_windows``: la franja del horario
  (``flow_schedule.franja_of``), una sola ventana todos los dias; con
  ``siempre``, de 00:00 a 23:59 en la zona fija del flujo, o en UTC si nunca
  tuvo una;
* ``grace_period``: las esperas antes del envio, o 0. La primera espera se
  cuenta desde el disparo y cada una desde la anterior (flow-definition-v1.md,
  «Los pasos»), asi que, si hay varias seguidas, la gracia es la suma; con una
  sola, es «la primera espera» del diseno;
* ``expires_after``: ``termina.vence``;
* ``max_automatic_messages``: los envios del camino. La v1 del motor manda uno
  solo (las capacidades de §2.10);
* ``purpose``: ``cart_recovery``, el unico que acepta la tabla
  (``20260803000100:23``);
* ``steps``: **un solo paso**, ``{mode: freeform, step_key}``, con el
  ``step_key`` de hoy de su ``legacy_kind``: las autorizaciones y los envios
  eligen el paso por ``step_key``.

La politica de ATT1 la comparten el carrito y el pago fallido, y tiene los dos
pasos (diferencia 9 de §1.7, clase C: dos flujos con los mismos valores). Por
eso ``differences_from_row`` no mira la clave ni la version, y pide el unico
paso compilado entre los pasos de la fila.

Lo que la v1 del motor no ejecuta (otro tipo de paso, mas de un envio, una
espera despues del envio, un flujo sin ``vence``) no se compila:
``PolicyCompileError`` con su codigo, nunca un valor inventado. Los codigos y
el orden en que se miran estan en ``compile_cases.json``.

Es puro: no lee el entorno, no hace red ni toca bases.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Any

from .flow_definition import is_flow_id, parse_duration, step_type
from .flow_schedule import DEFAULT_ZONE, ISO_WEEK_DAYS, franja_of, hour_text

POLICY_KEY_PREFIX = "flujo:"
PURPOSE = "cart_recovery"
STEP_MODE = "freeform"

# El paso de hoy de cada flujo del legado que usa followup_policy_versions
# (la politica de ATT1, att1_flow_config_20261016.json). El cupon y la
# reactivacion no tienen fila de politica.
STEP_KEY_BY_LEGACY_KIND: Mapping[str, str] = MappingProxyType({
    "carrito": "first_contact",
    "precheckout": "first_contact",
    "pago_fallido": "payment_failure_first_contact",
})

# Los campos que compara differences_from_row, en el orden en que los devuelve.
COMPARED_FIELDS = (
    "purpose", "timezone", "business_windows", "grace_period", "expires_after",
    "max_automatic_messages", "steps",
)

# Un intervalo como lo escribe Postgres con IntervalStyle = postgres: «1 day»,
# «2 days», «01:00:00», «1 day 02:00:00», con signo y fraccion. Meses y anios
# no son un timedelta y no se leen.
_INTERVAL = re.compile(
    r"(?:(?P<days>-?[0-9]+) days?)?"
    r"(?:(?(days) )(?P<sign>-?)(?P<hours>[0-9]+):(?P<minutes>[0-5][0-9]):(?P<seconds>[0-5][0-9])"
    r"(?:\.(?P<fraction>[0-9]{1,6}))?)?"
)


class PolicyCompileError(ValueError):
    """La definicion trae algo que la politica compilada no puede escribir.

    ``str(error)`` es el codigo (``^[a-z0-9_:]{1,80}$``), el mismo que da la
    gemela de SQL.
    """


@dataclass(frozen=True)
class CompiledPolicy:
    """La fila de ``followup_policy_versions`` de un flujo del motor, sin el estado ni la aprobacion."""

    policy_key: str                     # f"flujo:{flow_id}"
    version: int
    purpose: str                        # "cart_recovery"
    timezone: str
    business_windows: tuple[Mapping[str, Any], ...]
    grace_period: timedelta
    expires_after: timedelta
    max_automatic_messages: int
    steps: tuple[Mapping[str, str], ...]   # un solo paso

    def as_json(self) -> dict[str, Any]:
        """La forma de ``compile_cases.json``: la fila, con los intervalos en segundos."""
        return {
            "policy_key": self.policy_key,
            "version": self.version,
            "purpose": self.purpose,
            "timezone": self.timezone,
            "business_windows": [_plain(window) for window in self.business_windows],
            "grace_period_seconds": int(self.grace_period.total_seconds()),
            "expires_after_seconds": int(self.expires_after.total_seconds()),
            "max_automatic_messages": self.max_automatic_messages,
            "steps": [_plain(step) for step in self.steps],
        }


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _plain(value: object) -> object:
    """Un valor JSON con dict y list, para comparar como compara jsonb."""
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    return value


def _path(steps: object) -> tuple[int, int]:
    """La gracia en minutos y los envios del unico camino que ejecuta la v1."""
    if not isinstance(steps, list):
        raise PolicyCompileError("compile_policy_without_send")
    grace = 0
    sends = 0
    for step in steps:
        kind = step_type(step)
        if kind == "esperar":
            if sends:
                raise PolicyCompileError("compile_policy_step_after_send")
            minutes = parse_duration(step["esperar"])
            if minutes is None:
                raise PolicyCompileError("compile_policy_wait_invalid")
            grace += minutes
        elif kind == "plantilla":
            if sends:
                raise PolicyCompileError("compile_policy_more_than_one_send")
            sends += 1
        else:
            raise PolicyCompileError(f"compile_policy_step_not_supported:{kind or 'desconocido'}")
    if not sends:
        raise PolicyCompileError("compile_policy_without_send")
    return grace, sends


def compile_policy(definition: Mapping[str, Any], *, flow_id: str, version: int,
                   legacy_kind: str | None, fixed_zone: str | None = None) -> CompiledPolicy:
    """La fila de la politica de ``flujo:<flow_id>`` vN, desde su definicion.

    Lee solo ``horario``, ``pasos`` y ``termina.vence``: sirve igual con la
    definicion publicada y normalizada que con la que da el traductor.
    ``fixed_zone`` es la zona fija del flujo (``WorldFlow.fixed_zone``), la que
    toma un ``siempre``. Mira, en este orden, el ``flow_id``, la version, el
    ``legacy_kind``, el horario, los pasos y el vence; el primero que falla es
    el codigo del error.
    """
    if not is_flow_id(flow_id):
        raise PolicyCompileError("compile_policy_flow_id_invalid")
    if not _is_int(version) or version < 1:
        raise PolicyCompileError("compile_policy_version_invalid")
    step_key = STEP_KEY_BY_LEGACY_KIND.get(legacy_kind) if isinstance(legacy_kind, str) else None
    if step_key is None:
        raise PolicyCompileError("compile_policy_legacy_kind_without_step")
    if not isinstance(definition, Mapping):
        raise PolicyCompileError("compile_policy_horario_invalid")
    try:
        franja = franja_of(definition.get("horario"), fixed_zone=fixed_zone)
    except ValueError as error:
        raise PolicyCompileError("compile_policy_horario_invalid") from error
    grace, sends = _path(definition.get("pasos"))
    ends = definition.get("termina")
    deadline = parse_duration(ends.get("vence")) if isinstance(ends, Mapping) else None
    if deadline is None or deadline < 1:
        raise PolicyCompileError("compile_policy_vence_invalid")
    window = MappingProxyType({
        "days": ISO_WEEK_DAYS,
        "start": hour_text(franja.start_minute),
        "end": hour_text(franja.end_minute),
    })
    step = MappingProxyType({"mode": STEP_MODE, "step_key": step_key})
    return CompiledPolicy(
        policy_key=f"{POLICY_KEY_PREFIX}{flow_id}",
        version=version,
        purpose=PURPOSE,
        timezone=franja.zone if franja.zone is not None else DEFAULT_ZONE,
        business_windows=(window,),
        grace_period=timedelta(minutes=grace),
        expires_after=timedelta(minutes=deadline),
        max_automatic_messages=sends,
        steps=(step,),
    )


def interval_of(value: object) -> timedelta | None:
    """Un intervalo de la fila (texto de Postgres, o un timedelta), o None si no se lee."""
    if isinstance(value, timedelta):
        return value
    if not isinstance(value, str):
        return None
    match = _INTERVAL.fullmatch(value)
    if match is None or (match.group("days") is None and match.group("hours") is None):
        return None
    total = timedelta(days=int(match.group("days") or 0))
    if match.group("hours") is not None:
        clock = timedelta(
            hours=int(match.group("hours")), minutes=int(match.group("minutes")),
            seconds=int(match.group("seconds")),
            microseconds=int((match.group("fraction") or "0").ljust(6, "0")),
        )
        total += -clock if match.group("sign") else clock
    return total


def differences_from_row(compiled: CompiledPolicy, row: Mapping[str, Any]) -> tuple[str, ...]:
    """Los campos que difieren de una fila de hoy: sin la clave ni la version, y con el paso
    compilado entre los pasos de la fila (la politica compartida, diferencia 9).

    Un campo de la fila que no se puede leer (un intervalo con meses, un numero
    que no es entero) cuenta como distinto. Con mas o menos de un paso
    compilado, ``steps`` difiere siempre: la compilada tiene uno.
    """
    found: list[str] = []
    if row.get("purpose") != compiled.purpose:
        found.append("purpose")
    if row.get("timezone") != compiled.timezone:
        found.append("timezone")
    if _plain(row.get("business_windows")) != [_plain(window) for window in compiled.business_windows]:
        found.append("business_windows")
    if interval_of(row.get("grace_period")) != compiled.grace_period:
        found.append("grace_period")
    if interval_of(row.get("expires_after")) != compiled.expires_after:
        found.append("expires_after")
    maximum = row.get("max_automatic_messages")
    if not _is_int(maximum) or maximum != compiled.max_automatic_messages:
        found.append("max_automatic_messages")
    row_steps = row.get("steps")
    if not (
        len(compiled.steps) == 1
        and isinstance(row_steps, (list, tuple))
        and _plain(compiled.steps[0]) in [_plain(step) for step in row_steps]
    ):
        found.append("steps")
    return tuple(found)
