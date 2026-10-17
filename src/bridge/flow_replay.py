"""La historia y el replay del motor sobre una captura de ATT1.

Diseno de la T3a (2026-10-17), §3.2, §4 y §6. Lee los hechos de una captura
con la forma v2 (``flow_parity.Capture``), los traduce a disparos y recorridos
del motor (``History``) y reconstruye, disparo por disparo, lo que el motor
habria hecho con una version elegida de cada flujo (``replay``): el arranque,
las esperas, la puerta y cada envio, con la hora y que la movio.

**Una sola puerta.** El replay no decide nada de la puerta: en cada hora
tentativa arma los ``GateFacts`` desde la historia y llama a
``flow_engine.evaluate_gate``, como atributo del modulo, para que un espia la
intercepte. La compra, la baja, los topes, el control y la franja se deciden
solo ahi.

**La cola.** Los disparos y las horas tentativas van a una sola cola, en el
orden ``(hora, source_ref)``. La E se recalcula al sacarla, cuando ya se conocen
todas las autorizaciones concedidas hasta esa hora (las de la misma hora que
salieron antes en la cola incluidas); si se movio, vuelve a la cola. La E solo
crece, asi que converge.

**Los dos libros de la bolsa.** El observado (``ObservedLedger``) cuenta las
autorizaciones de la captura estrictamente antes del instante, como la T1
(``flow_parity.eligible_at``). El predicho (``PredictedLedger``) cuenta las
sembradas mas las que concede el replay, hasta la misma hora inclusive: los
carritos de la noche salen todos a las 09:00:00 de CDMX y, contando
estrictamente antes, ninguno veria a los otros de su tanda.

**Lo que no sabe** queda en ``unknown`` con su nombre (§4.3): la baja despues
del envio observado, la respuesta despues de un envio, la compra despues de la
captura, la vida de la intencion a una hora nueva y la repeticion de un
disparo sin contacto. Un paso con E posterior a ``captured_at`` queda
``en_curso``, y uno detras de la salida ``contesta``, ``sin_saber``: ninguno de
los dos cuenta como enviado.

**Lo que copia de la T1, minimo** (§2.1): la linea del control
(``CaptureControl``), la puerta observada (``observed_gate_inputs``), el caso
abierto a una hora y las intenciones correlacionadas. Solo importa nombres
publicos de ``flow_parity``; las copias las ata
``tests/test_flow_engine_differential.py`` contra las funciones publicas que
las usan.

Es puro: no lee el entorno, no hace red ni toca bases, y el resultado no
depende del orden de las filas de la captura.
"""

from __future__ import annotations

import dataclasses
import heapq
from bisect import bisect_left, bisect_right, insort
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from types import MappingProxyType
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from . import flow_engine as engine
from .flow_capture import parse_instant
from .flow_definition import trigger_of
from .flow_engine import (
    ARMED,
    ENGINE_CAPABILITIES_V1,
    LEGACY_REASON_TO_MOTIVO,
    LIVE_INTENT,
    EngineCapabilities,
    EngineFlow,
    Firing,
    FlowRef,
    GateFacts,
    PersonView,
    Plan,
    RunView,
    StartDecision,
)
from .flow_parity import EXCEPTIONS_V1, MATCH_ORDER, Capture, CaptureError, Supersession, control_state_at
from .flow_policy import interval_of
from .flow_schedule import Franja, franja_of_policy, local_day

__all__ = [
    "History", "ObservedGate", "ObservedLedger", "PredictedLedger", "PurchaseFact", "CaptureControl",
    "Replay", "RunOutcome", "StepOutcome", "VersionChoice", "Versions", "as_ran", "engine_flows_from_definitions",
    "exceptions_for", "fixed", "history_from_capture", "in_flight_at", "observed_gate_inputs",
    "observed_supersessions", "observed_winners_without_cancel", "replay",
]

# Los disparos de hoy y su evento del motor (el legacy_kind del flujo).
FIRING_EVENT: Mapping[str, str] = MappingProxyType({
    "PURCHASE_OUT_OF_SHOPPING_CART": "carrito",
    "PURCHASE_CANCELED": "pago_fallido",
})
PURCHASE_EVENT = "PURCHASE_APPROVED"
PRECHECKOUT_EVENT_TYPE = "PRECHECKOUT_FORM_SUBMITTED"
SUPERSEDED = "superseded_by_provider_event"
ACTIVATION = "pilot_scope_version_activated"
INACTIVE = "inactive"
PROCESSED = "processed"
REJECTION_PREFIX = "pilot_scope_rejected:"
CASE_FAILED = "create_recovery_case_failed"
# Lo que el adaptador rechaza y el motor vuelve a decidir: el arranque con el
# control sin armar, la version, los topes y la repeticion. No se copian de la
# captura (§4.2, paso 1): los decide plan_start o la puerta.
ENGINE_DECIDES = frozenset({
    "pilot_runtime_not_armed", "pilot_scope_version_mismatch", "pilot_daily_budget_exhausted",
    "pilot_total_budget_exhausted", "pilot_case_binding_conflict",
})
# El permiso de un disparo que el adaptador dejo pasar: el de consented_intent
# (tests/flow_store_translator.py, AUDIENCES).
ADMITTED_PERMISSION = "formulario"
# Los estados de un caso en curso (20260803000100:475-485), como flow_parity.
OPEN_CASE_STATUSES = frozenset({"grace_period", "active", "paused"})
# Las excepciones que tiene todo flujo del motor (§2.8 del diseno).
ALWAYS_LAYERS = ("sql", "despacho")
# F#11 (20260904000100:142-154): la repeticion del pago fallido adelanta la accion.
F11_LEGACY_KIND = "pago_fallido"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_POPS = 1_000_000


def _thaw(value: Any) -> Any:
    # flow_schedule lee la fila como la da JSON: dict y list.
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _instant(text: object) -> datetime:
    return parse_instant(text)


def _optional(row: Mapping[str, Any], key: str) -> datetime | None:
    value = row.get(key)
    return None if value is None else parse_instant(value)


def _from_ms(value: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=value)


# ---------------------------------------------------------------------------
# El control y los libros de la bolsa.
# ---------------------------------------------------------------------------

