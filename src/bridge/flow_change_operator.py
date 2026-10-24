"""El lado del producto del comando del operador del almacen de flujos.

Diseno: el almacen, §10.2 («El comando del operador»). El script del VPS
(``scripts/flow_change_operator.py``) lee la propuesta, el almacen y el
catalogo con ``psql`` y le pasa todo a este modulo por la entrada estandar,
en un contenedor nuevo desde la imagen del bridge fijada por digest y sin red
(``docker run --rm -i --read-only --network none``), nunca con ``docker exec``
en el bridge vivo (V2#4). Este modulo no lee la base, no hace red y no
imprime secretos ni datos personales: corre el validador y arma la vista
previa sobre lo que recibe.

Subcomandos (``python -m bridge.flow_change_operator <subcomando>``):

* ``normalizar``: lee ``{instance, store, items, catalog}`` (los items de una
  propuesta del operador, todavia sin guardar), corre el validador en fase
  ``draft`` con ``operador`` e imprime los items normalizados, que son los que
  se guardan (``operator_save_flow_change_draft_v1``), y los bloqueos. Mismas
  salidas que ``validar``.
* ``validar``: lee ``{instance, store, proposal, catalog}`` e imprime la base
  validada, el validador, el resultado, el ``catalog_sha256`` y la vista
  previa. Sale con 0 sin bloqueos, con 3 con bloqueos y con 2 si la entrada no
  sirve (con ``{"ok": false, "reason_code"}``).

La entrada de ``validar``::

    {"instance": {"tenant_ref", "ally_ref", "products": [...], "time_zone"},
     "store": {"flows": [{"flow_id", "product_ref", "flow_key", "state",
                          "active_version", "generation",
                          "versions": {"<n>": <definicion>}}]},
     "proposal": {"id", "revision", "items", "draft_base", "purpose",
                  "requested_by_kind"},
     "catalog": {"message_templates": [...]} | null}

``product_version`` y ``git_sha`` del validador salen de la imagen
(``SETTER_VERSION`` y ``GIT_SHA``): quien llama no los elige.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .flow_change_preview import build_preview
from .flow_change_simulation import build_new_flow_simulation, build_simulation
from .flow_definition import (
    FlowWorld,
    InstanceView,
    ProposalItemsInvalid,
    TemplateCatalog,
    WorldFlow,
    loads_strict,
    parse_items,
)
from .flow_validator import FlowValidationInputError, validate

__all__ = ["OperatorInputError", "main", "normalize_items", "validate_proposal", "world_from_store"]

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_BLOCKED = 3
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_FLOWS = 50
_PRODUCT_VERSION = re.compile(r"^v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class OperatorInputError(ValueError):
    """La entrada no sirve: un error de quien llama, con su codigo cerrado."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OperatorInputError(code)
    return value


def instance_from_input(value: object) -> InstanceView:
    data = _mapping(value, "operator_instance_invalid")
    products = data.get("products")
    if not isinstance(products, list):
        raise OperatorInputError("operator_instance_invalid")
    try:
        return InstanceView(
            tenant_ref=data.get("tenant_ref"),  # type: ignore[arg-type]
            ally_ref=data.get("ally_ref"),  # type: ignore[arg-type]
            products=frozenset(products),
            time_zone=data.get("time_zone"),
        )
    except (TypeError, ValueError) as error:
        raise OperatorInputError("operator_instance_invalid") from error


def world_from_store(value: object) -> FlowWorld:
    """La foto del almacen que leyo el script, como ``FlowWorld``.

    Cada flujo trae todas sus versiones publicadas: el validador necesita la
    activa, la mas vieja con un horario objeto y el destino de un ``volver``.
    """
    data = _mapping(value, "operator_store_invalid")
    flows = data.get("flows")
    if not isinstance(flows, list) or len(flows) > MAX_FLOWS:
        raise OperatorInputError("operator_store_invalid")
    built: list[WorldFlow] = []
    try:
        for flow in flows:
            row = _mapping(flow, "operator_store_invalid")
            versions_in = _mapping(row.get("versions", {}), "operator_store_invalid")
            versions: dict[int, Mapping[str, Any]] = {}
            for number, definition in versions_in.items():
                if not isinstance(number, str) or not number.isdigit():
                    raise OperatorInputError("operator_store_invalid")
                versions[int(number)] = _mapping(definition, "operator_store_invalid")
            built.append(WorldFlow(
                flow_id=row.get("flow_id"),  # type: ignore[arg-type]
                product_ref=row.get("product_ref"),  # type: ignore[arg-type]
                flow_key=row.get("flow_key"),  # type: ignore[arg-type]
                state=row.get("state"),  # type: ignore[arg-type]
                active_version=row.get("active_version"),  # type: ignore[arg-type]
                generation=row.get("generation"),  # type: ignore[arg-type]
                versions=versions,
                native=row.get("native") is True,
            ))
        return FlowWorld(built)
    except (TypeError, ValueError) as error:
        if isinstance(error, OperatorInputError):
            raise
        raise OperatorInputError("operator_store_invalid") from error


def _image_identity(environ: Mapping[str, str]) -> tuple[str, str]:
    version = environ.get("SETTER_VERSION", "").strip()
    sha = environ.get("GIT_SHA", "").strip().lower()
    if not _PRODUCT_VERSION.fullmatch(version) or not _GIT_SHA.fullmatch(sha):
        raise OperatorInputError("operator_image_identity_missing")
    return version, sha


