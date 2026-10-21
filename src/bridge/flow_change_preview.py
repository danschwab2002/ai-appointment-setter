"""La vista previa de una propuesta de cambio de flujos.

Contrato: docs/contracts/flow-change-cycle-v1.md («La vista previa», los
apartados de :85-93). Diseno: el almacen, §10.2 («La vista previa»).

Arma lo que el operador ve antes de decir que si:

* ``cambios``: por item, el producto, el flujo, el tipo, la version de base y
  el diff campo por campo, con JSON Pointer, contra la definicion activa (una
  version nueva o un ``volver``) o el estado de antes y el de despues (un
  ``prender`` o un ``apagar``);
* ``para_que``: el texto de quien propone, tal cual. Es texto del agente: quien
  lo muestre lo escapa y lo cerca (V2#3);
* ``volver_atras``: a que version se vuelve cada flujo que cambia de contenido;
* ``huecos``: lo que la vista previa todavia no sabe, con su codigo. La
  simulacion, el conteo de lo que esta en curso y a quien afecta los construye
  el motor (pieza 2); hasta entonces van como hueco y el comando de aprobar se
  niega a aprobar sin simulacion salvo que se lo pidan explicitamente.

Es puro: no lee el entorno, no hace red ni toca bases.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .flow_definition import FlowWorld, ProposalItem, diff

__all__ = [
    "GAP_AUDIENCE",
    "GAP_DIFF_TRUNCATED",
    "GAP_IN_FLIGHT",
    "GAP_SIMULATION",
    "PREVIEW_MAX_BYTES",
    "build_preview",
]

# Lo que acepta la base en la columna validation (operator_record_flow_change_validation_v1).
PREVIEW_MAX_BYTES = 131072
# Por item: un diff mas largo se corta y queda el hueco con el total.
DIFF_MAX_CHANGES = 200

GAP_SIMULATION = "simulation_not_built"
GAP_IN_FLIGHT = "in_flight_not_counted"
GAP_AUDIENCE = "audience_change_not_computed"
GAP_DIFF_TRUNCATED = "diff_truncated"


def _state_after(tipo: str, before: str) -> str:
    if tipo == "prender":
        return "prendido"
    if tipo == "apagar":
        return "apagado"
    return before


def _change(item: ProposalItem, world: FlowWorld) -> tuple[dict[str, Any], int]:
    flow = world.flow(item.flow_id)
    if flow is None:
        raise ValueError("flow_not_found")
    out: dict[str, Any] = {
        "producto": item.producto, "flujo": item.flujo, "tipo": item.tipo,
        "base_version": flow.active_version,
    }
    if item.tipo in ("prender", "apagar"):
        out["estado"] = {"antes": flow.state, "despues": _state_after(item.tipo, flow.state)}
        return out, 0
    if item.tipo == "version":
        after: object = item.definicion
    else:  # volver
        if item.version not in flow.versions:
            raise ValueError("flow_change_rollback_target_invalid")
        after = flow.versions[item.version]
        out["a_version"] = item.version
    before = flow.active_definition
    changes = [
        {"ruta": change.path, "antes": change.before, "despues": change.after}
        for change in diff(before if before is not None else {}, after)
        if change.path not in ("/estado", "/version")
    ]
    out["en_curso"] = item.en_curso
    out["diff"] = changes[:DIFF_MAX_CHANGES]
    return out, max(0, len(changes) - DIFF_MAX_CHANGES)


def build_preview(
    items: Sequence[ProposalItem],
    world: FlowWorld,
    *,
    purpose: str,
    warnings: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """La vista previa de los items sobre la foto del almacen.

    ``warnings`` son las advertencias del validador (``ValidationResult.to_json()["warnings"]``):
    van por codigo y ruta, sin el texto.
    """
    cambios: list[dict[str, Any]] = []
    truncated: list[dict[str, Any]] = []
    for item in items:
        change, cut = _change(item, world)
        cambios.append(change)
        if cut:
            truncated.append({"producto": item.producto, "flujo": item.flujo, "sin_mostrar": cut})
    volver_atras = [
        {"producto": item.producto, "flujo": item.flujo, "a_version": flow.active_version}
        for item in items
        if item.is_content
        for flow in (world.flow(item.flow_id),)
        if flow is not None and flow.active_version is not None
    ]
    politicas = sorted({item.en_curso for item in items if item.en_curso is not None})
    huecos: list[dict[str, Any]] = [{"code": GAP_SIMULATION}, {"code": GAP_IN_FLIGHT}, {"code": GAP_AUDIENCE}]
    if truncated:
        huecos.append({"code": GAP_DIFF_TRUNCATED, "items": truncated})
    preview: dict[str, Any] = {
        "cambios": cambios,
        "para_que": purpose,
        "a_quien": None,
        "en_curso": {"politica": politicas[0] if len(politicas) == 1 else (politicas or None),
                     "recorridos": None},
        "simulacion": None,
        "advertencias": [
            {"code": warning.get("code"), "path": warning.get("path")}
            for warning in warnings if isinstance(warning, Mapping)
        ],
        "empieza": "al_publicar",
        "volver_atras": volver_atras,
        "huecos": huecos,
    }
    if len(json.dumps(preview, ensure_ascii=False).encode("utf-8")) > PREVIEW_MAX_BYTES:
        for change in preview["cambios"]:
            if "diff" in change:
                change["diff_count"] = len(change["diff"])
                change["diff"] = []
        preview["huecos"] = [gap for gap in huecos if gap["code"] != GAP_DIFF_TRUNCATED]
        preview["huecos"].append({"code": GAP_DIFF_TRUNCATED, "items": "all"})
    return preview
