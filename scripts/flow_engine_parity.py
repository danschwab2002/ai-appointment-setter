#!/usr/bin/env python3
"""Corre la paridad del motor (P3 y P6) sobre la captura de ATT1 e imprime el reporte.

Diseno del motor de flujos (2026-10-16), §3.1, §5.2 y §5.5:

    uv run python scripts/flow_engine_parity.py \\
        --capture tests/fixtures/flow_engine/att1_recovery_capture_20261016.json \\
        --named tests/fixtures/flow_engine/att1_parity_named_differences.json

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
from bridge.flow_parity import TOLERANCE, Capture, CaptureError, ParityReport, parity_report


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
    args = parser.parse_args(argv)
    try:
        capture = Capture.from_json(_read(args.capture))
        report = parity_report(capture, _named(_read(args.named)))
    except CaptureError as error:
        print(f"error: {error}")
        return 1
    except OSError as error:
        print(f"error: no se pudo leer el archivo ({error.strerror})")
        return 1
    if args.json:
        print(json.dumps(report.as_json(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render(report, capture))
    return 0 if report.ok() else 1


if __name__ == "__main__":
    sys.exit(main())
