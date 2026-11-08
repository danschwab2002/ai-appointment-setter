"""El control de calidad de una campana de la base: el comando del operador (agent-campaigns-v1, E-355).

El juez (``segment_check``, 3 corridas de hasta 5 minutos cada una) no corre
adentro del bridge. Lo corre el operador con este comando, sobre la misma funcion
del alta de un segmento, y el resultado queda en la fila: con ``limpio`` la pagina
muestra el boton, con preguntas abiertas el agente las rehace con el duenio.

    python -m bridge.campaign_check <id de la campana> --instancia <carpeta> --soul <SOUL.md>

Lee la base con ``SUPABASE_BASE_URL`` y ``SUPABASE_SERVICE_ROLE_KEY`` del entorno
del bridge (solo las funciones de service_role de esta migracion) y el juez, con
``OPENROUTER_API_KEY``. Sale con 0 si dio limpio, 1 si hay preguntas o no hubo
veredicto, 2 si no pudo leer o anotar. No imprime claves.

Con preguntas abiertas imprime cada una. Sobre una campana publicada o pausada la
base no pisa su control limpio: las guarda en el historial y el comando sale con 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from .agent_campaigns import parse_campaign, render_toml, segment_mapping
from .instance_manifest import InstanceManifest
from .segment_check import (
    DEFAULT_MODEL,
    DEFAULT_RUNS,
    Judge,
    JudgeError,
    Report,
    SegmentCheckError,
    check_files,
    openrouter_judge,
    read_recorded,
)

__all__ = ["main", "report_for_row", "run_check"]

_MAX_ITEMS = 40


def report_for_row(report: Report) -> dict[str, Any]:
    """Lo que queda en la fila: lo que necesitan la pagina y el agente, sin las citas enteras."""
    if report.clean:
        verdict = "limpio"
    elif report.valid_runs == 0:
        verdict = "sin_veredicto"
    else:
        verdict = "abiertos"
    return {"veredicto": verdict, "abiertos": len(report.items), "corridas_validas": report.valid_runs,
            "corridas_pedidas": report.requested_runs,
            "items": [{"numero": item.number, "tipo": item.kind, "pregunta": f"{item.title}: {item.question}"[:1000]}
                      for item in report.items[:_MAX_ITEMS]]}


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


def run_check(store: Any, *, manifest: InstanceManifest, instance_dir: Path, campaign_id: str, judge: Judge,
              soul_path: Path | None, runs: int) -> tuple[Report, dict[str, Any]]:
    """Corre el juez sobre la fila y anota el resultado. Devuelve el reporte y lo que contesto la base."""
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
        report = check_files(path, judge=judge, instance_dir=instance_dir, soul_path=soul_path, runs=runs)
    saved = store.call("record_agent_campaign_check_v1", {
        "p_instance": instance, "p_campaign_id": campaign_id, "p_fingerprint": fingerprint,
        "p_report": report_for_row(report), "p_check_sha": report.chequeo_sha if report.clean else None})
    return report, saved


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bridge.campaign_check", description=__doc__)
    parser.add_argument("campana", help="el id de la campana (el que trae la lista del agente)")
    parser.add_argument("--instancia", type=Path, required=True, help="la carpeta de la instancia (instancia.toml)")
    parser.add_argument("--soul", type=Path, help="el SOUL comun (la imagen del bridge no lo trae)")
    parser.add_argument("--modelo", default=os.environ.get("SEGMENT_CHECK_MODEL", DEFAULT_MODEL))
    parser.add_argument("--corridas", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--grabadas", type=Path, nargs="+", help="respuestas grabadas del juez, sin red (para probar)")
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
                                  campaign_id=args.campana, judge=judge, soul_path=args.soul, runs=args.corridas)
    except (JudgeError, SegmentCheckError, KeyError, ValueError, httpx.HTTPError) as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    kept_clean = saved.get("reason_code") == "agent_campaign_check_not_clean"
    if saved.get("ok") is not True and not (kept_clean and not report.clean):
        print(f"ERROR: la base no lo anoto: {saved.get('reason_code')}", file=sys.stderr)
        return 2
    row_report = report_for_row(report)
    print(f"VEREDICTO {row_report['veredicto']} abiertos={len(report.items)} "
          f"corridas={report.valid_runs}/{report.requested_runs}")
    for item in row_report["items"]:
        print(f"  {item['numero']}. [{item['tipo']}] {item['pregunta']}")
    if kept_clean:
        print("La campana sigue con su control limpio anterior: la base no lo pisa. "
              "Las preguntas quedaron en su historial (agent_campaign_checked).")
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
