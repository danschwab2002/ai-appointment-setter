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
* ``mensajes`` (1.10.0, M16 P9): con el catalogo de la bandeja, cada plantilla
  de los pasos como la ve el lead: el encabezado, el cuerpo con cada marcador
  cambiado por el nombre de su variable entre corchetes (``[nombre]``), el pie y
  los botones. Sin catalogo no va;
* ``huecos``: lo que la vista previa todavia no sabe, con su codigo. La
  simulacion, el conteo de lo que esta en curso y a quien afecta los construye
  el motor (pieza 2); hasta entonces van como hueco y el comando de aprobar se
  niega a aprobar sin simulacion salvo que se lo pidan explicitamente.

Es puro: no lee el entorno, no hace red ni toca bases.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .flow_definition import FlowWorld, ProposalItem, TemplateCatalog, diff

__all__ = [
    "GAP_AUDIENCE",
    "GAP_DIFF_TRUNCATED",
    "GAP_IN_FLIGHT",
    "GAP_MESSAGES_TRUNCATED",
    "GAP_SIMULATION",
    "GAP_SIMULATION_FAILED",
    "PREVIEW_MAX_BYTES",
    "build_preview",
    "message_as_seen",
    "messages_for_items",
    "template_view",
]

# Lo que acepta la base en la columna validation (operator_record_flow_change_validation_v1).
PREVIEW_MAX_BYTES = 131072
# Por item: un diff mas largo se corta y queda el hueco con el total.
DIFF_MAX_CHANGES = 200

GAP_SIMULATION = "simulation_not_built"
GAP_SIMULATION_FAILED = "simulation_failed"
GAP_IN_FLIGHT = "in_flight_not_counted"
GAP_AUDIENCE = "audience_change_not_computed"
GAP_DIFF_TRUNCATED = "diff_truncated"
GAP_MESSAGES_TRUNCATED = "messages_truncated"


# Lo que se muestra de una plantilla (1.10.0): Meta acepta hasta 1024 caracteres en el cuerpo.
TEXT_MAX_CHARS = 1100
MESSAGES_MAX = 20
_MARKER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
_LINK_WORDS = {"checkout": "link de pago", "ficha": "link de la ficha"}


def _text(value: object) -> str | None:
    return value[:TEXT_MAX_CHARS] if isinstance(value, str) else None