def validate_proposal(payload: object, *, environ: Mapping[str, str]) -> dict[str, Any]:
    """Valida una propuesta guardada, en fase ``publish``, con la clase de quien la pidio.

    Devuelve lo que piden ``operator_record_flow_change_validation_v1``
    (``validated_base``, ``validator``, ``result``, ``preview``) y la
    revalidacion de ``operator_publish_flow_change_v1`` (``catalog_sha256`` y
    ``validated_at``; la huella la pone el script, que la lee de la propuesta).
    """
    data = _mapping(payload, "operator_input_invalid")
    instance = instance_from_input(data.get("instance"))
    world = world_from_store(data.get("store"))
    proposal = _mapping(data.get("proposal"), "operator_proposal_invalid")
    actor = proposal.get("requested_by_kind")
    if actor not in ("automation_expert", "operador"):
        raise OperatorInputError("operator_proposal_invalid")
    purpose = proposal.get("purpose")
    if not isinstance(purpose, str):
        raise OperatorInputError("operator_proposal_invalid")
    draft_base = proposal.get("draft_base")
    if not isinstance(draft_base, Mapping):
        raise OperatorInputError("operator_proposal_invalid")
    catalog_in = data.get("catalog")
    try:
        items = parse_items(proposal.get("items"))
    except ProposalItemsInvalid as error:
        raise OperatorInputError("flow_change_items_invalid") from error
    catalog: TemplateCatalog | None
    try:
        catalog = None if catalog_in is None else TemplateCatalog.from_inbox_payload(catalog_in)
    except ValueError:
        catalog = None
    version, sha = _image_identity(environ)
    try:
        result = validate(items, actor=actor, world=world, instance=instance, catalog=catalog,
                          phase="publish", draft_base=draft_base)
    except FlowValidationInputError as error:
        raise OperatorInputError(error.code) from error
    result_json = result.to_json()
    validator = {**result.validator_json(), "product_version": version, "git_sha": sha}
    try:
        simulacion = None
        unavailable = data.get("simulation_unavailable")
        if isinstance(unavailable, str):
            simulacion = {"status": "failed", "reason_code": unavailable}
        elif "new_flow_counts" in data:
            simulacion = build_new_flow_simulation(data.get("new_flow_counts"), items)
        elif "engine" in data:
            simulacion = build_simulation(data.get("engine"), data.get("history"), items,
                                          time_zone=instance.time_zone, store=data.get("store"))
        preview = build_preview(items, world, purpose=purpose, warnings=result_json["warnings"],
                                simulacion=simulacion)
    except ValueError as error:
        raise OperatorInputError(str(error)) from error
    return {
        "ok": result.ok,
        "blocks": len(result.blocks),
        "validated_base": world.base_for(items),
        "validator": validator,
        "result": result_json,
        "preview": preview,
        "catalog_sha256": None if catalog is None else catalog.catalog_sha256,
        "validated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def normalize_items(payload: object) -> dict[str, Any]:
    """Los items de una propuesta del operador, normalizados en fase ``draft``.

    La base solo acepta items con la definicion normalizada
    (``_flow_change_items_are_valid``); el operador la escribe a mano.
    """
    data = _mapping(payload, "operator_input_invalid")
    instance = instance_from_input(data.get("instance"))
    world = world_from_store(data.get("store"))
    try:
        items = parse_items(data.get("items"))
    except ProposalItemsInvalid as error:
        raise OperatorInputError("flow_change_items_invalid") from error
    catalog_in = data.get("catalog")
    try:
        catalog = None if catalog_in is None else TemplateCatalog.from_inbox_payload(catalog_in)
    except ValueError:
        catalog = None
    try:
        result = validate(items, actor="operador", world=world, instance=instance, catalog=catalog,
                          phase="draft", draft_base=world.draft_base_for(items))
    except FlowValidationInputError as error:
        raise OperatorInputError(error.code) from error
    return {
        "ok": result.ok,
        "blocks": len(result.blocks),
        "result": result.to_json(),
        "items": result.items_json(),
        "draft_base": world.draft_base_for(items),
    }


def _read_stdin(stream: Any) -> object:
    raw = stream.buffer.read(MAX_INPUT_BYTES + 1) if hasattr(stream, "buffer") else stream.read().encode()
    if len(raw) > MAX_INPUT_BYTES:
        raise OperatorInputError("operator_input_too_large")
    try:
        return loads_strict(raw)
    except ValueError as error:
        raise OperatorInputError("operator_input_invalid") from error


def main(argv: list[str] | None = None, *, stdin: Any = None, stdout: Any = None,
         environ: Mapping[str, str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    environ = os.environ if environ is None else environ
    if args not in (["validar"], ["normalizar"]):
        stdout.write(json.dumps({"ok": False, "reason_code": "operator_usage"}) + "\n")
        return EXIT_INPUT
    try:
        payload = _read_stdin(stdin)
        out = (validate_proposal(payload, environ=environ) if args == ["validar"]
               else normalize_items(payload))
    except OperatorInputError as error:
        stdout.write(json.dumps({"ok": False, "reason_code": error.code}) + "\n")
        return EXIT_INPUT
    stdout.write(json.dumps(out, ensure_ascii=True, sort_keys=True) + "\n")
    return EXIT_OK if out["ok"] else EXIT_BLOCKED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
