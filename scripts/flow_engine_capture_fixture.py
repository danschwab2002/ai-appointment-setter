#!/usr/bin/env python3
"""Arma el fixture recortado de la captura de ATT1 y corre su prueba de privacidad.

Diseno del motor de flujos (2026-10-16), §6.1 y §6.4. La captura es la salida
de ``tests/fixtures/flow_engine/att1_recovery_capture_query_v2.sql``, corrida
en att1-db en solo lectura; el fixture es lo que entra al repo:

    uv run python scripts/flow_engine_capture_fixture.py \\
        --capture captura.json --out tests/fixtures/flow_engine/att1_recovery_capture_AAAAMMDD.json
    uv run python scripts/flow_engine_capture_fixture.py --check-privacy captura.json

``--check-privacy`` sirve para la captura completa y para el fixture: sale con
0 si la forma esta en verde y con 1 si no, y dice el camino de cada problema,
nunca el valor. ``--capture`` valida antes de escribir, y si el fixture pasa de
1.000.000 bytes no escribe nada y dice cuanto pesa cada seccion.

No hace red ni lee el entorno. Lo que imprime son caminos, conteos y pesos.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from bridge.flow_capture import (
    FIXTURE_MAX_BYTES,
    CaptureFixtureError,
    CaptureShapeError,
    FixtureTooLargeError,
    build_fixture,
    is_fixture,
    section_sizes,
    serialize,
    shape_problems,
)
from bridge.flow_definition import StrictJSONError, loads_strict

# Cuantos problemas se listan; el total va siempre.
MAX_LISTED = 20


def _read(path: Path) -> tuple[bytes, Any]:
    raw = path.read_bytes()
    try:
        return raw, loads_strict(raw)
    except StrictJSONError as error:
        # El codigo alcanza (invalid_json o duplicate_key): la ruta de una clave
        # repetida podria nombrar un dato.
        raise CaptureFixtureError(f"{path.name}: no es un JSON que se pueda leer ({error.code})") from error


def _print_problems(problems: Sequence[object]) -> None:
    for problem in problems[:MAX_LISTED]:
        print(f"  {problem}")
    if len(problems) > MAX_LISTED:
        print(f"  ... y {len(problems) - MAX_LISTED} mas")


def check_privacy(path: Path) -> int:
    raw, data = _read(path)
    problems = shape_problems(data)
    kind = "fixture" if isinstance(data, dict) and is_fixture(data) else "captura"
    if problems:
        print(f"privacidad: rojo ({kind}, {len(raw)} bytes): {len(problems)} problemas")
        _print_problems(problems)
        return 1
    print(f"privacidad: verde ({kind}, {len(raw)} bytes): listas cerradas y formas en orden")
    return 0


def build(capture_path: Path, out_path: Path) -> int:
    raw, capture = _read(capture_path)
    try:
        fixture = build_fixture(capture, source_sha256=hashlib.sha256(raw).hexdigest(),
                                source_bytes=len(raw))
    except CaptureShapeError as error:
        print(f"captura: rojo, {len(error.problems)} problemas de forma; no se escribe nada")
        _print_problems(error.problems)
        return 1
    except FixtureTooLargeError as error:
        print(f"fixture: {error.size} bytes, pasa del limite de {error.limit}; no se escribe nada")
        for name, size in sorted(error.sections.items(), key=lambda item: -item[1]):
            print(f"  {name}: {size} bytes")
        return 1
    body = serialize(fixture)
    out_path.write_bytes(body)
    meta = fixture["_captured"]["fixture"]
    print(f"fixture: {out_path} ({len(body)} bytes, limite {FIXTURE_MAX_BYTES}; captura {len(raw)} bytes)")
    print(f"  sha256 de la captura: {meta['source_sha256']}")
    for name, size in section_sizes(fixture).items():
        print(f"  {name}: {size} bytes")
    for section, values in sorted(meta["constantes"].items()):
        print(f"  constantes de {section}: {', '.join(sorted(values))}")
    for section, trim in sorted(meta["recortes"].items()):
        print(f"  recorte de {section}: {trim['captura']} -> {trim['fixture']}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture", type=Path, help="el JSON que devolvio la consulta")
    mode.add_argument("--check-privacy", type=Path, metavar="JSON",
                      help="una captura o un fixture: 0 si la forma esta en verde, 1 si no")
    parser.add_argument("--out", type=Path, help="donde escribir el fixture (con --capture)")
    args = parser.parse_args(argv)
    if args.capture is not None and args.out is None:
        parser.error("--capture necesita --out")
    if args.check_privacy is not None and args.out is not None:
        parser.error("--out va solo con --capture")
    try:
        if args.check_privacy is not None:
            return check_privacy(args.check_privacy)
        return build(args.capture, args.out)
    except CaptureFixtureError as error:
        print(f"error: {error}")
        return 1
    except OSError as error:
        print(f"error: no se pudo leer o escribir el archivo ({error.strerror})")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