def _components(template: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    components = template.get("components")
    return [c for c in components if isinstance(c, Mapping)] if isinstance(components, list) else []


def template_view(template: Mapping[str, Any]) -> dict[str, Any]:
    """Una plantilla del catalogo de Chatwoot como la lista el Automation Expert (list_templates).

    Lo que necesita para elegirla: nombre, idioma, estado y categoria, cuantos
    marcadores tiene el cuerpo, el texto de cada parte y los botones, con su
    tipo y si el link cambia por persona (``{{1}}`` en la URL).
    """
    header = body = footer = None
    buttons: list[dict[str, Any]] = []
    for component in _components(template):
        kind = component.get("type")
        if kind == "HEADER":
            header = {"formato": component.get("format", "TEXT") if isinstance(component.get("format", "TEXT"), str)
                      else None, "texto": _text(component.get("text"))}
        elif kind == "BODY":
            body = _text(component.get("text"))
        elif kind == "FOOTER":
            footer = _text(component.get("text"))
        elif kind == "BUTTONS" and isinstance(component.get("buttons"), list):
            for button in component["buttons"]:
                if not isinstance(button, Mapping):
                    continue
                url = button.get("url") if isinstance(button.get("url"), str) else None
                buttons.append({"tipo": button.get("type") if isinstance(button.get("type"), str) else None,
                                "texto": _text(button.get("text")),
                                "link": _text(url),
                                "link_cambia": bool(url and _MARKER.search(url))})
    markers = sorted({match for match in _MARKER.findall(body or "")}, key=lambda m: (not m.isdigit(), m.zfill(4)))
    return {
        "nombre": template.get("name") if isinstance(template.get("name"), str) else None,
        "idioma": template.get("language").strip() if isinstance(template.get("language"), str) else None,
        "estado": template.get("status") if isinstance(template.get("status"), str) else None,
        "aprobada": template.get("status") == "APPROVED",
        "categoria": template.get("category") if isinstance(template.get("category"), str) else None,
        "marcadores": len(markers),
        "encabezado": header,
        "cuerpo": body,
        "pie": footer,
        "botones": buttons,
    }


def _fill(text: str | None, variables: Sequence[object], *, fallback: str) -> str | None:
    if text is None:
        return None

    def name(match: re.Match[str]) -> str:
        marker = match.group(1)
        if marker.isdigit() and 1 <= int(marker) <= len(variables) and isinstance(variables[int(marker) - 1], str):
            return f"[{variables[int(marker) - 1]}]"
        return f"[{fallback}]"

    return _MARKER.sub(name, text)


def message_as_seen(template: Mapping[str, Any], step: Mapping[str, Any]) -> dict[str, Any]:
    """El mensaje de un paso como lo ve el lead: cada marcador, con el nombre de su variable.

    Las variables son las del paso, en el orden de los marcadores
    (``{{1}}`` es la primera). Un marcador sin variable queda como
    ``[sin variable]``, que es lo que el validador frena. Un boton con link que
    cambia muestra la base y ``[link de pago]`` o ``[link de la ficha]``.
    """
    view = template_view(template)
    variables = step.get("variables") if isinstance(step.get("variables"), list) else []
    button = step.get("boton") if isinstance(step.get("boton"), Mapping) else {}
    link_word = _LINK_WORDS.get(str(button.get("link")), "link")
    header = view["encabezado"]
    return {
        "encabezado": None if header is None else (
            header["texto"] if header.get("formato") == "TEXT" else f"[{str(header.get('formato')).lower()}]"),
        "cuerpo": _fill(view["cuerpo"], variables, fallback="sin variable"),
        "pie": view["pie"],
        "botones": [{"tipo": b["tipo"], "texto": b["texto"],
                     **({"link": _fill(b["link"], [], fallback=link_word)} if b["link"] else {})}
                    for b in view["botones"]],
    }


def _template_steps(steps: object, at: str) -> list[tuple[str, Mapping[str, Any]]]:
    """Los pasos de plantilla de una definicion, con su ruta (JSON Pointer), ramas incluidas."""
    found: list[tuple[str, Mapping[str, Any]]] = []
    if not isinstance(steps, list):
        return found
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            continue
        path = f"{at}/{index}"
        if "plantilla" in step:
            found.append((path, step))
        for branch in ("entonces", "si_no"):
            found += _template_steps(step.get(branch), f"{path}/{branch}")
    return found


def messages_for_items(items: Sequence[ProposalItem], world: FlowWorld,
                       catalog: TemplateCatalog) -> list[dict[str, Any]]:
    """Por cada paso de plantilla de lo que queda publicado, el mensaje como lo ve el lead."""
    out: list[dict[str, Any]] = []
    for item in items:
        if item.tipo in ("version", "nuevo"):
            definition = item.definicion
        elif item.tipo == "volver" and item.flow_id is not None:
            flow = world.flow(item.flow_id)
            definition = None if flow is None else flow.versions.get(item.version)  # type: ignore[arg-type]
        else:
            continue
        if not isinstance(definition, Mapping):
            continue
        for path, step in _template_steps(definition.get("pasos"), "/pasos"):
            name, language = step.get("plantilla"), step.get("idioma")
            entry: dict[str, Any] = {"producto": item.producto, "flujo": item.flujo, "ruta": path}
            if isinstance(name, Mapping):
                entry.update(plantilla=None, a_crear=name.get("a_crear"), encontrada=False)
            else:
                same = [t for t in catalog.named(name) if isinstance(t.get("language"), str)
                        and t["language"].strip() == language] if isinstance(name, str) else []
                entry.update(plantilla=name, idioma=language, encontrada=len(same) == 1)
                if len(same) == 1:
                    entry.update(categoria=same[0].get("category"), estado=same[0].get("status"),
                                 como_lo_ve=message_as_seen(same[0], step))
            out.append(entry)
            if len(out) >= MESSAGES_MAX:
                return out
    return out


def _state_after(tipo: str, before: str, a: str | None = None) -> str:
    if tipo == "prender":
        # Ciclo 1.8.0: prender con a: cohorte vuelve a la cohorte de prueba.
        return a if a == "cohorte" else "prendido"
    if tipo == "apagar":
        return "apagado"
    return before


def _new_flow_change(item: ProposalItem) -> tuple[dict[str, Any], int]:
    """Un flujo nuevo (ciclo 1.7.0): todo es nuevo, nace en cohorte y no hay version a la que volver."""
    changes = [
        {"ruta": change.path, "antes": change.before, "despues": change.after}
        for change in diff({}, item.definicion or {})
        if change.path not in ("/estado", "/version")
    ]
    out: dict[str, Any] = {
        "producto": item.producto, "flujo": item.flujo, "tipo": item.tipo, "base_version": None,
        "nace_en": "cohorte", "en_curso": None, "diff": changes[:DIFF_MAX_CHANGES],
    }
    return out, max(0, len(changes) - DIFF_MAX_CHANGES)


def _change(item: ProposalItem, world: FlowWorld) -> tuple[dict[str, Any], int]:
    if item.is_new_flow:
        return _new_flow_change(item)
    flow = world.flow(item.flow_id) if item.flow_id is not None else None
    if flow is None:
        raise ValueError("flow_not_found")
    out: dict[str, Any] = {
        "producto": item.producto, "flujo": item.flujo, "tipo": item.tipo,
        "base_version": flow.active_version,
    }
    if item.tipo in ("prender", "apagar"):
        out["estado"] = {"antes": flow.state, "despues": _state_after(item.tipo, flow.state, item.a)}
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
    simulacion: Mapping[str, Any] | None = None,
    catalog: TemplateCatalog | None = None,
) -> dict[str, Any]:
    """La vista previa de los items sobre la foto del almacen.

    ``warnings`` son las advertencias del validador (``ValidationResult.to_json()["warnings"]``):
    van por codigo y ruta, sin el texto. ``simulacion`` es la de
    ``flow_change_simulation.build_simulation``: con ella, el hueco
    ``simulation_not_built`` se va; si fallo, queda ``simulation_failed`` con el motivo.
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
        if item.is_content and item.flow_id is not None
        for flow in (world.flow(item.flow_id),)
        if flow is not None and flow.active_version is not None
    ]
    # Un flujo nuevo no tiene version anterior: su vuelta atras es pausarlo o apagarlo (flow-engine-v2 §2).
    volver_atras += [
        {"producto": item.producto, "flujo": item.flujo, "a_version": None, "como": "pausar_o_apagar"}
        for item in items if item.is_new_flow
    ]
    politicas = sorted({item.en_curso for item in items if item.en_curso is not None})
    if simulacion is None:
        huecos: list[dict[str, Any]] = [{"code": GAP_SIMULATION}]
    elif simulacion.get("status") == "built":
        huecos = []
    else:
        huecos = [{"code": GAP_SIMULATION_FAILED, "reason_code": simulacion.get("reason_code")}]
    huecos += [{"code": GAP_IN_FLIGHT}, {"code": GAP_AUDIENCE}]
    if truncated:
        huecos.append({"code": GAP_DIFF_TRUNCATED, "items": truncated})
    preview: dict[str, Any] = {
        "cambios": cambios,
        "para_que": purpose,
        # Un flujo nuevo le llega solo a los contactos de su cohorte de prueba (D6).
        # Ciclo 1.8.0: lo mismo un prender con a: cohorte.
        "a_quien": "cohorte" if any(item.is_new_flow or item.a == "cohorte" for item in items) else None,
        "en_curso": {"politica": politicas[0] if len(politicas) == 1 else (politicas or None),
                     "recorridos": None},
        "simulacion": None if simulacion is None else dict(simulacion),
        "advertencias": [
            {"code": warning.get("code"), "path": warning.get("path")}
            for warning in warnings if isinstance(warning, Mapping)
        ],
        "empieza": "al_publicar",
        "volver_atras": volver_atras,
        "huecos": huecos,
    }
    if catalog is not None:
        preview["mensajes"] = messages_for_items(items, world, catalog)
    if len(json.dumps(preview, ensure_ascii=False).encode("utf-8")) > PREVIEW_MAX_BYTES:
        for change in preview["cambios"]:
            if "diff" in change:
                change["diff_count"] = len(change["diff"])
                change["diff"] = []
        preview["huecos"] = [gap for gap in huecos if gap["code"] != GAP_DIFF_TRUNCATED]
        preview["huecos"].append({"code": GAP_DIFF_TRUNCATED, "items": "all"})
    # Los mensajes ocupan lugar (E-213): si sin los diffs todavia no entra, salen los mensajes y queda
    # su hueco, para que la base no rechace la validacion (flow_change_preview_invalid).
    if "mensajes" in preview and len(json.dumps(preview, ensure_ascii=False).encode("utf-8")) > PREVIEW_MAX_BYTES:
        preview["mensajes_count"] = len(preview["mensajes"])
        preview["mensajes"] = []
        preview["huecos"].append({"code": GAP_MESSAGES_TRUNCATED})
    return preview