class CaptureControl:
    """``ControlLine`` sobre la linea del control de la captura, para un scope (la bolsa).

    ``state_at`` es ``flow_parity.control_state_at``. ``next_armed`` copia la
    logica de ``flow_parity._next_armed`` (``flow_parity.py:505-510``): el primer
    evento posterior a ``at`` que deja ``armed``. La activacion de una version
    deja ``inactive``.
    """

    def __init__(self, capture: Capture, scope_key: str) -> None:
        self._capture = capture
        self._scope = scope_key
        steps: list[tuple[datetime, int, str]] = []
        for event in capture.config["pilot_control_events_control_level"]:
            if event.get("scope_key") != scope_key:
                continue
            if event.get("event_type") == ACTIVATION:
                state = INACTIVE
            elif event.get("to_state") is not None:
                state = event["to_state"]
            else:
                raise CaptureError("config.pilot_control_events_control_level.to_state: falta, y no es una activacion")
            steps.append((_instant(event["created_at"]), event["runtime_generation"], state))
        steps.sort(key=lambda step: (step[0], step[1]))
        self._steps = tuple(steps)
        self._times = [step[0] for step in steps]

    def state_at(self, at: datetime) -> str:
        return control_state_at(self._capture, self._scope, at)[1]

    def version_at(self, at: datetime) -> int:
        """La version activa del scope en ``at``."""
        return control_state_at(self._capture, self._scope, at)[0]

    def next_armed(self, at: datetime) -> datetime | None:
        for when, _, state in self._steps[bisect_right(self._times, at):]:
            if state == ARMED:
                return when
        return None


class ObservedLedger:
    """``BolsaLedger`` con las autorizaciones observadas de una bolsa: cuenta estrictamente antes de ``at``.

    El dia local es el ``local_budget_date`` de la fila, como
    ``flow_parity._bolsa_at`` (``flow_parity.py:560-565``).
    """

    def __init__(self, capture: Capture, scope_key: str) -> None:
        instants: list[datetime] = []
        by_day: dict[str, list[datetime]] = {}
        for row in capture.authorizations:
            if row.get("scope_key") != scope_key:
                continue
            when = _instant(row["authorized_at"])
            instants.append(when)
            by_day.setdefault(row["local_budget_date"], []).append(when)
        self._instants = sorted(instants)
        self._by_day = {day: sorted(values) for day, values in by_day.items()}

    def counts_before(self, at: datetime, zone: str) -> tuple[int, int]:
        day = local_day(zone, at).isoformat()
        return bisect_left(self._instants, at), bisect_left(self._by_day.get(day, []), at)


class PredictedLedger:
    """``BolsaLedger`` de una bolsa con las autorizaciones sembradas mas las que concede el replay.

    Cuenta hasta ``at`` inclusive: lo ya concedido a la misma hora salio antes
    en la cola ``(at, source_ref)`` (§2.2 del diseno de la T3a). El dia local se
    calcula de la hora, en la zona que pide la puerta.
    """

    def __init__(self, seeded: Sequence[datetime] = ()) -> None:
        self._instants: list[datetime] = sorted(seeded)
        self.granted: list[tuple[datetime, str]] = []

    def grant(self, at: datetime, source_ref: str) -> None:
        insort(self._instants, at)
        self.granted.append((at, source_ref))

    def counts_before(self, at: datetime, zone: str) -> tuple[int, int]:
        total = bisect_right(self._instants, at)
        midnight = datetime.combine(local_day(zone, at), time(0), tzinfo=ZoneInfo(zone))
        return total, total - bisect_left(self._instants, midnight)


# ---------------------------------------------------------------------------
# La puerta observada, para la diferencial y el libro observado.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ObservedGate:
    """La puerta de un caso observado: la de la fila del scope y de la politica (``flow_parity._gate_of``)."""

    bolsa: str
    zone: str
    per_day: int
    total: int
    franja: Franja
    start: datetime
    expires: datetime


def observed_gate_inputs(capture: Capture, action: Mapping[str, Any]) -> ObservedGate:
    """La franja, la gracia, los topes y el vencimiento de la accion, leidos como los lee la T1.

    Copia de ``flow_parity._gate_of`` (``flow_parity.py:525-557``) sobre los
    atributos publicos de la captura: el binding del caso da el scope y su
    version; la politica del caso, la franja y la gracia.
    """
    case = next((row for row in capture.cases if row["id"] == action.get("recovery_case_id")), None)
    if case is None or case.get("binding") is None:
        raise CaptureError(f"accion {action.get('id')}: su caso o su binding no estan en la captura")
    binding = case["binding"]
    scope = next((row for row in capture.config["pilot_scope_versions"]
                  if (row["scope_key"], row["version"]) == (binding["scope_key"], binding["scope_version"])), None)
    policy = next((row for row in capture.config["followup_policy_versions"]
                   if (row["policy_key"], row["version"]) == (case.get("policy_key"), case.get("policy_version"))),
                  None)
    if scope is None or policy is None:
        raise CaptureError(f"caso {case.get('id')}: la version del scope o la politica no esta en config")
    grace = interval_of(policy.get("grace_period"))
    if grace is None:
        raise CaptureError(f"politica {policy.get('policy_key')}.grace_period: no se lee")
    return ObservedGate(
        bolsa=binding["scope_key"], zone=scope["timezone"],
        per_day=scope["max_outbound_request_starts_per_day"], total=scope["max_outbound_request_starts_total"],
        franja=franja_of_policy(_thaw(policy)),
        start=_instant(action["anchor_observed_at"]) + grace, expires=_instant(action["expires_at"]),
    )


# ---------------------------------------------------------------------------
# Las excepciones y las versiones.
# ---------------------------------------------------------------------------

def exceptions_for(legacy_kind: str | None) -> frozenset[str]:
    """Las excepciones del catalogo que tiene un flujo del motor (§2.8 del diseno).

    Las de capa ``sql`` y ``despacho`` siempre: las aplica codigo que el motor
    reusa. Las de capa ``plan`` y ``puerta``, solo las del ``legacy_kind`` del
    flujo. El catalogo es ``flow_parity.EXCEPTIONS_V1``, importado.
    """
    return frozenset(
        item.name for item in EXCEPTIONS_V1
        if item.layer in ALWAYS_LAYERS or (legacy_kind is not None and legacy_kind in item.legacy_kinds)
    )


