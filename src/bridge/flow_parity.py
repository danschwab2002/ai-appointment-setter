"""La paridad del motor sobre la captura de ATT1: la hora de salida (P3) y quien
le gana a quien (P6), con el catalogo de excepciones y las diferencias con
nombre.

Diseno del motor de flujos (2026-10-16), §3.1, §3.2, §5.2 y §6.5. La captura
es la salida de ``captura-paridad-recuperacion-v2`` (o su fixture recortado,
``flow_capture.build_fixture``); ``Capture.from_json`` la valida con la forma
de ``flow_capture``, le devuelve las constantes y recuenta las secciones contra
``_captured.conteos``.

**P3, la hora de salida.** Para cada envio de un disparo desde «desde», E es
el primer instante en que la puerta dice «mandar» con los hechos observados y
R es el ``request_started_at`` observado. Nunca antes (R >= E - 1 s, siempre:
el segundo es el reloj de Hotmart contra el de la base, porque el ancla es el
``creation_date`` del evento) y a lo sumo ``TOLERANCE`` despues, salvo un hecho
observado que lo explique: un intento anterior que fallo antes del pedido, un
incierto o un rechazo de Meta, o un arranque rechazado (un intento que quedo
``reserved`` sin resultado). El hecho explica solo su propia demora: R tiene que
quedar a lo sumo ``TOLERANCE`` despues del reintento que pidio (o, con un
arranque rechazado, del vencimiento de su lease, ``LEASE``). La puerta que
modela ``eligible_at`` es la del motor, no la del legado:

* la franja y la gracia, de la politica del caso
  (``flow_schedule.franja_of_policy``);
* el control armado, segun los eventos de control con ``to_state`` (la
  activacion de una version deja ``inactive``). **La version activa no frena:**
  lo en curso sigue con su version (decision 3, §1.7 fila 10, clase C). Donde el
  legado rechaza por ``pilot_scope_version_mismatch`` y el motor manda, queda
  una diferencia, y la explica una diferencia con nombre;
* la bolsa: los topes de la version del recorrido (la del binding del caso,
  §2.6), sobre las autorizaciones **observadas** de la bolsa antes del
  instante, todas las versiones juntas, y el dia local de la zona del scope
  (§6.5);
* antes de ``expires_at``.

**P6, quien le gana a quien.** Cada primer contacto cancelado o no planificado
con ``superseded_by_provider_event`` tiene antes un disparo de carrito o de
pago fallido que le gana, y cada disparo que le gana a un primer contacto que
esperaba corresponde a uno cancelado. Le gana como lo mira hoy
``_portable_precheckout_stop_reason`` (``20261001000200:293-313``) y como lo
escribe ``le_gana_a`` (flow-definition-v1.md, «Como conviven los flujos»):

* **por persona**, si el disparo tiene un caso del mismo contacto y producto en
  curso en ese momento (``recovery_cases`` en ``grace_period``, ``active`` o
  ``paused``);
* **por intencion**, si la correlacion del disparo resolvio la misma intencion
  de compra, aunque haya llegado desde otro telefono o sin telefono (la
  intencion queda clasificada, ``:297-299``).

Se prueba primero la persona y despues la intencion (``MATCH_ORDER``). El
reparto por ruta que no depende de ese orden (las dos, solo persona, solo
intencion) va en ``ParityReport.supersessions_by_routes``.

**Llego al flujo** (§5.2): el disparo tiene caso, o la planificacion lo rechazo
(``pilot_scope_rejected:*``, ``pilot_case_binding_conflict``,
``payment_failure_correlation_unresolved``). ``create_recovery_case_failed`` es
del adaptador, salvo cuando la persona del disparo tenia un caso del producto en
curso: el plan intento abrir otra secuencia en ese caso y la base contesto
23505 (G14, la 5b de §1.7). Ese llego, frenado, y sale como
``create_recovery_case_failed:caso_en_curso``.

**Las diferencias.** Lo que la paridad observa fuera de P3 y P6 es una
diferencia, con su disparo (el uuid interno del evento; en P6, el del plan).
Las acepta solo una diferencia con nombre de
``tests/fixtures/flow_engine/att1_parity_named_differences.json``, con la clase
de §1.7 y el caso generado que la reproduce (G1 a G25) o la cita de un hecho
del legado (``archivo:linea``). Lo demas queda sin explicar, y el reporte no
esta en verde. Un envio antes de E no se acepta nunca.

La captura tiene una accion por caso (el carrito y el pago fallido de hoy
mandan uno); con mas, ``Capture`` falla en voz alta en vez de elegir una.

**La v3 (T2.2).** ``CaptureV3`` lee la captura v3, la del carrito de tres
toques (A1, A2 y Z), y ``parity_report_v3`` mide P3 por paso, los finales de
cada paso y P6: ver la seccion del final del modulo. Lo de arriba, la v2, no
cambia.

Es puro: no lee el entorno, no hace red ni toca bases.
"""

from __future__ import annotations

import math
import re
import statistics
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any

from .flow_capture import (
    CART_SCOPE_KEY,
    FIRING_EVENT_TYPES,
    FORM_V2,
    FORM_V3,
    QUERY_NAME,
    CaptureFixtureError,
    CaptureForm,
    count_sections,
    firings,
    is_fixture,
    parse_instant,
    restore_fixture,
    validate_shape,
)
from .flow_engine import legacy_motivo
from .flow_policy import interval_of
from .flow_schedule import LAST_MINUTE, Franja, franja_of_policy, is_open, local_day, next_open

__all__ = [
    "DIFFERENCE_KINDS", "EXCEPTIONS_V1", "LEASE", "MATCH_ORDER", "NAMED_CLASSES", "NEVER_BEFORE_SLACK",
    "TOLERANCE", "Capture", "CaptureError", "FlowException", "ParityReport", "SendTiming",
    "Supersession", "control_state_at", "eligible_at", "parity_report", "send_timings",
    "supersessions", "winners_without_cancel",
    # La T2.2: la paridad sobre la captura v3, el carrito de tres toques.
    "DIFFERENCE_KINDS_V3", "COUPON_WAIT_BY_STEP", "CaptureV3", "ParityReportV3", "StepTiming",
    "parity_report_v3", "step_finals", "step_timings",
]

# R - E <= 5 minutos (P#2): un lease del despachador (worker.py:739). Con 15, un
# error de hasta 12 pasaba sin verse.
TOLERANCE = timedelta(minutes=5)
# El lease del despachador (worker.py:739). Una accion con un arranque rechazado
# se vuelve a tomar recien cuando vence su lease (claim_due_followup_actions,
# 20260803000100_followup_engine_v1.sql:2614).
LEASE = timedelta(minutes=5)
# Nunca antes: R >= E - 1 s. El ancla es el reloj de Hotmart y R el de la base.
NEVER_BEFORE_SLACK = timedelta(seconds=1)
# El orden en que P6 busca al ganador.
MATCH_ORDER = ("persona", "intencion")

# Los disparos y su flujo de hoy (legacy_kind).
FIRING_KIND: Mapping[str, str] = MappingProxyType({
    "PURCHASE_OUT_OF_SHOPPING_CART": "carrito",
    "PURCHASE_CANCELED": "pago_fallido",
})
# «Llego al flujo» (§5.2): el evento tiene caso, o un processing_error de la
# planificacion. create_recovery_case_failed es del adaptador, salvo con un caso
# de la persona en curso (G14): ver el docstring.
PLANNING_REJECTION_PREFIX = "pilot_scope_rejected:"
PLANNING_ERRORS = frozenset({"pilot_case_binding_conflict", "payment_failure_correlation_unresolved"})
CASE_FAILED = "create_recovery_case_failed"
CASE_FAILED_RUNNING = f"{CASE_FAILED}:caso_en_curso"
# El evento del primer contacto, para saber de que producto es (§5.2).
PRECHECKOUT_EVENT_TYPE = "PRECHECKOUT_FORM_SUBMITTED"
SUPERSEDED = "superseded_by_provider_event"
ACTIVATION = "pilot_scope_version_activated"
ARMED = "armed"
INACTIVE = "inactive"
# Los estados de una accion que todavia espera (el barrido de vencidas mira
# estos, 20260803000100:2558-2563) y los de un caso en curso (:475-485).
OPEN_ACTION_STATUSES = frozenset({"pending", "deferred", "retryable_failed"})
OPEN_CASE_STATUSES = frozenset({"grace_period", "active", "paused"})
# Los hechos observados que explican pasar la tolerancia: un intento anterior
# que termino asi (los resultados de followup_delivery_attempts,
# 20260803000100:310-313). El reintento a un minuto es worker.py:959.
EXPLAINING_OUTCOMES: Mapping[str, str] = MappingProxyType({
    "failed_before_request": "reintento_tras_fallo_antes_del_pedido",
    "rejected": "rechazo_de_meta",
    "delivery_unknown": "envio_incierto",
})
# Un intento reservado sin resultado: el arranque se rechazo (el control, la
# bolsa, la version), como los 720 del 13/10. La fase es la de la tabla
# (20260803000100:308-309).
RESERVED_PHASE = "reserved"
REJECTED_START = "arranque_rechazado"
# Las rutas de P6, en el reparto que no depende del orden.
ROUTE_GROUPS = ("las_dos", "solo_persona", "solo_intencion")

# Las diferencias que la paridad observa. Todas salvo la primera se aceptan con
# una diferencia con nombre.
NEVER_BEFORE = "envio_antes_de_E"
DIFFERENCE_KINDS = (
    NEVER_BEFORE,
    "envio_fuera_de_tolerancia",
    "envio_sin_E",
    "elegible_sin_envio",
    "cesion_sin_ganador",
    "ganador_sin_cancelacion",
    "persona_no_coincide",
)
# Las clases de §1.7 que puede tener una diferencia con nombre. La «—» va a Dan
# o a un cabo aparte, no a la paridad.
NAMED_CLASSES = ("E", "D", "C", "M")
NAMED_KEYS = frozenset({"disparo", "diferencia", "clase", "nombre", "caso", "cita", "descripcion"})
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_NAME = re.compile(r"[a-z][a-z0-9_]{2,59}")
# Los 25 casos que la cadena genera (§3.2, legacy_recovery_cases.json).
_GENERATED_CASE = re.compile(r"G(?:[1-9]|1[0-9]|2[0-5])")
# archivo:linea o archivo:inicio-fin, con la ruta desde la raiz del repo.
_CITATION = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*\.[a-z]{1,5}:[1-9][0-9]*(?:-[1-9][0-9]*)?")
_MAX_STEPS = 10_000


class CaptureError(ValueError):
    """La captura no se puede usar para la paridad: el mensaje dice donde, nunca el valor."""


# ---------------------------------------------------------------------------
# El catalogo de excepciones (§2.8).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FlowException:
    """Una «E» de §1.7: lo que el legado hace distinto del esquema y el motor conserva."""

    name: str                       # ^[a-z][a-z0-9_]{2,59}$, unico
    legacy_kinds: frozenset[str]    # a que flujos de hoy se aplica
    layer: str                      # "plan" | "puerta" | "despacho" | "sql"
    cita: str                       # archivo:linea del comportamiento de hoy
    descripcion: str
    # Los pasos (step_key) por los que la aplica el codigo de hoy: el bridge elige por el paso y la
    # base por su coupon_ordinal. Con pasos, la tiene solo un flujo de esos legacy_kinds cuyo plan
    # tiene alguno de esos pasos, aunque sea de capa sql o despacho: un carrito de un toque no
    # hereda las de Z. Las de los varios toques (T2.1).
    step_keys: frozenset[str] = frozenset()

    @property
    def by_step(self) -> bool:
        return bool(self.step_keys)


