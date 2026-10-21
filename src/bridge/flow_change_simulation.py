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

from .flow_definition import ProposalItem
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
    "build_simulation",
    "engine_flows_from_read",
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
              time_zone: str | None) -> dict[str, Any]:
    try:
        versions = engine_flows_from_read(engine, time_zone=time_zone)
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


def build_simulation(engine: Any, history: Any, items: Sequence[ProposalItem], *,
                     time_zone: str | None = None) -> dict[str, Any]:
    """La simulacion de los items sobre la historia: ``built`` con su resumen, o ``failed`` con el motivo.

    Nunca levanta: lo que no se puede simular es un ``failed`` y la vista previa
    lo muestra como «la simulacion no se pudo armar».
    """
    try:
        if not isinstance(engine, Mapping):
            raise _Failed(READ_INVALID)
        return _simulate(engine, history, items, time_zone)
    except _Failed as failed:
        return {"status": "failed", "reason_code": failed.reason_code}
    except Exception:  # noqa: BLE001 - la vista previa no se cae por la simulacion
        return {"status": "failed", "reason_code": SIMULATION_ERROR}