Versions = Mapping[FlowRef, Mapping[int, EngineFlow]]


def engine_flows_from_definitions(document: Mapping[str, Any]) -> Versions:
    """Las versiones de cada flujo de un documento de definiciones (``att1_definitions_by_scope_version.json``).

    ``flows`` da el ``flow_id`` y el ``legacy_kind`` de cada flujo; ``versions``,
    la definicion publicada de cada version. La zona fija es la de la version
    mas vieja con un horario objeto (``WorldFlow.fixed_zone``,
    ``flow_definition.py:764-772``), o la de la instancia. La bolsa, la de
    ``topes.bolsa`` o el ``scope_key`` del documento.
    """
    meta = document["flows"]
    versions = document["versions"]
    instance_zone = (document.get("instance") or {}).get("time_zone")
    out: dict[FlowRef, dict[int, EngineFlow]] = {}
    for key in sorted(meta):
        numbers = sorted(int(number) for number, flows in versions.items() if key in flows)
        fixed_zone = instance_zone
        for number in numbers:
            horario = versions[str(number)][key].get("horario")
            if isinstance(horario, Mapping) and isinstance(horario.get("zona"), str):
                fixed_zone = horario["zona"]
                break
        for number in numbers:
            definition = versions[str(number)][key]
            caps = definition.get("topes") if isinstance(definition.get("topes"), Mapping) else {}
            legacy_kind = meta[key].get("legacy_kind")
            flow = EngineFlow(
                flow_id=meta[key]["flow_id"], product_ref=definition["producto"], flow_key=key, version=number,
                definition=definition, legacy_kind=legacy_kind, fixed_zone=fixed_zone,
                pool=caps.get("bolsa") or document.get("scope_key"), exceptions=exceptions_for(legacy_kind),
            )
            out.setdefault(flow.ref, {})[number] = flow
    return MappingProxyType({ref: MappingProxyType(by_version) for ref, by_version in out.items()})


# ---------------------------------------------------------------------------
# La historia.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PurchaseFact:
    """Una compra aprobada de la captura: su hora, el producto, la oferta y las claves de persona."""

    source_ref: str
    at: datetime
    product_id: str | None
    offer_ref: str | None
    keys: frozenset[str]


@dataclass(frozen=True)
class History:
    """Los hechos de una ``Capture``, traducidos a disparos y recorridos del motor, con indices propios."""

    capture: Capture
    window: tuple[datetime, datetime | None]     # desde y captured_at
    firings: tuple[Firing, ...]                   # desde «desde», ordenados por (at, source_ref)
    seed_runs: tuple[RunView, ...]                # los casos de antes de «desde»: cuentan en la persona
    admission: Mapping[str, str]                  # source_ref -> processing_error, o processed
    observed_sends: Mapping[str, datetime]        # source_ref -> el request_started_at observado
    purchases: tuple[PurchaseFact, ...]
    first_contacts: tuple[Mapping[str, Any], ...]
    seed_authorizations: Mapping[str, tuple[datetime, ...]]   # por bolsa
    # Lo que suma esta implementacion a §3.2, para P6 y la eleccion de version.
    all_firings: tuple[Firing, ...]               # siembra incluida, por (received_at, source_ref)
    case_contact: Mapping[str, str]               # source_ref -> el contacto de su caso (ancla o evidencia)
    case_version: Mapping[str, int]               # source_ref -> la version del binding de su caso
    case_created: Mapping[str, datetime]          # source_ref -> la creacion del caso que ancla
    external_product: Mapping[str, str | None]    # source_ref -> el product_id de Hotmart
    firing_scope: Mapping[str, str | None]        # source_ref -> el scope que recibe ese evento
    contact_keys: Mapping[str, frozenset[str]]    # contacto -> sus claves de persona, digitos y mail
    precheckout_product: str | None
    window_actions: frozenset[str] = frozenset()  # las acciones de los casos que el replay replanifica


def _correlated_intents(event: Mapping[str, Any]) -> frozenset[str]:
    """Las intenciones resueltas del disparo: hasta dos (copia de ``flow_parity._correlated_intents``)."""
    out = set()
    for name in ("cart_correlation", "payment_failure_correlation"):
        correlation = event.get(name) or {}
        if correlation.get("outcome") == "resolved" and correlation.get("purchase_intent_id"):
            out.add(correlation["purchase_intent_id"])
    return frozenset(out)


def _request_started(action: Mapping[str, Any]) -> datetime | None:
    """El ``request_started_at`` del primer tramo que lo tiene."""
    tramos = sorted(action.get("intentos") or (), key=lambda tramo: tramo["primero"]["attempt_number"])
    for tramo in tramos:
        started = tramo["primero"].get("request_started_at")
        if started is not None:
            return _instant(started)
    return None


def _case_closed_at(case: Mapping[str, Any]) -> datetime | None:
    closed = _optional(case, "closed_at")
    if closed is not None or case.get("status") in OPEN_CASE_STATUSES:
        return closed
    return _instant(case["created_at"])