_CARRITO = frozenset({"carrito"})
_CARRITO_PAGO = frozenset({"carrito", "pago_fallido"})
_CON_PRIMER_CONTACTO = frozenset({"carrito", "pago_fallido", "precheckout"})

# Las «E» de §1.7 del carrito y el pago fallido, en ese orden: la 1, 2, 2b, 3,
# 4, 4b, 4c, 5, 5b y 6. Las de capa sql y despacho las aplica codigo que el
# motor reusa, asi que las hereda todo flujo del motor (§2.8). Las citas son de
# 7c0a61b.
#
# Despues, las de los varios toques (T2.1, 2026-10-21; las citas son de b163669):
# lo que hacen hoy A1, A2 y Z del carrito y el seguimiento del pago rechazado y
# que el esquema no escribe. Las de la ficha y las variantes de B1 no entran: no
# se migran en la Etapa 1. Son de capa sql o despacho, pero el codigo que las
# aplica elige por el paso (el bridge por el step_key del carrito o del pago
# rechazado, la base por coupon_ordinal), asi que llevan sus step_keys: las tiene
# solo un flujo de sus legacy_kinds cuyo plan tiene alguno de esos pasos.
EXCEPTIONS_V1: tuple[FlowException, ...] = (
    FlowException(
        "toma_humana_en_su_conversacion", _CARRITO_PAGO, "sql",
        "supabase/migrations/20260805000300_per_case_conversation_anchor.sql:297-306",
        "La toma humana mira solo la conversacion del caso, que no existe antes del envio.",
    ),
    FlowException(
        "caso_hotmart_pausado_frena", _CARRITO_PAGO, "sql",
        "supabase/migrations/20260803000100_followup_engine_v1.sql:475-485",
        "Un caso de Hotmart pausado sigue abierto y frena los carritos y los pagos de la misma"
        " oferta, y al primer contacto. Hoy se llega por la compra ambigua (2b).",
    ),
    FlowException(
        "compra_ambigua_pausa", _CON_PRIMER_CONTACTO, "sql",
        "supabase/migrations/20260808000400_hotmart_purchase_safety_fences.sql:57-307",
        "La compra ambigua cancela la accion con purchase_correlation_ambiguous y pausa el caso.",
    ),
    FlowException(
        "sin_horario_escala", _CON_PRIMER_CONTACTO, "sql",
        "supabase/migrations/20260805000300_per_case_conversation_anchor.sql:348-351",
        "Sin horario antes de vencer, se escala, no se termina.",
    ),
    FlowException(
        "intencion_viva", _CARRITO_PAGO, "sql",
        "supabase/migrations/20260930000300_pilot_scope_audience_mode.sql:597-610",
        "Si la intencion dejo de estar viva, el arranque se rechaza y se reintenta hasta vencer.",
    ),
    FlowException(
        "compra_conocida_antes_del_plan", _CON_PRIMER_CONTACTO, "sql",
        "supabase/migrations/20260808000200_hotmart_purchase_ordering_guard.sql:9-266",
        "La compra conocida antes del plan gana el caso al nacer: mira la oferta, la hora y un"
        " unico contacto por el mail o por los digitos tal cual.",
    ),
    FlowException(
        "conflicto_de_compra_frena_todo", _CON_PRIMER_CONTACTO, "sql",
        "supabase/migrations/20260808000500_hotmart_purchase_semantic_conflicts.sql:302-335",
        "Un conflicto semantico de compra sin resolver frena todo arranque.",
    ),
    FlowException(
        "nombre_inseguro_cierra", _CON_PRIMER_CONTACTO, "despacho",
        "src/bridge/worker.py:1484-1500",
        "El nombre que no pasa el filtro cierra la accion sin reintento (D1).",
    ),
    FlowException(
        "fallo_permanente_deja_el_caso_abierto", _CON_PRIMER_CONTACTO, "sql",
        "supabase/migrations/20260813000100_absolute_followup_deadlines.sql:454-488",
        "Un fallo que no se reintenta deja la accion permanent_failed (o expired con"
        " retry_beyond_expiration) y el caso abierto para siempre.",
    ),
    FlowException(
        "persona_es_el_contacto", _CON_PRIMER_CONTACTO, "sql",
        "supabase/migrations/20260803000100_followup_engine_v1.sql:475-485",
        "La persona es el contacto, no las dos formas del movil.",
    ),
    FlowException(
        "a1_espera_al_cupon", _CARRITO, "despacho",
        "src/bridge/cart_first_touch.py:130-139",
        "A1 espera unos minutos a que el cupon 1 este valid; si no llega a tiempo, o murio, sale la"
        " plantilla de hoy, sin descuento (la del manifiesto). A1 no se pierde por el cupon.",
        step_keys=frozenset({"first_contact"}),
    ),
    FlowException(
        "a1_cupon_por_flag", _CARRITO, "despacho",
        "src/bridge/app.py:5049-5050",
        "Que A1 lleve el cupon personal lo decide RECOVERY_DISCOUNT_FIRST_TOUCH_ENABLED, no la"
        " politica: el primer paso no tiene coupon_ordinal.",
        step_keys=frozenset({"first_contact"}),
    ),
    FlowException(
        "toque_con_cupon_cierra_sin_cupon", _CARRITO_PAGO, "despacho",
        "src/bridge/cart_followups.py:215-230",
        "Un toque siguiente con cupon (Z del carrito, y B2 o Z del pago rechazado, que la reusan) espera"
        " al cupon 2 y, si no esta valid a tiempo o murio, cierra sin mensaje: no hay toque sin el"
        " descuento que promete.",
        step_keys=frozenset({"cart_last_touch", "payment_failure_followup"}),
    ),
    FlowException(
        "renovacion_adelanta_el_ultimo_toque", _CARRITO, "sql",
        "supabase/migrations/20261019000600_recovery_followup_steps_v1.sql:214-217",
        "La respuesta «Quiero mi 10 %» a A2 adelanta Z: el paso pendiente con cupon de esa"
        " conversacion pasa a vencer ya, en vez de esperar su delay.",
        step_keys=frozenset({"cart_last_touch"}),
    ),
    FlowException(
        "renovacion_sobrevive_a_la_respuesta", _CARRITO, "sql",
        "supabase/migrations/20261019000600_recovery_followup_steps_v1.sql:243-248",
        "El paso que adelanto la renovacion no se cancela por la respuesta que la pidio"
        " (prospect_replied); la compra, la baja y la derivacion lo cortan igual.",
        step_keys=frozenset({"cart_last_touch"}),
    ),
    FlowException(
        "boton_con_off_discount", _CARRITO_PAGO, "despacho",
        "src/bridge/cart_followups.py:277-279",
        "El boton de un toque con cupon es nuestro link emitido mas &offDiscount=<codigo>: el cupon"
        " va en el boton, no en la definicion.",
        step_keys=frozenset({"first_contact", "cart_last_touch", "payment_failure_followup"}),
    ),
)


# ---------------------------------------------------------------------------
# La captura.
# ---------------------------------------------------------------------------

def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    # Las funciones de flow_schedule leen la fila como la da JSON: dict y list.
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _need(row: Mapping[str, Any], key: str, where: str) -> Any:
    value = row.get(key)
    if value is None:
        raise CaptureError(f"{where}.{key}: falta")
    return value


def _instant(row: Mapping[str, Any], key: str, where: str) -> datetime:
    try:
        return parse_instant(_need(row, key, where))
    except CaptureFixtureError as error:
        raise CaptureError(f"{where}.{key}: no es una hora de la captura") from error


def _optional_instant(row: Mapping[str, Any], key: str, where: str) -> datetime | None:
    return None if row.get(key) is None else _instant(row, key, where)


def _require_aware(at: datetime) -> None:
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("instant_without_zone")


@dataclass(frozen=True)
class _ControlStep:
    at: datetime
    generation: int
    version: int
    state: str


@dataclass(frozen=True)
class Capture:
    """La captura de la paridad, leida y congelada: cada fila es un ``Mapping`` que no se escribe.

    Se arma con ``from_json``. Los indices (por id, la linea del control por
    scope, las autorizaciones por bolsa y dia) los arma ``__post_init__``.
    """

    meta: Mapping[str, Any]             # _captured
    config: Mapping[str, Any]
    events: tuple[Mapping[str, Any], ...]
    cases: tuple[Mapping[str, Any], ...]
    actions: tuple[Mapping[str, Any], ...]
    authorizations: tuple[Mapping[str, Any], ...]
    reevaluations: tuple[Mapping[str, Any], ...]   # en tramos
    first_contacts: tuple[Mapping[str, Any], ...]
    intents: tuple[Mapping[str, Any], ...]
    contacts: tuple[Mapping[str, Any], ...]
    purchase_conflicts: tuple[Mapping[str, Any], ...]
    _index: Mapping[str, Any] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_index", MappingProxyType(_build_index(self)))

    # La forma de la captura que lee: la v2. ``CaptureV3`` lee la v3.
    FORM = FORM_V2

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Capture:
        """Valida la forma (la de ``flow_capture``) y los conteos, y devuelve la captura congelada.

        Sirve con la captura completa y con el fixture recortado: al fixture le
        devuelve las constantes (``restore_fixture``), y sus dos secciones
        recortadas se recuentan contra ``_captured.fixture.recortes``.
        """
        restored = cls._restored(data)
        return cls(**cls._fields_of(restored))

    @classmethod
    def _restored(cls, data: Mapping[str, Any]) -> dict[str, Any]:
        """Valida la forma de ``cls.FORM``, devuelve las constantes y recuenta contra los conteos."""
        form: CaptureForm = cls.FORM
        try:
            validate_shape(data, form)
            restored = restore_fixture(data)
        except CaptureFixtureError as error:
            raise CaptureError(str(error)) from error
        captured = restored["_captured"]
        if captured.get("query") != form.query:
            raise CaptureError(f"_captured.query: se esperaba {form.query}")
        try:
            counted = count_sections(restored, form)
        except CaptureFixtureError as error:
            raise CaptureError(str(error)) from error
        expected = dict(captured["conteos"])
        if is_fixture(restored):
            for section, trim in (captured["fixture"].get("recortes") or {}).items():
                if section in expected and trim.get("fixture") is not None:
                    expected[section] = trim["fixture"]
        for name, value in counted.items():
            if expected.get(name) != value:
                raise CaptureError(
                    f"_captured.conteos.{name}: la captura dice {expected.get(name)} y las filas dan {value}")
        return restored

    @staticmethod
    def _fields_of(restored: Mapping[str, Any]) -> dict[str, Any]:
        captured = restored["_captured"]
        return dict(
            meta=_freeze(captured),
            config=_freeze(restored["config"]),
            events=_freeze(restored["eventos"]),
            cases=_freeze(restored["casos"]),
            actions=_freeze(restored["acciones"]),
            authorizations=_freeze(restored["autorizaciones"]),
            reevaluations=_freeze(restored["reevaluaciones"]),
            first_contacts=_freeze(restored["primer_contacto"]),
            intents=_freeze(restored["intenciones"]),
            contacts=_freeze(restored["contactos"]),
            purchase_conflicts=_freeze(restored["conflictos_de_compra"]),
        )

    @property
    def desde(self) -> datetime:
        """El comienzo de la ventana: la activacion de la v3 del carrito (§3.2)."""
        return self._index["desde"]

    @property
    def captured_at(self) -> datetime | None:
        return self._index["captured_at"]

    def firings(self) -> Iterator[Mapping[str, Any]]:
        """Los disparos desde «desde», en el orden de la captura."""
        return iter(self._index["firings"])

    def case_of_event(self, event_id: str) -> Mapping[str, Any] | None:
        """El caso del evento: el que ancla o al que se sumo como evidencia."""
        return self._index["case_by_event"].get(event_id)


