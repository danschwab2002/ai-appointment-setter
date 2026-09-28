"""CLI del instalador: ``python -m bridge.instance_cli validate <carpeta-de-la-instancia>``.

Diseno: docs/design/setter-producto-instalable-v1.md §4.5. Este es el primer
comando, ``validate``: lee el manifiesto y el conocimiento de una instancia y dice
en castellano que esta bien, que falta y que flujos se pueden prender. No usa la
red ni secretos, y no escribe nada.

Codigos de salida: 0 valida, 1 invalida, 2 uso incorrecto.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .commercial_knowledge import CommercialKnowledge, KnowledgeError
from .instance_manifest import FLOWS, InstanceManifest, ManifestError

MANIFEST_FILE = "instancia.toml"


def validate_instance(directory: Path) -> dict[str, Any]:
    """Valida una carpeta de instancia y devuelve el informe; no lanza por datos invalidos."""

    report: dict[str, Any] = {"instancia": str(directory), "valida": False, "errores": [], "avisos": []}
    manifest_path = directory / MANIFEST_FILE
    try:
        manifest = InstanceManifest.from_toml_file(manifest_path)
    except ManifestError as exc:
        report["errores"].append(f"{MANIFEST_FILE}: {exc}")
        return report

    report["manifiesto"] = {
        "producto": manifest.product_version,
        "tenant_ref": manifest.tenant_ref,
        "ally_ref": manifest.ally_ref,
        "marca": manifest.brand,
        "hotmart": f"{manifest.hotmart_product_id} / {manifest.hotlink} / {manifest.currency} {manifest.price}",
        "ofertas": [
            {
                "codigo": offer.code,
                "landing": f"{offer.site}/{offer.landing_id}",
                "por_defecto": offer.default,
            }
            for offer in manifest.offers
        ],
        "chatwoot": f"cuenta {manifest.chatwoot_account_id}, inbox {manifest.chatwoot_inbox_id}",
        "eventos": sorted(manifest.events),
    }
    flows: dict[str, Any] = {}
    for flow in FLOWS:
        blockers = manifest.flow_blockers(flow)
        flows[flow] = {
            "prendido": manifest.flows[flow],
            "se_puede_prender": not blockers,
            "falta": list(blockers),
        }
    report["flujos"] = flows

    knowledge_path = directory / manifest.agent_knowledge_path
    try:
        knowledge = CommercialKnowledge.from_toml_file(knowledge_path, require_approved=False)
    except KnowledgeError as exc:
        report["errores"].append(f"{manifest.agent_knowledge_path}: {exc}")
        return report
    report["conocimiento"] = {
        "archivo": manifest.agent_knowledge_path,
        "version": knowledge.version,
        "estado": knowledge.status,
        "sha256": knowledge.rendered_sha256,
        "bytes": len(knowledge.render().encode("utf-8")),
    }
    if knowledge.ally_ref != manifest.ally_ref:
        report["errores"].append(
            f"el conocimiento es de '{knowledge.ally_ref}' y el manifiesto de '{manifest.ally_ref}'"
        )
    if not knowledge.approved:
        report["avisos"].append(
            "el conocimiento esta en borrador: el agente no puede responder hasta que se apruebe"
        )
    if manifest.flows["inbound"] and not knowledge.approved:
        report["errores"].append("flujos.inbound esta prendido con el conocimiento en borrador")
    for flow, state in flows.items():
        if not state["se_puede_prender"]:
            report["avisos"].append(f"{flow} no se puede prender: " + "; ".join(state["falta"]))
    report["valida"] = not report["errores"]
    return report


def _print_human(report: dict[str, Any]) -> None:
    print(f"Instancia: {report['instancia']}")
    manifest = report.get("manifiesto")
    if manifest:
        print(f"  producto {manifest['producto']} · {manifest['tenant_ref']}/{manifest['ally_ref']} · {manifest['marca']}")
        print(f"  hotmart {manifest['hotmart']}")
        for offer in manifest["ofertas"]:
            mark = " (por defecto)" if offer["por_defecto"] else ""
            print(f"  oferta {offer['codigo']} ← {offer['landing']}{mark}")
        print(f"  chatwoot {manifest['chatwoot']}")
        print(f"  eventos: {', '.join(manifest['eventos'])}")
    for flow, state in report.get("flujos", {}).items():
        status = "prendido" if state["prendido"] else "apagado"
        ready = "" if state["se_puede_prender"] else " — no se puede prender"
        print(f"  flujo {flow}: {status}{ready}")
    knowledge = report.get("conocimiento")
    if knowledge:
        print(
            f"  conocimiento v{knowledge['version']} ({knowledge['estado']}), "
            f"{knowledge['bytes']} bytes, sha256 {knowledge['sha256'][:12]}"
        )
    for warning in report["avisos"]:
        print(f"AVISO: {warning}")
    for error in report["errores"]:
        print(f"ERROR: {error}")
    print("VALIDA" if report["valida"] else "INVALIDA")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="setter", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="valida el manifiesto y el conocimiento, sin red")
    validate.add_argument("instancia", type=Path, help="carpeta con instancia.toml")
    validate.add_argument("--json", action="store_true", help="salida en JSON")
    args = parser.parse_args(argv)

    if not args.instancia.is_dir():
        print(f"no existe la carpeta {args.instancia}", file=sys.stderr)
        return 2
    report = validate_instance(args.instancia)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0 if report["valida"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