def history_from_capture(capture: Capture) -> History:
    """La historia de la captura: la forma, la privacidad y los conteos ya los valido ``Capture.from_json``."""
    desde, captured_at = capture.desde, capture.captured_at
    actions_by_case = {action["recovery_case_id"]: action for action in capture.actions}
    contact_keys: dict[str, frozenset[str]] = {}
    contacts_by_person: dict[str, set[str]] = {}
    for contact in capture.contacts:
        keys = (set(contact.get("person_keys") or ()) | set(contact.get("digits_keys") or ())
                | set(contact.get("email_keys") or ()))
        contact_keys[contact["id"]] = frozenset(keys)
        for key in contact.get("person_keys") or ():
            contacts_by_person.setdefault(key, set()).add(contact["id"])
    scopes = {}
    precheckout = set()
    for row in capture.config["pilot_scope_versions"]:
        types = (row.get("source_event_type"), *(row.get("additional_source_event_types") or ()))
        for event_type in types:
            scopes.setdefault((row.get("external_product_id"), event_type), set()).add(row["scope_key"])
        if row.get("source_event_type") == PRECHECKOUT_EVENT_TYPE:
            precheckout.add(row.get("external_product_id"))

    firings: list[Firing] = []
    all_firings: list[Firing] = []
    admission: dict[str, str] = {}
    observed_sends: dict[str, datetime] = {}
    case_contact: dict[str, str] = {}
    case_version: dict[str, int] = {}
    case_created: dict[str, datetime] = {}
    external_product: dict[str, str | None] = {}
    firing_scope: dict[str, str | None] = {}
    window_cases: set[str] = set()
    window_actions: set[str] = set()
    firing_by_ref: dict[str, Firing] = {}
    for event in capture.events:
        event_type = event.get("event_type")
        if event_type not in FIRING_EVENT:
            continue
        ref = event["id"]
        case = capture.case_of_event(ref)
        if case is not None and case.get("contact_id") is not None:
            contact: str | None = case["contact_id"]
            case_contact[ref] = case["contact_id"]
        else:
            candidates = contacts_by_person.get(event.get("person_key") or "", set())
            contact = next(iter(candidates)) if len(candidates) == 1 else None
        if case is not None and case.get("binding") is not None:
            case_version[ref] = case["binding"]["scope_version"]
        received = _instant(event["received_at"])
        status = event.get("processing_status")
        admitted = status == PROCESSED or case is not None
        firing = Firing(
            source_ref=ref, event=FIRING_EVENT[event_type],
            at=_from_ms(event["creation_date_ms"]) if event.get("creation_date_ms") is not None else received,
            received_at=received, product_ref=event.get("funnel_ref") or event.get("product_id"),
            offer_ref=event.get("offer_code"), contact_ref=contact, intent_refs=_correlated_intents(event),
            permission=ADMITTED_PERMISSION if admitted else None,
        )
        firing_by_ref[ref] = firing
        all_firings.append(firing)
        external_product[ref] = event.get("product_id")
        candidates_scope = scopes.get((event.get("product_id"), event_type), set())
        firing_scope[ref] = min(candidates_scope) if len(candidates_scope) == 1 else None
        admission[ref] = event.get("processing_error") or status or ""
        if received < desde:
            continue
        firings.append(firing)
        if case is not None and case["abandonment_event_id"] == ref:
            window_cases.add(case["id"])
            case_created[ref] = _instant(case["created_at"])
            action = actions_by_case.get(case["id"])
            if action is not None:
                window_actions.add(action["id"])
                started = _request_started(action)
                if started is not None:
                    observed_sends[ref] = started

    seed_runs: list[RunView] = []
    for case in sorted(capture.cases, key=lambda row: row["id"]):
        if case["id"] in window_cases:
            continue
        firing = firing_by_ref.get(case["abandonment_event_id"])
        if firing is None:
            continue
        firing = dataclasses.replace(firing, contact_ref=case.get("contact_id"))
        action = actions_by_case.get(case["id"])
        started = _request_started(action) if action is not None else None
        binding = case.get("binding") or {}
        # El flujo de la siembra es el del legacy_kind de su disparo: en ATT1, flow_key es el evento.
        seed_runs.append(RunView(
            run_ref=case["id"], flow=(firing.product_ref, firing.event), version=binding.get("scope_version", 0),
            firing=firing, started_at=firing.at, sends=(started,) if started is not None else (),
            closed_at=_case_closed_at(case),
        ))

    seeded: dict[str, list[datetime]] = {}
    for row in capture.authorizations:
        if row["action_id"] in window_actions:
            continue
        seeded.setdefault(row["scope_key"], []).append(_instant(row["authorized_at"]))

    purchases = []
    for event in capture.events:
        if event.get("event_type") != PURCHASE_EVENT or event.get("approved_date_ms") is None:
            continue
        keys = frozenset(key for key in (event.get("person_key"), event.get("digits_key"), event.get("email_key"))
                         if key)
        purchases.append(PurchaseFact(event["id"], _from_ms(event["approved_date_ms"]), event.get("product_id"),
                                      event.get("offer_code"), keys))
    purchases.sort(key=lambda fact: (fact.at, fact.source_ref))

    return History(
        capture=capture,
        window=(desde, captured_at),
        firings=tuple(sorted(firings, key=lambda item: (item.at, item.source_ref))),
        seed_runs=tuple(seed_runs),
        admission=MappingProxyType(admission),
        observed_sends=MappingProxyType(observed_sends),
        purchases=tuple(purchases),
        first_contacts=tuple(capture.first_contacts),
        seed_authorizations=MappingProxyType({key: tuple(sorted(value)) for key, value in seeded.items()}),
        all_firings=tuple(sorted(all_firings, key=lambda item: (item.received_at, item.source_ref))),
        case_contact=MappingProxyType(case_contact),
        case_version=MappingProxyType(case_version),
        case_created=MappingProxyType(case_created),
        external_product=MappingProxyType(external_product),
        firing_scope=MappingProxyType(firing_scope),
        contact_keys=MappingProxyType(contact_keys),
        precheckout_product=next(iter(precheckout)) if len(precheckout) == 1 else None,
        window_actions=frozenset(window_actions),
    )


# ---------------------------------------------------------------------------
# La eleccion de la version.
# ---------------------------------------------------------------------------

class VersionChoice(Protocol):
    def __call__(self, flow: FlowRef, firing: Firing, history: History) -> int | None: ...


def as_ran() -> VersionChoice:
    """«Como corrio»: la version del binding del caso del disparo; sin caso, la activa al llegar."""

    def choose(flow: FlowRef, firing: Firing, history: History) -> int | None:
        version = history.case_version.get(firing.source_ref)
        if version is not None:
            return version
        scope = history.firing_scope.get(firing.source_ref)
        if scope is None:
            return None
        try:
            return control_state_at(history.capture, scope, firing.received_at)[0]
        except CaptureError:
            return None

    return choose


def fixed(versions: Mapping[FlowRef, int | None]) -> VersionChoice:
    """Una version fija por flujo; ``None`` (o un flujo que no esta) es el flujo apagado."""
    chosen = dict(versions)

    def choose(flow: FlowRef, firing: Firing, history: History) -> int | None:
        return chosen.get(flow)

    return choose