def _build_index(capture: Capture, *, form: CaptureForm = FORM_V2,
                 one_action_per_case: bool = True) -> dict[str, Any]:
    meta = capture.meta
    index: dict[str, Any] = {
        "desde": _instant(meta, "desde", "_captured"),
        "captured_at": _optional_instant(meta, "captured_at", "_captured"),
    }
    index["events"] = {_need(row, "id", f"eventos[{i}]"): row for i, row in enumerate(capture.events)}
    index["firings"] = tuple(firings({"_captured": meta, "eventos": capture.events}, form))
    index["cases"] = {_need(row, "id", f"casos[{i}]"): row for i, row in enumerate(capture.cases)}
    case_by_event: dict[str, Mapping[str, Any]] = {}
    for i, case in enumerate(capture.cases):
        anchor = _need(case, "abandonment_event_id", f"casos[{i}]")
        case_by_event[anchor] = case
        for item in case.get("eventos") or ():
            case_by_event.setdefault(_need(item, "webhook_event_id", f"casos[{i}].eventos"), case)
    index["case_by_event"] = case_by_event
    action_by_case: dict[str, Mapping[str, Any]] = {}
    for i, row in enumerate(capture.actions):
        case_id = _need(row, "recovery_case_id", f"acciones[{i}]")
        if not one_action_per_case:
            # La v3: un caso del carrito tiene un paso por accion (A1, A2, Z).
            continue
        if case_id in action_by_case:
            # P3 mira la accion del disparo: con dos (un segundo paso, el cupon,
            # la reactivacion) no se elige una en silencio.
            raise CaptureError(f"acciones[{i}]: su caso ya tiene otra accion, y la paridad mira una por caso")
        action_by_case[case_id] = row
    index["action_by_case"] = action_by_case
    index["contacts"] = {_need(row, "id", f"contactos[{i}]"): row for i, row in enumerate(capture.contacts)}
    contacts_by_person: dict[str, set[str]] = {}
    for contact_id, contact in index["contacts"].items():
        for key in contact.get("person_keys") or ():
            contacts_by_person.setdefault(key, set()).add(contact_id)
    index["contacts_by_person"] = {key: frozenset(value) for key, value in contacts_by_person.items()}
    cases_by_contact: dict[str, list[Mapping[str, Any]]] = {}
    for case in capture.cases:
        if case.get("contact_id") is not None:
            cases_by_contact.setdefault(case["contact_id"], []).append(case)
    index["cases_by_contact"] = {key: tuple(value) for key, value in cases_by_contact.items()}
    index["scopes"] = {
        (row["scope_key"], row["version"]): row for row in capture.config["pilot_scope_versions"]
    }
    index["policies"] = {
        (row["policy_key"], row["version"]): row for row in capture.config["followup_policy_versions"]
    }
    control: dict[str, list[_ControlStep]] = {}
    for i, event in enumerate(capture.config["pilot_control_events_control_level"]):
        where = f"config.pilot_control_events_control_level[{i}]"
        if event.get("event_type") == ACTIVATION:
            state = INACTIVE
        elif event.get("to_state") is not None:
            state = event["to_state"]
        else:
            # Un evento de control sin to_state que no es una activacion: no se
            # sabe que estado deja, y no se adivina.
            raise CaptureError(f"{where}.to_state: falta, y no es una activacion")
        control.setdefault(_need(event, "scope_key", where), []).append(_ControlStep(
            _instant(event, "created_at", where), _need(event, "runtime_generation", where),
            _need(event, "scope_version", where), state,
        ))
    index["control"] = {
        scope: tuple(sorted(steps, key=lambda step: (step.at, step.generation)))
        for scope, steps in control.items()
    }
    by_bolsa: dict[str, list[datetime]] = {}
    by_day: dict[tuple[str, str], list[datetime]] = {}
    for i, row in enumerate(capture.authorizations):
        where = f"autorizaciones[{i}]"
        bolsa = _need(row, "scope_key", where)
        at = _instant(row, "authorized_at", where)
        by_bolsa.setdefault(bolsa, []).append(at)
        by_day.setdefault((bolsa, _need(row, "local_budget_date", where)), []).append(at)
    index["authorized"] = {key: sorted(value) for key, value in by_bolsa.items()}
    index["authorized_by_day"] = {key: sorted(value) for key, value in by_day.items()}
    return index


# ---------------------------------------------------------------------------
# El control y la puerta.
# ---------------------------------------------------------------------------

def _control_steps(capture: Capture, scope_key: str) -> tuple[_ControlStep, ...]:
    steps = capture._index["control"].get(scope_key)
    if not steps:
        raise CaptureError(f"config.pilot_control_events_control_level: sin eventos de {scope_key}")
    return steps


def control_state_at(capture: Capture, scope_key: str, at: datetime) -> tuple[int, str]:
    """La version activa y el estado (armed, paused, inactive, closed), de los eventos de control con to_state.

    Aplica, en orden de hora y de generacion, los eventos con ``created_at <=
    at``. La activacion de una version deja ``inactive``. Antes del primer
    evento del scope no se sabe: ``CaptureError``.
    """
    _require_aware(at)
    steps = _control_steps(capture, scope_key)
    position = bisect_right([step.at for step in steps], at) - 1
    if position < 0:
        raise CaptureError(f"config.pilot_control_events_control_level: {scope_key} sin estado antes del instante")
    return steps[position].version, steps[position].state


def _next_armed(capture: Capture, scope_key: str, at: datetime) -> datetime | None:
    steps = _control_steps(capture, scope_key)
    for step in steps[bisect_right([item.at for item in steps], at):]:
        if step.state == ARMED:
            return step.at
    return None


@dataclass(frozen=True)
class _Gate:
    bolsa: str
    zone: str
    max_per_day: int
    max_total: int
    franja: Franja
    midnight: Franja                     # la franja de 00:00: su proxima apertura es la medianoche
    start: datetime
    expires: datetime


def _gate_of(capture: Capture, action: Mapping[str, Any], start: datetime | None = None) -> _Gate:
    where = f"accion {action.get('id')}"
    case = capture._index["cases"].get(_need(action, "recovery_case_id", where))
    if case is None:
        raise CaptureError(f"{where}: su caso no esta en la captura")
    binding = case.get("binding")
    if binding is None:
        raise CaptureError(f"caso {case.get('id')}: sin binding del piloto")
    scope_key = _need(binding, "scope_key", "binding")
    scope = capture._index["scopes"].get((scope_key, _need(binding, "scope_version", "binding")))
    if scope is None:
        raise CaptureError(f"caso {case.get('id')}: la version del scope no esta en config")
    policy = capture._index["policies"].get(
        (_need(case, "policy_key", "caso"), _need(case, "policy_version", "caso")))
    if policy is None:
        raise CaptureError(f"caso {case.get('id')}: la politica no esta en config")
    try:
        franja = franja_of_policy(_thaw(policy))
    except ValueError as error:
        raise CaptureError(f"politica {policy.get('policy_key')}: {error}") from error
    grace = interval_of(policy.get("grace_period"))
    if grace is None:
        raise CaptureError(f"politica {policy.get('policy_key')}.grace_period: no se lee")
    zone = scope["timezone"]
    return _Gate(
        bolsa=scope_key, zone=zone,
        max_per_day=scope["max_outbound_request_starts_per_day"],
        max_total=scope["max_outbound_request_starts_total"],
        franja=franja,
        midnight=Franja(always=False, zone=zone, start_minute=0, end_minute=LAST_MINUTE),
        start=start if start is not None else _instant(action, "anchor_observed_at", where) + grace,
        expires=_instant(action, "expires_at", where),
    )


def _bolsa_at(capture: Capture, gate: _Gate, at: datetime) -> tuple[int, int]:
    """Las autorizaciones observadas de la bolsa antes de ``at``: en total y en su dia local."""
    total = bisect_left(capture._index["authorized"].get(gate.bolsa, []), at)
    day: date = local_day(gate.zone, at)
    daily = bisect_left(capture._index["authorized_by_day"].get((gate.bolsa, day.isoformat()), []), at)
    return total, daily


def eligible_at(capture: Capture, action: Mapping[str, Any], *, start: datetime | None = None) -> datetime | None:
    """E de P3: el primer instante >= anchor_observed_at + gracia con la franja abierta, el control armado
    en la version del recorrido y la bolsa con cupo en el dia local, antes de expires_at.

    «Armado» es el estado del control, sea cual sea la version activa: la
    version del recorrido es la del binding del caso, y de ella salen los
    topes (decision 3, ver el docstring del modulo). Cada condicion que falta
    da la primera hora en que puede cumplirse (la proxima apertura, el proximo
    ``armed``, la proxima medianoche local), y se vuelve a mirar todo desde la
    mayor. Con el tope total agotado no vuelve: las autorizaciones no bajan.

    ``start`` reemplaza ``anchor_observed_at + gracia`` como primer instante:
    lo usa la v3 para A2 y Z (la primera aceptacion mas el delay del paso, o
    la renovacion que adelanta Z).
    """
    if start is not None:
        _require_aware(start)
    gate = _gate_of(capture, action, start)
    at = gate.start
    for _ in range(_MAX_STEPS):
        if at >= gate.expires:
            return None
        waits: list[datetime] = []
        _, state = control_state_at(capture, gate.bolsa, at)
        if state != ARMED:
            armed = _next_armed(capture, gate.bolsa, at)
            if armed is None:
                return None
            waits.append(armed)
        if not is_open(gate.franja, at):
            opens = next_open(gate.franja, at)
            if opens is None:
                return None
            waits.append(opens)
        total, daily = _bolsa_at(capture, gate, at)
        if total >= gate.max_total:
            return None
        if daily >= gate.max_per_day:
            midnight = next_open(gate.midnight, at)
            assert midnight is not None
            waits.append(midnight)
        if not waits:
            return at
        at = max(waits)
    raise CaptureError(f"accion {action.get('id')}: eligible_at no converge")  # pragma: no cover


# ---------------------------------------------------------------------------
# P3, la hora de salida.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SendTiming:
    """Un envio de un disparo desde «desde»: E, R y la demora."""

    action_id: str
    firing_ref: str
    eligible_at: datetime | None
    request_started_at: datetime
    delay_seconds: float | None         # R - E; None si la puerta no dice «mandar» nunca
    explained_by: str | None            # un hecho observado que explica pasar los 5 minutos
    # Desde cuando explica el hecho: el reintento que pidio el fallo, o el
    # vencimiento del lease del arranque rechazado. R tiene que quedar a lo sumo
    # la tolerancia despues de max(E, esto).
    explained_since: datetime | None = None


