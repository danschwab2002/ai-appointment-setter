"""La simulacion de una propuesta, para la vista previa (flow-engine-v1, «La simulacion de una propuesta»).

La T4c. Toma lo que leyo quien valida (el bridge con la clave del rol, o el
comando del operador con ``psql``) y no lee nada por su cuenta:

* ``engine``: la respuesta de ``read_flow_engine_v1`` (seccion 1), que se
  traduce a ``Versions`` con ``engine_flows_from_read``;
* ``history``: la de ``read_flow_engine_history_v1`` (seccion 2), la captura
  v3 de la paridad, que se valida entera antes de usarla (``CaptureV3.from_json``
  rechaza todo lo que no sea su forma cerrada, y la forma es la prueba de
  privacidad);
* los ``ProposalItem`` de la propuesta.

La vigente de cada flujo es la ``active_version`` que dio la lectura del motor
(un flujo apagado no arranca nada), y la propuesta sale de ``proposed_versions``.
Las dos corren sobre la misma historia con las capacidades del motor de la
instancia (``ENGINE_CAPABILITIES_V1``), nunca con las del lienzo.

**El resultado** es lo que queda congelado en ``validation.preview.simulacion``
y lee ``_flow_change_approve`` (migracion 20261021000100):

* ``{"status": "built", "summary": {...}, "summary_sha256": "<64 hex>", "cambios": [...]}``;
* ``{"status": "failed", "reason_code": "..."}`` si la simulacion no se pudo
  armar: la historia vino cortada o con otra forma, la lectura del motor no se
  entiende, o la propuesta trae algo que el motor no ejecuta.

El ``summary_sha256`` es el sha256 del resumen en JSON canonico (claves
ordenadas, sin espacios, UTF-8): lo que se aprobo se puede volver a calcular.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from .flow_definition import FlowWorld, ProposalItem
from .flow_engine import ENGINE_CAPABILITIES_V1, EngineFlow, PlanNotSupported, not_supported
from .flow_parity import CaptureV3
from .flow_replay import (
    Versions,
    exceptions_for,
    fixed,
    history_from_capture,
    plan_step_keys,
    proposed_versions,
    simulate,
)

__all__ = [
    "CAMBIOS_MAX",
    "NEW_FLOW_COUNTS_CONTRACT",
    "build_native_flow_simulation",
    "build_new_flow_simulation",
    "build_simulation",
    "engine_flows_from_read",
    "native_flow_counts_request",
    "summary_sha256",
]

ENGINE_CONTRACT = "flow-engine-v1"
ENGINE_CAPABILITIES = "v1"
# La vista previa muestra los primeros cambios; el resumen los cuenta todos.
CAMBIOS_MAX = 20
_REASON = re.compile(r"[a-z][a-z0-9_:]{0,79}")

READ_INVALID = "flow_engine_read_invalid"
CAPABILITIES_UNKNOWN = "flow_engine_capabilities_unknown"
HISTORY_INVALID = "flow_engine_history_invalid"
MULTI_PRODUCT = "simulation_multi_product"
SIMULATION_ERROR = "simulation_error"


class _Failed(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code if _REASON.fullmatch(reason_code) else SIMULATION_ERROR


def engine_flows_from_read(document: Mapping[str, Any], *, time_zone: str | None = None) -> Versions:
    """Las versiones de cada flujo de una respuesta de ``read_flow_engine_v1``.

    Lo mismo que ``flow_replay.engine_flows_from_definitions`` con el documento
    de la paridad: una ``EngineFlow`` por version, con el ``legacy_kind`` de su
    ruta, la zona fija (la de la version mas vieja con un horario objeto, o la
    de la instancia), la bolsa que trae la version y las excepciones de su
    ``legacy_kind``. Levanta ``ValueError`` si la forma no es la del contrato.
    """
    if not isinstance(document, Mapping) or document.get("contract") != ENGINE_CONTRACT:
        raise ValueError(READ_INVALID)
    flows = document.get("flows")
    if not isinstance(flows, list):
        raise ValueError(READ_INVALID)
    out: dict[tuple[str, str], dict[int, EngineFlow]] = {}
    for flow in flows:
        if not isinstance(flow, Mapping) or not isinstance(flow.get("versions"), list):
            raise ValueError(READ_INVALID)
        route = flow.get("route")
        legacy_kind = route.get("legacy_kind") if isinstance(route, Mapping) else None
        rows = sorted(flow["versions"], key=lambda row: row.get("version") if isinstance(row, Mapping) else 0)
        fixed_zone = time_zone
        for row in rows:
            horario = row.get("definition", {}).get("horario") if isinstance(row, Mapping) else None
            if isinstance(horario, Mapping) and isinstance(horario.get("zona"), str):
                fixed_zone = horario["zona"]
                break
        for row in rows:
            if not isinstance(row, Mapping) or type(row.get("version")) is not int \
                    or not isinstance(row.get("definition"), Mapping) or not isinstance(row.get("pool"), str):
                raise ValueError(READ_INVALID)
            definition = row["definition"]
            engine_flow = EngineFlow(
                flow_id=flow["flow_id"], product_ref=flow["product_ref"], flow_key=flow["flow_key"],
                version=row["version"], definition=MappingProxyType(dict(definition)), legacy_kind=legacy_kind,
                fixed_zone=fixed_zone, pool=row["pool"],
                exceptions=exceptions_for(legacy_kind, plan_step_keys(definition, legacy_kind)),
            )
            out.setdefault(engine_flow.ref, {})[engine_flow.version] = engine_flow
    return MappingProxyType({ref: MappingProxyType(by_version) for ref, by_version in out.items()})


def _with_return_targets(engine: Mapping[str, Any], store: Any, items: Sequence[ProposalItem]) -> Mapping[str, Any]:
    """La lectura del motor con el destino de cada ``volver`` que no trae, sacado del almacen.

    ``read_flow_engine_v1`` devuelve de cada flujo la activa y las versiones con
    recorridos del motor abiertos (20261021000200, seccion 7): un ``volver`` a
    una version publicada que ya no corre no esta ahi, y la simulacion daba
    ``proposal_version_unknown`` (ATT1, 22/10, ``volver att1/carrito 1`` con la
    v2 activa). El almacen (``read_flow_store_v1``) trae todas las versiones
    publicadas: la que falta entra con su definicion y la bolsa como la arma la
    lectura del motor (``topes.bolsa`` o el ``legacy_scope_key`` de la ruta).
    Sin ``volver``, sin almacen o sin esa version en el almacen, la lectura
    queda tal cual.
    """
    wanted = {(item.flow_id, item.version) for item in items if item.tipo == "volver"}
    flows = engine.get("flows")
    if not wanted or not isinstance(store, Mapping) or not isinstance(store.get("flows"), list) \
            or not isinstance(flows, list):
        return engine
    published = {flow.get("flow_id"): flow.get("versions") for flow in store["flows"]
                 if isinstance(flow, Mapping) and isinstance(flow.get("versions"), Mapping)}
    out = []
    for flow in flows:
        if not isinstance(flow, Mapping) or not isinstance(flow.get("versions"), list):
            out.append(flow)
            continue
        present = {row.get("version") for row in flow["versions"] if isinstance(row, Mapping)}
        route = flow.get("route")
        scope = route.get("legacy_scope_key") if isinstance(route, Mapping) else None
        added = []
        for flow_id, number in sorted(wanted, key=lambda pair: (str(pair[0]), pair[1] or 0)):
            definition = (published.get(flow_id) or {}).get(str(number))
            if flow_id != flow.get("flow_id") or number in present or not isinstance(definition, Mapping):
                continue
            topes = definition.get("topes")
            pool = topes.get("bolsa") if isinstance(topes, Mapping) else None
            pool = pool if isinstance(pool, str) else scope
            if isinstance(pool, str):
                added.append({"version": number, "definition": definition, "pool": pool, "in_flight": 0})
        out.append({**flow, "versions": [*flow["versions"], *added]} if added else flow)
    return {**engine, "flows": out}


def _current(document: Mapping[str, Any], versions: Versions) -> dict[tuple[str, str], int | None]:
    """La vigente de cada flujo: la activa de la lectura, si el flujo esta prendido."""
    out: dict[tuple[str, str], int | None] = {}
    for flow in document["flows"]:
        ref = (flow["product_ref"], flow["flow_key"])
        active = flow.get("active_version")
        out[ref] = active if flow.get("state") == "prendido" and active in versions.get(ref, {}) else None
    return out


def summary_sha256(summary: Mapping[str, Any]) -> str:
    canonical = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _simulate(engine: Mapping[str, Any], history_in: Any, items: Sequence[ProposalItem],
              time_zone: str | None, store: Any) -> dict[str, Any]:
    try:
        versions = engine_flows_from_read(_with_return_targets(engine, store, items), time_zone=time_zone)
    except (ValueError, KeyError, TypeError) as error:
        raise _Failed(READ_INVALID) from error
    if (engine.get("engine") or {}).get("capabilities") != ENGINE_CAPABILITIES:
        raise _Failed(CAPABILITIES_UNKNOWN)
    if len({item.producto for item in items}) != 1:
        raise _Failed(MULTI_PRODUCT)
    if isinstance(history_in, Mapping) and history_in.get("ok") is False:
        raise _Failed(str(history_in.get("reason_code") or HISTORY_INVALID))
    try:
        history = history_from_capture(CaptureV3.from_json(history_in))
    except (ValueError, KeyError, TypeError) as error:
        raise _Failed(HISTORY_INVALID) from error
    for item in items:
        if item.tipo == "version":
            codes = not_supported(item.definicion or {}, ENGINE_CAPABILITIES_V1)
            if codes:
                raise _Failed(codes[0])
    current = _current(engine, versions)
    try:
        proposed, chosen = proposed_versions(versions, current, items)
    except ValueError as error:
        raise _Failed(str(error).split(" ", 1)[0].rstrip(":") or SIMULATION_ERROR) from error
    try:
        result = simulate(history, proposed, fixed(current), fixed(chosen), capabilities=ENGINE_CAPABILITIES_V1)
    except PlanNotSupported as error:
        raise _Failed(error.codes[0] if error.codes else SIMULATION_ERROR) from error
    summary = json.loads(json.dumps(dict(result.summary), ensure_ascii=False, default=str))
    return {
        "status": "built",
        "summary": summary,
        "summary_sha256": summary_sha256(summary),
        "cambios": [{"disparo": change.firing_ref, "flujo": change.after.flow[1],
                     "antes": change.before.end, "despues": change.after.end}
                    for change in result.changes[:CAMBIOS_MAX]],
    }


# La simulacion de un flujo nuevo (flow-engine-v2 §14, la 2.2.0): la lectura
# read_flow_engine_native_trigger_counts_v1 cuenta los disparos de su disparador.
NEW_FLOW_COUNTS_CONTRACT = "flow-engine-v2"
NEW_FLOW_NOT_BUILT = "simulation_new_flow_not_built"
NEW_FLOW_MIXED = "simulation_new_flow_mixed"
NEW_FLOW_COUNTS_INVALID = "simulation_new_flow_counts_invalid"
_COUNT_KEYS = frozenset({"contract", "trigger", "product_ref", "since", "until", "events", "people"})


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _Failed(NEW_FLOW_COUNTS_INVALID)
    return value


def _sharing_the_event(world: FlowWorld | None, product: str, trigger: object) -> list[dict[str, str]]:
    """Los flujos nativos del producto, abiertos al evento, que arrancan con el mismo (E-139).

    El motor abre un solo caso por evento: con uno de estos al lado, «si fuera
    para todos» no le llega a todas esas personas. Pausado no abre caso y
    apagado no se admite, asi que no cuentan.
    """
    if world is None or not isinstance(trigger, str):
        return []
    found = []
    for flow in world.flows:
        definition = flow.active_definition
        event = ((definition or {}).get("dispara") or {}).get("evento") if isinstance(definition, Mapping) else None
        if flow.native and flow.product_ref == product and flow.state in ("prendido", "cohorte") \
                and event == trigger:
            found.append({"flujo": f"{flow.product_ref}/{flow.flow_key}", "estado": flow.state})
    return sorted(found, key=lambda entry: entry["flujo"])


def build_new_flow_simulation(counts: Any, items: Sequence[ProposalItem], *,
                              world: FlowWorld | None = None) -> dict[str, Any]:
    """La simulacion de un item ``nuevo``: cuantos disparos de su disparador hubo y a cuantas personas
    les habria llegado si estuviera para todos.

    ``counts`` es la respuesta de ``read_flow_engine_native_trigger_counts_v1`` para el producto y el
    disparador del flujo nuevo, con la ventana de la vista previa. Es una estimacion con los eventos
    reales: con ``repeticion.otra_despues_de: nunca`` le llega una sola vez a cada persona, y mientras
    este en cohorte solo a los contactos de la cohorte. Nunca levanta.

    Con ``world`` (la foto del almacen que ya leyo quien valida), si otro flujo nativo del producto
    arranca con el mismo evento, el resumen lo nombra en ``comparte_el_evento_con`` (E-139): el motor
    abre un solo caso por evento y uno de los dos no arranca. Sin ninguno, el resumen es el de antes.
    """
    try:
        if len(items) != 1 or not items[0].is_new_flow:
            raise _Failed(NEW_FLOW_MIXED)
        if not isinstance(counts, Mapping) or set(counts) != _COUNT_KEYS \
                or counts.get("contract") != NEW_FLOW_COUNTS_CONTRACT:
            raise _Failed(NEW_FLOW_COUNTS_INVALID)
        item = items[0]
        trigger = ((item.definicion or {}).get("dispara") or {}).get("evento")
        if counts.get("product_ref") != item.producto or counts.get("trigger") != trigger:
            raise _Failed(NEW_FLOW_COUNTS_INVALID)
        events, people = _count(counts.get("events")), _count(counts.get("people"))
        if people > events:
            raise _Failed(NEW_FLOW_COUNTS_INVALID)
        summary = {
            "flujo_nuevo": f"{item.producto}/{item.flujo}", "disparador": trigger,
            "desde": counts.get("since"), "hasta": counts.get("until"),
            "disparos": events, "personas": people, "mensajes_si_fuera_para_todos": people,
            "nace_en": "cohorte",
        }
        sharing = _sharing_the_event(world, item.producto, trigger)
        if sharing:
            summary["comparte_el_evento_con"] = sharing
        return {"status": "built", "summary": summary, "summary_sha256": summary_sha256(summary), "cambios": []}
    except _Failed as failed:
        return {"status": "failed", "reason_code": failed.reason_code}
    except Exception:  # noqa: BLE001 - la vista previa no se cae por la simulacion
        return {"status": "failed", "reason_code": SIMULATION_ERROR}


# La simulacion de un cambio sobre un flujo nativo (E-241): prender, version, volver o apagar un
# flujo que nacio en el motor. La historia del motor (read_flow_engine_history_v1) es la del
# carrito del legado y exige su ruta (flow_engine_history_cart_route_not_found en una instancia sin
# ella, como Maribel); un flujo nativo no corre sobre esa historia. Se cuentan los disparos de su
# evento con la misma lectura del nuevo.
NATIVE_FLOW_MIXED = "simulation_native_flow_mixed"
NATIVE_FLOW_COUNTS_INVALID = "simulation_native_flow_counts_invalid"
NATIVE_ITEM_TYPES = frozenset({"prender", "version", "volver", "apagar"})


def _event_of(definition: object) -> object:
    return ((definition.get("dispara") or {}).get("evento")) if isinstance(definition, Mapping) else None


def native_flow_counts_request(store: Any, items: Any) -> dict[str, Any] | None:
    """Que lectura pide la simulacion de una propuesta sobre flujos nativos, con lo que ya leyo quien valida.

    ``store`` es la respuesta de ``read_flow_store_v1`` (o la del operador, con la misma forma) e
    ``items``, los de la propuesta tal cual estan guardados. Devuelve:

    * ``None`` si algun item no es de un flujo nativo (o es un ``nuevo``): la simulacion sigue por
      la historia del motor, como antes;
    * ``{"product_ref", "trigger"}`` si hay un solo item, de un flujo nativo: el evento es el de la
      definicion que va a quedar (la del item en una ``version``, la del destino en un ``volver``,
      la activa en un ``prender`` o un ``apagar``);
    * ``{"simulation_unavailable": <motivo>}`` si son todos nativos pero no se puede pedir.

    Nativo es lo que dice el almacen (``native``, una ruta del motor sin ``legacy_kind``) o un flujo en
    ``cohorte``, que siempre es nativo (``WorldFlow.is_native``). La gemela, sin importar el bridge,
    esta en ``scripts/flow_change_operator.py`` (``_native_flow_counts_request``).
    """
    if not isinstance(items, list) or not items or not isinstance(store, Mapping) \
            or not isinstance(store.get("flows"), list):
        return None
    flows = {flow.get("flow_id"): flow for flow in store["flows"] if isinstance(flow, Mapping)}
    found = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("tipo") not in NATIVE_ITEM_TYPES:
            return None
        flow = flows.get(item.get("flow_id"))
        if not isinstance(flow, Mapping) or not (flow.get("native") is True or flow.get("state") == "cohorte"):
            return None
        found.append((item, flow))
    if len(found) != 1:
        return {"simulation_unavailable": NATIVE_FLOW_MIXED}
    item, flow = found[0]
    versions = flow.get("versions") if isinstance(flow.get("versions"), Mapping) else {}
    if item["tipo"] == "version":
        definition = item.get("definicion")
    elif item["tipo"] == "volver":
        definition = versions.get(str(item.get("version")))
    else:
        definition = versions.get(str(flow.get("active_version")))
    trigger = _event_of(definition)
    product = item.get("producto")
    if not isinstance(trigger, str) or not isinstance(product, str):
        return {"simulation_unavailable": NATIVE_FLOW_COUNTS_INVALID}
    return {"product_ref": product, "trigger": trigger}


def build_native_flow_simulation(counts: Any, items: Sequence[ProposalItem], *,
                                 world: FlowWorld | None) -> dict[str, Any]:
    """La simulacion de un ``prender``, ``version``, ``volver`` o ``apagar`` de un flujo nativo (E-241).

    ``counts`` es la respuesta de ``read_flow_engine_native_trigger_counts_v1`` para el evento del
    flujo en la ventana de la vista previa (7 dias). El evento se vuelve a sacar aca de ``world`` y
    los items (no se cree el que mando quien leyo) y la respuesta tiene que ser de ese producto y ese
    evento. Es la misma estimacion que la de un ``nuevo``: cuantos disparos hubo y de cuantas
    personas; el resumen dice ademas el flujo, el tipo de cambio y el estado de hoy, y nombra en
    ``comparte_el_evento_con`` otro flujo nativo del producto, abierto, con el mismo evento.
    Nunca levanta.
    """
    try:
        if world is None or len(items) != 1:
            raise _Failed(NATIVE_FLOW_MIXED)
        item = items[0]
        flow = world.flow(item.flow_id) if item.flow_id is not None else None
        if item.tipo not in NATIVE_ITEM_TYPES or flow is None or not flow.is_native:
            raise _Failed(NATIVE_FLOW_MIXED)
        if item.tipo == "version":
            definition: object = item.definicion
        elif item.tipo == "volver":
            definition = flow.versions.get(item.version) if item.version is not None else None
        else:
            definition = flow.active_definition
        trigger = _event_of(definition)
        if not isinstance(trigger, str) or not isinstance(counts, Mapping) or set(counts) != _COUNT_KEYS \
                or counts.get("contract") != NEW_FLOW_COUNTS_CONTRACT \
                or counts.get("product_ref") != item.producto or counts.get("trigger") != trigger:
            raise _Failed(NATIVE_FLOW_COUNTS_INVALID)
        try:
            events, people = _count(counts.get("events")), _count(counts.get("people"))
        except _Failed as error:
            raise _Failed(NATIVE_FLOW_COUNTS_INVALID) from error
        if people > events:
            raise _Failed(NATIVE_FLOW_COUNTS_INVALID)
        summary: dict[str, Any] = {
            "flujo": f"{item.producto}/{item.flujo}", "tipo": item.tipo, "estado_hoy": flow.state,
            "disparador": trigger, "desde": counts.get("since"), "hasta": counts.get("until"),
            "disparos": events, "personas": people,
        }
        own = f"{flow.product_ref}/{flow.flow_key}"
        sharing = [entry for entry in _sharing_the_event(world, item.producto, trigger) if entry["flujo"] != own]
        if sharing:
            summary["comparte_el_evento_con"] = sharing
        return {"status": "built", "summary": summary, "summary_sha256": summary_sha256(summary), "cambios": []}
    except _Failed as failed:
        return {"status": "failed", "reason_code": failed.reason_code}
    except Exception:  # noqa: BLE001 - la vista previa no se cae por la simulacion
        return {"status": "failed", "reason_code": SIMULATION_ERROR}


def build_simulation(engine: Any, history: Any, items: Sequence[ProposalItem], *,
                     time_zone: str | None = None, store: Any = None) -> dict[str, Any]:
    """La simulacion de los items sobre la historia: ``built`` con su resumen, o ``failed`` con el motivo.

    ``store`` es la foto del almacen (``read_flow_store_v1``) que ya leyo quien
    valida: de ahi sale el destino de un ``volver`` que la lectura del motor no
    trae (``_with_return_targets``).

    Nunca levanta: lo que no se puede simular es un ``failed`` y la vista previa
    lo muestra como «la simulacion no se pudo armar».
    """
    try:
        if any(item.is_new_flow for item in items):
            # Un flujo nuevo no tiene historia en el motor: se simula con su propia lectura.
            raise _Failed(NEW_FLOW_NOT_BUILT)
        if not isinstance(engine, Mapping):
            raise _Failed(READ_INVALID)
        return _simulate(engine, history, items, time_zone, store)
    except _Failed as failed:
        return {"status": "failed", "reason_code": failed.reason_code}
    except Exception:  # noqa: BLE001 - la vista previa no se cae por la simulacion
        return {"status": "failed", "reason_code": SIMULATION_ERROR}
