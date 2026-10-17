#!/usr/bin/env python3
"""Corre la simulacion del motor de flujos sobre una captura e imprime el resultado.

Diseno de la T3a (2026-10-17), §3.3, §5.3 y §7:

    uv run python scripts/flow_engine_simulate.py --consistencia
    uv run python scripts/flow_engine_simulate.py --propuesta propuesta.json [--varios-toques] \\
        [--lienzo salida.json] [--etiqueta propuesto] [--json]

Sin ``--capture``, ``--definiciones`` ni ``--named`` lee los fixtures del repo:
la captura de ATT1 del 16/10, las definiciones de la v3, la v4 y la v5, y las
diferencias con nombre de la T1.

* ``--consistencia`` corre §7 (``flow_replay.consistency_report``) y sale con 0
  si cierra y con 1 si no.
* ``--propuesta`` lee los items de una propuesta con la forma de
  ``flow_definition.parse_items`` (la lista, o un objeto con ``items``) e
  imprime el resumen de la vista previa y los cambios por disparo, contra la
  vigente en toda la ventana (``flow_replay.simulate``).
* Una propuesta que las capacidades v1 no ejecutan sale con 2: con mas de un
  envio por camino, ``engine_not_supported:mas_de_un_envio``. ``--varios-toques``
  corre con ``PREVIEW_CAPABILITIES`` (N envios, la regla del legado) y la
  salida lo dice en la cabecera. Solo es para la vista previa del lienzo.
* ``--lienzo`` escribe, por flujo y por version, cuantos recorridos pasan por
  cada paso, para la skill ``lienzo-de-flujos``: indexado por el ``flow_key``,
  la etiqueta de la version (``hoy`` para la vigente; ``--etiqueta`` para la
  propuesta, ``propuesto`` por defecto) y el indice del paso dentro de
  ``pasos``, que es el ``p<n>`` que le da ``armar.py``. Lleva la ventana, la
  hora de la captura, si corrio con ``--varios-toques`` y una cota por numero
  (``exacta`` o ``como_maximo``). Solo conteos, con una lista cerrada de claves
  que se verifica antes de escribir.

No hace red ni lee el entorno. Lo que imprime son conteos, horas, uuid internos
y motivos; el archivo del lienzo, solo conteos. Ningun dato de una persona: la
captura no lo trae.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bridge.flow_definition import StrictJSONError, loads_strict, parse_items, step_type
from bridge.flow_engine import ENGINE_CAPABILITIES_V1, PREVIEW_CAPABILITIES, PlanNotSupported, not_supported
from bridge.flow_parity import Capture, CaptureError
from bridge.flow_replay import (
    ConsistencyReport,
    History,
    Replay,
    RunOutcome,
    Simulation,
    Versions,
    consistency_report,
    current_versions,
    engine_flows_from_definitions,
    fixed,
    flow_summary,
    history_from_capture,
    proposed_versions,
    replay,
    replay_summary,
    simulate,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAPTURE = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_recovery_capture_20261016.json"
DEFAULT_NAMED = ROOT / "tests" / "fixtures" / "flow_engine" / "att1_parity_named_differences.json"
DEFAULT_DEFINITIONS = ROOT / "tests" / "fixtures" / "flow_simulation" / "att1_definitions_by_scope_version.json"

EXIT_OK = 0
EXIT_RED = 1
EXIT_REJECTED = 2

MORE_THAN_ONE_SEND = "engine_not_supported:mas_de_un_envio"
CURRENT_LABEL = "hoy"
DEFAULT_LABEL = "propuesto"
LIENZO_SCHEMA = "setter-simulacion-lienzo/v1"

# ---------------------------------------------------------------------------
# La salida del lienzo: la lista cerrada de claves (§5.3).
# ---------------------------------------------------------------------------

LIENZO_KEYS = frozenset({"esquema", "captura", "ventana", "varios_toques", "flujos"})
WINDOW_KEYS = frozenset({"desde", "hasta"})
VERSION_KEYS = frozenset({"version", "recorridos", "en_curso", "pasos"})
STEP_KEYS = frozenset({"paso", "envio", "enviados", "terminan", "movidos_por_el_tope", "cota"})
# Como termina un recorrido en un paso que no es «enviado» (flow_replay.StepOutcome.status).
STEP_ENDS = frozenset({"sin_saber", "compro", "vencio", "fuera_de_plazo", "en_curso"})
BOUNDS = frozenset({"exacta", "como_maximo"})
_LABEL = re.compile(r"[a-z][a-z_]{0,31}")
_INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
_MAX_COUNT = 99_999_999


class RejectedProposal(Exception):
    """La propuesta no se simula: ``codes`` dice por que."""

    def __init__(self, codes: Sequence[str]) -> None:
        super().__init__(", ".join(codes))
        self.codes = tuple(codes)


def _read(path: Path) -> Any:
    try:
        return loads_strict(path.read_bytes())
    except StrictJSONError as error:
        raise CaptureError(f"{path.name}: no es un JSON que se pueda leer ({error.code})") from error


def _named(data: Any) -> list[Any]:
    if not isinstance(data, dict) or not isinstance(data.get("diferencias"), list):
        raise CaptureError("las diferencias con nombre: se esperaba un objeto con la lista «diferencias»")
    return data["diferencias"]


def _instant(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# La propuesta.
# ---------------------------------------------------------------------------

def load_proposal(data: Any, history: History, versions: Versions, *, several_touches: bool
                  ) -> tuple[Versions, dict[Any, int | None], dict[Any, int | None]]:
    """Las versiones con la propuesta aplicada, la vigente y la propuesta de cada flujo.

    Rechaza (``RejectedProposal``) lo que ``parse_items`` o ``proposed_versions``
    no aceptan, y una version nueva que las capacidades no ejecutan: con la v1,
    mas de un envio por camino, salvo con ``several_touches``.
    """
    items_json = data.get("items") if isinstance(data, Mapping) else data
    capabilities = PREVIEW_CAPABILITIES if several_touches else ENGINE_CAPABILITIES_V1
    try:
        items = parse_items(items_json)
        current = current_versions(history, versions)
        proposed, chosen = proposed_versions(versions, current, items)
    except CaptureError:
        raise
    except ValueError as error:
        raise RejectedProposal([str(error) or getattr(error, "code", "propuesta_invalida")]) from error
    codes: list[str] = []
    for item in items:
        if item.tipo == "version":
            codes += [code for code in not_supported(item.definicion or {}, capabilities) if code not in codes]
    if codes:
        raise RejectedProposal(codes)
    return proposed, current, chosen


def run_proposal(history: History, versions: Versions, current: Mapping[Any, int | None],
                 chosen: Mapping[Any, int | None], *, several_touches: bool) -> Simulation:
    capabilities = PREVIEW_CAPABILITIES if several_touches else ENGINE_CAPABILITIES_V1
    try:
        return simulate(history, versions, fixed(current), fixed(chosen), capabilities=capabilities)
    except PlanNotSupported as error:
        raise RejectedProposal(error.codes) from error


# ---------------------------------------------------------------------------
# El lienzo.
# ---------------------------------------------------------------------------

def _send_positions(definition: Mapping[str, Any]) -> list[int]:
    """El indice dentro de ``pasos`` de cada envio, en orden: el ``p<n>`` de ``armar.py``."""
    steps = definition.get("pasos")
    return [index for index, step in enumerate(steps if isinstance(steps, list) else [])
            if step_type(step) == "plantilla"]


def _lienzo_version(result: Replay, versions: Versions, ref: Any, version: int | None) -> dict[str, Any]:
    summary = flow_summary(result).get(ref[1]) or {"recorridos": 0, "en_curso": 0, "pasos": []}
    positions = _send_positions(versions[ref][version].definition) if version is not None else []
    steps = []
    for row in summary["pasos"]:
        if row["paso"] >= len(positions):
            raise ValueError(f"lienzo_step_without_template: {ref[1]} envio {row['paso']}")
        steps.append({
            "paso": positions[row["paso"]], "envio": row["paso"], "enviados": row["enviados"],
            "terminan": dict(row["terminan"]), "movidos_por_el_tope": row["movidos_por_el_tope"],
            "cota": row["cota"],
        })
    return {"version": version, "recorridos": summary["recorridos"], "en_curso": summary["en_curso"],
            "pasos": steps}


def build_lienzo(history: History, versions: Versions, current_result: Replay,
                 current: Mapping[Any, int | None], *, proposed_result: Replay | None = None,
                 chosen: Mapping[Any, int | None] | None = None, label: str = DEFAULT_LABEL,
                 several_touches: bool = False) -> dict[str, Any]:
    """Los conteos por paso de la vigente (``hoy``) y, si hay, de la propuesta (``label``), por flujo."""
    flows: dict[str, Any] = {}
    for ref in sorted(versions):
        by_label = {CURRENT_LABEL: _lienzo_version(current_result, versions, ref, current.get(ref))}
        if proposed_result is not None and chosen is not None:
            by_label[label] = _lienzo_version(proposed_result, versions, ref, chosen.get(ref))
        flows[ref[1]] = by_label
    since, until = history.window
    return {
        "esquema": LIENZO_SCHEMA,
        "captura": _instant(until),
        "ventana": {"desde": _instant(since), "hasta": _instant(until)},
        "varios_toques": several_touches,
        "flujos": flows,
    }


def _count_problem(path: str, value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNT:
        return f"{path}: no es un conteo"
    return None


def _keys_problem(path: str, value: Any, allowed: frozenset[str]) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path}: no es un objeto"]
    extra = sorted(key if isinstance(key, str) and _LABEL.fullmatch(key) else "<rara>"
                   for key in value if key not in allowed)
    missing = sorted(allowed - set(value))
    return ([f"{path}: clave fuera de la lista ({key})" for key in extra]
            + [f"{path}: falta la clave {key}" for key in missing])


def lienzo_problems(document: Any) -> list[str]:
    """Lo que el archivo del lienzo trae fuera de su forma cerrada. Vacio es verde.

    Cada objeto con su lista de claves, cada valor con su forma: conteos, horas
    con la forma fija, etiquetas cortas en minusculas. Los mensajes dicen el
    camino, nunca el valor.
    """
    problems = _keys_problem("lienzo", document, LIENZO_KEYS)
    if not isinstance(document, dict):
        return problems
    if document.get("esquema") != LIENZO_SCHEMA:
        problems.append("lienzo.esquema: no es el esquema del lienzo")
    if not isinstance(document.get("captura"), str) or not _INSTANT.fullmatch(document["captura"]):
        problems.append("lienzo.captura: no es una hora")
    if not isinstance(document.get("varios_toques"), bool):
        problems.append("lienzo.varios_toques: no es un booleano")
    window = document.get("ventana")
    problems += _keys_problem("lienzo.ventana", window, WINDOW_KEYS)
    if isinstance(window, dict):
        for key in sorted(WINDOW_KEYS & set(window)):
            if not isinstance(window[key], str) or not _INSTANT.fullmatch(window[key]):
                problems.append(f"lienzo.ventana.{key}: no es una hora")
    flows = document.get("flujos")
    if not isinstance(flows, dict):
        return problems + ["lienzo.flujos: no es un objeto"]
    for flow_key, by_label in flows.items():
        if not isinstance(flow_key, str) or not _LABEL.fullmatch(flow_key):
            problems.append("lienzo.flujos: clave con forma no permitida")
            continue
        if not isinstance(by_label, dict) or not by_label:
            problems.append(f"lienzo.flujos.{flow_key}: no es un objeto con versiones")
            continue
        for label, entry in by_label.items():
            if not isinstance(label, str) or not _LABEL.fullmatch(label):
                problems.append(f"lienzo.flujos.{flow_key}: etiqueta con forma no permitida")
                continue
            path = f"lienzo.flujos.{flow_key}.{label}"
            problems += _keys_problem(path, entry, VERSION_KEYS)
            if not isinstance(entry, dict):
                continue
            version = entry.get("version")
            if version is not None and (_count_problem(path, version) or version < 1):
                problems.append(f"{path}.version: no es un numero de version")
            for key in ("recorridos", "en_curso"):
                problem = _count_problem(f"{path}.{key}", entry.get(key))
                if problem:
                    problems.append(problem)
            steps = entry.get("pasos")
            if not isinstance(steps, list):
                problems.append(f"{path}.pasos: no es una lista")
                continue
            for index, row in enumerate(steps):
                row_path = f"{path}.pasos[{index}]"
                problems += _keys_problem(row_path, row, STEP_KEYS)
                if not isinstance(row, dict):
                    continue
                for key in ("paso", "envio", "enviados", "movidos_por_el_tope"):
                    problem = _count_problem(f"{row_path}.{key}", row.get(key))
                    if problem:
                        problems.append(problem)
                if row.get("cota") not in BOUNDS:
                    problems.append(f"{row_path}.cota: no es exacta ni como_maximo")
                ends = row.get("terminan")
                if not isinstance(ends, dict):
                    problems.append(f"{row_path}.terminan: no es un objeto")
                    continue
                for end, count in ends.items():
                    if end not in STEP_ENDS:
                        problems.append(f"{row_path}.terminan: clave fuera de la lista"
                                        f" ({end if isinstance(end, str) and _LABEL.fullmatch(end) else '<rara>'})")
                    problem = _count_problem(f"{row_path}.terminan", count)
                    if problem:
                        problems.append(problem)
    return problems


def write_lienzo(path: Path, document: Mapping[str, Any]) -> list[str]:
    """Escribe el archivo si pasa la forma cerrada; si no, no escribe nada y devuelve los problemas."""
    problems = lienzo_problems(document)
    if not problems:
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return problems


# ---------------------------------------------------------------------------
# Lo que se imprime.
# ---------------------------------------------------------------------------

def _header(capture: Capture, history: History, title: str, *, several_touches: bool | None = None) -> list[str]:
    meta = capture.meta
    lines = [
        f"{title} sobre {meta.get('query')} (captura {meta.get('captured_at', '-')})",
        f"ventana: desde {_instant(history.window[0])} hasta {_instant(history.window[1])}",
    ]
    if several_touches is not None:
        lines.append("capacidades: " + ("vista previa de varios toques (PREVIEW_CAPABILITIES, --varios-toques)"
                                        if several_touches else "v1, un envio por camino"))
    return lines


def render_consistency(result: ConsistencyReport, capture: Capture, history: History) -> str:
    lines = _header(capture, history, "Consistencia del motor") + [
        "",
        f"Vigente contra si misma: {result.self_changes} cambios",
        "Conteos (replay / T1):",
    ]
    for key in ("disparos", "llegaron", "arranques", "envios", "envios_del_legado"):
        t1 = result.t1_counts.get(key)
        lines.append(f"  {key}: {result.replay_counts.get(key, '-')} / {'-' if t1 is None else t1}")
    lines += [
        f"E distinta de la T1: {len(result.e_mismatches)}",
        (f"Envios de mas en el replay: {len(result.extra_sends)}"
         f" (explicados por una diferencia con nombre: {len(result.extra_explained_by_named)})"),
    ]
    lines += [f"  {ref}" for ref in result.extra_sends]
    lines.append(f"Con nombre y sin envio en el replay: {len(result.named_not_in_replay)}")
    lines += [f"  {ref}" for ref in result.named_not_in_replay]
    lines.append("P6 (replay / T1): " + ", ".join(
        f"{key} {result.p6.get(key)}/{result.t1_p6.get(key)}" for key in result.t1_p6))
    lines.append(f"Resultado: {'verde' if result.ok() else 'rojo'}")
    return "\n".join(lines)


def consistency_json(result: ConsistencyReport) -> dict[str, Any]:
    return {
        "ok": result.ok(), "self_changes": result.self_changes, "replay_counts": dict(result.replay_counts),
        "t1_counts": dict(result.t1_counts), "e_mismatches": list(result.e_mismatches),
        "extra_sends": list(result.extra_sends), "extra_explained_by_named": list(result.extra_explained_by_named),
        "named_not_in_replay": list(result.named_not_in_replay), "p6": dict(result.p6), "t1_p6": dict(result.t1_p6),
    }


def _counts(mapping: Mapping[str, int]) -> str:
    return ", ".join(f"{key} {value}" for key, value in mapping.items()) or "-"


def _render_summary(name: str, summary: Mapping[str, Any]) -> list[str]:
    lines = [
        f"{name}: de {summary['disparos']} disparos, {summary['recibieron']} habrian recibido el mensaje",
        f"  fin: {_counts(summary['fin'])}",
        f"  no arrancaron: {_counts(summary['no_arrancaron'])}",
        f"  en curso a la hora de la captura: {summary['en_curso']}",
        f"  sin saber: {_counts(summary['sin_saber'])}",
        f"  dias al tope: {', '.join(summary['dias_al_tope']) or '-'}",
    ]
    for flow, data in summary["flujos"].items():
        lines.append(f"  {flow}: {data['recorridos']} recorridos, {data['en_curso']} en curso")
        for row in data["pasos"]:
            lines.append(f"    envio {row['paso']}: {row['enviados']} enviados ({row['cota']}),"
                         f" terminan: {_counts(row['terminan'])}, movidos por el tope: {row['movidos_por_el_tope']}")
    return lines


def _steps_text(run: RunOutcome) -> str:
    if not run.steps:
        return run.start.reason or "-"
    return "; ".join(
        f"{step.index}: {step.status} {_instant(step.at)}"
        + (f" ({step.reason})" if step.reason else "")
        + (f" movido por {'+'.join(step.moved_by)}" if step.moved_by else "")
        for step in run.steps)


def render_simulation(result: Simulation, capture: Capture, history: History, *, several_touches: bool,
                      current: Mapping[Any, int | None], chosen: Mapping[Any, int | None]) -> str:
    versions = ", ".join(f"{ref[1]} v{current.get(ref)} -> "
                         + (f"v{chosen.get(ref)}" if chosen.get(ref) is not None else "apagado")
                         for ref in sorted(set(current) | set(chosen)))
    lines = _header(capture, history, "Simulacion del motor", several_touches=several_touches) + [
        f"versiones: {versions}",
        "",
    ]
    lines += _render_summary("Vigente", result.summary["vigente"])
    lines += _render_summary("Propuesta", result.summary["propuesta"])
    lines += ["", f"Cambios: {len(result.changes)} de {result.summary['disparos']} disparos"]
    for change in result.changes:
        lines.append(f"  {change.firing_ref} {change.after.flow[1]}: {change.before.end} -> {change.after.end}")
        lines.append(f"    antes: {_steps_text(change.before)}")
        lines.append(f"    despues: {_steps_text(change.after)}")
    return "\n".join(lines)


def _step_json(run: RunOutcome) -> list[dict[str, Any]]:
    return [{"paso": step.index, "estado": step.status, "hora": None if step.at is None else _instant(step.at),
             "motivo": step.reason, "movido_por": list(step.moved_by), "sin_saber": sorted(step.unknown)}
            for step in run.steps]


def simulation_json(result: Simulation, *, several_touches: bool) -> dict[str, Any]:
    return {
        "varios_toques": several_touches,
        "resumen": json.loads(json.dumps(dict(result.summary))),
        "cambios": [{"disparo": change.firing_ref, "flujo": change.after.flow[1],
                     "antes": {"fin": change.before.end, "pasos": _step_json(change.before)},
                     "despues": {"fin": change.after.end, "pasos": _step_json(change.after)}}
                    for change in result.changes],
    }


# ---------------------------------------------------------------------------
# El comando.
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE, help="la captura (por defecto, el fixture)")
    parser.add_argument("--definiciones", type=Path, default=DEFAULT_DEFINITIONS,
                        help="las definiciones por version (por defecto, las de ATT1 del fixture)")
    parser.add_argument("--named", type=Path, default=DEFAULT_NAMED,
                        help="las diferencias con nombre de la T1, para --consistencia")
    parser.add_argument("--consistencia", action="store_true", help="la consistencia contra la T1 (§7)")
    parser.add_argument("--propuesta", type=Path, help="los items de una propuesta (parse_items)")
    parser.add_argument("--varios-toques", action="store_true",
                        help="simula mas de un envio por camino (solo la vista previa del lienzo)")
    parser.add_argument("--lienzo", type=Path, help="escribe los conteos por paso para el lienzo de flujos")
    parser.add_argument("--etiqueta", default=DEFAULT_LABEL,
                        help="la etiqueta de la propuesta en el lienzo (hoy es la vigente)")
    parser.add_argument("--json", action="store_true", help="la salida como JSON")
    args = parser.parse_args(argv)
    if not _LABEL.fullmatch(args.etiqueta) or args.etiqueta == CURRENT_LABEL:
        print("error: --etiqueta va en minusculas, de 1 a 32 letras o guiones bajos, y no puede ser «hoy»")
        return EXIT_REJECTED

    try:
        capture = Capture.from_json(_read(args.capture))
        history = history_from_capture(capture)
        versions = engine_flows_from_definitions(_read(args.definiciones))
        named = _named(_read(args.named)) if args.consistencia else []
        proposal = _read(args.propuesta) if args.propuesta is not None else None
    except CaptureError as error:
        print(f"error: {error}")
        return EXIT_RED
    except OSError as error:
        print(f"error: no se pudo leer el archivo ({error.strerror})")
        return EXIT_RED

    code = EXIT_OK
    output: dict[str, Any] = {}
    texts: list[str] = []
    if args.consistencia:
        report = consistency_report(history, versions, named)
        output["consistencia"] = consistency_json(report)
        texts.append(render_consistency(report, capture, history))
        code = EXIT_OK if report.ok() else EXIT_RED

    current = current_versions(history, versions)
    current_result: Replay | None = None
    proposed_result: Replay | None = None
    chosen: dict[Any, int | None] | None = None
    lienzo_versions = versions
    if proposal is not None:
        try:
            lienzo_versions, current, chosen = load_proposal(proposal, history, versions,
                                                             several_touches=args.varios_toques)
            result = run_proposal(history, lienzo_versions, current, chosen, several_touches=args.varios_toques)
        except RejectedProposal as rejected:
            print(f"propuesta rechazada: {', '.join(rejected.codes)}")
            if MORE_THAN_ONE_SEND in rejected.codes:
                print("  las capacidades v1 son de un envio por camino; la vista previa de varios toques"
                      " corre con --varios-toques")
            return EXIT_REJECTED
        proposed_result = result.proposed
        current_result = result.current
        output["simulacion"] = simulation_json(result, several_touches=args.varios_toques)
        texts.append(render_simulation(result, capture, history, several_touches=args.varios_toques,
                                       current=current, chosen=chosen))
    elif args.lienzo is not None or not args.consistencia:
        capabilities = PREVIEW_CAPABILITIES if args.varios_toques else ENGINE_CAPABILITIES_V1
        current_result = replay(history, versions, fixed(current), capabilities=capabilities)
        summary = replay_summary(current_result, history.window)
        output["vigente"] = summary
        texts.append("\n".join(_header(capture, history, "La vigente del motor",
                                       several_touches=args.varios_toques)
                               + [""] + _render_summary("Vigente", summary)))

    if args.lienzo is not None:
        assert current_result is not None
        document = build_lienzo(history, lienzo_versions, current_result, current,
                                proposed_result=proposed_result, chosen=chosen, label=args.etiqueta,
                                several_touches=args.varios_toques)
        problems = write_lienzo(args.lienzo, document)
        if problems:
            print("error: la salida del lienzo no tiene la forma cerrada; no se escribio")
            for problem in problems:
                print(f"  {problem}")
            return EXIT_RED
        texts.append(f"lienzo: {args.lienzo.name} ({len(document['flujos'])} flujos)")
        output["lienzo"] = args.lienzo.name

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("\n\n".join(texts))
    return code


if __name__ == "__main__":
    sys.exit(main())