def _window_actions(capture: Capture) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Las acciones de los casos que ancla un disparo desde «desde», con su disparo."""
    for event in capture.firings():
        case = capture._index["case_by_event"].get(event["id"])
        if case is None or case["abandonment_event_id"] != event["id"]:
            continue
        action = capture._index["action_by_case"].get(case["id"])
        if action is not None:
            yield event, action


def _attempts(action: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return sorted(action.get("intentos") or (), key=lambda tramo: tramo["primero"]["attempt_number"])


def _fact_of(last: Mapping[str, Any], where: str) -> tuple[str, datetime] | None:
    """El hecho que deja el ultimo intento de un tramo sin pedido, y desde cuando explica la demora.

    Un fallo, un incierto o un rechazo explican hasta el reintento que pidieron
    (``finalized_next_attempt_at``; el de un fallo es a 1 minuto, worker.py:959)
    o, sin el, hasta el comienzo del intento. Un arranque rechazado (reservado,
    sin resultado) explica hasta que vence su lease: recien ahi se vuelve a tomar
    la accion.
    """
    outcome = last.get("outcome")
    fact = EXPLAINING_OUTCOMES.get(outcome or "")
    if fact is not None:
        since = _optional_instant(last, "finalized_next_attempt_at", where)
        if since is None:
            since = _optional_instant(last, "started_at", where)
        return None if since is None else (fact, since)
    if outcome is None and last.get("phase") == RESERVED_PHASE:
        started = _optional_instant(last, "started_at", where)
        return None if started is None else (REJECTED_START, started + LEASE)
    return None


def _request_started(action: Mapping[str, Any]) -> tuple[datetime | None, str | None, datetime | None]:
    """El ``request_started_at`` del primer tramo que lo tiene, y el ultimo hecho anterior que explica la demora.

    Devuelve (R, el hecho, desde cuando explica). El hecho sale del ultimo
    intento de cada tramo (``ultimo``, o ``primero`` si el tramo tiene uno).
    """
    where = f"accion {action['id']}"
    explained: tuple[str, datetime] | None = None
    for tramo in _attempts(action):
        first = tramo["primero"]
        if first.get("request_started_at") is not None:
            fact, since = explained if explained is not None else (None, None)
            return _instant(first, "request_started_at", where), fact, since
        last = tramo.get("ultimo") or first
        found = _fact_of(last, where)
        if found is not None:
            explained = found
    return None, None, None


def send_timings(capture: Capture) -> tuple[SendTiming, ...]:
    """Cada envio de un disparo desde «desde», con su E y su R, en el orden de los disparos."""
    out: list[SendTiming] = []
    for event, action in _window_actions(capture):
        started, explained, since = _request_started(action)
        if started is None:
            continue
        eligible = eligible_at(capture, action)
        out.append(SendTiming(
            action_id=action["id"], firing_ref=event["id"], eligible_at=eligible,
            request_started_at=started,
            delay_seconds=None if eligible is None else (started - eligible).total_seconds(),
            explained_by=explained, explained_since=since,
        ))
    return tuple(out)


def _explains(timing: SendTiming, tolerance: timedelta) -> bool:
    """Si el hecho observado explica la demora: R a lo sumo la tolerancia despues de max(E, el hecho)."""
    if timing.explained_by is None or timing.explained_since is None or timing.eligible_at is None:
        return False
    return timing.request_started_at - max(timing.eligible_at, timing.explained_since) <= tolerance


def _unsent_differences(capture: Capture, tolerance: timedelta) -> Iterator[dict[str, str]]:
    """Las acciones sin envio a las que la puerta les dice «mandar»: el motor habria mandado.

    Una accion cortada (cancelada, ganada) antes de E + la tolerancia es el
    corte de P5, no P3. Una que espera todavia cuenta contra la hora de la
    captura.
    """
    for event, action in _window_actions(capture):
        started, _, _ = _request_started(action)
        if started is not None:
            continue
        eligible = eligible_at(capture, action)
        if eligible is None:
            continue
        where = f"accion {action['id']}"
        if action.get("status") in OPEN_ACTION_STATUSES:
            end = capture.captured_at
            if end is None:
                continue
        else:
            end = _instant(action, "updated_at", where)
        if eligible + tolerance < end:
            yield {
                "ref": event["id"], "diferencia": "elegible_sin_envio", "accion": action["id"],
                "detalle": (f"E={_text(eligible)} sin envio; la accion termino {action.get('status')}"
                            f" ({action.get('terminal_reason') or 'sin motivo'})"),
            }


# ---------------------------------------------------------------------------
# P6, quien le gana a quien.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Supersession:
    """Un primer contacto que cedio con ``superseded_by_provider_event``, y quien le gano."""

    plan_ref: str                       # el uuid interno del plan o de su caso
    stopped_at: datetime
    winner_ref: str | None              # el disparo que le gano, por intencion o por persona
    matched_by: str | None              # "intencion" | "persona": la primera ruta de MATCH_ORDER
    routes: frozenset[str] = frozenset()  # todas las rutas por las que hay un ganador


def _precheckout_product(capture: Capture) -> str:
    """El producto del primer contacto: el de sus scopes, que tiene que ser uno solo.

    El fixture no trae el scope de cada plan (§6.4, paso 2); con un producto
    por instancia, como hoy, es exacto. Con varios, la captura tiene que traer
    el del plan, y aca falla en voz alta.
    """
    products = {
        row["external_product_id"] for row in capture.config["pilot_scope_versions"]
        if row.get("source_event_type") == PRECHECKOUT_EVENT_TYPE
    }
    if len(products) != 1:
        raise CaptureError("config.pilot_scope_versions: el primer contacto no tiene un solo producto")
    return next(iter(products))


def _plan_ref(plan: Mapping[str, Any]) -> str:
    return plan.get("recovery_case_id") or plan["purchase_intent_id"]


def _correlated_intents(event: Mapping[str, Any]) -> frozenset[str]:
    out = set()
    for name in ("cart_correlation", "payment_failure_correlation"):
        correlation = event.get(name) or {}
        if correlation.get("outcome") == "resolved" and correlation.get("purchase_intent_id"):
            out.add(correlation["purchase_intent_id"])
    return frozenset(out)


def _case_open_at(case: Mapping[str, Any], at: datetime) -> bool:
    created = _instant(case, "created_at", f"caso {case['id']}")
    closed = _optional_instant(case, "closed_at", f"caso {case['id']}")
    if created > at:
        return False
    if closed is not None:
        return closed > at
    return case.get("status") in OPEN_CASE_STATUSES


def _wins(capture: Capture, event: Mapping[str, Any], plan: Mapping[str, Any], route: str,
          product: str, at: datetime | None) -> bool:
    """Si el disparo le gana al plan por la ruta dada, mirado en ``at``.

    Por persona, el caso del disparo es del contacto del plan y del producto, y
    esta en curso en ``at`` (sin ``at``, alcanza con que exista: es el disparo
    que lo abre). Por intencion, la correlacion del disparo resolvio la
    intencion del plan.
    """
    if event.get("product_id") != product:
        return False
    if route == "intencion":
        return plan.get("purchase_intent_id") in _correlated_intents(event)
    if route != "persona" or plan.get("contact_id") is None:
        return False
    case = capture._index["case_by_event"].get(event["id"])
    if case is None or case.get("contact_id") != plan["contact_id"]:
        return False
    if case.get("external_product_id") != product:
        return False
    return at is None or _case_open_at(case, at)


def _superseded(plan: Mapping[str, Any]) -> bool:
    if plan.get("outcome") == "not_planned":
        return plan.get("reason_code") == SUPERSEDED
    return plan.get("accion_status") == "cancelled" and plan.get("accion_terminal_reason") == SUPERSEDED


def _all_firing_events(capture: Capture) -> list[tuple[datetime, Mapping[str, Any]]]:
    """Todos los carritos y pagos fallidos de la captura, siembra incluida, por hora de llegada."""
    out = [
        (_instant(event, "received_at", f"evento {event['id']}"), event)
        for event in capture.events if event.get("event_type") in FIRING_EVENT_TYPES
    ]
    out.sort(key=lambda item: item[0])
    return out


def supersessions(capture: Capture) -> tuple[Supersession, ...]:
    """Cada primer contacto que cedio, con su ganador.

    El ganador es un carrito o un pago fallido de la captura (la siembra
    cuenta: una intencion clasificada antes de «desde» sigue clasificada)
    recibido hasta el corte, que le gana por la primera ruta de ``MATCH_ORDER``
    que encuentra uno; de esa ruta, el mas reciente.
    """
    product = _precheckout_product(capture)
    events = _all_firing_events(capture)
    out: list[Supersession] = []
    for index, plan in enumerate(capture._index.get("p6_plans", capture.first_contacts)):
        if not _superseded(plan):
            continue
        where = f"primer_contacto[{index}]"
        stopped = (_instant(plan, "created_at", where) if plan.get("outcome") == "not_planned"
                   else _instant(plan, "accion_cerrada_at", where))
        before = [event for received, event in events if received <= stopped]
        winner: str | None = None
        matched: str | None = None
        routes: set[str] = set()
        for route in MATCH_ORDER:
            found = [event for event in before if _wins(capture, event, plan, route, product, stopped)]
            if found:
                routes.add(route)
                if winner is None:
                    winner, matched = found[-1]["id"], route
        if capture._index.get("open_case_route"):
            # La v3: el legado cede tambien ante cualquier caso de Hotmart abierto del producto
            # (20261001000200:300-311, §1.7 fila 13), aunque su disparo no sea de la captura
            # (la ficha, PURCHASE_BILLET_PRINTED). El caso es un hecho de la persona.
            case = _open_case_of(capture, plan, product, stopped)
            if case is not None:
                routes.add(OPEN_CASE_ROUTE)
                if winner is None:
                    winner, matched = case["abandonment_event_id"], OPEN_CASE_ROUTE
            # Y ante la intencion clasificada por un carrito o un pago fallido (20261001000200:297-299),
            # aunque el disparo que la clasifico no sea de la captura (llego antes de la siembra). Es una
            # cota: la captura trae la foto de la intencion, no desde cuando esta clasificada, asi que
            # se usa solo si ninguna otra ruta encuentra un ganador.
            intent = capture._index["intents"].get(plan.get("purchase_intent_id"))
            if (winner is None and intent is not None
                    and intent.get("current_classification") in CLASSIFIED_INTENT):
                routes.add(CLASSIFIED_INTENT_ROUTE)
                winner, matched = intent["id"], CLASSIFIED_INTENT_ROUTE
        out.append(Supersession(_plan_ref(plan), stopped, winner, matched, frozenset(routes)))
    return tuple(out)


OPEN_CASE_ROUTE = "caso_en_curso"
CLASSIFIED_INTENT_ROUTE = "intencion_clasificada"
CLASSIFIED_INTENT = frozenset({"confirmed_abandonment", "payment_failure_supported"})


def _open_case_of(capture: Capture, plan: Mapping[str, Any], product: str,
                  at: datetime) -> Mapping[str, Any] | None:
    """El caso de Hotmart del contacto del plan y del producto que estaba en curso en ``at``, el mas reciente."""
    contact = plan.get("contact_id")
    if contact is None:
        return None
    found = [case for case in capture._index["cases_by_contact"].get(contact, ())
             if case.get("external_product_id") == product and _case_open_at(case, at)]
    return max(found, key=lambda case: case["created_at"]) if found else None


def _route_group(routes: frozenset[str]) -> str | None:
    if {"persona", "intencion"} <= routes:
        return "las_dos"
    if "persona" in routes:
        return "solo_persona"
    if "intencion" in routes:
        return "solo_intencion"
    return None


def winners_without_cancel(capture: Capture) -> tuple[tuple[str, str], ...]:
    """Los disparos desde «desde» que le ganan a un primer contacto que esperaba y que no se cancelo.

    Espera el plan planificado antes de la llegada del disparo, con su accion
    sin cerrar y sin envio. Si termina cancelado, por cualquier motivo, cumple.
    Si salio o termino de otra forma, es una diferencia cuando el disparo le
    seguia ganando en ese momento (por intencion, o con su caso todavia en
    curso). Uno que sigue esperando a la hora de la captura no se juzga: su
    reevaluacion no llego. Devuelve los pares (disparo, plan).
    """
    product = _precheckout_product(capture)
    out: list[tuple[str, str]] = []
    # En la v3 los disparos son solo los carritos, y P6 mira tambien el pago
    # fallido, como en la T1 (``p6_firings``).
    for event in capture._index.get("p6_firings", capture._index["firings"]):
        arrived = _instant(event, "received_at", f"evento {event['id']}")
        for index, plan in enumerate(capture._index.get("p6_plans", capture.first_contacts)):
            if plan.get("outcome") != "planned":
                continue
            where = f"primer_contacto[{index}]"
            closed = _optional_instant(plan, "accion_cerrada_at", where)
            started = _optional_instant(plan, "request_started_at", where)
            if _instant(plan, "created_at", where) > arrived:
                continue
            if (closed is not None and closed <= arrived) or (started is not None and started <= arrived):
                continue
            if not any(_wins(capture, event, plan, route, product, None) for route in MATCH_ORDER):
                continue
            if plan.get("accion_status") == "cancelled":
                continue
            end = started or closed
            if end is None:
                continue
            if any(_wins(capture, event, plan, route, product, end) for route in MATCH_ORDER):
                out.append((event["id"], _plan_ref(plan)))
    return tuple(out)


def _person_mismatches(capture: Capture) -> Iterator[dict[str, str]]:
    """Los casos cuyo evento no tiene la clave de persona de su contacto (§5.2): un hallazgo."""
    for case in capture.cases:
        event = capture._index["events"].get(case["abandonment_event_id"])
        contact = capture._index["contacts"].get(case.get("contact_id"))
        keys = set((contact or {}).get("person_keys") or ())
        if event is None or contact is None or event.get("person_key") not in keys:
            yield {
                "ref": case["id"], "diferencia": "persona_no_coincide",
                "detalle": "la clave de persona del evento no esta entre las del contacto del caso",
            }


# ---------------------------------------------------------------------------
# El reporte.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParityReport:
    """Lo que mide la paridad sobre una captura. ``ok()`` es cero sin explicar."""

    firings: Mapping[str, int]          # por tipo y por resultado, desde «desde»
    reached_flow: int
    sends: int
    delay: Mapping[str, float]          # min, mediana, p95, max
    never_before_violations: tuple[str, ...]
    over_tolerance: tuple[str, ...]
    supersessions: int
    supersessions_without_winner: tuple[str, ...]
    winners_without_cancel: tuple[str, ...]
    named: Mapping[str, int]
    named_total: int
    unexplained: tuple[Mapping[str, Any], ...]
    # Lo que suma esta implementacion a la firma de §5.2: P6 por ruta, que el
    # documento de docs/operations/ pide (§5.5). supersessions_by depende de
    # MATCH_ORDER (la primera ruta que encuentra); supersessions_by_routes no
    # (las dos, solo persona, solo intencion).
    supersessions_by: Mapping[str, int] = field(default_factory=dict)
    supersessions_by_routes: Mapping[str, int] = field(default_factory=dict)
    # Los primeros contactos que cedieron ante un disparo sin caso (que no
    # mando nada), por el resultado del disparo.
    ceded_to_a_firing_without_case: Mapping[str, int] = field(default_factory=dict)

    def ok(self) -> bool:
        return not self.unexplained and not self.never_before_violations

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok(),
            "firings": dict(self.firings),
            "reached_flow": self.reached_flow,
            "sends": self.sends,
            "delay_seconds": dict(self.delay),
            "never_before_violations": list(self.never_before_violations),
            "over_tolerance": list(self.over_tolerance),
            "supersessions": self.supersessions,
            "supersessions_by": dict(self.supersessions_by),
            "supersessions_by_routes": dict(self.supersessions_by_routes),
            "ceded_to_a_firing_without_case": dict(self.ceded_to_a_firing_without_case),
            "supersessions_without_winner": list(self.supersessions_without_winner),
            "winners_without_cancel": list(self.winners_without_cancel),
            "named": dict(self.named),
            "named_total": self.named_total,
            "unexplained": [dict(item) for item in self.unexplained],
        }


def _text(at: datetime | None) -> str:
    return "-" if at is None else at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _case_running_for(capture: Capture, event: Mapping[str, Any]) -> bool:
    """Si la persona del disparo tenia un caso del producto en curso cuando llego (la 5b, G14)."""
    person = event.get("person_key")
    if person is None:
        return False
    arrived = _instant(event, "received_at", f"evento {event['id']}")
    for contact_id in capture._index["contacts_by_person"].get(person, ()):
        for case in capture._index["cases_by_contact"].get(contact_id, ()):
            if case.get("external_product_id") == event.get("product_id") and _case_open_at(case, arrived):
                return True
    return False


def _firing_result(capture: Capture, event: Mapping[str, Any]) -> str:
    if event.get("processing_status") == "processed":
        case = capture._index["case_by_event"].get(event["id"])
        if case is None:
            return "processed:sin_caso"
        return "processed" if case["abandonment_event_id"] == event["id"] else "processed:evidencia"
    error = event.get("processing_error")
    if error == CASE_FAILED and _case_running_for(capture, event):
        return CASE_FAILED_RUNNING
    return error or event.get("processing_status") or "sin_estado"


def _reached_flow(capture: Capture, event: Mapping[str, Any]) -> bool:
    if capture._index["case_by_event"].get(event["id"]) is not None:
        return True
    error = event.get("processing_error") or ""
    if error == CASE_FAILED:
        return _case_running_for(capture, event)
    return error.startswith(PLANNING_REJECTION_PREFIX) or error in PLANNING_ERRORS


def _percentile(values: Sequence[float], share: float) -> float:
    """El percentil por rango mas cercano: el valor en la posicion ceil(share * n)."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(share * len(ordered))) - 1]


