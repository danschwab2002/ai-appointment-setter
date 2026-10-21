"""La politica compilada de un flujo del motor: su fila de followup_policy_versions.

Diseno del motor de flujos, §2.5, con lo que cambio la T2.1 (2026-10-21,
«el esquema de varios toques»). Por la clave foranea, el sustrato exige una
fila de ``followup_policy_versions`` para cada caso y cada accion (§1.0). El
motor la compila de la definicion publicada, con la clave ``flujo:<flow_id>`` y
la version del flujo. Este modulo es la gemela en Python de
``_flow_engine_compile_policy`` (T2); las dos se atan con
``tests/fixtures/flow_engine/compile_cases.json``.

**La forma de cada flujo del legado** (``STEPS_BY_LEGACY_KIND``) es la lista de
sus pasos, en orden: el ``step_key``, el cupon personal que admite y las
variables que el bridge manda en ese paso. Las autorizaciones, los envios y el
bridge eligen el paso por ``step_key``, y el bridge compone cada paso con su
tupla fija de variables, asi que el motor no puede inventar otro: una
definicion con mas envios que esa lista, con el cupon personal en un paso que
no lo admite, con otras variables (u otro orden), o con dos envios sin al menos
un minuto entre ellos, no se compila (``flow_policy_not_compilable:<detalle>``).
Se pueden cambiar las esperas, el plazo, el horario y los topes; agregar o sacar
un toque, mover el cupon o cambiar sus variables es un cambio de codigo. Una
definicion con menos envios que la forma se compila con los primeros pasos (la
del 16/10, un toque).

**La plantilla de cada paso todavia no se cambia con una version.** La fila no
la lleva: el bridge manda la de su entorno (``RECOVERY_DISCOUNT_TEMPLATE_NAME``,
``RECOVERY_CART_SECOND_TOUCH_TEMPLATE_NAME``...) o la del manifiesto. Una
definicion con otra plantilla se compila a la misma fila, y el legado manda la
de siempre. La plantilla por paso de la version fijada es el principio 10 del
diseno y llega con la T2.4.

Los campos:

* ``timezone`` y ``business_windows``: la franja del horario
  (``flow_schedule.franja_of``), una sola ventana todos los dias; con
  ``siempre``, de 00:00 a 23:59 en la zona fija del flujo, o en UTC si nunca
  tuvo una;
* ``grace_period``: la suma de las esperas antes del primer envio, o 0. La
  primera espera se cuenta desde el disparo y cada una desde la anterior
  (flow-definition-v1.md, «Los pasos»);
* ``expires_after``: ``termina.vence``;
* ``max_automatic_messages``: los envios del camino;
* ``purpose``: ``cart_recovery``, el unico que acepta la tabla
  (``20260803000100:23``);
* ``steps``: un paso por envio, ``{mode: freeform, step_key}`` con el
  ``step_key`` de la forma. Cada paso despues del primero lleva ``delay``, la
  suma de las esperas desde el primer envio hasta el (la regla del legado: cada
  toque a la aceptacion del primero mas su ``delay``, ``20260813000100``), y
  ``approved_template: true``. El paso con un cupon personal de ordinal 2 o mas
  lleva ``coupon_ordinal``; el primero va sin marca, porque lo gobierna
  ``RECOVERY_DISCOUNT_FIRST_TOUCH_ENABLED`` (excepcion ``a1_cupon_por_flag``).
  Las marcas son las de ``20261019000600`` (``portable-cart-followups-v1.md``,
  «Como entra cada paso»).

Lo que la v1 del motor no ejecuta (otro tipo de paso, una espera despues del
ultimo envio, un flujo sin ``vence``) tampoco se compila: ``PolicyCompileError``
con su codigo, nunca un valor inventado. Los codigos y el orden en que se miran
estan en ``compile_cases.json``.

``differences_from_row`` compara con una fila de hoy: los pasos enteros y en
orden. Las filas del 16/10 que compartian el carrito y el pago fallido
(``att1-recuperacion-un-toque``, diferencia 9 de §1.7, clase C) se comparan con
``shared_row=True``: el unico paso compilado tiene que ser el primero de la
fila con su ``step_key``.

Es puro: no lee el entorno, no hace red ni toca bases.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Any

from .flow_definition import is_flow_id, is_personal_coupon, parse_duration, step_type
from .flow_schedule import DEFAULT_ZONE, ISO_WEEK_DAYS, franja_of, hour_text

POLICY_KEY_PREFIX = "flujo:"
PURPOSE = "cart_recovery"
STEP_MODE = "freeform"

NOT_COMPILABLE = "flow_policy_not_compilable"


@dataclass(frozen=True)
class LegacyStep:
    """Un paso de la forma de un flujo del legado: su ``step_key``, el cupon personal que admite y sus variables.

    ``coupon_ordinal`` es el ordinal del cupon personal de ese paso en el caso
    (``recovery_coupons.ordinal``), o None si el paso no lleva cupon personal.
    Con 1 el cupon es opcional (lo gobierna el flag del primer toque); con 2 o
    mas, el paso lo exige: el bridge cierra sin mensaje un paso con
    ``coupon_ordinal`` que no tiene su cupon (``portable-cart-followups-v1.md``,
    «Z»), asi que compilar ese paso sin el cupon de la definicion seria mentir.

    ``variables`` son las que el bridge manda en ese paso sin cupon personal, y
    ``coupon_variables`` las que manda con el cupon, en el orden {{1}}, {{2}}...:
    el bridge compone cada paso con su tupla fija, no con la de la definicion,
    asi que una definicion con otras (u otro orden) no se compila. None: el paso
    no se manda de esa forma. Las fija ``tests/test_flow_policy.py`` contra las
    constantes del bridge.
    """

    step_key: str
    coupon_ordinal: int | None = None
    variables: tuple[str, ...] | None = None
    coupon_variables: tuple[str, ...] | None = None


# La forma de cada flujo del legado que usa followup_policy_versions: sus pasos
# en orden (la politica de ATT1 en att1_flow_config_20261021.json). El cupon y
# la reactivacion no tienen fila de politica, y la ficha no se migra en la
# Etapa 1.
# * carrito: A1 (first_contact, cupon 1), A2 (cart_second_touch, sin cupon) y Z
#   (cart_last_touch, cupon 2): att1-carrito-tres-toques v1;
# * pago_fallido: B1 (payment_failure_first_contact; su cupon lo elige la
#   variante en el bridge, no la definicion) y el seguimiento
#   (payment_failure_followup, cupon 2): att1-pago-rechazado-dos-toques v1;
# * precheckout: el primer contacto, uno solo.
# Las variables, en el bridge:
# * el primer contacto de los tres, sin cupon: instance_manifest.DEFAULT_TEMPLATE_PARAMETERS
#   (src/bridge/instance_manifest.py:97), las del manifiesto por defecto, que usa ATT1;
# * A1 con el cupon: cart_first_touch.CART_DISCOUNT_VARIABLES (src/bridge/cart_first_touch.py:33);
# * A2: cart_followups.CART_SECOND_TOUCH_VARIABLES (src/bridge/cart_followups.py:67);
# * Z: cart_followups.CART_LAST_TOUCH_VARIABLES (:68);
# * el seguimiento del pago rechazado: payment_failure_followups.PAYMENT_FAILURE_FOLLOWUP_VARIABLES
#   (src/bridge/payment_failure_followups.py:65).
_FIRST_CONTACT_VARIABLES = ("nombre", "producto")
_A1_COUPON_VARIABLES = ("nombre", "producto", "cuando", "precio", "vence")
_A2_VARIABLES = ("nombre", "producto")
_LAST_TOUCH_VARIABLES = ("nombre", "producto", "vence")
STEPS_BY_LEGACY_KIND: Mapping[str, tuple[LegacyStep, ...]] = MappingProxyType({
    "carrito": (LegacyStep("first_contact", 1, _FIRST_CONTACT_VARIABLES, _A1_COUPON_VARIABLES),
                LegacyStep("cart_second_touch", None, _A2_VARIABLES),
                LegacyStep("cart_last_touch", 2, None, _LAST_TOUCH_VARIABLES)),
    "precheckout": (LegacyStep("first_contact", None, _FIRST_CONTACT_VARIABLES),),
    "pago_fallido": (LegacyStep("payment_failure_first_contact", None, _FIRST_CONTACT_VARIABLES),
                     LegacyStep("payment_failure_followup", 2, None, _LAST_TOUCH_VARIABLES)),
})
# El primer paso de cada forma: el que reevalua el arranque.
STEP_KEY_BY_LEGACY_KIND: Mapping[str, str] = MappingProxyType({
    kind: steps[0].step_key for kind, steps in STEPS_BY_LEGACY_KIND.items()})

# Los campos que compara differences_from_row, en el orden en que los devuelve.
# status no es de la compilada: la fila tiene que estar publicada, porque con
# otro estado la reevaluacion escala con policy_step_invalid
# (20260805000300_per_case_conversation_anchor.sql:339-341).
COMPARED_FIELDS = (
    "status", "purpose", "timezone", "business_windows", "grace_period", "expires_after",
    "max_automatic_messages", "steps",
)
ROW_STATUS = "published"

# Un intervalo como lo escribe Postgres con IntervalStyle = postgres: «1 day»,
# «2 days», «01:00:00», «1 day 02:00:00», con signo y fraccion. Meses y anios
# no son un timedelta y no se leen.
_INTERVAL = re.compile(
    r"(?:(?P<days>-?[0-9]+) days?)?"
    r"(?:(?(days) )(?P<sign>-?)(?P<hours>[0-9]+):(?P<minutes>[0-5][0-9]):(?P<seconds>[0-5][0-9])"
    r"(?:\.(?P<fraction>[0-9]{1,6}))?)?"
)

# El delay de un paso como lo escribe el legado (portable-cart-followups-v1.md: '24 hours').
_DELAY = re.compile(r"([0-9]{1,6}) (minute|hour|day)s?")
_DELAY_UNITS = {"minute": 1, "hour": 60, "day": 24 * 60}


class PolicyCompileError(ValueError):
    """La definicion trae algo que la politica compilada no puede escribir.

    ``str(error)`` es el codigo (``^[a-z0-9_:]{1,80}$``), el mismo que da la
    gemela de SQL. ``send_index`` es el envio (0, 1, 2...) que no entra en la
    forma, cuando el error es de un envio: el validador lo usa para la ruta.
    """

    def __init__(self, code: str, send_index: int | None = None) -> None:
        super().__init__(code)
        self.send_index = send_index


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
    steps: tuple[Mapping[str, Any], ...]   # uno por envio, en orden

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


@dataclass(frozen=True)
class _Send:
    minutes: int              # el paso 0: la gracia; los demas: desde el primer envio
    coupon_ordinal: int | None


def _delay_text(minutes: int) -> str:
    """El ``delay`` de un paso como lo escribe el legado (``'24 hours'``, ``'2 minutes'``)."""
    if minutes and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def _path(steps: object, shape: tuple[LegacyStep, ...]) -> list[_Send]:
    """Los envios del unico camino que ejecuta la v1, contra la forma de su flujo del legado."""
    if not isinstance(steps, list):
        raise PolicyCompileError("compile_policy_without_send")
    sends: list[_Send] = []
    waited = 0
    since_first = 0
    waited_after_send = False

    def refuse(detail: str) -> PolicyCompileError:
        return PolicyCompileError(f"{NOT_COMPILABLE}:{detail}", send_index=len(sends))

    for step in steps:
        kind = step_type(step)
        if kind == "esperar":
            minutes = parse_duration(step["esperar"])
            if minutes is None:
                raise PolicyCompileError("compile_policy_wait_invalid")
            if sends:
                since_first += minutes
            else:
                waited += minutes
            waited_after_send = bool(sends)
            continue
        if kind != "plantilla":
            raise PolicyCompileError(f"compile_policy_step_not_supported:{kind or 'desconocido'}")
        waited_after_send = False
        if len(sends) >= len(shape):
            raise refuse("more_sends_than_steps")
        slot = shape[len(sends)]
        if sends and since_first <= (sends[-1].minutes if len(sends) > 1 else 0):
            # Entre dos envios, al menos un minuto: el delay crece, y el traductor lo vuelve a leer.
            raise refuse(f"espera_entre_envios:{slot.step_key}")
        button = step.get("boton")
        coupon = button.get("cupon") if isinstance(button, Mapping) else None
        personal = is_personal_coupon(coupon)
        if personal:
            if slot.coupon_ordinal is None:
                raise refuse(f"coupon_not_allowed:{slot.step_key}")
        elif coupon is not None:
            # Un codigo fijo (o uno sin resolver): el legado no manda un cupon fijo en un paso.
            raise refuse(f"fixed_coupon:{slot.step_key}")
        elif slot.coupon_ordinal is not None and slot.coupon_ordinal > 1:
            raise refuse(f"coupon_missing:{slot.step_key}")
        variables = step.get("variables")
        wanted = slot.coupon_variables if personal else slot.variables
        if not isinstance(variables, list) or tuple(variables) != wanted:
            raise refuse(f"variables_differ:{slot.step_key}")
        sends.append(_Send(minutes=since_first if sends else waited,
                           coupon_ordinal=slot.coupon_ordinal if is_personal_coupon(coupon) else None))
    if not sends:
        raise PolicyCompileError("compile_policy_without_send")
    if waited_after_send:
        raise PolicyCompileError("compile_policy_step_after_send")
    return sends


def _compiled_steps(sends: list[_Send], shape: tuple[LegacyStep, ...]) -> tuple[Mapping[str, Any], ...]:
    out: list[Mapping[str, Any]] = []
    for index, send in enumerate(sends):
        step: dict[str, Any] = {"mode": STEP_MODE, "step_key": shape[index].step_key}
        if index:
            step["delay"] = _delay_text(send.minutes)
            step["approved_template"] = True
            if send.coupon_ordinal is not None and send.coupon_ordinal > 1:
                step["coupon_ordinal"] = send.coupon_ordinal
        out.append(MappingProxyType(step))
    return tuple(out)


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
    shape = STEPS_BY_LEGACY_KIND.get(legacy_kind) if isinstance(legacy_kind, str) else None
    if shape is None:
        raise PolicyCompileError("compile_policy_legacy_kind_without_step")
    if not isinstance(definition, Mapping):
        raise PolicyCompileError("compile_policy_horario_invalid")
    try:
        franja = franja_of(definition.get("horario"), fixed_zone=fixed_zone)
    except ValueError as error:
        raise PolicyCompileError("compile_policy_horario_invalid") from error
    sends = _path(definition.get("pasos"), shape)
    ends = definition.get("termina")
    deadline = parse_duration(ends.get("vence")) if isinstance(ends, Mapping) else None
    if deadline is None or deadline < 1:
        raise PolicyCompileError("compile_policy_vence_invalid")
    window = MappingProxyType({
        "days": ISO_WEEK_DAYS,
        "start": hour_text(franja.start_minute),
        "end": hour_text(franja.end_minute),
    })
    return CompiledPolicy(
        policy_key=f"{POLICY_KEY_PREFIX}{flow_id}",
        version=version,
        purpose=PURPOSE,
        timezone=franja.zone if franja.zone is not None else DEFAULT_ZONE,
        business_windows=(window,),
        grace_period=timedelta(minutes=sends[0].minutes),
        expires_after=timedelta(minutes=deadline),
        max_automatic_messages=len(sends),
        steps=_compiled_steps(sends, shape),
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


def _step_delay(value: object) -> timedelta | None:
    """El ``delay`` de un paso: el texto del legado (``'24 hours'``) o un intervalo de Postgres."""
    if isinstance(value, str):
        match = _DELAY.fullmatch(value)
        if match is not None:
            return timedelta(minutes=int(match.group(1)) * _DELAY_UNITS[match.group(2)])
    return interval_of(value)


def _same_step(row_step: object, compiled: Mapping[str, Any]) -> bool:
    """Un paso de la fila igual al compilado: las mismas claves, y el ``delay`` como intervalo."""
    if not isinstance(row_step, Mapping) or set(row_step) != set(compiled):
        return False
    for key, value in compiled.items():
        if key == "delay":
            if _step_delay(row_step[key]) is None or _step_delay(row_step[key]) != _step_delay(value):
                return False
        elif _plain(row_step[key]) != _plain(value) or isinstance(row_step[key], bool) != isinstance(value, bool):
            return False
    return True


def differences_from_row(compiled: CompiledPolicy, row: Mapping[str, Any], *,
                         shared_row: bool = False) -> tuple[str, ...]:
    """Los campos que difieren de una fila de hoy, sin la clave ni la version.

    Un campo de la fila que no se puede leer (un intervalo con meses, un numero
    que no es entero) cuenta como distinto. Mira lo que mira la base, no la forma:

    * ``status``: la fila tiene que estar ``published``;
    * ``steps``: los pasos enteros y en orden, cada uno con las mismas claves y
      los mismos valores; el ``delay`` se compara como intervalo
      (``'24 hours'`` es ``'1 day'``). Con ``shared_row`` (la fila del 16/10 que
      compartian el carrito y el pago fallido, diferencia 9), el compilado
      tiene que tener un solo paso, y ser el que usaria la reevaluacion: el
      **primero** de la fila con ese ``step_key`` (``select ... where step_key
      = ... limit 1``, ``20260805000300_per_case_conversation_anchor.sql:214-217``;
      la tabla no impide dos pasos con la misma clave);
    * los intervalos se comparan como ``timedelta``, asi que ``'24:00:00'`` y
      ``'1 day'`` son iguales. En la base lo son para el vencimiento solo con la
      ``TimeZone`` de la sesion en UTC (la de Supabase): con otra,
      ``timestamptz + '1 day'`` sigue el dia civil y da 23 o 25 horas en un
      cambio de horario. La gemela de T2 lo tiene que comparar igual.
    """
    found: list[str] = []
    if row.get("status") != ROW_STATUS:
        found.append("status")
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
    if shared_row:
        same = _step_in_use(row_steps, compiled) is not None
    else:
        same = isinstance(row_steps, (list, tuple)) and len(row_steps) == len(compiled.steps) and all(
            _same_step(row_step, step) for row_step, step in zip(row_steps, compiled.steps))
    if not same:
        found.append("steps")
    return tuple(found)


def _step_in_use(row_steps: object, compiled: CompiledPolicy) -> Mapping[str, Any] | None:
    """El paso de la fila que usaria la base para el compilado, si es igual al compilado; si no, None."""
    if len(compiled.steps) != 1 or not isinstance(row_steps, (list, tuple)):
        return None
    wanted = _plain(compiled.steps[0])
    assert isinstance(wanted, dict)
    for step in row_steps:
        if isinstance(step, Mapping) and step.get("step_key") == wanted["step_key"]:
            return step if _plain(step) == wanted else None
    return None