# ---------------------------------------------------------------------------
# El resultado.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StepOutcome:
    index: int
    at: datetime | None                 # la E predicha de este paso
    status: str                         # "enviado" | "sin_saber" | "compro" | "vencio" | "fuera_de_plazo" | "en_curso"
    moved_by: tuple[str, ...]
    unknown: frozenset[str]             # "baja", "respuesta", "compra", "intencion"
    reason: str | None = None           # el motivo de la puerta (§2.9), o next_step_outside_expiration


@dataclass(frozen=True)
class RunOutcome:
    firing_ref: str
    flow: FlowRef
    version: int | None
    start: StartDecision
    plan: Plan | None
    steps: tuple[StepOutcome, ...]      # un paso por cada envio que el plan llego a mirar
    end: str                            # el status del ultimo paso, o "no_arranco" | "no_llego" | "sin_horario"
    end_at: datetime | None             # cuando se cerro; None si sigue en curso o no arranco
    legacy_note: str | None             # lo que el legado habria hecho distinto, con su causa (§4.5)
    unknown: frozenset[str]             # la union de los pasos, mas la repeticion sin contacto
    firing: Firing | None = None

    @property
    def sends(self) -> tuple[datetime, ...]:
        """Las horas de los pasos ``enviado``."""
        return tuple(step.at for step in self.steps if step.status == "enviado" and step.at is not None)


@dataclass(frozen=True)
class Replay:
    runs: tuple[RunOutcome, ...]
    supersessions: tuple[Supersession, ...]       # P6 con los intervalos del replay
    bolsa_by_day: Mapping[tuple[str, str], int]   # (bolsa, dia local) -> autorizaciones, sembradas y predichas
    winners_without_cancel: tuple[tuple[str, str], ...] = ()

    def counts(self) -> Mapping[str, int]:
        return MappingProxyType(dict(Counter(run.end for run in self.runs)))


def in_flight_at(result: Replay, at: datetime) -> Mapping[FlowRef, int]:
    """Los recorridos en curso en ``at``, por flujo: ya llego su disparo y todavia no se cerraron."""
    counts: Counter[FlowRef] = Counter()
    for run in result.runs:
        if run.plan is None or run.firing is None:
            continue
        if run.firing.received_at <= at and (run.end_at is None or run.end_at > at):
            counts[run.flow] += 1
    return MappingProxyType(dict(counts))


# ---------------------------------------------------------------------------
# P6: quien le gana a quien, con un intervalo dado.
# ---------------------------------------------------------------------------

def _superseded(plan: Mapping[str, Any]) -> bool:
    if plan.get("outcome") == "not_planned":
        return plan.get("reason_code") == SUPERSEDED
    return plan.get("accion_status") == "cancelled" and plan.get("accion_terminal_reason") == SUPERSEDED


def _plan_ref(plan: Mapping[str, Any]) -> str:
    return plan.get("recovery_case_id") or plan["purchase_intent_id"]


def _winners(history: History, contact_of: Callable[[str], str | None]) -> list[Firing]:
    """Los disparos que pueden ganarle al primer contacto, con el contacto de su recorrido (o ninguno)."""
    product = history.precheckout_product
    if product is None:
        raise CaptureError("config.pilot_scope_versions: el primer contacto no tiene un solo producto")
    return [dataclasses.replace(firing, contact_ref=contact_of(firing.source_ref)) for firing in history.all_firings
            if history.external_product.get(firing.source_ref) == product]


def _supersessions(history: History, run_open_at: Callable[[str, datetime], bool],
                   contact_of: Callable[[str], str | None]) -> tuple[Supersession, ...]:
    if not any(_superseded(plan) for plan in history.first_contacts):
        return ()
    winners = _winners(history, contact_of)
    out: list[Supersession] = []
    for plan in history.first_contacts:
        if not _superseded(plan):
            continue
        stopped = (_instant(plan["created_at"]) if plan.get("outcome") == "not_planned"
                   else _instant(plan["accion_cerrada_at"]))
        before = [firing for firing in winners if firing.received_at <= stopped]
        winner: str | None = None
        matched: str | None = None
        routes: set[str] = set()
        for route in MATCH_ORDER:
            found = [firing for firing in before if engine.wins(
                firing, plan.get("contact_id"), plan.get("purchase_intent_id"), route=route,
                product_ref=firing.product_ref, run_open_at=run_open_at, at=stopped)]
            if found:
                routes.add(route)
                if winner is None:
                    winner, matched = found[-1].source_ref, route
        out.append(Supersession(_plan_ref(plan), stopped, winner, matched, frozenset(routes)))
    # Por la hora del corte y el plan: el resultado no depende del orden de las filas.
    return tuple(sorted(out, key=lambda item: (item.stopped_at, item.plan_ref)))


def _winners_without_cancel(history: History, run_open_at: Callable[[str, datetime], bool],
                            contact_of: Callable[[str], str | None]) -> tuple[tuple[str, str], ...]:
    if not any(plan.get("outcome") == "planned" for plan in history.first_contacts):
        return ()
    window_refs = {firing.source_ref for firing in history.firings}
    winners = [firing for firing in _winners(history, contact_of) if firing.source_ref in window_refs]
    winners.sort(key=lambda firing: (firing.at, firing.source_ref))
    out: list[tuple[str, str]] = []
    for firing in winners:
        arrived = firing.received_at
        for plan in history.first_contacts:
            if plan.get("outcome") != "planned":
                continue
            closed = _optional(plan, "accion_cerrada_at")
            started = _optional(plan, "request_started_at")
            if _instant(plan["created_at"]) > arrived:
                continue
            if (closed is not None and closed <= arrived) or (started is not None and started <= arrived):
                continue

            def beats(at: datetime | None) -> bool:
                return any(engine.wins(firing, plan.get("contact_id"), plan.get("purchase_intent_id"), route=route,
                                       product_ref=firing.product_ref, run_open_at=run_open_at, at=at)
                           for route in MATCH_ORDER)

            if not beats(None) or plan.get("accion_status") == "cancelled":
                continue
            end = started or closed
            if end is not None and beats(end):
                out.append((firing.source_ref, _plan_ref(plan)))
    return tuple(sorted(out))