def _named_problem(entry: object, kinds: tuple[str, ...] = DIFFERENCE_KINDS) -> str | None:
    if not isinstance(entry, Mapping):
        return "no_es_un_objeto"
    if set(entry) - NAMED_KEYS:
        return "clave_desconocida"
    if not isinstance(entry.get("disparo"), str) or not _UUID.fullmatch(entry["disparo"]):
        return "disparo_sin_uuid"
    if entry.get("diferencia") not in kinds:
        return "diferencia_desconocida"
    if entry["diferencia"] == NEVER_BEFORE:
        return "nunca_antes_no_se_acepta"
    if entry.get("clase") not in NAMED_CLASSES:
        return "clase_invalida"
    if not isinstance(entry.get("nombre"), str) or not _NAME.fullmatch(entry["nombre"]):
        return "nombre_invalido"
    has_case, has_cite = entry.get("caso") is not None, entry.get("cita") is not None
    if not has_case and not has_cite:
        return "sin_caso_ni_cita"
    if has_case and has_cite:
        return "caso_y_cita"
    if has_case and not (isinstance(entry["caso"], str) and _GENERATED_CASE.fullmatch(entry["caso"])):
        return "caso_invalido"
    if has_cite and not (isinstance(entry["cita"], str) and _CITATION.fullmatch(entry["cita"])):
        return "cita_invalida"
    if entry.get("descripcion") is not None and not isinstance(entry["descripcion"], str):
        return "descripcion_invalida"
    return None


def parity_report(capture: Capture, named: Sequence[Mapping[str, Any]], *,
                  tolerance: timedelta = TOLERANCE) -> ParityReport:
    """Mide P3 y P6 sobre la captura y cruza las diferencias con las que tienen nombre.

    Una diferencia con nombre explica la diferencia observada de su disparo y
    su clase. Es invalida si le falta el caso y la cita, si trae los dos, si su
    clase no es de §1.7 o si nombra un envio antes de E; y no explica nada si
    no hay ninguna diferencia observada con su disparo y su clase. Las dos
    cosas van a ``unexplained``.
    """
    if not isinstance(tolerance, timedelta) or tolerance < timedelta(0):
        raise ValueError("tolerance_invalid")
    firing_counts: Counter[str] = Counter()
    reached = 0
    for event in capture.firings():
        firing_counts[f"{FIRING_KIND[event['event_type']]}/{_firing_result(capture, event)}"] += 1
        reached += _reached_flow(capture, event)

    differences: list[dict[str, str]] = []
    timings = send_timings(capture)
    never_before: list[str] = []
    over: list[str] = []
    delays: list[float] = []
    for timing in timings:
        if timing.eligible_at is None or timing.delay_seconds is None:
            differences.append({
                "ref": timing.firing_ref, "diferencia": "envio_sin_E", "accion": timing.action_id,
                "detalle": f"R={_text(timing.request_started_at)} y la puerta no dice mandar antes de vencer",
            })
            continue
        delays.append(timing.delay_seconds)
        detail = (f"E={_text(timing.eligible_at)} R={_text(timing.request_started_at)}"
                  f" R-E={timing.delay_seconds:.3f}s")
        if timing.request_started_at < timing.eligible_at - NEVER_BEFORE_SLACK:
            never_before.append(timing.firing_ref)
            differences.append({"ref": timing.firing_ref, "diferencia": NEVER_BEFORE,
                                "accion": timing.action_id, "detalle": detail})
        elif timing.delay_seconds > tolerance.total_seconds():
            over.append(timing.firing_ref)
            if not _explains(timing, tolerance):
                if timing.explained_by is not None:
                    detail += (f"; {timing.explained_by} explica hasta"
                               f" {_text(timing.explained_since)} y no alcanza")
                differences.append({"ref": timing.firing_ref, "diferencia": "envio_fuera_de_tolerancia",
                                    "accion": timing.action_id, "detalle": detail})
    differences.extend(_unsent_differences(capture, tolerance))

    ceded = supersessions(capture)
    without_winner = tuple(item.plan_ref for item in ceded if item.winner_ref is None)
    for item in ceded:
        if item.winner_ref is None:
            differences.append({"ref": item.plan_ref, "diferencia": "cesion_sin_ganador",
                                "detalle": f"cedio a las {_text(item.stopped_at)} sin un disparo que le gane"})
    unexpected = winners_without_cancel(capture)
    for firing_ref, plan_ref in unexpected:
        differences.append({"ref": firing_ref, "diferencia": "ganador_sin_cancelacion",
                            "detalle": f"le gana al primer contacto {plan_ref}, que no se cancelo"})
    differences.extend(_person_mismatches(capture))

    observed = {(item["ref"], item["diferencia"]) for item in differences}
    explained: set[tuple[str, str]] = set()
    problems: list[dict[str, str]] = []
    named_counts: Counter[str] = Counter()
    for position, entry in enumerate(named):
        reason = _named_problem(entry)
        disparo = entry.get("disparo") if isinstance(entry, Mapping) else None
        ref = disparo if isinstance(disparo, str) else f"nombrada[{position}]"
        if reason is not None:
            problems.append({"ref": ref, "diferencia": f"nombrada_invalida:{reason}",
                             "detalle": f"la diferencia con nombre {position} no es valida"})
            continue
        key = (entry["disparo"], entry["diferencia"])
        if key in explained:
            problems.append({"ref": ref, "diferencia": "nombrada_repetida",
                             "detalle": f"la diferencia con nombre {position} repite disparo y clase"})
        elif key not in observed:
            problems.append({"ref": ref, "diferencia": "nombrada_sin_diferencia",
                             "detalle": f"{entry['nombre']}: la paridad no observa esa diferencia"})
        else:
            explained.add(key)
            named_counts[entry["nombre"]] += 1
    unexplained = [
        item for item in differences
        if item["diferencia"] == NEVER_BEFORE or (item["ref"], item["diferencia"]) not in explained
    ] + problems

    delay: dict[str, float] = {}
    if delays:
        delay = {"min": min(delays), "mediana": statistics.median(delays),
                 "p95": _percentile(delays, 0.95), "max": max(delays)}
    by_route = Counter(item.matched_by for item in ceded if item.matched_by is not None)
    by_routes = Counter(_route_group(item.routes) for item in ceded)
    without_case: Counter[str] = Counter()
    for item in ceded:
        if item.winner_ref is not None and capture._index["case_by_event"].get(item.winner_ref) is None:
            without_case[_firing_result(capture, capture._index["events"][item.winner_ref])] += 1
    return ParityReport(
        firings=MappingProxyType(dict(sorted(firing_counts.items()))),
        reached_flow=reached,
        sends=len(timings),
        delay=MappingProxyType(delay),
        never_before_violations=tuple(never_before),
        over_tolerance=tuple(over),
        supersessions=len(ceded),
        supersessions_without_winner=without_winner,
        winners_without_cancel=tuple(firing for firing, _ in unexpected),
        named=MappingProxyType(dict(sorted(named_counts.items()))),
        named_total=sum(named_counts.values()),
        unexplained=tuple(MappingProxyType(item) for item in unexplained),
        supersessions_by=MappingProxyType({route: by_route.get(route, 0) for route in MATCH_ORDER}),
        supersessions_by_routes=MappingProxyType({group: by_routes.get(group, 0) for group in ROUTE_GROUPS}),
        ceded_to_a_firing_without_case=MappingProxyType(dict(sorted(without_case.items()))),
    )


