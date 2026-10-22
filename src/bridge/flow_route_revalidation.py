"""La revalidacion de mover la ruta de un flujo a ``motor`` (contrato flow-engine-v1, 3.2, regla 3).

El script del VPS (``scripts/flow_change_operator.py mover``) lee el almacen,
la ruta y el catalogo con ``psql`` y le pasa todo a este modulo por la entrada
estandar, en un contenedor nuevo desde la imagen del bridge fijada por digest,
sin red y de solo lectura, como ``bridge.flow_change_operator`` (V2#4). Este
modulo no lee la base, no hace red y no imprime secretos ni datos personales.

Subcomando (``python -m bridge.flow_route_revalidation revalidar``). La entrada::

    {"instance": {"tenant_ref", "ally_ref", "products": [...], "time_zone"},
     "store": {"flows": [... la misma forma que bridge.flow_change_operator ...]},
     "flow": {"product_ref", "flow_key", "legacy_kind", "definition_sha256"},
     "catalog": {"message_templates": [...]} | null}

Corre sobre la version activa del flujo, tal cual esta publicada:

* el validador del ciclo en fase ``publish`` con ``operador``, sobre un item
  ``version`` que es la activa como borrador (``as_draft``) y con el
  ``legacy_kind`` de la ruta, para que mire la regla 14 de la forma del legado
  (que compile a su politica, T2.1);
* ``flow_engine.not_supported`` con ``ENGINE_CAPABILITIES_V1``: lo que el motor
  de esta imagen no ejecuta (la regla 14 del lado del motor).

Imprime ``{"ok", "revalidation", "blocks_detail"}``. ``revalidation`` tiene las
seis claves exactas que pide ``operator_route_flow_v1``: ``version`` y
``definition_sha256`` (las de la activa; el sha lo leyo el script de la base en
la misma lectura que la definicion), ``rules_version`` (2: las reglas del
validador mas las capacidades del motor v1, el ``engine.rules_version`` de
``read_flow_engine_v1``), ``capabilities`` (``"v1"``), ``blocks`` (los codigos;
vacia para pasar) y ``validator`` (las seis claves del ciclo, con la version y
el sha de la imagen). Sale con 0 sin bloqueos, con 3 con bloqueos y con 2 si la
entrada no sirve.

Lo que no mira: la regla 16 (el manifiesto como techo de los flujos de hoy), que
el validador de esta imagen tampoco mira al publicar.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
from collections.abc import Mapping
from typing import Any

from .flow_change_operator import (
    OperatorInputError,
    _image_identity,
    _mapping,
    _read_stdin,
    instance_from_input,
    world_from_store,
)
from .flow_definition import FlowWorld, ProposalItemsInvalid, TemplateCatalog, as_draft, parse_items
from .flow_engine import ENGINE_CAPABILITIES_V1, not_supported
from .flow_validator import FlowValidationInputError, validate

__all__ = ["ENGINE_RULES_VERSION", "main", "revalidate_route"]

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_BLOCKED = 3
# Las reglas con las que se mueve una ruta a motor: las del validador del ciclo y las
# capacidades v1 del motor. La base exige 2 o mas (_flow_engine_revalidation_is_valid).
ENGINE_RULES_VERSION = 2
ENGINE_CAPABILITIES_NAME = "v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_KINDS = ("carrito", "pago_fallido", "ficha", "precheckout", "descuento", "reactivacion")


def revalidate_route(payload: object, *, environ: Mapping[str, str]) -> dict[str, Any]:
    data = _mapping(payload, "operator_input_invalid")
    instance = instance_from_input(data.get("instance"))
    world = world_from_store(data.get("store"))
    target = _mapping(data.get("flow"), "route_flow_invalid")
    legacy_kind = target.get("legacy_kind")
    sha = target.get("definition_sha256")
    if legacy_kind not in _LEGACY_KINDS or not isinstance(sha, str) or not _SHA256.fullmatch(sha):
        raise OperatorInputError("route_flow_invalid")
    flow = world.find(target.get("product_ref"), target.get("flow_key"))  # type: ignore[arg-type]
    if flow is None:
        raise OperatorInputError("flow_not_found")
    if flow.active_version is None or flow.active_definition is None:
        raise OperatorInputError("route_flow_without_active_version")
    # El validador mira la regla 14 del legado solo con legacy_kind en el mundo (WorldFlow.legacy_kind).
    world = FlowWorld([dataclasses.replace(f, legacy_kind=legacy_kind) if f.flow_id == flow.flow_id else f
                       for f in world.flows])
    definition = flow.active_definition
    try:
        items = parse_items([{"producto": flow.product_ref, "flujo": flow.flow_key, "flow_id": flow.flow_id,
                              "tipo": "version", "definicion": as_draft(definition), "en_curso": "seguir"}])
    except ProposalItemsInvalid as error:
        raise OperatorInputError("flow_change_items_invalid") from error
    catalog_in = data.get("catalog")
    try:
        catalog = None if catalog_in is None else TemplateCatalog.from_inbox_payload(catalog_in)
    except ValueError:
        catalog = None
    if catalog is None:
        # Sin catalogo la regla 13 no se puede mirar: no se arma una revalidacion a medias.
        raise OperatorInputError("template_catalog_unavailable")
    version, git_sha = _image_identity(environ)
    try:
        result = validate(items, actor="operador", world=world, instance=instance, catalog=catalog,
                          phase="publish", draft_base=world.draft_base_for(items))
    except FlowValidationInputError as error:
        raise OperatorInputError(error.code) from error
    result_json = result.to_json()
    detail = [{"code": block["code"], "path": block["path"], "rule": block["rule"]} for block in result_json["blocks"]]
    for code in not_supported(definition, ENGINE_CAPABILITIES_V1):
        detail.append({"code": code, "path": "", "rule": 14})
    codes: list[str] = []
    for block in detail:
        if block["code"] not in codes:
            codes.append(block["code"])
    return {
        "ok": not codes,
        "revalidation": {
            "version": flow.active_version,
            "definition_sha256": sha,
            "rules_version": ENGINE_RULES_VERSION,
            "capabilities": ENGINE_CAPABILITIES_NAME,
            "blocks": codes,
            "validator": {**result.validator_json(), "product_version": version, "git_sha": git_sha},
        },
        "blocks_detail": detail,
        "warnings": [{"code": w["code"], "path": w["path"]} for w in result_json["warnings"]],
    }


def main(argv: list[str] | None = None, *, stdin: Any = None, stdout: Any = None,
         environ: Mapping[str, str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    environ = os.environ if environ is None else environ
    if args != ["revalidar"]:
        stdout.write(json.dumps({"ok": False, "reason_code": "operator_usage"}) + "\n")
        return EXIT_INPUT
    try:
        out = revalidate_route(_read_stdin(stdin), environ=environ)
    except OperatorInputError as error:
        stdout.write(json.dumps({"ok": False, "reason_code": error.code}) + "\n")
        return EXIT_INPUT
    stdout.write(json.dumps(out, ensure_ascii=True, sort_keys=True) + "\n")
    return EXIT_OK if out["ok"] else EXIT_BLOCKED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
