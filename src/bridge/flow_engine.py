"""El modelo del motor de flujos: las capacidades, el plan, el arranque y la puerta.

Diseno de la T3a (2026-10-17), §3.1, sobre el diseno del motor de flujos
(2026-10-16), §1.0, §2.8 a §2.10. Es la gemela en Python de lo que la base
decide para un flujo del motor; la simulacion (``flow_replay``) la llama
sobre la captura de ATT1, y la T4 la va a armar desde la base.

**Lo que decide, y lo que recibe como hecho.** Decide solo lo que dice la
definicion: si arranca (el permiso pedido, la repeticion,
``no_arranca_mientras`` y si otro flujo le gana), el plan (las esperas, la
plantilla, el plazo y la bolsa) y, en la puerta, el plazo, el horario, los
topes, el estado del flujo y las salidas escritas. Todo lo demas es un hecho
con hora (``GateFacts``): la baja, la compra, la toma humana, la respuesta,
el conflicto de compra y la vida de la intencion. El control y la bolsa
llegan como protocolos (``ControlLine`` y ``BolsaLedger``), para que la
captura, la base o una prueba los armen.

**La puerta** (``evaluate_gate``) sigue el orden de la reevaluacion compartida
(``20260805000300_per_case_conversation_anchor.sql:256-404``) y despues el de
la autorizacion (``20260930000300_pilot_scope_audience_mode.sql:459-640``), con
las excepciones de capa ``sql`` y ``despacho`` del catalogo
(``flow_parity.EXCEPTIONS_V1``) aplicadas a todo flujo del motor. El orden lo
fija ``tests/fixtures/flow_simulation/gate_cases.json``. Dos diferencias con
el legado, a proposito:

* **la version activa no frena** a un recorrido en curso (decision 3; el
  ciclo, ``flow-change-cycle-v1.md:154-159``): el control se mira solo por su
  estado, como en ``flow_parity.eligible_at``;
* **la primera hora elegible** sale de ``first_eligible``, que junta el control,
  la franja y la bolsa como lo hace ``flow_parity.eligible_at``: cada condicion
  que falta da la primera hora en que puede cumplirse y se vuelve a mirar todo
  desde la mayor.

**Varios toques.** Desde la T2.1 (2026-10-21), ``ENGINE_CAPABILITIES_V1`` manda
hasta tres envios por camino, con la espera entre uno y otro: los de la forma
de su flujo del legado (``flow_policy.STEPS_BY_LEGACY_KIND``; el carrito de ATT1
tiene tres). La vista previa del lienzo (``PREVIEW_CAPABILITIES``) no tiene
limite. Los dos siguen la regla del legado
(``20260813000100_absolute_followup_deadlines.sql``): cada paso despues del
primero se mide desde el primer envio aceptado (``:337-350``) y todos comparten
el vencimiento del primero (``:355``, ``:380``).

Es puro: no lee el entorno, no hace red ni toca bases. No importa
``flow_parity``: el orden de los imports es ``flow_parity -> flow_engine ->
flow_schedule / flow_definition``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Protocol

from .flow_definition import (
    DEFAULT_VARIABLES,
    MAX_DEPTH,
    NEVER,
    NO_CAP,
    TO_CREATE,
    is_pool,
    parse_duration,
    parse_flow_ref,
    step_type,
    trigger_of,
)
from .flow_policy import STEPS_BY_LEGACY_KIND
from .flow_schedule import DEFAULT_ZONE, LAST_MINUTE, Franja, franja_of, is_open, next_open

__all__ = [
    "ENGINE_CAPABILITIES_V1", "LEGACY_REASON_TO_MOTIVO", "PREVIEW_CAPABILITIES", "PROPOSED_TO_CONTRACT_OWNER",
    "BolsaLedger", "ControlLine", "Eligible", "EngineCapabilities", "EngineFlow", "Firing", "FlowRef",
    "GateDecision", "GateFacts", "PersonView", "Plan", "PlanInvalid", "PlanNotSupported", "PlannedStep",
    "RunView", "StartDecision", "evaluate_gate", "first_eligible", "not_supported", "plan_start",
    "plan_steps", "pool_zone", "step_start", "wins",
]

NOT_SUPPORTED = "engine_not_supported"
# Mas envios por camino de los que ejecuta el motor (las capacidades).
TOO_MANY_SENDS = "demasiados_envios"
# El estado del control que deja arrancar (pilot_runtime_controls.runtime_state).
ARMED = "armed"
# El motivo de un control que no esta armado (§2.9 del diseno): solo paused es
# flujo_pausado; closed es el flujo apagado; inactive (una version activada
# sin armar) no tiene motivo en el contrato.
CONTROL_REASON: Mapping[str, str | None] = MappingProxyType({
    "paused": "flujo_pausado",
    "closed": "flujo_apagado",
    "inactive": None,
})
# Las excepciones del catalogo (flow_parity.EXCEPTIONS_V1) que cambian lo que
# decide la puerta. Las de capa sql y despacho las tiene todo flujo del motor
# (§2.8); sin ellas, la puerta hace lo que dice el esquema.
TAKEOVER_ONLY_IN_ITS_CONVERSATION = "toma_humana_en_su_conversacion"
NO_WINDOW_ESCALATES = "sin_horario_escala"
LIVE_INTENT = "intencion_viva"
PURCHASE_CONFLICT_STOPS_ALL = "conflicto_de_compra_frena_todo"
# Las decisiones de la puerta.
DECISIONS = ("mandar", "esperar", "cancelar", "pausar", "escalar", "vencer")
_MAX_STEPS = 10_000


def _require_aware(at: datetime) -> None:
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("instant_without_zone")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Las capacidades (§2.10 del diseno). La regla 14 de la T2 las importa.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EngineCapabilities:
    """Lo que el motor ejecuta. Todo lo demas es «todavia no» (``engine_not_supported``)."""

    triggers: frozenset[str]          # "carrito", "pago_fallido", "intencion", "silencio:agente", "silencio:persona"
    step_types: frozenset[str]        # "esperar", "plantilla"
    max_sends_per_path: int | None    # 3 en la v1; None (sin limite) solo en la vista previa del lienzo
    waits_between_sends: bool         # una espera despues de un envio y antes del siguiente
    variables: frozenset[str]         # "nombre", "producto"
    buttons: frozenset[str]           # "checkout"
    exits: frozenset[str]             # "compra", y "contesta" solo con un silencio
    relations_between_engine_flows_only: bool


ENGINE_CAPABILITIES_V1 = EngineCapabilities(
    triggers=frozenset({"carrito", "pago_fallido", "intencion", "silencio:agente", "silencio:persona"}),
    step_types=frozenset({"esperar", "plantilla"}),
    max_sends_per_path=3,
    waits_between_sends=True,
    variables=frozenset({"nombre", "producto"}),
    buttons=frozenset({"checkout"}),
    exits=frozenset({"compra", "contesta"}),
    relations_between_engine_flows_only=True,
)
# La v1 sin limite de envios por camino, y nada mas distinto: la vista previa del
# lienzo de flujos (diseno de la T3a, §5.3 y §6).
PREVIEW_CAPABILITIES = dataclasses.replace(ENGINE_CAPABILITIES_V1, max_sends_per_path=None)


def _max_sends(steps: object, depth: int = 0) -> int:
    """Los envios del camino que mas tiene: cada plantilla y cada conversar, y de una condicion la rama con mas.

    Copiada de ``flow_validator._max_sends`` (``flow_validator.py:537-548``), que
    es privada; la ata con la politica la tabla de equivalencias de
    ``tests/test_flow_engine_shared_cases.py``.
    """
    if not isinstance(steps, list) or depth > MAX_DEPTH:
        return 0
    total = 0
    for step in steps:
        kind = step_type(step)
        if kind in ("plantilla", "conversar"):
            total += 1
        elif kind == "condicion":
            total += max(_max_sends(step.get(name), depth + 1) for name in ("entonces", "si_no"))
    return total


def _walk_steps(steps: object, depth: int = 0) -> list[Mapping[str, Any] | object]:
    """Todos los pasos, ramas de una condicion incluidas, en el orden en que se escriben."""
    if not isinstance(steps, list) or depth > MAX_DEPTH:
        return []
    out: list[Mapping[str, Any] | object] = []
    for step in steps:
        out.append(step)
        if step_type(step) == "condicion":
            for name in ("entonces", "si_no"):
                out.extend(_walk_steps(step.get(name), depth + 1))
    return out


def not_supported(definition: Mapping[str, Any], capabilities: EngineCapabilities = ENGINE_CAPABILITIES_V1
                  ) -> tuple[str, ...]:
    """Los codigos engine_not_supported:<que> de la definicion, en orden; vacio si el motor la ejecuta.

    Los envios por camino se cuentan con la regla de flow_validator._max_sends (flow_validator.py:537-548:
    cada plantilla y cada conversar, y de una condicion la rama con mas), copiada, no importada.

    Mira, en este orden: el disparador, el tipo de cada paso (con sus variables
    y su boton), una espera despues de un envio (entre dos envios, solo si las
    capacidades no la admiten; despues del ultimo, siempre), los envios por
    camino y las salidas. Lo que la definicion no trae no se marca: la forma la mira la
    regla 1. Las relaciones con un flujo del legado necesitan el mundo, y las
    mira la regla 14 (``relation_with_legacy_flow``), no esta funcion.
    """
    found: list[str] = []

    def add(what: str) -> None:
        code = f"{NOT_SUPPORTED}:{what}"
        if code not in found:
            found.append(code)

    trigger = trigger_of(definition) if isinstance(definition.get("dispara"), Mapping) else None
    if "dispara" in definition:
        if trigger is None:
            add("disparo_desconocido")
        elif trigger.kind == "silencio":
            silence = definition["dispara"].get("silencio")
            speaker = silence.get("lo_ultimo") if isinstance(silence, Mapping) else None
            if f"silencio:{speaker}" not in capabilities.triggers:
                add("disparo_silencio")
        elif trigger.event not in capabilities.triggers:
            add(f"disparo_{trigger.event or 'desconocido'}")
    steps = definition.get("pasos")
    for step in _walk_steps(steps):
        kind = step_type(step)
        if kind not in capabilities.step_types:
            add(kind or "desconocido")
            continue
        if kind == "plantilla":
            assert isinstance(step, Mapping)
            for variable in step.get("variables") or ():
                if variable not in capabilities.variables:
                    add(f"variable_{variable}")
            button = step.get("boton")
            if isinstance(button, Mapping) and button.get("link") not in capabilities.buttons:
                add(f"boton_{button.get('link')}")
    if isinstance(steps, list):
        # Una espera despues del ultimo envio no lleva a nada; una entre dos envios
        # vale si las capacidades la admiten.
        sends_seen = 0
        wait_after_send = False
        for step in steps:
            kind = step_type(step)
            if kind in ("plantilla", "conversar"):
                sends_seen += 1
                wait_after_send = False
            elif kind == "esperar" and sends_seen:
                wait_after_send = True
                if not capabilities.waits_between_sends:
                    add("espera_despues_del_envio")
        if wait_after_send:
            add("espera_despues_del_envio")
    if capabilities.max_sends_per_path is not None and _max_sends(steps) > capabilities.max_sends_per_path:
        add(TOO_MANY_SENDS)
    ends = definition.get("termina")
    exits = ends.get("si") if isinstance(ends, Mapping) else None
    for exit_name in exits if isinstance(exits, list) else ():
        if exit_name not in capabilities.exits:
            add(f"salida_{exit_name}")
        elif exit_name == "contesta" and trigger is not None and trigger.kind == "evento":
            # Con un disparo de evento, contesta es «todavia no» (diseno, §2.10).
            add("contesta_con_disparo_de_evento")
    return tuple(found)


class PlanNotSupported(ValueError):
    """La definicion trae algo que las capacidades no ejecutan. ``codes`` son los de ``not_supported``."""

    def __init__(self, codes: tuple[str, ...]) -> None:
        super().__init__(codes[0] if codes else NOT_SUPPORTED)
        self.codes = codes


class PlanInvalid(ValueError):
    """La definicion no tiene la forma que el plan lee (un horario, una espera o un vence que no se leen).

    ``str(error)`` es el codigo: ``plan_horario_invalid``, ``plan_wait_invalid``,
    ``plan_without_send``, ``plan_vence_invalid`` o ``plan_topes_invalid``.
    """


# ---------------------------------------------------------------------------
# El flujo, el disparo y el plan.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EngineFlow:
    """Una version de un flujo que ejecuta el motor."""

    flow_id: str
    product_ref: str
    flow_key: str
    version: int
    definition: Mapping[str, Any]     # publicada y normalizada (flow_definition.as_published, :982)
    legacy_kind: str | None
    fixed_zone: str | None            # WorldFlow.fixed_zone (flow_definition.py:764-772)
    pool: str
    exceptions: frozenset[str]

    @property
    def ref(self) -> FlowRef:
        return (self.product_ref, self.flow_key)


FlowRef = tuple[str, str]             # (product_ref, flow_key)


@dataclass(frozen=True)
class Firing:
    """Un disparo: el evento que puede arrancar un recorrido."""

    source_ref: str                   # el id interno del evento, el firing_ref de send_timings
    event: str                        # "carrito" | "pago_fallido" | "intencion"
    at: datetime                      # creation_date (resolution.py:283-286) o submitted_at
    received_at: datetime
    product_ref: str
    offer_ref: str | None
    contact_ref: str | None           # el contacto del caso (excepcion 6); None si no se sabe
    intent_refs: frozenset[str]       # las correlaciones resueltas: hasta dos
    permission: str | None            # el permiso que tiene la persona (entra.permiso), o None


@dataclass(frozen=True)
class PlannedStep:
    """Un envio del plan."""

    index: int                        # 0, 1, 2...
    after: timedelta                  # el paso 0, desde el disparo; los demas, desde el primer envio
                                      # aceptado (la regla del legado, 20260813000100:337-350). No es opcional.
    template: str
    language: str
    variables: tuple[str, ...]
    button: str | None
    step_key: str | None              # el del legado en ese indice (flow_policy.STEPS_BY_LEGACY_KIND), o None


@dataclass(frozen=True)
class Plan:
    """Lo que un recorrido va a hacer, segun la definicion de su version."""

    steps: tuple[PlannedStep, ...]    # hasta tres con ENGINE_CAPABILITIES_V1
    expires_after: timedelta          # desde el disparo, el mismo para todos los pasos (20260813000100:355, :380)
    franja: Franja
    pool: str
    per_day: int | None               # None con sin_tope (flow_definition.py:77)
    total: int | None
    exits: frozenset[str]


def _cap(caps: Mapping[str, Any], key: str) -> int | None:
    value = caps.get(key)
    if value == NO_CAP:
        return None
    if not _is_int(value) or value < 1:
        raise PlanInvalid("plan_topes_invalid")
    return value


def _legacy_step_key(legacy_kind: str | None, index: int) -> str | None:
    """El ``step_key`` del paso ``index`` de la forma del flujo del legado, o None si no tiene."""
    shape = STEPS_BY_LEGACY_KIND.get(legacy_kind or "", ())
    return shape[index].step_key if index < len(shape) else None


def plan_steps(flow: EngineFlow, firing: Firing, *,
               capabilities: EngineCapabilities = ENGINE_CAPABILITIES_V1) -> Plan:
    """El plan de la definicion de ``flow`` para ``firing``.

    No compila la politica: ``compile_policy`` rechaza los envios de mas sobre
    la forma de su flujo del legado, y la vista previa necesita N. Usa los mismos
    ayudantes (``franja_of``, ``parse_duration``), y los ata
    ``tests/test_flow_engine_shared_cases.py``. Mira, en este orden, el
    horario, lo que las capacidades no ejecutan (``PlanNotSupported``), las
    esperas y los envios, el vence y los topes; el primero que falla es el
    error. El flow_id, la version y el legacy_kind no los mira: los valida el
    almacen.

    Las esperas antes del primer envio se suman y se cuentan desde el disparo.
    Despues, cada envio se cuenta desde el primero: su ``after`` es la suma de
    las esperas entre el primero y el. Es la regla del legado (cada toque a
    ``min(accepted_at) + delay``), no la del esquema (cada espera desde la
    anterior): las dos dan lo mismo mientras ningun toque salga tarde.

    El plan de la v1 no depende del disparo: ``firing`` queda en la firma para
    la T4, que arma el plan desde la base.
    """
    definition = flow.definition
    try:
        franja = franja_of(definition.get("horario"), fixed_zone=flow.fixed_zone)
    except ValueError as error:
        raise PlanInvalid("plan_horario_invalid") from error
    codes = not_supported(definition, capabilities)
    if codes:
        raise PlanNotSupported(codes)
    steps = definition.get("pasos")
    if not isinstance(steps, list):
        raise PlanInvalid("plan_without_send")
    planned: list[PlannedStep] = []
    waited = 0
    for step in steps:
        kind = step_type(step)
        if kind == "esperar":
            minutes = parse_duration(step["esperar"])
            if minutes is None:
                raise PlanInvalid("plan_wait_invalid")
            waited += minutes
            continue
        assert kind == "plantilla"  # not_supported ya freno los demas tipos
        template = step["plantilla"]
        if isinstance(template, Mapping):
            template = template.get(TO_CREATE)
        button = step.get("boton")
        planned.append(PlannedStep(
            index=len(planned),
            after=timedelta(minutes=waited),
            template=template if isinstance(template, str) else "",
            language=step.get("idioma") if isinstance(step.get("idioma"), str) else "",
            variables=tuple(step.get("variables") or DEFAULT_VARIABLES),
            button=button.get("link") if isinstance(button, Mapping) else None,
            step_key=_legacy_step_key(flow.legacy_kind, len(planned)),
        ))
        if len(planned) == 1:
            waited = 0
    if not planned:
        raise PlanInvalid("plan_without_send")
    ends = definition.get("termina")
    deadline = parse_duration(ends.get("vence")) if isinstance(ends, Mapping) else None
    if deadline is None or deadline < 1:
        raise PlanInvalid("plan_vence_invalid")
    caps = definition.get("topes")
    if not isinstance(caps, Mapping):
        raise PlanInvalid("plan_topes_invalid")
    pool = caps.get("bolsa", flow.pool)
    if not is_pool(pool):
        raise PlanInvalid("plan_topes_invalid")
    exits = ends.get("si") if isinstance(ends, Mapping) else None
    return Plan(
        steps=tuple(planned),
        expires_after=timedelta(minutes=deadline),
        franja=franja,
        pool=pool,
        per_day=_cap(caps, "por_dia"),
        total=_cap(caps, "total"),
        exits=frozenset(exits) if isinstance(exits, list) else frozenset(),
    )


# ---------------------------------------------------------------------------
# La persona y el arranque.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunView:
    """Un recorrido, visto desde la persona y la puerta."""

    run_ref: str
    flow: FlowRef
    version: int
    firing: Firing
    started_at: datetime              # desde cuando el paso 0 mira la puerta: el disparo mas su espera;
                                      # lo adelanta F#11 (20260904000100:142-154)
    sends: tuple[datetime, ...]       # vacio antes del primer envio
    closed_at: datetime | None        # None: en curso (espera, pausado, abierto para siempre)

    def open_at(self, at: datetime) -> bool:
        """En curso en ``at``: ya llego su disparo y todavia no se cerro."""
        return self.firing.received_at <= at and (self.closed_at is None or self.closed_at > at)


@dataclass(frozen=True)
class PersonView:                     # por contacto y producto (principio 13)
    runs: tuple[RunView, ...]
    winners_fired: tuple[Firing, ...]


@dataclass(frozen=True)
class StartDecision:
    starts: bool
    reason: str | None                # §2.9: ya_en_curso, no_arranca_mientras:<flujo>, no_entra:permiso, le_gano:<flujo>
    joins_run: str | None             # la repeticion que se suma como evidencia (F#11)


def _same_key(unit: object, run: RunView, firing: Firing) -> bool:
    """Si el recorrido es de la misma clave de repeticion que el disparo.

    La persona ya es el contacto y el producto (``PersonView``). Con
    ``persona_y_oferta`` mira ademas la oferta; con ``persona`` y con
    ``conversacion`` (la de un silencio, que la v1 no simula) no.
    """
    return unit != "persona_y_oferta" or run.firing.offer_ref == firing.offer_ref


def plan_start(flow: EngineFlow, firing: Firing, person: PersonView, *,
               flows: Mapping[FlowRef, EngineFlow]) -> StartDecision:
    """Si el disparo arranca un recorrido de ``flow``, con la persona hasta ese instante.

    Mira, en este orden (el contrato de lectura, «Los motivos»):

    1. el permiso que pide ``entra.permiso``: si la persona no lo tiene,
       ``no_entra:permiso``;
    2. la repeticion (``topes.repeticion``): un recorrido del mismo flujo, de la
       misma clave, en curso cuando llega el disparo, es ``ya_en_curso`` y el
       disparo se suma a el (``joins_run``, F#11); uno cerrado frena si no paso
       ``otra_despues_de`` desde su ultimo envio, o siempre con ``nunca``;
    3. ``prioridad.no_arranca_mientras``: un recorrido en curso de ese flujo, de
       la misma clave, es ``no_arranca_mientras:<flujo>``;
    4. si un flujo que le gana (``prioridad.le_gana_a`` de otro) ya disparo para
       la persona: ``le_gano:<flujo>``.

    Sin contacto, la persona no se conoce: no mira la repeticion ni
    ``no_arranca_mientras``, y el replay lo anota como algo que no sabe. El
    estado del flujo no lo mira: un flujo apagado no tiene version (su
    disparo ni se registra) y el pausado lo ve la puerta por el control.
    """
    if firing.product_ref != flow.product_ref:
        raise ValueError("firing_product_differs_from_flow")
    definition = flow.definition
    entry = definition.get("entra")
    required = entry.get("permiso") if isinstance(entry, Mapping) else None
    if required is not None and firing.permission != required:
        return StartDecision(False, "no_entra:permiso", None)
    caps = definition.get("topes") if isinstance(definition.get("topes"), Mapping) else {}
    repeat = caps.get("repeticion") if isinstance(caps.get("repeticion"), Mapping) else {}
    unit = repeat.get("una_por")
    arrived = firing.received_at
    if firing.contact_ref is not None:
        own = [run for run in person.runs
               if run.flow == flow.ref and run.firing.received_at <= arrived and _same_key(unit, run, firing)]
        for run in own:
            if run.open_at(arrived):
                return StartDecision(False, "ya_en_curso", run.run_ref)
        again = repeat.get("otra_despues_de")
        for run in own:
            if again == NEVER:
                return StartDecision(False, "ya_en_curso", None)
            minutes = parse_duration(again)
            if minutes and run.sends and run.sends[-1] + timedelta(minutes=minutes) > arrived:
                return StartDecision(False, "ya_en_curso", None)
        priority = definition.get("prioridad") if isinstance(definition.get("prioridad"), Mapping) else {}
        for value in priority.get("no_arranca_mientras") or ():
            other = parse_flow_ref(value, flow.product_ref)
            if other is None:
                continue
            if any(run.flow == other and run.open_at(arrived) and _same_key(unit, run, firing)
                   for run in person.runs):
                return StartDecision(False, f"no_arranca_mientras:{other[1]}", None)
    for winner_flow in flows.values():
        if winner_flow.ref == flow.ref:
            continue
        priority = winner_flow.definition.get("prioridad")
        beaten = priority.get("le_gana_a") if isinstance(priority, Mapping) else None
        if not any(parse_flow_ref(value, winner_flow.product_ref) == flow.ref for value in beaten or ()):
            continue
        trigger = trigger_of(winner_flow.definition)
        if trigger is None or trigger.event is None:
            continue
        if any(fired.event == trigger.event and fired.product_ref == winner_flow.product_ref
               and fired.received_at <= arrived for fired in person.winners_fired):
            return StartDecision(False, f"le_gano:{winner_flow.flow_key}", None)
    return StartDecision(True, None, None)


def wins(winner: Firing, loser_contact: str | None, loser_intent: str | None, *, route: str,
         product_ref: str, run_open_at: Callable[[str, datetime], bool], at: datetime | None) -> bool:
    """Por persona: el recorrido del disparo es del contacto y del producto, y esta en curso en at.
    Con at None alcanza con que el recorrido exista y run_open_at no se llama (flow_parity.py:798-799, :813):
    es como lo llama winners_without_cancel (:897), que despues vuelve a mirar con la hora (:904).
    Por intencion: loser_intent esta en winner.intent_refs.

    Es la regla de ``le_gana_a`` (``_portable_precheckout_stop_reason``,
    ``20261001000200:293-313``), copiada de ``flow_parity._wins``
    (``flow_parity.py:793-813``). ``run_open_at`` recibe el ``source_ref`` del
    disparo y la hora; el contacto del disparo es el de su caso
    (``winner.contact_ref``), y sin caso no gana por persona.
    """
    if winner.product_ref != product_ref:
        return False
    if route == "intencion":
        return loser_intent is not None and loser_intent in winner.intent_refs
    if route != "persona" or loser_contact is None:
        return False
    if winner.contact_ref is None or winner.contact_ref != loser_contact:
        return False
    return at is None or run_open_at(winner.source_ref, at)


# ---------------------------------------------------------------------------
# La puerta.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateFacts:                      # los hechos con hora que no decide la definicion (§2.10)
    blocked_at: datetime | None       # la baja
    purchase_at: datetime | None      # la compra, segun la salida compra y sus excepciones
    takeover_at: datetime | None
    replied_at: datetime | None       # solo despues de un envio
    purchase_conflict_open: bool      # 4c
    intent_alive_until: datetime | None   # 4: None si no se sabe
    unknown: frozenset[str]           # lo que la historia no trae (§4.3): "baja", "respuesta", "compra"...


NO_FACTS = GateFacts(None, None, None, None, False, None, frozenset())


class ControlLine(Protocol):
    def state_at(self, at: datetime) -> str: ...
    def next_armed(self, at: datetime) -> datetime | None: ...


class BolsaLedger(Protocol):
    def counts_before(self, at: datetime, zone: str) -> tuple[int, int]: ...
    # (total, del dia local). El libro observado cuenta < at; el predicho, <= at con lo ya concedido (§2.2).


@dataclass(frozen=True)
class Eligible:
    at: datetime | None               # E, o None si no llega antes de vencer
    moved_by: tuple[str, ...]         # "control", "franja", "bolsa_dia", en el orden en que la movieron
    stopped_by: str | None            # "bolsa_total", "control_sin_volver", "sin_apertura", "vencio"


def _midnight(zone: str) -> Franja:
    """La franja de 00:00: su proxima apertura es la proxima medianoche local."""
    return Franja(always=False, zone=zone, start_minute=0, end_minute=LAST_MINUTE)


def first_eligible(start: datetime, expires: datetime, *, franja: Franja, control: ControlLine,
                   bolsa: BolsaLedger, zone: str, per_day: int | None, total: int | None) -> Eligible:
    """E: el primer instante >= start con el control armado, la franja abierta y la bolsa con cupo, antes de vencer.

    Como ``flow_parity.eligible_at`` (``flow_parity.py:568-606``), sobre
    protocolos: cada condicion que falta da la primera hora en que puede
    cumplirse (el proximo ``armed``, la proxima apertura, la proxima medianoche
    local de ``zone``) y se vuelve a mirar todo desde la mayor. Con el tope
    total agotado no vuelve: las autorizaciones no bajan. ``moved_by`` dice que
    movio la hora, en orden; dos que la mueven a la misma hora van por nombre.
    """
    _require_aware(start)
    _require_aware(expires)
    at = start
    moved: list[str] = []
    for _ in range(_MAX_STEPS):
        if at >= expires:
            return Eligible(None, tuple(moved), "vencio")
        waits: list[tuple[datetime, str]] = []
        if control.state_at(at) != ARMED:
            armed = control.next_armed(at)
            if armed is None:
                return Eligible(None, tuple(moved), "control_sin_volver")
            waits.append((armed, "control"))
        if not is_open(franja, at):
            opens = next_open(franja, at)
            if opens is None:
                return Eligible(None, tuple(moved), "sin_apertura")
            waits.append((opens, "franja"))
        total_count, daily_count = bolsa.counts_before(at, zone)
        if total is not None and total_count >= total:
            return Eligible(None, tuple(moved), "bolsa_total")
        if per_day is not None and daily_count >= per_day:
            midnight = next_open(_midnight(zone), at)
            assert midnight is not None
            waits.append((midnight, "bolsa_dia"))
        if not waits:
            return Eligible(at, tuple(moved), None)
        at = max(when for when, _ in waits)
        moved.extend(why for when, why in sorted(waits, key=lambda item: item[1]) if when == at)
    raise ValueError("first_eligible_does_not_converge")  # pragma: no cover


@dataclass(frozen=True)
class GateDecision:
    decision: str                     # "mandar" | "esperar" | "cancelar" | "pausar" | "escalar" | "vencer"
    reason: str | None                # la lista cerrada de §2.9
    next_at: datetime | None          # con "esperar": la hora en que vuelve a mirar
    eligible: Eligible | None


def _happened(at: datetime | None, now: datetime) -> bool:
    return at is not None and at <= now


def _allowed_sends(plan: Plan, capabilities: EngineCapabilities) -> int:
    if capabilities.max_sends_per_path is None:
        return len(plan.steps)
    return min(len(plan.steps), capabilities.max_sends_per_path)


def pool_zone(flow: EngineFlow, plan: Plan) -> str:
    """La zona del dia local de la bolsa: la del horario, o la zona fija del flujo, o UTC (§2.6 del diseno)."""
    return plan.franja.zone or flow.fixed_zone or DEFAULT_ZONE


def step_start(run: RunView, plan: Plan) -> datetime:
    """Desde cuando mira la puerta el paso que le toca al recorrido.

    El paso 0, desde ``started_at``. Cada paso siguiente, desde el primer envio
    mas su ``after`` (``20260813000100:337-350``), y nunca antes que el envio
    anterior: un ``due_at`` que ya paso sale al tomarlo.
    """
    if not run.sends:
        return run.started_at
    step = plan.steps[len(run.sends)]
    return max(run.sends[0] + step.after, run.sends[-1])


def evaluate_gate(flow: EngineFlow, run: RunView, plan: Plan, facts: GateFacts, now: datetime, *,
                  control: ControlLine, bolsa: BolsaLedger,
                  capabilities: EngineCapabilities = ENGINE_CAPABILITIES_V1) -> GateDecision:
    """Lo que decide la puerta para el paso que le toca a ``run`` en ``now``.

    El orden, de la reevaluacion compartida (§1.0 del diseno, del 1 al 12) y
    despues de la autorizacion (del 1 al 11):

    1. vencido (``expires_at <= now``): ``vencer``, ``vencio``;
    2. la baja: ``cancelar``, ``baja``;
    3. la compra, si la definicion tiene la salida ``compra``: ``cancelar``, ``compro``;
    5. la toma humana: ``pausar``, ``tomada_por_el_equipo``. Con la excepcion 1
       mira solo la conversacion del caso, que nace con el primer envio;
    9. el limite de envios (el plan, o las capacidades): ``cancelar``,
       ``completo``. Con la v1 y un envio hecho, nunca ``mandar``;
    -  un paso cuya hora no llego: ``esperar`` hasta esa hora, sin motivo;
    10. la franja cerrada: sin apertura antes de vencer, ``escalar`` con la
        excepcion 3 (``vencer`` sin ella), ``sin_horario``; si no, ``esperar``,
        ``fuera_de_horario``;
    12. la respuesta despues de un envio: ``cancelar``, ``contesto``;
    y de la autorizacion: el control sin armar (6, ``flujo_pausado``,
    ``flujo_apagado`` o sin motivo), la intencion que dejo de estar viva (9,
    excepcion 4, ``ya_no_entra``), el tope total y el del dia (11,
    ``tope_del_flujo``) y el conflicto de compra sin resolver (4c, sin motivo).

    Con ``esperar``, ``next_at`` es la primera hora elegible (``first_eligible``)
    si la hay, o el vencimiento: lo que no vuelve (el tope total, la intencion,
    el conflicto, el control que no se rearma) espera hasta vencer, como el
    arranque que el legado rechaza y reintenta. ``GateFacts.unknown`` no lo mira:
    lo que no se sabe lo cuenta el replay.
    """
    _require_aware(now)
    expires = run.firing.at + plan.expires_after
    sent = len(run.sends)
    exceptions = flow.exceptions
    if now >= expires:
        return GateDecision("vencer", "vencio", None, None)
    if _happened(facts.blocked_at, now):
        return GateDecision("cancelar", "baja", None, None)
    if "compra" in plan.exits and _happened(facts.purchase_at, now):
        return GateDecision("cancelar", "compro", None, None)
    if _happened(facts.takeover_at, now) and (sent or TAKEOVER_ONLY_IN_ITS_CONVERSATION not in exceptions):
        return GateDecision("pausar", "tomada_por_el_equipo", None, None)
    if sent >= _allowed_sends(plan, capabilities):
        return GateDecision("cancelar", "completo", None, None)
    start = step_start(run, plan)
    if now < start:
        return GateDecision("esperar", None, start, None)
    zone = pool_zone(flow, plan)

    def eligible_from(origin: datetime) -> Eligible:
        return first_eligible(origin, expires, franja=plan.franja, control=control, bolsa=bolsa, zone=zone,
                              per_day=plan.per_day, total=plan.total)

    eligible = eligible_from(start)
    if eligible.at is not None and eligible.at < now:
        # Se mira tarde: la hora que explica el envio es la de ahora.
        eligible = eligible_from(now)
    wait_until = eligible.at if eligible.at is not None else expires

    def wait(reason: str | None, until: datetime | None = None) -> GateDecision:
        return GateDecision("esperar", reason, until or wait_until, eligible)

    if not is_open(plan.franja, now):
        opens = next_open(plan.franja, now)
        if opens is None or opens >= expires:
            decision = "escalar" if NO_WINDOW_ESCALATES in exceptions else "vencer"
            return GateDecision(decision, "sin_horario", None, eligible)
        return wait("fuera_de_horario")
    if sent and _happened(facts.replied_at, now):
        return GateDecision("cancelar", "contesto", None, eligible)
    state = control.state_at(now)
    if state != ARMED:
        return wait(CONTROL_REASON.get(state))
    if LIVE_INTENT in exceptions and facts.intent_alive_until is not None and now >= facts.intent_alive_until:
        return wait("ya_no_entra", expires)
    total_count, daily_count = bolsa.counts_before(now, zone)
    if plan.total is not None and total_count >= plan.total:
        return wait("tope_del_flujo", expires)
    if plan.per_day is not None and daily_count >= plan.per_day:
        return wait("tope_del_flujo")
    if PURCHASE_CONFLICT_STOPS_ALL in exceptions and facts.purchase_conflict_open:
        return wait(None, expires)
    return GateDecision("mandar", None, None, eligible)


# ---------------------------------------------------------------------------
# Los motivos (§2.9 del diseno).
# ---------------------------------------------------------------------------

# La tabla de §2.9 del diseno del motor, codigo por codigo: el codigo de hoy
# (sin el prefijo pilot_scope_rejected:) y el motivo del contrato de lectura
# (automation-expert-read-v1.md:170-219). None es «sin motivo»: el contrato no
# le da uno. Los parametros van como en el contrato (<flujo>, <motivo>,
# <regla>): los completa quien conoce el flujo o la regla.
# pilot_case_binding_conflict es ya_en_curso con la repeticion y
# no_arranca_mientras:<flujo> con un caso de otro flujo: el codigo no lo dice,
# lo dice plan_start.
LEGACY_REASON_TO_MOTIVO: Mapping[str, str | None] = MappingProxyType({
    "expired": "vencio",
    "reevaluation_expired": "vencio",
    "expired_after_worker_crash": "vencio",
    "contact_blocked": "baja",
    "contact_authorization_denied": "baja",
    "contact_opted_out": "baja",
    "precheckout_prior_opt_out": "baja",
    "pilot_audience_consented_intent_prior_opt_out": "baja",
    "pilot_chatwoot_opt_out_stop": "baja",
    "pending_chatwoot_opt_out_stop": "baja",
    "purchase_detected": "compro",
    "intent_purchased": "compro",
    "purchase_by_identity": "compro",
    "intent_purchase_ambiguous": "compro",
    "known_purchase_before_recovery_plan": "compro",
    "superseded_by_provider_event": "le_gano:<flujo>",
    "human_takeover_active": "tomada_por_el_equipo",
    "human_handoff_requested": "tomada_por_el_equipo",
    "chatwoot_conversation_blocked": "tomada_por_el_equipo",
    "precheckout_conversation_handoff": "tomada_por_el_equipo",
    "prospect_replied": "contesto",
    "lead_wrote_meanwhile": "contesto",
    "no_business_window_before_expiry": "sin_horario",
    "business_window_closed": "fuera_de_horario",
    "pilot_runtime_not_armed": "flujo_pausado",
    "flujo_pausado": "flujo_pausado",
    "flujo_apagado": "flujo_apagado",
    "pilot_case_binding_conflict": "ya_en_curso",
    "precheckout_contact_already_planned": "ya_en_curso",
    "pilot_audience_intent_unresolved": "no_entra:permiso",
    "pilot_audience_intent_scope_mismatch": "no_entra:permiso",
    "pilot_audience_consented_intent_not_authorized": "no_entra:permiso",
    "pilot_audience_consented_intent_phone_mismatch": "no_entra:permiso",
    "pilot_audience_consented_intent_contact_phone_mismatch": "no_entra:permiso",
    "pilot_audience_consented_intent_binding_unavailable": "no_entra:permiso",
    "pilot_audience_consented_intent_submission_missing": "no_entra:permiso",
    "precheckout_intent_not_live": "ya_no_entra",
    "precheckout_authorization_lost": "ya_no_entra",
    "pilot_audience_consented_intent_not_live": "ya_no_entra",
    "pilot_daily_budget_exhausted": "tope_del_flujo",
    "pilot_total_budget_exhausted": "tope_del_flujo",
    "pilot_contact_proactive_cap_reached": "tope_por_persona",
    "identity_not_authorized": "identidad:<motivo>",
    "contact_authorization_unknown": "identidad:<motivo>",
    "approved_template_mismatch": "meta:<regla>",
    "template_parameters_missing": "meta:nombre",
    "greeting_name_refused": "meta:<regla>",
    "delivery_uncertain_or_failed": "envio_incierto",
    "request_start_response_invalid": "envio_incierto",
    "accepted_by_chatwoot": "completo",
    "policy_exhausted": "completo",
    "automatic_message_limit_reached": "completo",
    "pilot_scope_version_mismatch": None,
    "pilot_contact_not_in_cohort": None,
    "payment_failure_correlation_unresolved": None,
    "precheckout_contact_ambiguous": None,
    "create_recovery_case_failed": None,
})
# Las asignaciones que no estan en el contrato y van como propuesta a su dueno
# (§2.9 del diseno, «Tres de esas asignaciones»), no como un hecho: el codigo y
# el motivo propuesto. La tercera propone un motivo para un codigo que la tabla
# deja sin motivo.
PROPOSED_TO_CONTRACT_OWNER: Mapping[str, str] = MappingProxyType({
    "known_purchase_before_recovery_plan": "compro",
    "flujo_pausado": "flujo_pausado",
    "flujo_apagado": "flujo_apagado",
    "payment_failure_correlation_unresolved": "no_entra:permiso",
})