# ---------------------------------------------------------------------------
# La T2.2: la paridad sobre la captura v3, el carrito de tres toques.
# ---------------------------------------------------------------------------
#
# Especificacion: «T2.2: la captura v3 y la paridad del carrito de tres
# toques» (2026-10-21, en el vault). La captura es la de
# ``captura-paridad-recuperacion-v3`` (``flow_capture.FORM_V3``): desde la
# activacion de la v7 de att1-recuperacion, con las secuencias, los cupones,
# los links, las renovaciones y la primera respuesta de cada envio.
#
# **P3, por paso.** Para cada A1, A2 y Z enviado de un carrito de la ventana
# (un caso del scope del carrito que ancla un disparo desde «desde»), E es el
# primer instante en que la puerta dice «mandar» (``eligible_at``, la misma de
# la T1) desde un comienzo que depende del paso:
#
# * A1 (``first_contact``): el ancla mas la gracia, como en la T1;
# * A2 y Z: la primera aceptacion de la secuencia mas el ``delay`` del paso
#   en la politica del caso. El delay se cuenta desde la primera aceptacion y
#   no desde el paso anterior (20260813000100:328-350: «Z cae a las 72 h de A1
#   aunque A2 se haya corrido por el horario»), y el paso siguiente no tiene
#   gracia;
# * Z con un pedido de renovacion: la hora del pedido, si es antes
#   (20261019000600:331-334). Es la excepcion
#   ``renovacion_adelanta_el_ultimo_toque`` y se cuenta como explicada.
#
# R es el ``request_started_at`` observado. Se cumple R >= E - 1 s siempre, y
# R - E <= 5 minutos salvo un hecho observado. Ademas de los de la T1, el
# cupon que se espera: un intento ``failed_before_request`` con
# ``recovery_coupon_not_ready`` es la excepcion ``a1_espera_al_cupon`` en A1
# (cart_first_touch.py:130-139) y ``toque_con_cupon_cierra_sin_cupon`` en Z
# (cart_followups.py:215-230); como todo hecho, explica hasta el reintento que
# pidio. Las diferencias de P3 llevan como ``ref`` el uuid de la accion: un
# disparo tiene hasta tres pasos.
#
# **Los finales.** Cada paso de la politica de cada carrito de la ventana es
# ``enviado``, ``pendiente`` (espera a la hora de la captura) o un motivo: el
# de la tabla de §2.9 (``flow_engine.LEGACY_REASON_TO_MOTIVO``: contesto,
# compro, vencio, baja...) o ``sin_cupon`` (el toque con cupon cerro sin el).
# Un paso que no llego a crearse hereda el final del anterior (A2 contesto: Z
# contesto), o el del cierre del caso si el anterior salio (la compra, el
# vencimiento antes del paso). Un paso sin motivo es la diferencia
# ``final_sin_motivo``. La cuenta cierra contra el censo: los pasos
# clasificados son los casos por sus pasos, y los enviados son los envios de P3
# (si no, ``censo_no_cierra``).
#
# Un paso que el legado no creo, con el anterior enviado y su E (la primera aceptacion mas su delay,
# con la puerta) mas la tolerancia ya pasado a la hora de la captura, es ``paso_no_creado``: una
# diferencia.
#
# **P6**, como en la T1, con los carritos y los pagos fallidos como ganadores, sobre los planes desde
# «desde». Suma dos rutas, despues de las de la T1: ``caso_en_curso`` (un caso de Hotmart abierto del
# producto, aunque su disparo no se capture, como la ficha) e ``intencion_clasificada`` (la intencion
# del plan clasificada por un carrito o un pago fallido; es una cota, porque la captura trae la foto de
# la intencion y no desde cuando esta clasificada). Los casos de la ficha no se comparan por persona:
# se cuentan aparte (``persons_not_comparable``).

DIFFERENCE_KINDS_V3 = DIFFERENCE_KINDS + ("final_sin_motivo", "censo_no_cierra", "paso_no_creado")
FIRST_STEP = "first_contact"
LAST_TOUCH_STEP = "cart_last_touch"
# El cupon que todavia no esta valid: el intento se difiere (worker.py:992-1030,
# con el retry_after del paso).
COUPON_NOT_READY = "recovery_coupon_not_ready"
COUPON_WAIT_BY_STEP: Mapping[str, str] = MappingProxyType({
    FIRST_STEP: "a1_espera_al_cupon",
    LAST_TOUCH_STEP: "toque_con_cupon_cierra_sin_cupon",
})
RENEWAL_ADVANCES = "renovacion_adelanta_el_ultimo_toque"
# Los cierres de un toque con cupon que no tuvo su cupon (cart_followups.py:
# 213-230 y cart_first_touch.py:_COUPON_DEAD): no hay toque sin el descuento.
COUPON_CLOSE_REASONS = frozenset({
    "recovery_coupon_create_failed", "recovery_coupon_expired", "recovery_coupon_redeemed",
    "cart_last_touch_coupon_not_ready_in_time",
})
WITHOUT_COUPON = "sin_cupon"
FINAL_SENT = "enviado"
FINAL_PENDING = "pendiente"
FINAL_WITHOUT_REASON = "sin_motivo"
# Un paso que el legado no creo aunque el anterior salio y su E (mas la tolerancia) ya paso a la hora
# de la captura: el motor lo habria mandado. Es una diferencia.
FINAL_NOT_CREATED = "paso_no_creado"
# Los cierres de la secuencia que explican un paso que no llego a crearse
# (20260813000100:398-416).
SEQUENCE_CLOSE_MOTIVES: Mapping[str, str] = MappingProxyType({
    "next_step_outside_expiration": "vencio",
    "policy_exhausted": "completo",
})


@dataclass(frozen=True)
class CaptureV3(Capture):
    """La captura v3, leida y congelada: la de ``Capture`` con las secciones nuevas.

    Un caso del carrito tiene una accion por paso: el indice las guarda por
    caso y por paso, sin la regla de una accion por caso de la v2.
    """

    sequences: tuple[Mapping[str, Any], ...] = ()
    coupons: tuple[Mapping[str, Any], ...] = ()
    links: tuple[Mapping[str, Any], ...] = ()
    renewals: tuple[Mapping[str, Any], ...] = ()

    FORM = FORM_V3

    def __post_init__(self) -> None:
        index = _build_index(self, form=FORM_V3, one_action_per_case=False)
        index.update(_build_index_v3(self))
        object.__setattr__(self, "_index", MappingProxyType(index))

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CaptureV3:
        """Valida la forma v3 y los conteos, y devuelve la captura congelada (tambien desde un fixture)."""
        restored = cls._restored(data)
        return cls(
            **cls._fields_of(restored),
            sequences=_freeze(restored["secuencias"]),
            coupons=_freeze(restored["cupones"]),
            links=_freeze(restored["links"]),
            renewals=_freeze(restored["renovaciones"]),
        )


def _build_index_v3(capture: CaptureV3) -> dict[str, Any]:
    """Lo que suma la v3 al indice: los disparos de P6, las acciones por caso y paso, las secuencias y las
    renovaciones por accion."""
    out: dict[str, Any] = {}
    out["p6_firings"] = tuple(firings({"_captured": capture.meta, "eventos": capture.events}, FORM_V2))
    # P6 mira los planes desde «desde», como la T1 sobre su fixture (§6.4, paso 2): asi la captura
    # completa y el fixture recortado dan lo mismo. Uno de la siembra cedio ante un disparo que
    # puede haber llegado antes de la siembra, y la captura no lo trae.
    desde = _instant(capture.meta, "desde", "_captured")
    out["p6_plans"] = tuple(
        plan for i, plan in enumerate(capture.first_contacts)
        if _instant(plan, "created_at", f"primer_contacto[{i}]") >= desde
    )
    out["open_case_route"] = True
    out["intents"] = {_need(row, "id", f"intenciones[{i}]"): row for i, row in enumerate(capture.intents)}
    actions_by_case: dict[str, dict[str, Mapping[str, Any]]] = {}
    for i, row in enumerate(capture.actions):
        where = f"acciones[{i}]"
        steps = actions_by_case.setdefault(_need(row, "recovery_case_id", where), {})
        step = _need(row, "step_key", where)
        if step in steps:
            # Dos acciones del mismo paso en un caso: no se elige una en silencio.
            raise CaptureError(f"{where}: su caso ya tiene otra accion del paso {step}")
        steps[step] = row
    out["actions_by_case"] = {key: MappingProxyType(value) for key, value in actions_by_case.items()}
    out["sequences"] = {_need(row, "id", f"secuencias[{i}]"): row for i, row in enumerate(capture.sequences)}
    renewals: dict[str, Mapping[str, Any]] = {}
    for i, row in enumerate(capture.renewals):
        renewals[_need(row, "action_id", f"renovaciones[{i}]")] = row
    out["renewals"] = renewals
    return out


@dataclass(frozen=True)
class StepTiming:
    """Un envio de un paso de un carrito de la ventana: desde donde se cuenta E, E, R y la demora."""

    action_id: str
    case_id: str
    firing_ref: str
    step_key: str
    start: datetime
    start_by: str                       # "gracia" | "delay" | "renovacion"
    eligible_at: datetime | None
    request_started_at: datetime
    delay_seconds: float | None
    explained_by: str | None
    explained_since: datetime | None = None