def _observed_open(history: History) -> Callable[[str, datetime], bool]:
    """El caso del disparo en curso en ``at``, con su intervalo observado (copia de ``flow_parity._case_open_at``)."""

    def open_at(source_ref: str, at: datetime) -> bool:
        case = history.capture.case_of_event(source_ref)
        if case is None:
            return False
        if _instant(case["created_at"]) > at:
            return False
        closed = _optional(case, "closed_at")
        if closed is not None:
            return closed > at
        return case.get("status") in OPEN_CASE_STATUSES

    return open_at


def observed_supersessions(history: History) -> tuple[Supersession, ...]:
    """P6 con ``flow_engine.wins`` y los intervalos observados: lo que la diferencial compara con la T1."""
    return _supersessions(history, _observed_open(history), history.case_contact.get)


def observed_winners_without_cancel(history: History) -> tuple[tuple[str, str], ...]:
    """Los ganadores sin cancelacion con los intervalos observados (``flow_parity.winners_without_cancel``)."""
    return _winners_without_cancel(history, _observed_open(history), history.case_contact.get)


# ---------------------------------------------------------------------------
# El replay.
# ---------------------------------------------------------------------------

class _Run:
    """Un recorrido del replay mientras corre la cola."""

    def __init__(self, firing: Firing, flow: EngineFlow, plan: Plan, start: StartDecision, started_at: datetime,
                 open_from: datetime, unknown: set[str]) -> None:
        self.firing = firing
        self.flow = flow
        self.plan = plan
        self.start = start
        self.started_at = started_at
        self.open_from = open_from
        self.sends: list[datetime] = []
        self.steps: list[StepOutcome] = []
        self.closed_at: datetime | None = None
        self.done = False
        self.end: str | None = None
        self.generation = 0
        self.step_moved: tuple[str, ...] = ()
        self.last_decision: str | None = None
        self.legacy_note: str | None = None
        self.unknown = unknown

    def view(self) -> RunView:
        return RunView(run_ref=self.firing.source_ref, flow=self.flow.ref, version=self.flow.version,
                       firing=self.firing, started_at=self.started_at, sends=tuple(self.sends),
                       closed_at=self.closed_at)

    def finish(self, end: str, closed_at: datetime | None) -> None:
        self.done, self.end, self.closed_at = True, end, closed_at

    def outcome(self) -> RunOutcome:
        unknown = set(self.unknown)
        for step in self.steps:
            unknown |= step.unknown
        return RunOutcome(
            firing_ref=self.firing.source_ref, flow=self.flow.ref, version=self.flow.version, start=self.start,
            plan=self.plan, steps=tuple(self.steps), end=self.end or "en_curso", end_at=self.closed_at,
            legacy_note=self.legacy_note, unknown=frozenset(unknown), firing=self.firing,
        )


def _allowed_sends(plan: Plan, capabilities: EngineCapabilities) -> int:
    if capabilities.max_sends_per_path is None:
        return len(plan.steps)
    return min(len(plan.steps), capabilities.max_sends_per_path)


