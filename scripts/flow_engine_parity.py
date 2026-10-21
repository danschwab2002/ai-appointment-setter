#!/usr/bin/env python3
"""Corre la paridad del motor (P3 y P6) sobre la captura de ATT1 e imprime el reporte.

Diseno del motor de flujos (2026-10-16), §3.1, §5.2 y §5.5:

    uv run python scripts/flow_engine_parity.py \\
        --capture tests/fixtures/flow_engine/att1_recovery_capture_20261016.json \\
        --named tests/fixtures/flow_engine/att1_parity_named_differences.json

Con ``--forma v3`` lee la captura v3 (la T2.2, el carrito de tres toques) y mide
P3 por paso (A1, A2 y Z), los finales de cada paso y P6:

    uv run python scripts/flow_engine_parity.py --forma v3 \\
        --capture tests/fixtures/flow_engine/att1_recovery_capture_v3_sintetico.json \\
        --named <diferencias.json>

(``--named`` es un objeto con la lista «diferencias», con las reglas de la T1;
en P3 de la v3, el «disparo» de una diferencia es el uuid de la accion del paso.)

Sale con 0 si el reporte esta en verde (cero sin explicar) y con 1 si no.
``--json`` imprime el reporte como JSON. Sirve con el fixture recortado y con
la captura completa de los anexos del vault.

No hace red ni lee el entorno. Lo que imprime son conteos, horas, uuid
internos y motivos: ningun dato de una persona.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bridge.flow_definition import StrictJSONError, loads_strict
from bridge.flow_parity import (
    TOLERANCE,
    Capture,
    CaptureError,
    CaptureV3,
    ParityReport,
    ParityReportV3,
    parity_report,
    parity_report_v3,
)


def _read(path: Path) -> Any:
    try:
        return loads_strict(path.read_bytes())
    except StrictJSONError as error:
        raise CaptureError(f"{path.name}: no es un JSON que se pueda leer ({error.code})") from error


def _named(data: Any) -> list[Any]:
    if not isinstance(data, dict) or not isinstance(data.get("diferencias"), list):
        raise CaptureError("las diferencias con nombre: se esperaba un objeto con la lista «diferencias»")
    return data["diferencias"]


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f} s"


def render(report: ParityReport, capture: Capture) -> str:
    meta = capture.meta
    lines = [
        f"Paridad del motor sobre {meta.get('query')} (captura {meta.get('captured_at', '-')})",
        f"desde: {meta.get('desde')} ({meta.get('desde_origen', '-')})",
        "",
        f"Disparos desde «desde»: {sum(report.firings.values())}",
    ]
    lines += [f"  {key}: {value}" for key, value in report.firings.items()]
    delay = report.delay
    lines += [
        f"Llegaron al flujo: {report.reached_flow}",
        f"Envios: {report.sends}",
        (f"R - E: minimo {_seconds(delay.get('min'))}, mediana {_seconds(delay.get('mediana'))},"
         f" p95 {_seconds(delay.get('p95'))}, maximo {_seconds(delay.get('max'))}"),
        f"Nunca antes (R >= E - 1 s): {len(report.never_before_violations)} violaciones",
        f"Fuera de la tolerancia ({int(TOLERANCE.total_seconds())} s): {len(report.over_tolerance)}",
    ]
    lines += [f"  {ref}" for ref in report.over_tolerance]
    by = report.supersessions_by
    lines += [
        (f"P6: {report.supersessions} primeros contactos cedieron"
         f" ({by.get('persona', 0)} por persona, {by.get('intencion', 0)} por intencion);"
         f" sin ganador: {len(report.supersessions_without_winner)};"
         f" ganadores sin cancelacion: {len(report.winners_without_cancel)}"),
    ]
    routes = report.supersessions_by_routes
    without_case = report.ceded_to_a_firing_without_case
    lines += [
        (f"  sin depender del orden: {routes.get('las_dos', 0)} por las dos rutas,"
         f" {routes.get('solo_persona', 0)} solo por persona, {routes.get('solo_intencion', 0)} solo por intencion"),
        f"  ante un disparo sin caso (no mando nada): {sum(without_case.values())}",
    ]
    lines += [f"    {result}: {count}" for result, count in without_case.items()]
    lines += [
        f"Diferencias con nombre: {report.named_total}",
    ]
    lines += [f"  {name}: {count}" for name, count in report.named.items()]
    lines.append(f"Sin explicar: {len(report.unexplained)}")
    lines += [f"  {item['diferencia']} {item['ref']}: {item.get('detalle', '')}" for item in report.unexplained]
    lines.append(f"Resultado: {'verde' if report.ok() else 'rojo'}")
    return "\n".join(lines)


def render_v3(report: ParityReportV3, capture: CaptureV3) -> str:
    meta = capture.meta
    lines = [
        f"Paridad del motor sobre {meta.get('query')} (captura {meta.get('captured_at', '-')})",
        f"desde: {meta.get('desde')} ({meta.get('desde_origen', '-')})",
        "",
        f"Disparos del carrito desde «desde»: {sum(report.firings.values())}",
    ]
    lines += [f"  {key}: {value}" for key, value in report.firings.items()]
    lines += [
        f"Llegaron al flujo: {report.reached_flow}",
        f"Carritos de la ventana: {report.cases}",
        f"Envios por paso: {', '.join(f'{step} {count}' for step, count in report.sends.items()) or '0'}",
    ]
    for step, delay in report.delay.items():
        lines.append(
            f"  R - E de {step}: minimo {_seconds(delay.get('min'))}, mediana {_seconds(delay.get('mediana'))},"
            f" p95 {_seconds(delay.get('p95'))}, maximo {_seconds(delay.get('max'))}")
    lines += [
        f"Nunca antes (R >= E - 1 s): {len(report.never_before_violations)} violaciones",
        f"Fuera de la tolerancia ({int(TOLERANCE.total_seconds())} s): {len(report.over_tolerance)}",
        f"Explicadas por una excepcion: {len(report.explained)}",
    ]
    lines += [f"  {name}: {count}" for name, count in report.explained_by.items()]
    lines += [f"    {item['excepcion']} {item['paso']} {item['ref']}" for item in report.explained]
    lines.append("Finales por paso:")
    for step, finals in report.finals.items():
        lines.append(f"  {step}: {', '.join(f'{final} {count}' for final, count in finals.items())}")
    census = report.census
    lines += [
        (f"Censo: {census.get('casos', 0)} carritos, {census.get('pasos_clasificados', 0)} de"
         f" {census.get('pasos_esperados', 0)} pasos clasificados, {census.get('enviados', 0)} enviados"
         f" ({census.get('envios_de_p3', 0)} en P3)"),
        (f"P6: {report.supersessions} primeros contactos cedieron; sin ganador:"
         f" {len(report.supersessions_without_winner)}; ganadores sin cancelacion:"
         f" {len(report.winners_without_cancel)}"),
        ("  por la primera ruta que encuentra un ganador: "
         + (", ".join(f"{route} {count}" for route, count in sorted(report.supersessions_by.items())) or "-")),
        f"Personas no comparables (su disparo no se captura: la ficha): {len(report.persons_not_comparable)}",
        f"Diferencias con nombre: {report.named_total}",
    ]
    lines += [f"  {name}: {count}" for name, count in report.named.items()]
    lines.append(f"Sin explicar: {len(report.unexplained)}")
    lines += [f"  {item['diferencia']} {item['ref']}: {item.get('detalle', '')}" for item in report.unexplained]
    lines.append(f"Resultado: {'verde' if report.ok() else 'rojo'}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--capture", type=Path, required=True, help="el fixture o la captura completa")
    parser.add_argument("--named", type=Path, required=True, help="las diferencias con nombre")
    parser.add_argument("--json", action="store_true", help="el reporte como JSON")
    parser.add_argument("--forma", choices=("v2", "v3"), default="v2",
                        help="la forma de la captura: v2 (la T1, por defecto) o v3 (el carrito de tres toques)")
    args = parser.parse_args(argv)
    try:
        if args.forma == "v3":
            capture_v3 = CaptureV3.from_json(_read(args.capture))
            report_v3 = parity_report_v3(capture_v3, _named(_read(args.named)))
        else:
            capture = Capture.from_json(_read(args.capture))
            report = parity_report(capture, _named(_read(args.named)))
    except CaptureError as error:
        print(f"error: {error}")
        return 1
    except OSError as error:
        print(f"error: no se pudo leer el archivo ({error.strerror})")
        return 1
    if args.forma == "v3":
        if args.json:
            print(json.dumps(report_v3.as_json(), ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(render_v3(report_v3, capture_v3))
        return 0 if report_v3.ok() else 1
    if args.json:
        print(json.dumps(report.as_json(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render(report, capture))
    return 0 if report.ok() else 1


if __name__ == "__main__":
    sys.exit(main())