def _window_cases(capture: CaptureV3) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Los casos del carrito que ancla un disparo desde «desde», con su disparo."""
    for event in capture.firings():
        case = capture._index["case_by_event"].get(event["id"])
        if case is None or case["abandonment_event_id"] != event["id"]:
            continue
        if (case.get("binding") or {}).get("scope_key") != CART_SCOPE_KEY:
            continue
        yield event, case


def _policy_steps(capture: CaptureV3, case: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    policy = capture._index["policies"].get(
        (_need(case, "policy_key", "caso"), _need(case, "policy_version", "caso")))
    if policy is None:
        raise CaptureError(f"caso {case.get('id')}: la politica no esta en config")
    steps = tuple(policy.get("steps") or ())
    if not steps:
        raise CaptureError(f"politica {policy.get('policy_key')}: sin pasos")
    return steps


def _sent(action: Mapping[str, Any]) -> bool:
    return any(tramo["primero"].get("request_started_at") is not None for tramo in action.get("intentos") or ())


def _first_acceptance(capture: CaptureV3, action: Mapping[str, Any]) -> datetime:
    """La primera aceptacion de la secuencia de la accion: desde ahi cuenta el delay de A2 y Z."""
    sequence = _need(action, "followup_sequence_id", f"accion {action['id']}")
    accepted: list[datetime] = []
    for other in capture.actions:
        if other.get("followup_sequence_id") != sequence:
            continue
        for tramo in other.get("intentos") or ():
            for attempt in (tramo["primero"], tramo.get("ultimo")):
                if attempt is not None and attempt.get("accepted_at") is not None:
                    accepted.append(_instant(attempt, "accepted_at", f"accion {other['id']}"))
    if not accepted:
        raise CaptureError(f"accion {action['id']}: un paso siguiente sin una aceptacion en su secuencia")
    return min(accepted)


def _step_start(capture: CaptureV3, case: Mapping[str, Any], action: Mapping[str, Any]) -> tuple[datetime, str]:
    """Desde cuando se cuenta E para la accion: el ancla con la gracia (A1), o la primera aceptacion con el
    delay del paso (A2 y Z), o la renovacion que lo adelanto."""
    step_key = _need(action, "step_key", f"accion {action['id']}")
    if action.get("action_type") == "first_contact_review" or step_key == FIRST_STEP:
        return _gate_of(capture, action).start, "gracia"
    step = next((row for row in _policy_steps(capture, case) if row.get("step_key") == step_key), None)
    if step is None:
        raise CaptureError(f"accion {action['id']}: su paso no esta en la politica del caso")
    delay = interval_of(step.get("delay"))
    if delay is None:
        raise CaptureError(f"accion {action['id']}: el delay de su paso no se lee")
    start, by = _first_acceptance(capture, action) + delay, "delay"
    # Toda fila de recovery_renewal_requests es un pedido que adelanto su paso
    # (la base la escribe solo con «advanced», 20261019000600:321-336). El
    # efecto que trae la consulta no decide: «movido_despues» es un diferido
    # posterior por el horario, que la franja de la puerta ya modela.
    renewal = capture._index["renewals"].get(action["id"])
    if renewal is not None:
        asked = _instant(renewal, "created_at", f"renovacion {action['id']}")
        if asked < start:
            start, by = asked, "renovacion"
    return start, by


# Cuanto pide reintentar el despachador despues de un fallo antes del pedido, por paso
# (worker.py:992-1030, _finalize_pre_request_failure: retry_after o, sin el, 1 minuto). El cupon que
# todavia no esta valid difiere 5 minutos en A1 (cart_first_touch.py:56-58, CartDiscountConfig) y 8 en
# Z (cart_followups.py:96-98, CartFollowupConfig), y solo en los primeros ``defer_attempts`` (2)
# intentos; despues A1 sale sin descuento y Z cierra sin mensaje (cart_first_touch.py:130-139,
# cart_followups.py:215-230).
COUPON_RETRY_BY_STEP: Mapping[str, timedelta] = MappingProxyType({
    FIRST_STEP: timedelta(minutes=5),
    LAST_TOUCH_STEP: timedelta(minutes=8),
})
COUPON_DEFER_ATTEMPTS = 2
GENERIC_RETRY = timedelta(minutes=1)
# Lo que tarda el despachador entre que arranca el intento y lo cierra (el reintento se cuenta desde
# el cierre, no desde el arranque): en ATT1, el 20/10, 1 s.
RETRY_SLACK = timedelta(minutes=1)
GENERIC_FAILURE = "reintento_tras_fallo_antes_del_pedido"


def _fact_of_step(last: Mapping[str, Any], step_key: str, where: str) -> tuple[str, datetime] | None:
    """El hecho de la T1 (``_fact_of``), con el cupon que se espera nombrado por su excepcion y el
    reintento acotado a lo que pide el despachador.

    Un fallo antes del pedido explica hasta su reintento, pero no mas alla del arranque del intento mas
    el ``retry_after`` del paso y una holgura: un ``finalized_next_attempt_at`` a 7 horas no lo pide el
    despachador. La espera del cupon vale solo en los primeros dos intentos; despues, el fallo es
    generico y explica un minuto.
    """
    found = _fact_of(last, where)
    if found is None or last.get("outcome") != "failed_before_request":
        return found
    fact, since = found
    coupon_wait = (last.get("reason_code") == COUPON_NOT_READY and step_key in COUPON_WAIT_BY_STEP
                   and (last.get("attempt_number") or 0) <= COUPON_DEFER_ATTEMPTS)
    if coupon_wait:
        fact, retry = COUPON_WAIT_BY_STEP[step_key], COUPON_RETRY_BY_STEP[step_key]
    else:
        fact, retry = GENERIC_FAILURE, GENERIC_RETRY
    started = _optional_instant(last, "started_at", where)
    if started is not None:
        since = min(since, started + retry + RETRY_SLACK)
    return fact, since


def _request_started_step(action: Mapping[str, Any]) -> tuple[datetime | None, str | None, datetime | None]:
    """``_request_started`` de la T1, con los hechos de ``_fact_of_step``."""
    where = f"accion {action['id']}"
    step_key = action.get("step_key") or ""
    explained: tuple[str, datetime] | None = None
    for tramo in _attempts(action):
        first = tramo["primero"]
        if first.get("request_started_at") is not None:
            fact, since = explained if explained is not None else (None, None)
            return _instant(first, "request_started_at", where), fact, since
        found = _fact_of_step(tramo.get("ultimo") or first, step_key, where)
        if found is not None:
            explained = found
    return None, None, None


def _window_actions_v3(capture: CaptureV3) -> Iterator[tuple[Mapping[str, Any], Mapping[str, Any],
                                                             Mapping[str, Any]]]:
    for event, case in _window_cases(capture):
        for action in (capture._index["actions_by_case"].get(case["id"]) or {}).values():
            yield event, case, action


def step_timings(capture: CaptureV3) -> tuple[StepTiming, ...]:
    """Cada envio de un paso de un carrito de la ventana, con su E y su R, en el orden de los disparos."""
    out: list[StepTiming] = []
    for event, case, action in _window_actions_v3(capture):
        started, explained, since = _request_started_step(action)
        if started is None:
            continue
        start, start_by = _step_start(capture, case, action)
        eligible = eligible_at(capture, action, start=start)
        out.append(StepTiming(
            action_id=action["id"], case_id=case["id"], firing_ref=event["id"],
            step_key=action["step_key"], start=start, start_by=start_by, eligible_at=eligible,
            request_started_at=started,
            delay_seconds=None if eligible is None else (started - eligible).total_seconds(),
            explained_by=explained, explained_since=since,
        ))
    return tuple(out)


def _final_motive(action: Mapping[str, Any]) -> str | None:
    """El motivo de un paso que termino sin envio, con los nombres de §2.9."""
    reason = action.get("terminal_reason")
    if reason in COUPON_CLOSE_REASONS:
        return WITHOUT_COUPON
    motive = legacy_motivo(reason)
    if motive is None and action.get("status") == "expired":
        return "vencio"
    return motive


def _absent_step_eligible(capture: CaptureV3, case: Mapping[str, Any], step: Mapping[str, Any],
                          previous_action: Mapping[str, Any]) -> datetime | None:
    """E de un paso que el legado no creo: la primera aceptacion de la secuencia mas su ``delay``, con la
    puerta de ``eligible_at`` y el vencimiento del paso anterior (el paso siguiente lo hereda,
    20260813000100:353-381). Sin accion no hay renovacion: el pedido se ata a una accion."""
    delay = interval_of(step.get("delay"))
    if delay is None:
        raise CaptureError(f"caso {case['id']}: el delay del paso {step.get('step_key')} no se lee")
    start = _first_acceptance(capture, previous_action) + delay
    phantom = {"id": f"{previous_action['id']}:{step.get('step_key')}", "recovery_case_id": case["id"],
               "expires_at": previous_action["expires_at"]}
    return eligible_at(capture, phantom, start=start)


def _absent_step_final(capture: CaptureV3, case: Mapping[str, Any], previous: str,
                       previous_action: Mapping[str, Any] | None, step: Mapping[str, Any] | None = None,
                       tolerance: timedelta = TOLERANCE) -> str:
    """El final de un paso que no llego a crearse: el del anterior, o el cierre del caso o de la secuencia.

    Si el anterior salio y nada lo cerro, el paso espera solo mientras su E mas la tolerancia no paso a
    la hora de la captura; si paso, el legado no lo creo y el motor lo habria mandado
    (``paso_no_creado``). Si la puerta no dice «mandar» antes de vencer, vencio.
    """
    if previous != FINAL_SENT:
        return previous
    if case.get("status") == "won" or case.get("purchase_event_id") is not None:
        return "compro"
    sequence = None
    if previous_action is not None:
        sequence = capture._index["sequences"].get(previous_action.get("followup_sequence_id"))
    if sequence is not None:
        # La secuencia se completa tambien cuando la reevaluacion corta un paso: su
        # completion_reason es el motivo (prospect_replied, purchase_detected).
        for reason in (sequence.get("completion_reason"), sequence.get("cancel_reason")):
            motive = SEQUENCE_CLOSE_MOTIVES.get(reason or "") or legacy_motivo(reason)
            if motive is not None:
                return motive
    if previous_action is not None and previous_action.get("status") == "delivery_unknown":
        return "envio_incierto"
    if case.get("status") in OPEN_CASE_STATUSES:
        if step is None or previous_action is None:
            return FINAL_PENDING
        eligible = _absent_step_eligible(capture, case, step, previous_action)
        if eligible is None:
            return "vencio"
        captured = capture.captured_at
        if captured is not None and eligible + tolerance < captured:
            return FINAL_NOT_CREATED
        # El paso anterior salio y el siguiente todavia no tenia que insertarse: espera.
        return FINAL_PENDING
    return FINAL_WITHOUT_REASON


def step_finals(capture: CaptureV3) -> tuple[tuple[str, str, str], ...]:
    """El final de cada paso de la politica de cada carrito de la ventana: (caso, paso, final)."""
    out: list[tuple[str, str, str]] = []
    for _, case in _window_cases(capture):
        by_step = capture._index["actions_by_case"].get(case["id"]) or {}
        previous, previous_action = FINAL_SENT, None
        for position, step in enumerate(_policy_steps(capture, case)):
            step_key = step.get("step_key") or ""
            action = by_step.get(step_key)
            if action is not None:
                if _sent(action):
                    final = FINAL_SENT
                elif action.get("status") in OPEN_ACTION_STATUSES:
                    final = FINAL_PENDING
                else:
                    final = _final_motive(action) or FINAL_WITHOUT_REASON
            elif position == 0:
                final = FINAL_WITHOUT_REASON
            else:
                final = _absent_step_final(capture, case, previous, previous_action, step)
            out.append((case["id"], step_key, final))
            previous, previous_action = final, action
    return tuple(out)


@dataclass(frozen=True)
class ParityReportV3:
    """Lo que mide la paridad sobre la captura v3. ``ok()`` es cero sin explicar."""

    firings: Mapping[str, int]
    reached_flow: int
    cases: int
    sends: Mapping[str, int]                    # por paso
    delay: Mapping[str, Mapping[str, float]]    # por paso: min, mediana, p95, max
    never_before_violations: tuple[str, ...]
    over_tolerance: tuple[str, ...]
    explained: tuple[Mapping[str, str], ...]    # las demoras y los comienzos explicados por una excepcion
    explained_by: Mapping[str, int]
    finals: Mapping[str, Mapping[str, int]]     # por paso, por final
    census: Mapping[str, int]
    supersessions: int
    supersessions_without_winner: tuple[str, ...]
    winners_without_cancel: tuple[str, ...]
    named: Mapping[str, int]
    named_total: int
    unexplained: tuple[Mapping[str, Any], ...]
    # P6 por la primera ruta que encuentra un ganador: persona, intencion, caso_en_curso o
    # intencion_clasificada (una cota).
    supersessions_by: Mapping[str, int] = field(default_factory=dict)
    # Los casos cuyo disparo la v3 no captura (la ficha): la persona no se compara.
    persons_not_comparable: tuple[str, ...] = ()

    def ok(self) -> bool:
        return not self.unexplained and not self.never_before_violations

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok(),
            "supersessions_by": dict(self.supersessions_by),
            "persons_not_comparable": list(self.persons_not_comparable),
            "firings": dict(self.firings),
            "reached_flow": self.reached_flow,
            "cases": self.cases,
            "sends": dict(self.sends),
            "delay_seconds": {step: dict(values) for step, values in self.delay.items()},
            "never_before_violations": list(self.never_before_violations),
            "over_tolerance": list(self.over_tolerance),
            "explained": [dict(item) for item in self.explained],
            "explained_by": dict(self.explained_by),
            "finals": {step: dict(values) for step, values in self.finals.items()},
            "census": dict(self.census),
            "supersessions": self.supersessions,
            "supersessions_without_winner": list(self.supersessions_without_winner),
            "winners_without_cancel": list(self.winners_without_cancel),
            "named": dict(self.named),
            "named_total": self.named_total,
            "unexplained": [dict(item) for item in self.unexplained],
        }


def _explains_step(capture: CaptureV3, timing: StepTiming, tolerance: timedelta) -> bool:
    """Si el hecho observado explica la demora: R a lo sumo la tolerancia despues de la primera vez que la
    puerta vuelve a decir «mandar» desde el hecho.

    En la T1 la cota era max(E, el hecho). Con la espera del cupon no alcanza: si el reintento cae con la
    franja cerrada (A1 que espera al cupon a las 20:55 de CDMX y reintenta a las 21:05), la reevaluacion
    difiere a las 09:00, y eso no es una demora sin explicar. La puerta se vuelve a mirar desde el hecho.
    """
    if timing.explained_by is None or timing.explained_since is None or timing.eligible_at is None:
        return False
    action = next(row for row in capture.actions if row["id"] == timing.action_id)
    again = eligible_at(capture, action, start=max(timing.eligible_at, timing.explained_since))
    if again is None:
        return False
    # Antes de que la puerta vuelva a decir «mandar» no hay excusa: salio con la puerta cerrada.
    return again - NEVER_BEFORE_SLACK <= timing.request_started_at <= again + tolerance


# Los roles de evento de un caso cuyo disparo la v3 no captura: la ficha (PURCHASE_BILLET_PRINTED,
# 20261019000100). La consulta trae los carritos, los pagos fallidos y las compras.
UNCAPTURED_EVENT_ROLES = frozenset({"payment_slip"})


def _not_comparable(capture: CaptureV3, case: Mapping[str, Any]) -> bool:
    """Un caso cuyo disparo es de un tipo que la v3 no captura: no hay clave del evento que comparar."""
    if capture._index["events"].get(case["abandonment_event_id"]) is not None:
        return False
    roles = {item.get("event_role") for item in case.get("eventos") or ()
             if item.get("webhook_event_id") == case["abandonment_event_id"]}
    return bool(roles) and roles <= UNCAPTURED_EVENT_ROLES


def _person_mismatches_v3(capture: CaptureV3) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    """``_person_mismatches`` de la T1, con los casos de la ficha aparte, como no comparables.

    Un caso de la ficha es un hecho de la persona: entra por su contacto, pero su evento no viene en la
    captura. Cualquier otro caso sin su evento (del carrito o del pago fallido) sigue siendo una
    diferencia.
    """
    mismatches: list[dict[str, str]] = []
    not_comparable: list[str] = []
    for item in _person_mismatches(capture):
        if _not_comparable(capture, capture._index["cases"][item["ref"]]):
            not_comparable.append(item["ref"])
        else:
            mismatches.append(item)
    return mismatches, tuple(not_comparable)


def _unsent_differences_v3(capture: CaptureV3, tolerance: timedelta) -> Iterator[dict[str, str]]:
    """Los pasos sin envio a los que la puerta les dice «mandar» antes de que terminaran (P3)."""
    for _, case, action in _window_actions_v3(capture):
        if _sent(action):
            continue
        start, _ = _step_start(capture, case, action)
        eligible = eligible_at(capture, action, start=start)
        if eligible is None:
            continue
        where = f"accion {action['id']}"
        if action.get("status") in OPEN_ACTION_STATUSES:
            end = capture.captured_at
            if end is None:
                continue
        else:
            end = _instant(action, "updated_at", where)
        if eligible + tolerance < end:
            yield {
                "ref": action["id"], "diferencia": "elegible_sin_envio", "paso": action["step_key"],
                "detalle": (f"E={_text(eligible)} sin envio; la accion termino {action.get('status')}"
                            f" ({action.get('terminal_reason') or 'sin motivo'})"),
            }


def parity_report_v3(capture: CaptureV3, named: Sequence[Mapping[str, Any]], *,
                     tolerance: timedelta = TOLERANCE) -> ParityReportV3:
    """Mide P3 por paso, los finales y P6 sobre la captura v3, y cruza las diferencias con las que tienen
    nombre (con las reglas de ``parity_report``; en P3, el ``disparo`` de una diferencia es la accion)."""
    if not isinstance(tolerance, timedelta) or tolerance < timedelta(0):
        raise ValueError("tolerance_invalid")
    firing_counts: Counter[str] = Counter()
    reached = 0
    for event in capture.firings():
        firing_counts[f"{FIRING_KIND[event['event_type']]}/{_firing_result(capture, event)}"] += 1
        reached += _reached_flow(capture, event)

    differences: list[dict[str, str]] = []
    timings = step_timings(capture)
    never_before: list[str] = []
    over: list[str] = []
    explained_items: list[dict[str, str]] = []
    delays: dict[str, list[float]] = {}
    sends: Counter[str] = Counter()
    for timing in timings:
        sends[timing.step_key] += 1
        if timing.start_by == "renovacion":
            explained_items.append({"ref": timing.action_id, "paso": timing.step_key,
                                    "excepcion": RENEWAL_ADVANCES})
        if timing.eligible_at is None or timing.delay_seconds is None:
            differences.append({
                "ref": timing.action_id, "diferencia": "envio_sin_E", "paso": timing.step_key,
                "detalle": f"R={_text(timing.request_started_at)} y la puerta no dice mandar antes de vencer",
            })
            continue
        delays.setdefault(timing.step_key, []).append(timing.delay_seconds)
        detail = (f"E={_text(timing.eligible_at)} R={_text(timing.request_started_at)}"
                  f" R-E={timing.delay_seconds:.3f}s")
        if timing.request_started_at < timing.eligible_at - NEVER_BEFORE_SLACK:
            never_before.append(timing.action_id)
            differences.append({"ref": timing.action_id, "diferencia": NEVER_BEFORE,
                                "paso": timing.step_key, "detalle": detail})
        elif timing.delay_seconds > tolerance.total_seconds():
            over.append(timing.action_id)
            if _explains_step(capture, timing, tolerance):
                explained_items.append({"ref": timing.action_id, "paso": timing.step_key,
                                        "excepcion": timing.explained_by or ""})
            else:
                if timing.explained_by is not None:
                    detail += (f"; {timing.explained_by} explica hasta"
                               f" {_text(timing.explained_since)} y no alcanza")
                differences.append({"ref": timing.action_id, "diferencia": "envio_fuera_de_tolerancia",
                                    "paso": timing.step_key, "detalle": detail})
    differences.extend(_unsent_differences_v3(capture, tolerance))

    finals = step_finals(capture)
    by_step: dict[str, Counter[str]] = {}
    for case_id, step_key, final in finals:
        by_step.setdefault(step_key, Counter())[final] += 1
        if final == FINAL_WITHOUT_REASON:
            differences.append({"ref": case_id, "diferencia": "final_sin_motivo", "paso": step_key,
                                "detalle": f"el paso {step_key} no salio y no tiene un motivo"})
        elif final == FINAL_NOT_CREATED:
            differences.append({"ref": case_id, "diferencia": FINAL_NOT_CREATED, "paso": step_key,
                                "detalle": (f"el paso {step_key} no se creo y su E mas la tolerancia ya habia"
                                            " pasado a la hora de la captura")})
    cases = list(_window_cases(capture))
    expected_steps = sum(len(_policy_steps(capture, case)) for _, case in cases)
    sent_steps = sum(1 for _, _, final in finals if final == FINAL_SENT)
    census = {"casos": len(cases), "pasos_esperados": expected_steps, "pasos_clasificados": len(finals),
              "enviados": sent_steps, "envios_de_p3": len(timings)}
    if len(finals) != expected_steps or sent_steps != len(timings):
        differences.append({"ref": str(capture.meta.get("query")), "diferencia": "censo_no_cierra",
                            "detalle": f"{census}"})

    ceded = supersessions(capture)
    without_winner = tuple(item.plan_ref for item in ceded if item.winner_ref is None)
    for item in ceded:
        if item.winner_ref is None:
            differences.append({"ref": item.plan_ref, "diferencia": "cesion_sin_ganador",
                                "detalle": f"cedio a las {_text(item.stopped_at)} sin un disparo que le gane"})
    unexpected = winners_without_cancel(capture)
    for firing_ref, plan_ref in unexpected:
        differences.append({"ref": firing_ref, "diferencia": "ganador_sin_cancelacion",
                            "detalle": f"le gana al primer contacto {plan_ref}, que no se cancelo"})
    mismatches, not_comparable = _person_mismatches_v3(capture)
    differences.extend(mismatches)

    observed = {(item["ref"], item["diferencia"]) for item in differences}
    explained: set[tuple[str, str]] = set()
    problems: list[dict[str, str]] = []
    named_counts: Counter[str] = Counter()
    for position, entry in enumerate(named):
        reason = _named_problem(entry, DIFFERENCE_KINDS_V3)
        disparo = entry.get("disparo") if isinstance(entry, Mapping) else None
        ref = disparo if isinstance(disparo, str) else f"nombrada[{position}]"
        if reason is not None:
            problems.append({"ref": ref, "diferencia": f"nombrada_invalida:{reason}",
                             "detalle": f"la diferencia con nombre {position} no es valida"})
            continue
        key = (entry["disparo"], entry["diferencia"])
        if key in explained:
            problems.append({"ref": ref, "diferencia": "nombrada_repetida",
                             "detalle": f"la diferencia con nombre {position} repite disparo y clase"})
        elif key not in observed:
            problems.append({"ref": ref, "diferencia": "nombrada_sin_diferencia",
                             "detalle": f"{entry['nombre']}: la paridad no observa esa diferencia"})
        else:
            explained.add(key)
            named_counts[entry["nombre"]] += 1
    unexplained = [
        item for item in differences
        if item["diferencia"] == NEVER_BEFORE or (item["ref"], item["diferencia"]) not in explained
    ] + problems

    delay = {
        step: MappingProxyType({"min": min(values), "mediana": statistics.median(values),
                                "p95": _percentile(values, 0.95), "max": max(values)})
        for step, values in sorted(delays.items())
    }
    return ParityReportV3(
        firings=MappingProxyType(dict(sorted(firing_counts.items()))),
        reached_flow=reached,
        cases=len(cases),
        sends=MappingProxyType(dict(sorted(sends.items()))),
        delay=MappingProxyType(delay),
        never_before_violations=tuple(never_before),
        over_tolerance=tuple(over),
        explained=tuple(MappingProxyType(item) for item in explained_items),
        explained_by=MappingProxyType(dict(sorted(Counter(item["excepcion"] for item in explained_items).items()))),
        finals=MappingProxyType({step: MappingProxyType(dict(sorted(values.items())))
                                 for step, values in sorted(by_step.items())}),
        census=MappingProxyType(census),
        supersessions=len(ceded),
        supersessions_without_winner=without_winner,
        winners_without_cancel=tuple(firing for firing, _ in unexpected),
        named=MappingProxyType(dict(sorted(named_counts.items()))),
        named_total=sum(named_counts.values()),
        unexplained=tuple(MappingProxyType(item) for item in unexplained),
        persons_not_comparable=not_comparable,
        supersessions_by=MappingProxyType({
            route: count for route, count in Counter(
                item.matched_by for item in ceded if item.matched_by is not None).items()}),
    )