class _Replayer:
    def __init__(self, history: History, versions: Versions, choose: VersionChoice,
                 capabilities: EngineCapabilities) -> None:
        self.history = history
        self.versions = versions
        self.choose = choose
        self.capabilities = capabilities
        self.captured_at = history.window[1]
        self.flow_by_event: dict[str, FlowRef] = {}
        for ref, by_version in sorted(versions.items()):
            for flow in by_version.values():
                trigger = trigger_of(flow.definition)
                if trigger is not None and trigger.event is not None:
                    self.flow_by_event.setdefault(trigger.event, ref)
                    break
        self.controls: dict[str, CaptureControl] = {}
        self.ledgers: dict[str, PredictedLedger] = {}
        self.runs: dict[str, _Run] = {}
        self.joined: dict[str, str] = {}             # la repeticion -> el recorrido al que se sumo
        self.outcomes: dict[str, RunOutcome] = {}
        self.by_contact: dict[str, list[_Run]] = {}
        self.seed_by_contact: dict[str, list[RunView]] = {}
        for seed in history.seed_runs:
            if seed.firing.contact_ref is not None:
                self.seed_by_contact.setdefault(seed.firing.contact_ref, []).append(seed)
        self.firings_by_contact: dict[str, list[Firing]] = {}
        for firing in history.all_firings:
            if firing.contact_ref is not None:
                self.firings_by_contact.setdefault(firing.contact_ref, []).append(firing)
        self.grants: Counter[tuple[str, str]] = Counter()
        self.queue: list[tuple[datetime, str, int, int]] = []

    # -- los recursos por bolsa --------------------------------------------

    def control(self, pool: str) -> CaptureControl:
        if pool not in self.controls:
            self.controls[pool] = CaptureControl(self.history.capture, pool)
        return self.controls[pool]

    def ledger(self, pool: str) -> PredictedLedger:
        if pool not in self.ledgers:
            self.ledgers[pool] = PredictedLedger(self.history.seed_authorizations.get(pool, ()))
        return self.ledgers[pool]

    def push(self, at: datetime, run: _Run) -> None:
        run.generation += 1
        heapq.heappush(self.queue, (at, run.firing.source_ref, 1, run.generation))

    # -- el arranque -------------------------------------------------------

    def not_started(self, firing: Firing, flow: FlowRef, version: int | None, start: StartDecision, end: str,
                    unknown: frozenset[str] = frozenset()) -> None:
        self.outcomes[firing.source_ref] = RunOutcome(
            firing_ref=firing.source_ref, flow=flow, version=version, start=start, plan=None, steps=(), end=end,
            end_at=None, legacy_note=None, unknown=unknown, firing=firing,
        )

    def person(self, firing: Firing) -> PersonView:
        contact = firing.contact_ref
        if contact is None:
            return PersonView((), ())
        runs = [run.view() for run in self.by_contact.get(contact, ())
                if run.firing.product_ref == firing.product_ref]
        runs += [seed for seed in self.seed_by_contact.get(contact, ())
                 if seed.firing.product_ref == firing.product_ref]
        fired = tuple(item for item in self.firings_by_contact.get(contact, ())
                      if item.received_at <= firing.received_at and item.source_ref != firing.source_ref)
        return PersonView(tuple(runs), fired)

    def flows_at(self, firing: Firing) -> dict[FlowRef, EngineFlow]:
        out: dict[FlowRef, EngineFlow] = {}
        for ref, by_version in self.versions.items():
            version = self.choose(ref, firing, self.history)
            if version is not None and version in by_version:
                out[ref] = by_version[version]
        return out

    def arrive(self, firing: Firing) -> None:
        ref = self.flow_by_event.get(firing.event)
        if ref is None:
            self.not_started(firing, (firing.product_ref, firing.event), None,
                             StartDecision(False, "flujo_apagado", None), "no_arranco")
            return
        version = self.choose(ref, firing, self.history)
        if version is None:
            # Apagado: un disparo ni se registra (el ciclo, :128).
            self.not_started(firing, ref, None, StartDecision(False, "flujo_apagado", None), "no_arranco")
            return
        if version not in self.versions[ref]:
            raise ValueError(f"replay_version_missing: {ref[1]} v{version}")
        flow = self.versions[ref][version]
        admission = self.history.admission.get(firing.source_ref, "")
        code = admission[len(REJECTION_PREFIX):] if admission.startswith(REJECTION_PREFIX) else admission
        if code == CASE_FAILED and firing.contact_ref is None:
            self.not_started(firing, ref, version, StartDecision(False, None, None), "no_llego")
            return
        if code not in (PROCESSED, CASE_FAILED, "") and code not in ENGINE_DECIDES:
            # Un hecho del adaptador (la correlacion, el permiso, la baja previa): vale con cualquier version.
            self.not_started(firing, ref, version, StartDecision(False, LEGACY_REASON_TO_MOTIVO.get(code), None),
                             "no_arranco")
            return
        if code in ENGINE_DECIDES and firing.permission is None:
            firing = dataclasses.replace(firing, permission=ADMITTED_PERMISSION)
        unknown = {"repeticion"} if firing.contact_ref is None else set()
        start = engine.plan_start(flow, firing, self.person(firing), flows=self.flows_at(firing))
        if not start.starts:
            self.not_started(firing, ref, version, start, "no_arranco", frozenset(unknown))
            if start.joins_run is not None:
                self.join(firing, start.joins_run)
            return
        plan = engine.plan_steps(flow, firing, capabilities=self.capabilities)
        run = _Run(firing, flow, plan, start, firing.at + plan.steps[0].after,
                   self.history.case_created.get(firing.source_ref, firing.received_at), unknown)
        self.runs[firing.source_ref] = run
        if firing.contact_ref is not None:
            self.by_contact.setdefault(firing.contact_ref, []).append(run)
        self.push(run.started_at, run)

    def join(self, firing: Firing, run_ref: str) -> None:
        """F#11: la repeticion del pago fallido adelanta el comienzo del recorrido abierto.

        Solo con el pago fallido, con el recorrido esperando su primer envio y
        sin pausar (``20260904000100:142-154``, la condicion en ``:145`` y
        ``:149``): ``started_at`` pasa a ``min(started_at, la llegada)``.
        """
        self.joined[firing.source_ref] = run_ref
        run = self.runs.get(run_ref)
        if run is None or run.done or run.sends or run.flow.legacy_kind != F11_LEGACY_KIND:
            return
        if run.last_decision == "pausar" or self.control(run.plan.pool).state_at(firing.received_at) != ARMED:
            return
        if firing.received_at < run.started_at:
            run.started_at = firing.received_at
            run.step_moved = ()
            self.push(run.started_at, run)

    # -- la puerta ----------------------------------------------------------

    def purchase_at(self, run: _Run) -> datetime | None:
        """La compra que corta el recorrido: la de la misma oferta antes del disparo (4b), o la primera despues."""
        firing = run.firing
        if firing.contact_ref is not None:
            keys = self.history.contact_keys.get(firing.contact_ref, frozenset())
        else:
            keys = frozenset()
        if not keys:
            return None
        product = self.history.external_product.get(firing.source_ref)
        mine = [fact for fact in self.history.purchases if fact.product_id == product and fact.keys & keys]
        known = [fact for fact in mine if fact.at < firing.at and fact.offer_ref == firing.offer_ref]
        if known:
            return known[0].at
        return next((fact.at for fact in mine if fact.at >= firing.received_at), None)

    def conflict_open(self, at: datetime) -> bool:
        for row in self.history.capture.purchase_conflicts:
            detected = _optional(row, "detected_at")
            resolved = _optional(row, "resolved_at")
            if detected is not None and detected <= at and (resolved is None or resolved > at):
                return True
        return False

    def facts(self, run: _Run, at: datetime) -> GateFacts:
        firing = run.firing
        unknown: set[str] = set()
        observed = self.history.observed_sends.get(firing.source_ref)
        seen_until = max(firing.at, observed) if observed is not None else firing.at
        if at > seen_until:
            # La forma v2 no trae las bajas: la historia solo sabe que no hubo hasta el envio observado.
            unknown.add("baja")
            if firing.intent_refs and LIVE_INTENT in run.flow.exceptions:
                unknown.add("intencion")
        if run.sends:
            unknown.add("respuesta")
        return GateFacts(
            blocked_at=None, purchase_at=self.purchase_at(run), takeover_at=None, replied_at=None,
            purchase_conflict_open=self.conflict_open(at), intent_alive_until=None, unknown=frozenset(unknown),
        )

    def evaluate(self, run: _Run, now: datetime) -> None:
        facts = self.facts(run, now)
        pool = run.plan.pool
        decision = engine.evaluate_gate(run.flow, run.view(), run.plan, facts, now, control=self.control(pool),
                                        bolsa=self.ledger(pool), capabilities=self.capabilities)
        run.last_decision = decision.decision
        if decision.eligible is not None:
            run.step_moved = decision.eligible.moved_by
        index = len(run.sends)
        if decision.decision == "mandar":
            self.send(run, now, facts)
            return
        if decision.decision == "esperar":
            following = decision.next_at
            if following is None or following <= now:
                raise ValueError("replay_does_not_progress")
            if facts.purchase_at is not None and now < facts.purchase_at < following:
                # La compra es un hecho con hora: el recorrido se vuelve a mirar cuando ocurre.
                following = facts.purchase_at
            self.push(following, run)
            return
        if decision.decision == "cancelar":
            status = "compro" if decision.reason == "compro" else (decision.reason or "cancelado")
        elif decision.decision in ("vencer", "escalar"):
            status = "sin_horario" if decision.reason == "sin_horario" else "vencio"
        else:   # pausar: la toma humana; el recorrido queda abierto
            status = "en_curso"
        run.steps.append(StepOutcome(index, None, status, run.step_moved, facts.unknown, decision.reason))
        run.finish(status, None if status == "en_curso" else now)

    def send(self, run: _Run, now: datetime, facts: GateFacts) -> None:
        index = len(run.sends)
        unknown = set(facts.unknown)
        if self.captured_at is not None and now > self.captured_at:
            status = "en_curso"
            unknown.add("compra")
        elif index >= 1 and "contesta" in run.plan.exits:
            status = "sin_saber"
        else:
            status = "enviado"
        pool = run.plan.pool
        self.ledger(pool).grant(now, run.firing.source_ref)
        self.grants[(pool, local_day(engine.pool_zone(run.flow, run.plan), now).isoformat())] += 1
        control = self.control(pool)
        if (run.legacy_note is None and control.version_at(now) != run.flow.version
                and control.version_at(run.firing.received_at) == run.flow.version):
            run.legacy_note = (f"lo_en_curso_sigue: el legado lo rechaza por la version"
                               f" (v{control.version_at(now)} activa, recorrido v{run.flow.version})")
        run.steps.append(StepOutcome(index, now, status, run.step_moved, frozenset(unknown), None))
        if status == "en_curso":
            run.finish(status, None)
            return
        run.sends.append(now)
        run.step_moved = ()
        if len(run.sends) >= _allowed_sends(run.plan, self.capabilities):
            run.finish(status, now)
            return
        following = engine.step_start(run.view(), run.plan)
        if following >= run.firing.at + run.plan.expires_after:
            run.steps.append(StepOutcome(len(run.sends), None, "fuera_de_plazo", (), frozenset(),
                                         "next_step_outside_expiration"))
            run.finish("fuera_de_plazo", now)
            return
        self.push(following, run)

    # -- la cola ------------------------------------------------------------

    def run_all(self) -> None:
        for firing in self.history.firings:
            heapq.heappush(self.queue, (firing.at, firing.source_ref, 0, 0))
        firings = {firing.source_ref: firing for firing in self.history.firings}
        for _ in range(_MAX_POPS):
            if not self.queue:
                return
            at, ref, kind, generation = heapq.heappop(self.queue)
            if kind == 0:
                self.arrive(firings[ref])
                continue
            run = self.runs[ref]
            if run.done or run.generation != generation:
                continue
            self.evaluate(run, at)
        raise ValueError("replay_does_not_converge")  # pragma: no cover

    def result(self) -> Replay:
        outcomes = dict(self.outcomes)
        for ref, run in self.runs.items():
            outcomes[ref] = run.outcome()
        runs = tuple(outcomes[firing.source_ref] for firing in self.history.firings)

        def replay_open(source_ref: str, at: datetime) -> bool:
            run = self.runs.get(self.joined.get(source_ref, source_ref))
            if run is None or run.open_from > at:
                return False
            return run.closed_at is None or run.closed_at > at

        def replay_contact(source_ref: str) -> str | None:
            run = self.runs.get(self.joined.get(source_ref, source_ref))
            if run is not None:
                return run.firing.contact_ref
            if source_ref in {firing.source_ref for firing in self.history.firings}:
                return None
            return self.history.case_contact.get(source_ref)

        observed = _observed_open(self.history)
        window = {firing.source_ref for firing in self.history.firings}

        def open_at(source_ref: str, at: datetime) -> bool:
            # La siembra no se replanifica: sus recorridos tienen el intervalo observado.
            return replay_open(source_ref, at) if source_ref in window else observed(source_ref, at)

        bolsa: Counter[tuple[str, str]] = Counter(self.grants)
        for row in self.history.capture.authorizations:
            if row["scope_key"] in self.ledgers and row["action_id"] not in self.history.window_actions:
                bolsa[(row["scope_key"], row["local_budget_date"])] += 1
        return Replay(
            runs=runs,
            supersessions=_supersessions(self.history, open_at, replay_contact),
            bolsa_by_day=MappingProxyType(dict(sorted(bolsa.items()))),
            winners_without_cancel=_winners_without_cancel(self.history, open_at, replay_contact),
        )


