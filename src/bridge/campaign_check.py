"""El control de calidad de una campana de la base: el comando del operador (agent-campaigns-v1, E-355).

El juez (``segment_check``, 3 corridas de hasta 5 minutos cada una) no corre
adentro del bridge. Lo corre el operador con este comando, sobre la misma funcion
del alta de un segmento, y el resultado queda en la fila: con ``limpio`` la pagina
muestra el boton, con preguntas abiertas el agente las rehace con el duenio.

    python -m bridge.campaign_check <id de la campana> --instancia <carpeta> --soul <SOUL.md>
        [--criterio frenos] [--grabar <carpeta>] [--sin-anotar]

Lee la base con ``SUPABASE_BASE_URL`` y ``SUPABASE_SERVICE_ROLE_KEY`` del entorno
del bridge (solo las funciones de service_role de esta migracion) y el juez, con
``OPENROUTER_API_KEY``. Sale con 0 si dio limpio, 1 si hay preguntas o no hubo
veredicto, 2 si no pudo leer o anotar. No poder grabar con ``--grabar`` no cambia ese codigo:
avisa por stderr. No imprime claves.

Con preguntas abiertas imprime cada una. Sobre una campana publicada o pausada la
base no pisa su control limpio: las guarda en el historial y el comando sale con 1.

``--criterio frenos`` (E-364a): frena solo lo que contradice un valor o una promesa
escrita (``campaign_gate``); lo demas queda como aviso y ``limpio`` significa que nada
frena. Sin la opcion, frena cualquier observacion. Con o sin la opcion, desde E-364a el
juez ve los flujos y plantillas de la campana (antes los buscaba con la clave y no los
encontraba), lo que puede sumar observaciones, y 2 de 3 corridas validas sin nada que
frene da ``sin_veredicto`` (antes, ``abiertos`` con la lista vacia).

``--grabar`` deja en la carpeta las respuestas crudas del juez (``respuesta-N.json``),
el TOML que leyo (``segmento.toml``) y lo que salio con sus citas (``reporte.json``),
para clasificarlas despues. No guarda claves ni el prompt: el unico texto de personas
es el de la campana. ``--grabar`` no cambia lo que se anota.

``--sin-anotar`` corre, graba y muestra, sin escribir nada en la base: es para una
captura real sobre una campana que no se quiere tocar.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .agent_campaigns import parse_campaign, render_toml, segment_mapping
from .campaign_gate import CRITERIO, razon_de_freno
from .instance_manifest import InstanceManifest
from .segment_check import (
    DEFAULT_MODEL,
    DEFAULT_RUNS,
    Gate,
    Item,
    Judge,
    JudgeError,
    Report,
    SegmentCheckError,
    _recording,
    check_files,
    openrouter_judge,
    read_recorded,
)

__all__ = ["main", "report_for_row", "run_check"]

_MAX_ITEMS = 40
_MAX_FIELD_CHARS = 200
_MAX_QUOTES = 6
_MAX_QUOTE_CHARS = 300
_MAX_SOURCE_CHARS = 120
# La fila admite 131072 bytes (check_report::text, migracion 20261108005000); se deja margen.
_MAX_ROW_BYTES = 120_000
# Sobre una publicada, las diez primeras van al evento (data admite 16384 bytes, migracion
# 20261108005000) con la pregunta recortada a 300 caracteres y el resto de cada item entero.
_EVENT_ITEMS = 10
_EVENT_QUESTION_CHARS = 300
_MAX_EVENT_BYTES = 15_000
_EVENT_FIELD_CHARS = 50
_EVENT_QUESTION_FLOOR_CHARS = 150

# El criterio de la opcion --criterio: el nombre que queda en la fila y la compuerta.
_CRITERIA: dict[str, tuple[str, Gate]] = {"frenos": (CRITERIO, razon_de_freno)}


def _row_item(item: Item) -> dict[str, Any]:
    return {"numero": item.number, "tipo": item.kind, "pregunta": f"{item.title}: {item.question}"[:1000]}


def _row_bytes(row: Mapping[str, Any]) -> int:
    return len(json.dumps(row, ensure_ascii=False).encode("utf-8"))


def _fit(row: dict[str, Any]) -> None:
    """Que entre en la columna: se van primero las citas, despues los avisos. ``abiertos`` sigue siendo el total."""
    while _row_bytes(row) > _MAX_ROW_BYTES:
        quoted = [item for item in row["items"] if item["citas"]]
        if quoted:
            max(quoted, key=lambda item: len(item["citas"]))["citas"].pop()
        elif row["avisos"]:
            row["avisos"].pop()
        elif row["items"]:
            row["items"].pop()
        else:
            break
    _fit_event(row)


def _event_bytes(row: Mapping[str, Any]) -> int:
    items = [item | {"pregunta": item["pregunta"][:_EVENT_QUESTION_CHARS]} for item in row["items"][:_EVENT_ITEMS]]
    return _row_bytes({"veredicto": row["veredicto"], "abiertos": row["abiertos"], "items": items})


def _fit_event(row: dict[str, Any]) -> None:
    """Que el evento de una publicada entre en su tope: se van las citas y, si no alcanza, se acorta el resto."""
    first = row["items"][:_EVENT_ITEMS]
    while _event_bytes(row) > _MAX_EVENT_BYTES and (quoted := [item for item in first if item["citas"]]):
        max(quoted, key=lambda item: len(json.dumps(item["citas"], ensure_ascii=False)))["citas"].pop()
    if _event_bytes(row) > _MAX_EVENT_BYTES:
        for item in first:
            item["campo"] = item["campo"][:_EVENT_FIELD_CHARS]
    if _event_bytes(row) > _MAX_EVENT_BYTES:
        for item in first:
            item["pregunta"] = item["pregunta"][:_EVENT_QUESTION_FLOOR_CHARS]


def report_for_row(report: Report, *, criterio: str | None = None) -> dict[str, Any]:
    """Lo que queda en la fila: lo que necesitan la pagina y el agente, sin las citas enteras.

    Sin ``criterio`` tiene la forma de siempre. Con criterio, cada item que frena suma ``razon``,
    ``campo`` y ``citas``, y ``avisos`` trae lo que no frena."""
    if report.clean:
        verdict = "limpio"
    elif report.valid_runs == 0 or not report.items:
        # Nada frena pero falto una corrida: lo que no se pudo mirar no se da por bueno ni por malo.
        verdict = "sin_veredicto"
    else:
        verdict = "abiertos"
    row: dict[str, Any] = {"veredicto": verdict, "abiertos": len(report.items), "corridas_validas": report.valid_runs,
                           "corridas_pedidas": report.requested_runs,
                           "items": [_row_item(item) for item in report.items[:_MAX_ITEMS]]}
    if criterio is None:
        return row
    row["criterio"] = criterio
    for shown, item in zip(row["items"], report.items):
        shown.update(razon=item.stop_reason, campo=item.field[:_MAX_FIELD_CHARS],
                     citas=[{"fuente": c.source[:_MAX_SOURCE_CHARS], "texto": c.text[:_MAX_QUOTE_CHARS]}
                            for c in item.citations[:_MAX_QUOTES]])
    row["avisos"] = [_row_item(item) | {"campo": item.field[:_MAX_FIELD_CHARS]} for item in report.notices[:_MAX_ITEMS]]
    row["avisos_total"] = len(report.notices)
    _fit(row)
    return row


class _Store:
    def __init__(self, base_url: str, key: str, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), transport=transport, timeout=30,
                                    headers={"apikey": key, "Authorization": f"Bearer {key}",
                                             "Content-Type": "application/json"})

    def call(self, function: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self._client.post(f"/rest/v1/rpc/{function}", content=json.dumps(payload))
        if response.status_code != 200:
            raise SegmentCheckError(f"la base contesto {response.status_code} en {function}")
        body = response.json()
        if not isinstance(body, dict):
            raise SegmentCheckError(f"la base contesto algo inesperado en {function}")
        return body


def _save_capture(directory: Path, report: Report, *, campaign_id: str, toml: str, criterio: str | None) -> None:
    """El TOML que leyo el juez y lo que salio con sus citas. Las respuestas crudas las graba ``_recording``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "segmento.toml").write_text(toml, encoding="utf-8")
    payload = report.to_json()
    for shown, item in zip(payload["items"], report.items):
        shown["razon"] = item.stop_reason
    payload.update(_grabado=datetime.now(timezone.utc).isoformat(), campana=campaign_id, criterio=criterio,
                   veredicto=report_for_row(report, criterio=criterio)["veredicto"],
                   no_frenan=[item.to_json() for item in report.notices])
    (directory / "reporte.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _warn_capture(error: OSError) -> None:
    print(f"AVISO: no se pudo grabar la captura ({type(error).__name__}: {error}). La corrida sigue.", file=sys.stderr)


def run_check(store: Any, *, manifest: InstanceManifest, instance_dir: Path, campaign_id: str, judge: Judge,
              soul_path: Path | None, runs: int, criterio: str | None = None,
              grabar: Path | None = None, anotar: bool = True) -> tuple[Report, dict[str, Any]]:
    """Corre el juez sobre la fila y anota el resultado. Devuelve el reporte y lo que contesto la base.

    Con ``anotar=False`` no escribe nada en la base. Una falla al grabar avisa y no tira la corrida."""
    name, gate = _CRITERIA[criterio] if criterio is not None else (None, None)
    instance = {"tenant_ref": manifest.tenant_ref, "ally_ref": manifest.ally_ref}
    row = store.call("operator_get_agent_campaign_v1", {"p_instance": instance, "p_campaign_id": campaign_id})
    fingerprint = row.get("fingerprint")
    if not isinstance(fingerprint, str) or row.get("status") not in ("validada", "publicada", "pausada"):
        raise SegmentCheckError(f"la campana esta {row.get('status')}: se chequea una vez validada")
    draft = parse_campaign(row.get("draft"))
    version = int(row["segment_version"])
    text = render_toml(segment_mapping(draft, ally_ref=manifest.ally_ref, version=version))
    with tempfile.TemporaryDirectory(prefix="campaign-check-") as folder:
        path = Path(folder) / f"{draft['clave']}-v{version}.toml"
        path.write_text(text, encoding="utf-8")
        report = check_files(path, judge=_recording(judge, grabar, on_error=_warn_capture) if grabar is not None else judge,
                             instance_dir=instance_dir, soul_path=soul_path, runs=runs,
                             label=draft["etiqueta"], compuerta=gate)
    if grabar is not None:
        try:
            _save_capture(grabar, report, campaign_id=campaign_id, toml=text, criterio=name)
        except OSError as error:
            _warn_capture(error)
    if not anotar:
        return report, {"ok": True, "anotado": False}
    saved = store.call("record_agent_campaign_check_v1", {
        "p_instance": instance, "p_campaign_id": campaign_id, "p_fingerprint": fingerprint,
        "p_report": report_for_row(report, criterio=name), "p_check_sha": report.chequeo_sha if report.clean else None})
    return report, saved


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bridge.campaign_check", description=__doc__)
    parser.add_argument("campana", help="el id de la campana (el que trae la lista del agente)")
    parser.add_argument("--instancia", type=Path, required=True, help="la carpeta de la instancia (instancia.toml)")
    parser.add_argument("--soul", type=Path, help="el SOUL comun (la imagen del bridge no lo trae)")
    parser.add_argument("--modelo", default=os.environ.get("SEGMENT_CHECK_MODEL", DEFAULT_MODEL))
    parser.add_argument("--corridas", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--grabadas", type=Path, nargs="+", help="respuestas grabadas del juez, sin red (para probar)")
    parser.add_argument("--criterio", choices=sorted(_CRITERIA),
                        help="frenos: frena solo lo que contradice un valor o una promesa escrita; lo demas es aviso")
    parser.add_argument("--grabar", type=Path, help="carpeta donde dejar las respuestas crudas del juez y el reporte con sus citas")
    parser.add_argument("--sin-anotar", action="store_true", help="corre, graba y muestra, sin escribir nada en la base")
    args = parser.parse_args(argv)
    try:
        base_url, key = os.environ["SUPABASE_BASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    except KeyError as missing:
        print(f"ERROR: falta {missing.args[0]} en el entorno", file=sys.stderr)
        return 2
    try:
        judge = read_recorded(args.grabadas) if args.grabadas else openrouter_judge(model=args.modelo)
        manifest = InstanceManifest.from_toml_file(args.instancia / "instancia.toml")
        report, saved = run_check(_Store(base_url, key), manifest=manifest, instance_dir=args.instancia,
                                  campaign_id=args.campana, judge=judge, soul_path=args.soul, runs=args.corridas,
                                  criterio=args.criterio, grabar=args.grabar, anotar=not args.sin_anotar)
    except (JudgeError, SegmentCheckError, KeyError, ValueError, httpx.HTTPError) as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    kept_clean = saved.get("reason_code") == "agent_campaign_check_not_clean"
    if saved.get("ok") is not True and not (kept_clean and not report.clean):
        print(f"ERROR: la base no lo anoto: {saved.get('reason_code')}", file=sys.stderr)
        return 2
    row_report = report_for_row(report, criterio=_CRITERIA[args.criterio][0] if args.criterio else None)
    print(f"VEREDICTO {row_report['veredicto']} abiertos={len(report.items)} "
          f"corridas={report.valid_runs}/{report.requested_runs}"
          + (f" avisos={row_report['avisos_total']}" if args.criterio else ""))
    for item in row_report["items"]:
        print(f"  {item['numero']}. [{item['tipo']}]" + (f" ({item['razon']})" if args.criterio else "") + f" {item['pregunta']}")
    for item in row_report.get("avisos", ()):
        print(f"  aviso {item['numero']}. [{item['tipo']}] {item['pregunta']}")
    if args.sin_anotar:
        print("Sin anotar: no se escribio nada en la base.")
    if kept_clean:
        print("La campana sigue con su control limpio anterior: la base no lo pisa. "
              "Las preguntas quedaron en su historial (agent_campaign_checked).")
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