def replay(history: History, versions: Versions, choose: VersionChoice, *,
           capabilities: EngineCapabilities = ENGINE_CAPABILITIES_V1) -> Replay:
    """Lo que el motor habria hecho con cada disparo de la ventana, con la version que da ``choose``.

    Un recorrido por disparo, por una cola de horas ``(hora, source_ref)``:

    1. **el arranque**: el flujo apagado no arranca; lo que el adaptador ya
       rechazo queda con su motivo (§2.9) y ``create_recovery_case_failed`` sin
       contacto, como «no llego»; lo demas lo decide ``plan_start`` con la
       persona del replay hasta ese instante. La repeticion de un pago fallido
       que espera su primer envio adelanta su comienzo (F#11, §4.4);
    2. **el plan**: ``plan_steps`` con ``capabilities``. Con la v1, una
       definicion de dos plantillas lanza ``PlanNotSupported``;
    3. **la puerta**: en cada hora tentativa, ``flow_engine.evaluate_gate`` con
       el control de la captura y el libro predicho. «mandar» concede la
       autorizacion; «esperar» vuelve a la cola; lo demas termina el recorrido;
    4. **los envios**: el envio predicho sale en E y se supone aceptado. Si el
       plan tiene otro paso y las capacidades lo dejan, vuelve a la cola en
       ``max(primer envio + espera, este envio)``, o termina en
       ``fuera_de_plazo`` si esa hora no es anterior al vencimiento comun;
    5. **P6**: quien le gana a cada primer contacto, con ``flow_engine.wins`` y
       los intervalos del replay (del caso a su ultimo envio, o a su fin).
    """
    replayer = _Replayer(history, versions, choose, capabilities)
    replayer.run_all()
    return replayer.result()
