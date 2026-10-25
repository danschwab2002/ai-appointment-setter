"""El validador de las definiciones de flujo: reglas 1 a 13, 15 y 17, las siete
advertencias y los chequeos del ciclo.

Contratos: docs/contracts/flow-definition-v1.md («Las reglas del validador» y
«Los siete flujos de validacion») y docs/contracts/flow-change-cycle-v1.md (la
propuesta, la decision 4 y el OK del cliente).

Fixtures:

* ``flow_store/schema_examples_v1.json``: los ocho bloques del contrato (la
  reactivacion tiene dos), sin ``version`` ni ``estado``.
* ``chatwoot_inbox_11_message_templates_20261010.json``: el catalogo de la
  bandeja 11 de ATT1, capturado de Chatwoot el 2026-10-10 (las cinco
  plantillas, APPROVED, MARKETING, es_MX).
* ``chatwoot_message_templates_inbox_9_20261001.json``: de la bandeja 9,
  capturada el 2026-10-01, sale ``hello_world``, la unica plantilla UTILITY
  capturada (en_US, con un HEADER de texto, un BODY sin marcadores y un
  FOOTER). Se suma al catalogo de la bandeja 11 donde la regla 17 y el OK del
  cliente necesitan una plantilla de utilidad: el payload es el de Chatwoot, no
  uno inventado.
* ``flow_store/invariant_cases.json``: los siete casos de los invariantes 3 y
  4 que comparten la base y el validador, con el formato fijo de la parte de
  la base (``validate_flow_store.mjs``, commit 1).
* ``flow_store/base_example.json``: el mundo de prueba y las bases que dio
  ``_flow_store_base`` en PGlite (``validate_flow_store.mjs``, paso 9).
* ``flow_store/validator_results_cases.json``: lo genera esta prueba (el
  puente con la base): los resultados de ``CASES`` y de dos bordes, con su
  ``to_json()`` y su ``validator_json()``, que PGlite pasa por las funciones de
  la migracion (``validate_flow_store.mjs``, paso 10).

Las funciones ``*_are_valid`` de abajo son las de la migracion del almacen,
escritas en Python con sus mismas formas.

Los caracteres de control se escriben con escapes ``\\u``.
"""

from __future__ import annotations

import dataclasses

import copy
import json
import os
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_definition as fd
from bridge import flow_validator as fv
from bridge.flow_definition import (
    Finding,
    FlowWorld,
    InstanceView,
    ProposalItem,
    ProposalItemsInvalid,
    StrictJSONError,
    TemplateCatalog,
    WorldFlow,
)
from bridge.flow_validator import FlowValidationInputError, ValidationResult, validate

FIXTURES = Path(__file__).parent / "fixtures"
FLOW_STORE = FIXTURES / "flow_store"
BLOCKS: dict[str, dict[str, Any]] = {
    block["block"]: block
    for block in json.loads((FLOW_STORE / "schema_examples_v1.json").read_text(encoding="utf-8"))["blocks"]
}
INBOX_11: dict[str, Any] = json.loads(
    (FIXTURES / "chatwoot_inbox_11_message_templates_20261010.json").read_text(encoding="utf-8")
)
CATALOG = TemplateCatalog.from_inbox_payload(INBOX_11)
INSTANCE = InstanceView("lancemos", "att1", frozenset({"att1"}), "America/Mexico_City")
KEYS = ("carrito", "pago_fallido", "precheckout", "descuento", "reactivacion", "ficha", "activacion")
BLOCK_OF = {"carrito": "1", "pago_fallido": "2", "precheckout": "3", "descuento": "4",
            "reactivacion": "5a", "ficha": "6", "activacion": "7"}
IDS = {key: f"{index:08x}-0000-4000-8000-{index:012x}" for index, key in enumerate(KEYS, start=1)}
CDMX = {"zona": "America/Mexico_City", "desde": "09:00", "hasta": "21:00"}
TEMPLATE_STEP = {"plantilla": "att1_carrito_abandonado_01", "idioma": "es_MX", "variables": ["nombre", "producto"]}
CLIENT_OK = {"quien": "Juan", "medio": "WhatsApp", "fecha": "2026-10-15"}
INBOX_9: dict[str, Any] = json.loads(
    (FIXTURES / "chatwoot_message_templates_inbox_9_20261001.json").read_text(encoding="utf-8")
)
HELLO_WORLD: dict[str, Any] = next(t for t in INBOX_9["message_templates"] if t["name"] == "hello_world")
# El BODY de hello_world no tiene marcadores: el paso no manda variables.
HELLO_WORLD_STEP = {"plantilla": "hello_world", "idioma": "en_US", "variables": []}
# La plantilla de utilidad de ATT1 con nombre y producto y sin boton, leida de Chatwoot el 2026-10-21
# (flow_change_operator/chatwoot_db_message_templates_inbox_11_20261021.json): la del flujo nuevo de la
# bienvenida (el ciclo 1.7.0) y la forma que ejecuta el motor nativo.
CONFIRMED: dict[str, Any] = next(
    t for t in json.loads((FIXTURES / "flow_change_operator/chatwoot_db_message_templates_inbox_11_20261021.json")
                          .read_text(encoding="utf-8"))["message_templates"]
    if t["name"] == "att1_pago_efectivo_confirmado_01")
CONFIRMED_STEP = {"plantilla": "att1_pago_efectivo_confirmado_01", "idioma": "es_MX", "variables": ["nombre", "producto"]}


# ---------------------------------------------------------------------------
# Armado del mundo y de las propuestas.
# ---------------------------------------------------------------------------

def block(block_id: str) -> dict[str, Any]:
    return copy.deepcopy(BLOCKS[block_id]["definition"])


def flow_def(key: str) -> dict[str, Any]:
    return block(BLOCK_OF[key])


def published(definition: dict[str, Any], version: int = 1) -> dict[str, Any]:
    """Como queda en flow_definitions: normalizada, con su numero y publicada."""
    result = fd.normalize_definition(definition, new_flow=True, instance=INSTANCE,
                                     other_schedules=(CDMX,), catalog=CATALOG)
    normalized = result.definition
    normalized["version"] = version
    normalized["estado"] = "publicada"
    return normalized


def flow(key: str, *, versions: dict[int, dict[str, Any]] | None = None, active: int | None = None,
         state: str | None = None, generation: int | None = None, product: str = "att1",
         flow_id: str | None = None, legacy_kind: str | None = None, native: bool = False) -> WorldFlow:
    versions = versions or {}
    if active is None and versions:
        active = max(versions)
    return WorldFlow(
        flow_id=flow_id or IDS[key], product_ref=product, flow_key=key,
        state=state or ("prendido" if active is not None else "apagado"),
        active_version=active, generation=generation if generation is not None else len(versions),
        versions=versions, legacy_kind=legacy_kind, native=native,
    )


def live(key: str, definition: dict[str, Any] | None = None, **kwargs: Any) -> WorldFlow:
    """Un flujo con su v1 publicada."""
    return flow(key, versions={1: published(definition if definition is not None else flow_def(key))}, **kwargs)


def world(*overrides: WorldFlow, keys: tuple[str, ...] = KEYS) -> FlowWorld:
    by_key = {override.flow_key: override for override in overrides}
    flows = [by_key.pop(key, None) or flow(key) for key in keys]
    return FlowWorld(flows + list(by_key.values()))


def version_item(definition: dict[str, Any], key: str = "carrito", *, en_curso: str = "seguir",
                 ok: dict[str, Any] | None = None, product: str = "att1", flow_id: str | None = None,
                 ) -> ProposalItem:
    return ProposalItem(producto=product, flujo=key, flow_id=flow_id or IDS[key], tipo="version",
                        definicion=definition, en_curso=en_curso, ok_del_cliente=ok)


def state_item(tipo: str, key: str = "carrito", *, version: int | None = None) -> ProposalItem:
    return ProposalItem(producto="att1", flujo=key, flow_id=IDS[key], tipo=tipo, version=version,
                        en_curso="seguir" if tipo == "volver" else None)


def native_catalog() -> TemplateCatalog:
    """El catalogo de la bandeja 11 con la plantilla de utilidad capturada, tal cual."""
    return catalog_with(lambda payload: payload["message_templates"].append(copy.deepcopy(CONFIRMED)))


def welcome(**changes: Any) -> dict[str, Any]:
    """Lo minimo que escribe el agente para la bienvenida despues de la compra; el resto son defaults."""
    definition: dict[str, Any] = {
        "nombre": "Bienvenida despues de la compra", "dispara": {"evento": "compra"},
        "entra": {"permiso": "compra"}, "pasos": [copy.deepcopy(CONFIRMED_STEP), {"etiquetar": "cliente"}],
        "horario": "siempre",
    }
    definition.update(changes)
    return definition


def new_item(definition: dict[str, Any], key: str = "bienvenida", *, prender: str = "cohorte",
             ok: dict[str, Any] | None = CLIENT_OK) -> ProposalItem:
    return ProposalItem(producto="att1", flujo=key, flow_id=None, tipo="nuevo", definicion=definition,
                        prender=prender, ok_del_cliente=ok)


def run_new(definition: dict[str, Any] | None = None, key: str = "bienvenida", *, prender: str = "cohorte",
            actor: str = "automation_expert", **kwargs: Any) -> list[Finding]:
    kwargs.setdefault("catalog", native_catalog())
    result = check([new_item(definition if definition is not None else welcome(), key, prender=prender)],
                   actor=actor, draft_base={}, **kwargs)
    return [*result.blocks, *result.warnings]


def _legacy_purchase(pasos: list[dict[str, Any]]) -> list[Finding]:
    """La activacion registrada sin version (un flujo del legado, no nativo) con la compra y su OK."""
    result = check([version_item(purchase_flow(pasos=pasos), "activacion", ok=CLIENT_OK)],
                   catalog=hello_world_catalog())
    return [*result.blocks, *result.warnings]


def check(items: list[ProposalItem], *, actor: str = "operador", phase: str = "publish",
          in_world: FlowWorld | None = None, catalog: TemplateCatalog | None = CATALOG,
          instance: InstanceView = INSTANCE, draft_base: dict[str, Any] | None = None) -> ValidationResult:
    return validate(items, actor=actor, world=in_world or world(), instance=instance,
                    catalog=catalog, phase=phase, draft_base=draft_base)


def run(definition: dict[str, Any], key: str = "carrito", **kwargs: Any) -> list[Finding]:
    result = check([version_item(definition, key)], **kwargs)
    return [*result.blocks, *result.warnings]


def draft_of(key: str, change: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Un borrador que arranca de la v1 publicada de ``key`` (como arranca el agente)."""
    draft = fd.as_draft(published(flow_def(key)))
    if change is not None:
        change(draft)
    return draft


def found(findings: list[Finding] | ValidationResult) -> set[str]:
    if isinstance(findings, ValidationResult):
        findings = [*findings.blocks, *findings.warnings]
    return {finding.code for finding in findings}


def catalog_with(change: Callable[[dict[str, Any]], None]) -> TemplateCatalog:
    payload = copy.deepcopy(INBOX_11)
    change(payload)
    return TemplateCatalog.from_inbox_payload(payload)


def with_hello_world(payload: dict[str, Any]) -> None:
    """Suma al catalogo la plantilla UTILITY capturada, tal cual."""
    payload["message_templates"].append(copy.deepcopy(HELLO_WORLD))


def hello_world_catalog(change: Callable[[dict[str, Any]], None] | None = None) -> TemplateCatalog:
    """El catalogo de la bandeja 11 con hello_world, y ``change`` aplicado a hello_world."""
    def build(payload: dict[str, Any]) -> None:
        with_hello_world(payload)
        if change is not None:
            change(payload["message_templates"][-1])
    return catalog_with(build)


def purchase_flow(**changes: Any) -> dict[str, Any]:
    definition = block("7")
    definition["entra"]["permiso"] = "compra"
    definition["pasos"][0] = copy.deepcopy(HELLO_WORLD_STEP)
    definition["termina"]["vence"] = "1d"
    definition.update(changes)
    return definition


def with_(definition: dict[str, Any], change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    change(definition)
    return definition


def template_of(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return next(template for template in payload["message_templates"] if template["name"] == name)


def blocks_at(result: ValidationResult) -> list[tuple[str, str]]:
    return [(finding.code, finding.path) for finding in result.blocks]


def warnings_at(result: ValidationResult) -> list[tuple[str, str]]:
    return [(finding.code, finding.path) for finding in result.warnings]


# ---------------------------------------------------------------------------
# Un caso negativo y uno positivo vecino por cada codigo del catalogo cerrado.
# ---------------------------------------------------------------------------

def _duplicate_key_negative() -> list[Finding]:
    try:
        fd.loads_strict('{"topes": {"bolsa": "a", "bolsa": "b"}}')
    except StrictJSONError as error:
        assert error.finding is not None
        return [error.finding]
    raise AssertionError("loads_strict acepto una clave repetida")


def _duplicate_key_positive() -> list[Finding]:
    definition = fd.loads_strict(json.dumps(flow_def("carrito")))
    return run(definition)


def _rollback(version: int) -> list[Finding]:
    two = flow("carrito", versions={1: published(flow_def("carrito")), 2: published(flow_def("carrito"), 2)})
    result = check([state_item("volver", version=version)], in_world=world(two))
    return [*result.blocks, *result.warnings]


def _stale(active: int | None) -> list[Finding]:
    result = check([version_item(draft_of("carrito"))], in_world=world(live("carrito")),
                   draft_base={IDS["carrito"]: {"active_version": active}})
    return [*result.blocks, *result.warnings]


def _same_trigger(related: bool) -> list[Finding]:
    other = flow("carrito_dos", flow_id="0000000a-0000-4000-8000-00000000000a",
                 versions={1: published(with_(flow_def("carrito"), lambda d: d.update(flujo="carrito_dos")))})
    definition = flow_def("carrito")
    if related:
        definition["prioridad"]["le_gana_a"].append("carrito_dos")
    return run(definition, in_world=world(other))


def _personal_touches(count: int) -> list[dict[str, Any]]:
    """``count`` envios con ``cupon: personal``, separados por 24 h."""
    steps: list[dict[str, Any]] = []
    for index in range(count):
        if index:
            steps.append({"esperar": "24h"})
        steps.append({**TEMPLATE_STEP, "boton": {"link": "checkout", "cupon": fd.PERSONAL_COUPON}})
    return steps


PERSONAL_BUTTON = {"link": "checkout", "cupon": "personal"}


def legacy(key: str = "carrito") -> FlowWorld:
    """El mundo con ``key`` registrado como el flujo del legado del mismo nombre (T2.1)."""
    return world(flow(key, legacy_kind=key))


def _legacy_run(pasos: list[dict[str, Any]], key: str = "carrito") -> list[Finding]:
    return run(with_(flow_def(key), lambda d: d.update(pasos=pasos)), key, in_world=legacy(key))


CASES: dict[str, tuple[Callable[[], list[Finding]], Callable[[], list[Finding]]]] = {
    # Regla 1.
    "unknown_key": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["topes"].update(max=5))),
        lambda: run(flow_def("carrito")),
    ),
    "invalid_value": (
        lambda: run(with_(flow_def("carrito"), lambda d: d.update(nombre="Carrito\u001babandonado"))),
        lambda: run(with_(flow_def("carrito"), lambda d: d.update(nombre="Carrito ñandú"))),
    ),
    "duplicate_key": (_duplicate_key_negative, _duplicate_key_positive),
    "required_key_missing": (
        lambda: run(with_(flow_def("carrito"), lambda d: d.pop("nombre"))),
        lambda: run(flow_def("carrito")),
    ),
    "version_set_by_cycle": (
        lambda: run(with_(flow_def("carrito"), lambda d: d.update(version=1))),
        lambda: run(flow_def("carrito")),
    ),
    "default_pool_invalid": (
        lambda: check([version_item(block("5a"), "reactivacion", product="1producto")],
                      in_world=world(flow("reactivacion", product="1producto")),
                      instance=InstanceView("lancemos", "att1", frozenset({"1producto"}), "America/Mexico_City")).blocks,
        lambda: check([version_item(with_(block("5a"), lambda d: d["topes"].update(bolsa="reactivacion")),
                                    "reactivacion", product="1producto")],
                      in_world=world(flow("reactivacion", product="1producto")),
                      instance=InstanceView("lancemos", "att1", frozenset({"1producto"}), "America/Mexico_City")).blocks,
    ),
    # Regla 2.
    "trigger_not_exactly_one": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["dispara"].update(
            silencio={"lo_ultimo": "agente", "desde": "24h", "hasta": "72h"}))),
        lambda: run(flow_def("carrito")),
    ),
    "silence_window_invalid": (
        lambda: run(with_(flow_def("descuento"), lambda d: d["dispara"]["silencio"].update(desde="72h", hasta="24h")),
                    "descuento"),
        lambda: run(flow_def("descuento"), "descuento"),
    ),
    # Regla 3.
    "permission_trigger_mismatch": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["entra"].update(permiso="conversacion"))),
        lambda: run(flow_def("carrito")),
    ),
    "contesto_a_requires_conversation": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["entra"].update(contesto_a=["precheckout"]))),
        lambda: run(flow_def("descuento"), "descuento"),
    ),
    # Regla 4.
    "purchase_exit_missing": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["termina"].update(si=[]))),
        lambda: run(flow_def("carrito")),
    ),
    "purchase_exit_with_purchase_trigger": (
        lambda: run(with_(flow_def("activacion"), lambda d: d["termina"].update(si=["compra"])), "activacion",
                    phase="draft"),
        lambda: run(flow_def("activacion"), "activacion", phase="draft"),
    ),
    # Regla 5.
    "ficha_vencida_requires_ficha_trigger": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["termina"].update(si=["compra", "ficha_vencida"]))),
        lambda: run(flow_def("ficha"), "ficha", phase="draft"),
    ),
    "medio_de_pago_requires_ficha_trigger": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["pasos"][0].update(variables=["nombre", "medio_de_pago"]))),
        lambda: run(flow_def("ficha"), "ficha", phase="draft"),
    ),
    "ficha_link_requires_ficha_trigger": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["pasos"][0].update(boton={"link": "ficha"}))),
        lambda: run(flow_def("ficha"), "ficha", phase="draft"),
    ),
    "coupon_requires_checkout_link": (
        lambda: run(with_(flow_def("descuento"), lambda d: d["pasos"][0].update(
            boton={"link": "ficha", "cupon": "TIROIDES10"})), "descuento"),
        lambda: run(flow_def("descuento"), "descuento"),
    ),
    # Regla 6.
    "event_requires_vence": (
        lambda: run(draft_of("carrito", lambda d: d["termina"].pop("vence")), in_world=world(live("carrito"))),
        lambda: run(draft_of("carrito"), in_world=world(live("carrito"))),
    ),
    "silence_forbids_vence": (
        lambda: run(with_(flow_def("descuento"), lambda d: d["termina"].update(vence="1d")), "descuento"),
        lambda: run(flow_def("descuento"), "descuento"),
    ),
    # Regla 7.
    "conversar_outside_open_window_branch": (
        lambda: run(with_(block("5b"), lambda d: d.update(pasos=[{"conversar": "Retomar la charla."}])),
                    "reactivacion"),
        lambda: run(block("5b"), "reactivacion"),
    ),
    "terminal_step_not_last": (
        lambda: run(with_(flow_def("carrito"), lambda d: d.update(
            pasos=[{"derivar": "Avisar al equipo."}, TEMPLATE_STEP]))),
        lambda: run(with_(flow_def("carrito"), lambda d: d.update(
            pasos=[TEMPLATE_STEP, {"derivar": "Avisar al equipo."}]))),
    ),
    "nested_condition": (
        lambda: run(with_(block("5b"), lambda d: d["pasos"][0].update(entonces=[{
            "condicion": {"dato": "ventana", "es": ["abierta"]},
            "entonces": [{"conversar": "Retomar la charla."}], "si_no": []}])), "reactivacion"),
        lambda: run(block("5b"), "reactivacion"),
    ),
    # Regla 8.
    "hour_invalid": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["horario"].update(desde="9:00"))),
        lambda: run(flow_def("carrito")),
    ),
    "window_not_increasing": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["horario"].update(desde="21:00", hasta="09:00"))),
        lambda: run(flow_def("carrito")),
    ),
    "zone_invalid": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["horario"].update(zona="America/Nowhere"))),
        lambda: run(flow_def("carrito")),
    ),
    "zone_changed": (
        lambda: run(draft_of("carrito", lambda d: d["horario"].update(zona="America/Bogota")),
                    in_world=world(live("carrito"))),
        lambda: run(draft_of("carrito", lambda d: d["horario"].update(desde="10:00")),
                    in_world=world(live("carrito"))),
    ),
    "pool_zone_mismatch": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["horario"].update(zona="America/Bogota")),
                    in_world=world(live("pago_fallido"))),
        lambda: run(flow_def("carrito"), in_world=world(live("pago_fallido"))),
    ),
    # Regla 9.
    "cap_invalid": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["topes"].update(por_dia=0))),
        lambda: run(flow_def("carrito")),
    ),
    "daily_cap_exceeds_total": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["topes"].update(por_dia=2000))),
        lambda: run(flow_def("carrito")),
    ),
    # Regla 10.
    "unknown_flow_reference": (
        lambda: run(flow_def("carrito"), in_world=world(keys=("carrito", "pago_fallido"))),
        lambda: run(flow_def("carrito")),
    ),
    "mutual_le_gana_a": (
        lambda: run(flow_def("carrito"), in_world=world(live("precheckout", with_(
            flow_def("precheckout"), lambda d: d.update(prioridad={"le_gana_a": ["carrito"]}))))),
        lambda: run(flow_def("carrito"), in_world=world(live("precheckout"))),
    ),
    "product_not_declared": (
        lambda: run(with_(flow_def("carrito"), lambda d: d.update(producto="att9"))),
        lambda: run(with_(flow_def("carrito"), lambda d: d.update(producto="att1"))),
    ),
    "product_required": (
        lambda: run(flow_def("carrito"),
                    instance=InstanceView("lancemos", "att1", frozenset({"att1", "att2"}), "America/Mexico_City")),
        lambda: run(with_(flow_def("carrito"), lambda d: d.update(producto="att1")),
                    instance=InstanceView("lancemos", "att1", frozenset({"att1", "att2"}), "America/Mexico_City")),
    ),
    "duplicate_flow_key": (
        lambda: run(with_(flow_def("pago_fallido"), lambda d: d.update(flujo="carrito")), "pago_fallido"),
        lambda: run(flow_def("pago_fallido"), "pago_fallido"),
    ),
    # Regla 11.
    "unresolved_value": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["pasos"].insert(0, {"esperar": "a_medir"}))),
        lambda: run(with_(flow_def("carrito"), lambda d: d["pasos"].insert(0, {"esperar": "a_medir"})),
                    phase="draft"),
    ),
    # Regla 12.
    "vence_not_after_waits": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["pasos"].insert(0, {"esperar": "2d"}))),
        lambda: run(with_(flow_def("carrito"), lambda d: (d["pasos"].insert(0, {"esperar": "2d"}),
                                                           d["termina"].update(vence="3d")))),
    ),
    # Regla 13.
    "template_not_in_catalog": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["pasos"][0].update(plantilla="att1_no_existe_01"))),
        lambda: run(flow_def("carrito")),
    ),
    "template_not_approved": (
        lambda: run(flow_def("carrito"), catalog=catalog_with(lambda p: next(
            t for t in p["message_templates"] if t["name"] == "att1_carrito_abandonado_01").update(status="PAUSED"))),
        lambda: run(flow_def("carrito")),
    ),
    "template_language_mismatch": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["pasos"][0].update(idioma="en_US"))),
        lambda: run(flow_def("carrito")),
    ),
    "template_variable_count_mismatch": (
        lambda: run(with_(block("5a"), lambda d: d["pasos"][0].update(variables=["nombre", "producto"])),
                    "reactivacion"),
        lambda: run(block("5a"), "reactivacion"),
    ),
    "template_button_mismatch": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["pasos"][0].update(boton={"link": "checkout"}))),
        lambda: run(flow_def("descuento"), "descuento"),
    ),
    "template_catalog_unavailable": (
        lambda: run(flow_def("carrito"), catalog=None),
        lambda: run(flow_def("carrito")),
    ),
    # Regla 14, con legacy_kind (T2.1): lo que se publica compila a la politica de su flujo del legado.
    "engine_not_supported:more_sends_than_steps": (
        lambda: _legacy_run([{"esperar": "60m"}, dict(TEMPLATE_STEP), {"esperar": "1h"}, dict(TEMPLATE_STEP)],
                            "precheckout"),
        lambda: _legacy_run([{"esperar": "60m"}, dict(TEMPLATE_STEP)], "precheckout"),
    ),
    "engine_not_supported:coupon_not_allowed": (
        lambda: _legacy_run([dict(TEMPLATE_STEP), {"esperar": "24h"},
                             {**TEMPLATE_STEP, "boton": dict(PERSONAL_BUTTON)}]),
        lambda: _legacy_run([dict(TEMPLATE_STEP), {"esperar": "24h"}, dict(TEMPLATE_STEP)]),
    ),
    "engine_not_supported:coupon_missing": (
        lambda: _legacy_run([dict(TEMPLATE_STEP), {"esperar": "24h"}, dict(TEMPLATE_STEP), {"esperar": "48h"},
                             dict(TEMPLATE_STEP)]),
        lambda: _legacy_run([dict(TEMPLATE_STEP), {"esperar": "24h"}, dict(TEMPLATE_STEP), {"esperar": "48h"},
                             {**TEMPLATE_STEP, "variables": ["nombre", "producto", "vence"],
                              "boton": dict(PERSONAL_BUTTON)}]),
    ),
    "engine_not_supported:fixed_coupon": (
        lambda: _legacy_run([{**TEMPLATE_STEP, "boton": {"link": "checkout", "cupon": "TIROIDES10"}}]),
        lambda: _legacy_run([dict(TEMPLATE_STEP)]),
    ),
    "engine_not_supported:variables_differ": (
        lambda: _legacy_run([{**TEMPLATE_STEP, "variables": ["producto", "nombre"]}]),
        lambda: _legacy_run([dict(TEMPLATE_STEP)]),
    ),
    "engine_not_supported:espera_entre_envios": (
        lambda: _legacy_run([dict(TEMPLATE_STEP), dict(TEMPLATE_STEP)]),
        lambda: _legacy_run([dict(TEMPLATE_STEP), {"esperar": "1m"}, dict(TEMPLATE_STEP)]),
    ),
    "engine_not_supported:espera_despues_del_envio": (
        lambda: _legacy_run([dict(TEMPLATE_STEP), {"esperar": "1h"}]),
        lambda: _legacy_run([dict(TEMPLATE_STEP)]),
    ),
    # Regla 15.
    "coupon_change_not_supported": (
        lambda: run(draft_of("descuento", lambda d: d["pasos"][0]["boton"].update(cupon="OTRO10")), "descuento",
                    in_world=world(live("descuento"))),
        lambda: run(draft_of("descuento"), "descuento", in_world=world(live("descuento"))),
    ),
    # Regla 15, el limite de la 1.1: a lo sumo dos cupones personales por camino.
    "coupon_personal_limit": (
        lambda: run(with_(flow_def("carrito"), lambda d: d.update(pasos=_personal_touches(3)))),
        lambda: run(with_(flow_def("carrito"), lambda d: d.update(pasos=_personal_touches(2)))),
    ),
    # Regla 5, con las variables de la 1.1: precio y vence solo con cupon personal; cuando solo con un evento.
    "variable_needs_personal_coupon": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["pasos"][0].update(
            variables=["nombre", "producto", "vence"])), phase="draft"),
        lambda: run(with_(flow_def("carrito"), lambda d: d["pasos"][0].update(
            variables=["nombre", "producto", "vence"], boton={"link": "checkout", "cupon": fd.PERSONAL_COUPON})),
            phase="draft"),
    ),
    "variable_needs_event_trigger": (
        lambda: run(with_(block("5a"), lambda d: d["pasos"][0].update(variables=["nombre", "cuando"])),
                    "reactivacion", phase="draft"),
        lambda: run(with_(flow_def("carrito"), lambda d: d["pasos"][0].update(
            variables=["nombre", "producto", "cuando"])), phase="draft"),
    ),
    # Regla 17.
    "purchase_permission_requires_single_utility_send": (
        lambda: check([version_item(purchase_flow(pasos=[dict(TEMPLATE_STEP)]), "activacion", ok=CLIENT_OK)]).blocks,
        lambda: check([version_item(purchase_flow(), "activacion", ok=CLIENT_OK)],
                      catalog=hello_world_catalog()).blocks,
    ),
    # El ciclo.
    "flow_creation_not_enabled": (
        lambda: run(flow_def("carrito"), actor="automation_expert"),
        lambda: run(flow_def("carrito"), actor="operador"),
    ),
    "change_outside_1b_scope": (
        lambda: run(draft_of("carrito", lambda d: d["topes"].update(bolsa="att1-carrito")),
                    actor="automation_expert", in_world=world(live("carrito"))),
        lambda: run(draft_of("carrito", lambda d: d["topes"].update(por_dia=90)),
                    actor="automation_expert", in_world=world(live("carrito"))),
    ),
    "trigger_change_is_another_flow": (
        lambda: run(draft_of("carrito", lambda d: d["dispara"].update(evento="pago_fallido")),
                    in_world=world(live("carrito"))),
        lambda: run(draft_of("carrito"), in_world=world(live("carrito"))),
    ),
    "flow_change_stale_base": (lambda: _stale(None), lambda: _stale(1)),
    "rollback_target_not_found": (lambda: _rollback(9), lambda: _rollback(1)),
    "rollback_target_is_active": (lambda: _rollback(2), lambda: _rollback(1)),
    "state_transition_invalid": (
        lambda: check([state_item("prender")], in_world=world(live("carrito"))).blocks,
        lambda: check([state_item("prender")], in_world=world(live("carrito", state="apagado"))).blocks,
    ),
    # Ciclo 1.8.0: prender con a: cohorte, solo para un flujo nativo pausado.
    "prender_a_cohorte_needs_paused_native": (
        lambda: check([dataclasses.replace(state_item("prender"), a="cohorte")],
                      in_world=world(live("carrito", state="pausado"))).blocks,
        lambda: check([dataclasses.replace(state_item("prender"), a="cohorte")],
                      in_world=world(live("carrito", state="pausado", native=True))).blocks,
    ),
    "flow_has_no_version": (
        lambda: check([state_item("prender")]).blocks,
        lambda: check([version_item(flow_def("carrito")), state_item("prender")]).blocks,
    ),
    "en_curso_not_supported": (
        lambda: check([version_item(flow_def("carrito"), en_curso="cancelar")]).blocks,
        lambda: check([version_item(flow_def("carrito"), en_curso="seguir")]).blocks,
    ),
    "identifier_not_allowed": (
        lambda: run(with_(flow_def("carrito"), lambda d: d.update(nombre="Carrito 5512345678"))),
        lambda: run(flow_def("carrito")),
    ),
    "client_purchase_ok_missing": (
        lambda: check([version_item(purchase_flow(), "activacion")], catalog=hello_world_catalog()).blocks,
        lambda: check([version_item(purchase_flow(), "activacion", ok=CLIENT_OK)],
                      catalog=hello_world_catalog()).blocks,
    ),
    # Las advertencias.
    "always_with_marketing": (
        lambda: run(block("5a"), "reactivacion"),
        lambda: run(with_(block("5a"), lambda d: d.update(horario=dict(CDMX))), "reactivacion"),
    ),
    "recovery_without_cap": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["topes"].update(por_dia="sin_tope"))),
        lambda: run(flow_def("carrito")),
    ),
    "silence_without_purchase_exit": (
        lambda: run(block("5a"), "reactivacion"),
        lambda: run(flow_def("descuento"), "descuento"),
    ),
    "same_trigger_unrelated": (lambda: _same_trigger(False), lambda: _same_trigger(True)),
    "wait_without_contesta": (
        lambda: run(flow_def("precheckout"), "precheckout"),
        lambda: run(with_(flow_def("precheckout"), lambda d: d["termina"].update(si=["compra", "contesta"])),
                    "precheckout"),
    ),
    "silence_window_shorter_than_closed_gap": (
        lambda: run(with_(flow_def("descuento"), lambda d: d["dispara"]["silencio"].update(hasta="30h")),
                    "descuento"),
        lambda: run(flow_def("descuento"), "descuento"),
    ),
    # El item nuevo (ciclo 1.7.0) y el motor nativo (flow-engine-v2 §9).
    "flow_already_registered": (lambda: run_new(key="activacion"), lambda: run_new()),
    "new_flow_must_start_in_cohort": (lambda: run_new(prender="prendido"), lambda: run_new()),
    "etiquetar_requires_purchase_trigger": (
        lambda: run(with_(flow_def("carrito"), lambda d: d["pasos"].append({"etiquetar": "cliente"}))),
        lambda: run_new(),
    ),
    "etiquetar_not_after_template": (
        lambda: run_new(welcome(pasos=[{"etiquetar": "cliente"}, copy.deepcopy(CONFIRMED_STEP)])),
        lambda: run_new(),
    ),
    "motor_nativo:disparador": (
        lambda: run_new(welcome(dispara={"evento": "carrito"}, entra={"permiso": "formulario"},
                                pasos=[copy.deepcopy(CONFIRMED_STEP)])),
        lambda: run_new(),
    ),
    "motor_nativo:permiso": (
        lambda: run_new(welcome(entra={"permiso": "formulario"})),
        lambda: run_new(),
    ),
    "motor_nativo:repeticion": (
        lambda: run_new(welcome(topes={"repeticion": {"una_por": "conversacion", "otra_despues_de": "nunca"}})),
        lambda: run_new(),
    ),
    "motor_nativo:prioridad": (
        lambda: run_new(welcome(prioridad={"le_gana_a": [], "no_arranca_mientras": "a_decidir"})),
        lambda: run_new(),
    ),
    "motor_nativo:forma": (
        lambda: run_new(welcome(pasos=[{**CONFIRMED_STEP, "variables": ["producto", "nombre"]}])),
        lambda: run_new(),
    ),
    "engine_not_supported:etiquetar": (
        lambda: _legacy_purchase([copy.deepcopy(HELLO_WORLD_STEP), {"etiquetar": "cliente"}]),
        lambda: _legacy_purchase([copy.deepcopy(HELLO_WORLD_STEP)]),
    ),
    "empty_branch_with_le_gana_a": (
        lambda: run(with_(flow_def("carrito"), lambda d: d.update(pasos=[{
            "condicion": {"dato": "ventana", "es": ["abierta"]}, "entonces": [TEMPLATE_STEP], "si_no": []}]))),
        lambda: run(with_(flow_def("carrito"), lambda d: (d.update(pasos=[{
            "condicion": {"dato": "ventana", "es": ["abierta"]}, "entonces": [TEMPLATE_STEP], "si_no": []}]),
            d["prioridad"].update(le_gana_a=[])))),
    ),
}


@pytest.mark.parametrize("code", sorted(CASES))
def test_each_code_has_a_negative_case_that_produces_it(code: str) -> None:
    negative, _ = CASES[code]
    produced = [finding for finding in negative() if finding.code == code]
    assert produced, f"{code} no aparecio"
    for finding in produced:
        assert (finding.rule, finding.kind) in fv.CODE_RULES[code], (code, finding.rule, finding.kind)


@pytest.mark.parametrize("code", sorted(CASES))
def test_each_code_has_a_positive_neighbour_that_does_not_produce_it(code: str) -> None:
    _, positive = CASES[code]
    assert code not in found(positive())


def test_every_code_of_the_closed_catalog_has_its_cases() -> None:
    assert set(CASES) == fv.BLOCK_CODES | fv.WARNING_CODES
    assert not fv.BLOCK_CODES & fv.WARNING_CODES
    assert len(fv.WARNING_CODES) == 7


def test_every_code_of_the_closed_catalog_is_produced_by_some_case() -> None:
    # Una regla apagada deja su codigo en el catalogo y sin nadie que lo produzca.
    produced: set[str] = set()
    for negative, _ in CASES.values():
        produced |= found(negative())
    assert fv.BLOCK_CODES | fv.WARNING_CODES <= produced, sorted(
        (fv.BLOCK_CODES | fv.WARNING_CODES) - produced)


def test_the_texts_of_every_code_are_clean_single_lines() -> None:
    for spec in fv.CATALOG:
        assert fd.is_clean_text(spec.reason, 300), spec.code
        for text in (spec.limit, spec.alternative):
            assert text is None or fd.is_clean_text(text, 300), spec.code
    for spec in fv.CATALOG:
        if spec.kind in ("proteccion", "todavia_no"):
            # El agente arma su frase con el limite, la razon y la alternativa.
            assert spec.limit and spec.alternative, spec.code


def test_the_catalog_has_the_rule_and_kind_of_the_design() -> None:
    rules = {code: {rule for rule, _ in pairs} for code, pairs in fv.CODE_RULES.items()}
    expected_rules = {
        1: {"unknown_key", "invalid_value", "duplicate_key", "version_set_by_cycle", "default_pool_invalid"},
        2: {"trigger_not_exactly_one", "silence_window_invalid"},
        3: {"permission_trigger_mismatch", "contesto_a_requires_conversation"},
        4: {"purchase_exit_missing", "purchase_exit_with_purchase_trigger"},
        5: {"ficha_vencida_requires_ficha_trigger", "medio_de_pago_requires_ficha_trigger",
            "ficha_link_requires_ficha_trigger", "coupon_requires_checkout_link",
            "variable_needs_personal_coupon", "variable_needs_event_trigger",
            "etiquetar_requires_purchase_trigger"},
        6: {"event_requires_vence", "silence_forbids_vence"},
        7: {"conversar_outside_open_window_branch", "terminal_step_not_last", "nested_condition",
            "etiquetar_not_after_template"},
        8: {"hour_invalid", "window_not_increasing", "zone_invalid", "zone_changed", "pool_zone_mismatch"},
        9: {"cap_invalid", "daily_cap_exceeds_total"},
        10: {"unknown_flow_reference", "mutual_le_gana_a", "product_not_declared", "product_required",
             "duplicate_flow_key"},
        11: {"unresolved_value"},
        12: {"vence_not_after_waits"},
        13: {"template_not_in_catalog", "template_not_approved", "template_language_mismatch",
             "template_variable_count_mismatch", "template_button_mismatch", "template_catalog_unavailable"},
        14: {"engine_not_supported:more_sends_than_steps", "engine_not_supported:coupon_not_allowed",
             "engine_not_supported:coupon_missing", "engine_not_supported:fixed_coupon",
             "engine_not_supported:variables_differ", "engine_not_supported:espera_entre_envios",
             "engine_not_supported:espera_despues_del_envio", "engine_not_supported:etiquetar",
             "motor_nativo:disparador", "motor_nativo:permiso", "motor_nativo:repeticion",
             "motor_nativo:prioridad", "motor_nativo:forma"},
        15: {"coupon_change_not_supported", "coupon_personal_limit"},
        17: {"purchase_permission_requires_single_utility_send"},
    }
    for rule, codes in expected_rules.items():
        for code in codes:
            assert rule in rules[code], code
    assert rules["required_key_missing"] == {1, None}
    protections = {code for code, pairs in fv.CODE_RULES.items() if any(kind == "proteccion" for _, kind in pairs)}
    assert protections == {
        "purchase_exit_missing", "zone_changed", "daily_cap_exceeds_total",
        "template_not_in_catalog", "template_not_approved", "template_language_mismatch",
        "template_variable_count_mismatch", "template_button_mismatch", "template_catalog_unavailable",
        "purchase_permission_requires_single_utility_send", "coupon_personal_limit",
        "variable_needs_personal_coupon", "variable_needs_event_trigger",
        "etiquetar_requires_purchase_trigger",
    }
    assert {code for code, pairs in fv.CODE_RULES.items() if any(k == "todavia_no" for _, k in pairs)} == {
        "coupon_change_not_supported", "engine_not_supported:more_sends_than_steps",
        "engine_not_supported:coupon_not_allowed", "engine_not_supported:coupon_missing",
        "engine_not_supported:fixed_coupon", "engine_not_supported:variables_differ",
        "engine_not_supported:espera_entre_envios", "engine_not_supported:espera_despues_del_envio",
        "engine_not_supported:etiquetar", "motor_nativo:disparador", "motor_nativo:permiso",
        "motor_nativo:repeticion", "motor_nativo:prioridad", "motor_nativo:forma"}
    cycle = {code for code, pairs in fv.CODE_RULES.items() if (None, "ciclo") in pairs}
    assert cycle == {
        "flow_creation_not_enabled", "change_outside_1b_scope", "trigger_change_is_another_flow",
        "required_key_missing", "flow_change_stale_base", "rollback_target_not_found",
        "rollback_target_is_active", "state_transition_invalid", "flow_has_no_version",
        "en_curso_not_supported", "identifier_not_allowed", "client_purchase_ok_missing",
        "flow_already_registered", "new_flow_must_start_in_cohort", "prender_a_cohorte_needs_paused_native",
    }
    assert all(pairs == {(None, "advertencia")} for code, pairs in fv.CODE_RULES.items()
               if code in fv.WARNING_CODES)


# ---------------------------------------------------------------------------
# Los ocho bloques del esquema (§5.8 del diseno): uno por uno, como operador,
# con los siete flujos registrados y sin version.
# ---------------------------------------------------------------------------

def _blocks_result(block_id: str, phase: str) -> ValidationResult:
    definition = block(block_id)
    return check([version_item(definition, definition["flujo"])], phase=phase)


@pytest.mark.parametrize("block_id", sorted(BLOCKS))
def test_the_eight_schema_blocks_save_as_drafts_without_blocks(block_id: str) -> None:
    result = _blocks_result(block_id, "draft")
    assert result.blocks == () and result.warnings == ()


@pytest.mark.parametrize(
    ("block_id", "warnings"),
    [
        ("1", []),
        ("2", []),
        ("3", ["wait_without_contesta"]),
        ("4", []),
        ("5a", ["always_with_marketing", "silence_without_purchase_exit"]),
        # La 5b pasa en esta pieza: la bloquea la regla 14 (conversar), que llega con
        # el motor. La pieza 2 cambia esta expectativa.
        ("5b", ["always_with_marketing", "silence_without_purchase_exit"]),
    ],
)
def test_the_schema_blocks_that_publish_do_it_with_the_contract_warnings(block_id: str, warnings: list[str]) -> None:
    result = _blocks_result(block_id, "publish")
    assert result.blocks == ()
    assert [finding.code for finding in result.warnings] == warnings


def test_the_ficha_does_not_publish_only_because_of_rule_11() -> None:
    # La 13 no mira a_crear; la 14 (la ficha impresa, medio_de_pago, link: ficha) llega con el motor.
    result = _blocks_result("6", "publish")
    assert [(f.rule, f.code, f.path) for f in result.blocks] == [
        (11, "unresolved_value", "/0/definicion/pasos/0/esperar"),
        (11, "unresolved_value", "/0/definicion/pasos/1/entonces/0/plantilla"),
        (11, "unresolved_value", "/0/definicion/pasos/1/si_no/0/plantilla"),
        (11, "unresolved_value", "/0/definicion/termina/vence"),
    ]
    assert result.warnings == ()


def test_the_activation_does_not_publish_only_because_of_rule_11() -> None:
    # La 14 (la compra como disparador, nombre con la compra) llega con el motor.
    result = _blocks_result("7", "publish")
    assert [(f.rule, f.code, f.path) for f in result.blocks] == [
        (11, "unresolved_value", "/0/definicion/pasos/0/plantilla"),
        (11, "unresolved_value", "/0/definicion/termina/vence"),
    ]
    assert result.warnings == ()


def test_the_four_flows_of_today_and_the_reactivation_publish_together_with_three_warnings() -> None:
    # La prueba 2 del dato real (§7.5) usa la salida del traductor; esta usa los
    # bloques del esquema, que tienen que ser iguales a esa salida.
    keys = ("carrito", "pago_fallido", "precheckout", "descuento", "reactivacion")
    items = [version_item(flow_def(key), key) for key in keys]
    result = check(items, in_world=world(keys=keys))
    assert result.blocks == ()
    assert [(f.code, f.item_index) for f in result.warnings] == [
        ("wait_without_contesta", 2),
        ("always_with_marketing", 4),
        ("silence_without_purchase_exit", 4),
    ]


# ---------------------------------------------------------------------------
# Los dos actores.
# ---------------------------------------------------------------------------

def test_the_same_pool_change_blocks_for_the_automation_expert_and_passes_for_the_operator() -> None:
    draft = draft_of("carrito", lambda d: d["topes"].update(bolsa="att1-carrito"))
    in_world = world(live("carrito"))
    agent = check([version_item(draft)], actor="automation_expert", in_world=in_world)
    assert [(f.code, f.path) for f in agent.blocks] == [("change_outside_1b_scope", "/0/definicion/topes/bolsa")]
    assert check([version_item(draft)], actor="operador", in_world=in_world).blocks == ()


def test_a_first_version_blocks_for_the_automation_expert_and_passes_for_the_operator() -> None:
    agent = check([version_item(flow_def("carrito"))], actor="automation_expert", phase="draft")
    assert [(f.code, f.path, f.rule) for f in agent.blocks] == [("flow_creation_not_enabled", "/0", None)]
    assert check([version_item(flow_def("carrito"))], actor="operador", phase="draft").blocks == ()


@pytest.mark.parametrize(
    ("change", "paths"),
    [
        (lambda d: d["topes"].update(bolsa="att1-carrito"), ["/topes/bolsa"]),
        (lambda d: d["topes"]["repeticion"].update(otra_despues_de="24h"), ["/topes/repeticion"]),
        (lambda d: d["termina"].update(si=["compra", "contesta"]), ["/termina/si"]),
        (lambda d: d["prioridad"].update(le_gana_a=[]), ["/prioridad/le_gana_a"]),
        (lambda d: d["prioridad"].update(no_arranca_mientras=[]), ["/prioridad/no_arranca_mientras"]),
        (lambda d: d.update(nombre="Carrito"), ["/nombre"]),
        (lambda d: d["entra"].update(contesto_a=["precheckout"]), ["/entra/contesto_a"]),
        (lambda d: d["pasos"].append(dict(TEMPLATE_STEP)), ["/pasos"]),
        (lambda d: d["pasos"][0].update(boton={"link": "checkout"}), ["/pasos"]),
        (lambda d: (d["topes"].update(bolsa="att1-carrito"), d.update(nombre="Carrito")),
         ["/nombre", "/topes/bolsa"]),
    ],
)
def test_the_automation_expert_cannot_change_what_decision_4_leaves_out(
    change: Callable[[dict[str, Any]], None], paths: list[str],
) -> None:
    result = check([version_item(draft_of("carrito", change))], actor="automation_expert",
                   in_world=world(live("carrito")), phase="draft")
    assert sorted(f.definition_path for f in result.blocks if f.code == "change_outside_1b_scope") == sorted(paths)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["pasos"].insert(0, {"esperar": "2h"}),
        lambda d: d["pasos"][0].update(plantilla="att1_compra_fallida_01"),
        lambda d: d["pasos"][0].update(idioma="es_MX", variables=["nombre", "producto"]),
        lambda d: d["topes"].update(por_dia=90, total=1200),
        lambda d: d["termina"].update(vence="2d"),
        lambda d: d["horario"].update(desde="08:00", hasta="22:00"),
        lambda d: d.update(horario="siempre"),
    ],
)
def test_the_automation_expert_changes_waits_template_caps_deadline_and_schedule(
    change: Callable[[dict[str, Any]], None],
) -> None:
    result = check([version_item(draft_of("carrito", change))], actor="automation_expert",
                   in_world=world(live("carrito")))
    assert "change_outside_1b_scope" not in found(result)
    assert result.blocks == ()


def test_a_wait_inside_a_branch_is_also_allowed_to_the_automation_expert() -> None:
    def with_branch(definition: dict[str, Any]) -> None:
        definition["pasos"] = [{"condicion": {"dato": "ventana", "es": ["abierta"]},
                                "entonces": [dict(TEMPLATE_STEP)], "si_no": [dict(TEMPLATE_STEP)]}]

    base = with_(flow_def("carrito"), with_branch)
    in_world = world(live("carrito", base))
    draft = fd.as_draft(published(base))
    draft["pasos"][0]["si_no"].insert(0, {"esperar": "3h"})
    assert "change_outside_1b_scope" not in found(
        check([version_item(draft)], actor="automation_expert", in_world=in_world, phase="draft"))


# La identidad del disparador (decision de Andy del 16/10, con el contrato): la
# clase y el evento, o la clase y el lo_ultimo del silencio (flow-definition-v1.md:46,
# :58-61 y :844-856). desde y hasta son el plazo del silencio (:855 y :161).

@pytest.mark.parametrize("actor", fv.ACTORS)
@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["dispara"]["silencio"].update(hasta="96h"),
        lambda d: d["dispara"]["silencio"].update(desde="12h"),
        lambda d: d["dispara"]["silencio"].update(desde="12h", hasta="48h"),
    ],
    ids=["hasta", "desde", "los-dos"],
)
def test_the_silence_window_is_a_new_version_of_the_same_flow(
    change: Callable[[dict[str, Any]], None], actor: str,
) -> None:
    # El contrato espera que alguien suba el hasta del mismo flujo (:76), y la
    # decision 4 le deja al agente las esperas y el plazo.
    result = check([version_item(draft_of("descuento", change), "descuento")], actor=actor,
                   in_world=world(live("descuento")))
    assert result.blocks == ()


def test_who_enters_a_silence_is_outside_the_1b_scope_for_the_agent() -> None:
    draft = draft_of("descuento", lambda d: d["dispara"]["silencio"].update(conversaciones=["abierta"]))
    in_world = world(live("descuento"))
    agent = check([version_item(draft, "descuento")], actor="automation_expert", in_world=in_world)
    assert [(f.code, f.definition_path) for f in agent.blocks] == [
        ("change_outside_1b_scope", "/dispara/silencio/conversaciones")]
    # Para el operador es una version nueva del mismo flujo.
    assert check([version_item(draft, "descuento")], actor="operador", in_world=in_world).blocks == ()


_ANOTHER_FLOW = [("trigger_change_is_another_flow", "/dispara")]
# Del evento a un silencio cambia tambien quien entra (conversaciones), y el
# permiso y el plazo dejan de corresponder.
_TO_A_SILENCE = [("permission_trigger_mismatch", "/entra/permiso"), ("silence_forbids_vence", "/termina/vence")]


@pytest.mark.parametrize("actor", fv.ACTORS)
@pytest.mark.parametrize(
    ("key", "change", "for_the_operator", "for_the_agent"),
    [
        ("descuento", lambda d: d["dispara"]["silencio"].update(lo_ultimo="persona"), _ANOTHER_FLOW, _ANOTHER_FLOW),
        ("carrito", lambda d: d["dispara"].update(evento="pago_fallido"), _ANOTHER_FLOW, _ANOTHER_FLOW),
        ("carrito", lambda d: d.update(dispara={"silencio": {
            "lo_ultimo": "agente", "desde": "24h", "hasta": "72h", "conversaciones": ["abierta"]}}),
         _ANOTHER_FLOW + _TO_A_SILENCE,
         _ANOTHER_FLOW + [("change_outside_1b_scope", "/dispara/silencio")] + _TO_A_SILENCE),
    ],
    ids=["otro-lo_ultimo", "otro-evento", "evento-a-silencio"],
)
def test_another_event_or_another_last_speaker_is_another_flow(
    key: str, change: Callable[[dict[str, Any]], None], for_the_operator: list[tuple[str, str]],
    for_the_agent: list[tuple[str, str]], actor: str,
) -> None:
    result = check([version_item(draft_of(key, change), key)], actor=actor, in_world=world(live(key)),
                   phase="draft")
    # La lista entera, no la pertenencia a un conjunto: un hallazgo de mas o
    # repetido (el evento que el alcance de la 1b no saca, V16) falla.
    expected = for_the_agent if actor == "automation_expert" else for_the_operator
    assert [(f.code, f.definition_path) for f in result.blocks] == expected


def test_a_rollback_is_not_looked_at_by_the_scope_of_decision_4() -> None:
    old = flow_def("carrito")
    old["topes"]["bolsa"] = "att1-carrito-v1"
    two = flow("carrito", versions={1: published(old), 2: published(flow_def("carrito"), 2)})
    result = check([state_item("volver", version=1)], actor="automation_expert", in_world=world(two))
    assert result.blocks == ()


# ---------------------------------------------------------------------------
# El ciclo: volver, prender, apagar, la base y lo que esta en curso.
# ---------------------------------------------------------------------------

def test_a_rollback_validates_its_target_with_the_publish_rules() -> None:
    two = flow("carrito", versions={1: published(flow_def("carrito")), 2: published(
        with_(flow_def("carrito"), lambda d: d["pasos"][0].update(plantilla="att1_compra_fallida_01")), 2)})
    paused = catalog_with(lambda p: next(t for t in p["message_templates"]
                                         if t["name"] == "att1_carrito_abandonado_01").update(status="PAUSED"))
    result = check([state_item("volver", version=1)], in_world=world(two), catalog=paused)
    assert [(f.code, f.path) for f in result.blocks] == [("template_not_approved", "/0/version")]
    assert check([state_item("volver", version=1)], in_world=world(two), catalog=paused, phase="draft").blocks == ()


def test_switching_on_revalidates_the_active_definition_with_rules_13_and_17() -> None:
    off = live("carrito", state="apagado")
    paused = catalog_with(lambda p: next(t for t in p["message_templates"]
                                         if t["name"] == "att1_carrito_abandonado_01").update(status="PAUSED"))
    result = check([state_item("prender")], in_world=world(off), catalog=paused)
    assert [(f.code, f.path) for f in result.blocks] == [("template_not_approved", "/0")]
    assert check([state_item("prender")], in_world=world(off), catalog=paused, phase="draft").blocks == ()
    assert check([state_item("prender")], in_world=world(off)).blocks == ()
    # La 17 depende de la categoria viva: una plantilla que paso a MARKETING frena el prender.
    purchase = live("activacion", purchase_flow(), state="apagado")
    marketing = hello_world_catalog(lambda template: template.update(category="MARKETING"))
    blocked = check([state_item("prender", "activacion")], in_world=world(purchase), catalog=marketing)
    assert [f.code for f in blocked.blocks] == ["purchase_permission_requires_single_utility_send"]
    assert check([state_item("prender", "activacion")], in_world=world(purchase),
                 catalog=hello_world_catalog()).blocks == ()


def test_switching_off_only_looks_at_the_transition() -> None:
    assert check([state_item("apagar")], in_world=world(live("carrito")), catalog=None).blocks == ()
    assert check([state_item("apagar")], in_world=world(live("carrito", state="pausado"))).blocks == ()
    assert [f.code for f in check([state_item("apagar")]).blocks] == ["state_transition_invalid"]


def test_switching_on_a_paused_flow_is_a_valid_transition() -> None:
    assert check([state_item("prender")], in_world=world(live("carrito", state="pausado"))).blocks == ()


def test_publish_the_first_version_and_switch_it_on_is_one_proposal() -> None:
    result = check([version_item(flow_def("carrito")), state_item("prender")])
    assert result.blocks == ()


def test_the_draft_base_has_to_be_the_active_version_of_each_item() -> None:
    in_world = world(live("carrito"))
    items = [version_item(draft_of("carrito"))]
    good = {IDS["carrito"]: {"active_version": 1}}
    assert check(items, in_world=in_world, draft_base=good).blocks == ()
    for base in ({IDS["carrito"]: {"active_version": 2}}, {}, {IDS["carrito"]: {}},
                 {**good, IDS["pago_fallido"]: {"active_version": None}}, []):
        assert "flow_change_stale_base" in found(check(items, in_world=in_world, draft_base=base)), base
    assert in_world.draft_base_for(items) == good


def test_only_seguir_is_accepted_for_what_is_in_flight() -> None:
    for policy in ("cancelar", "migrar"):
        result = check([version_item(flow_def("carrito"), en_curso=policy)])
        assert [(f.code, f.path) for f in result.blocks] == [("en_curso_not_supported", "/0/en_curso")]
    two = flow("carrito", versions={1: published(flow_def("carrito")), 2: published(flow_def("carrito"), 2)})
    rollback = ProposalItem(producto="att1", flujo="carrito", flow_id=IDS["carrito"], tipo="volver",
                            version=1, en_curso="cancelar")
    assert [f.code for f in check([rollback], in_world=world(two)).blocks] == ["en_curso_not_supported"]


def test_the_client_ok_is_needed_only_when_the_permission_becomes_purchase() -> None:
    utility = hello_world_catalog()
    already = live("activacion", purchase_flow())
    draft = fd.as_draft(published(purchase_flow()))
    assert check([version_item(draft, "activacion")], in_world=world(already), catalog=utility).blocks == ()
    assert check([version_item(purchase_flow(), "activacion")], catalog=utility, phase="draft").blocks == ()
    result = check([version_item(purchase_flow(), "activacion")], catalog=utility)
    assert [(f.code, f.path) for f in result.blocks] == [("client_purchase_ok_missing", "/0/ok_del_cliente")]


def test_a_malformed_client_ok_is_rule_1() -> None:
    result = check([version_item(purchase_flow(), "activacion", ok={"quien": "Juan\u202e", "medio": "x"})],
                   catalog=hello_world_catalog(), phase="draft")
    assert {(f.code, f.path) for f in result.blocks} == {
        ("invalid_value", "/0/ok_del_cliente/quien"), ("required_key_missing", "/0/ok_del_cliente/fecha")}


# ---------------------------------------------------------------------------
# Precisiones de las reglas.
# ---------------------------------------------------------------------------

def test_rule_17_counts_sends_per_path() -> None:
    utility = hello_world_catalog()
    one_per_branch = purchase_flow(pasos=[{
        "condicion": {"dato": "ventana", "es": ["abierta"]},
        "entonces": [copy.deepcopy(HELLO_WORLD_STEP)],
        "si_no": [copy.deepcopy(HELLO_WORLD_STEP)]}])
    assert check([version_item(one_per_branch, "activacion", ok=CLIENT_OK)], catalog=utility).blocks == ()
    two_in_a_row = purchase_flow(pasos=[copy.deepcopy(HELLO_WORLD_STEP), {"esperar": "1h"},
                                        copy.deepcopy(HELLO_WORLD_STEP)])
    result = check([version_item(two_in_a_row, "activacion", ok=CLIENT_OK)], catalog=utility)
    assert [(f.code, f.path) for f in result.blocks] == [
        ("purchase_permission_requires_single_utility_send", "/0/definicion/pasos")]
    above_and_below = purchase_flow(pasos=[copy.deepcopy(HELLO_WORLD_STEP), {
        "condicion": {"dato": "ventana", "es": ["abierta"]},
        "entonces": [], "si_no": [copy.deepcopy(HELLO_WORLD_STEP)]}])
    assert "purchase_permission_requires_single_utility_send" in found(
        check([version_item(above_and_below, "activacion", ok=CLIENT_OK)], catalog=utility))


def test_rule_12_sums_the_waits_of_the_longest_path() -> None:
    def branchy(deadline: str) -> dict[str, Any]:
        return with_(flow_def("carrito"), lambda d: (d.update(pasos=[
            {"esperar": "6h"},
            {"condicion": {"dato": "ventana", "es": ["abierta"]},
             "entonces": [{"esperar": "12h"}, dict(TEMPLATE_STEP)],
             "si_no": [{"esperar": "1h"}, dict(TEMPLATE_STEP)]}]), d["termina"].update(vence=deadline)))

    assert "vence_not_after_waits" in found(run(branchy("18h")))
    assert "vence_not_after_waits" not in found(run(branchy("19h")))
    assert "vence_not_after_waits" not in found(run(with_(branchy("1d"), lambda d: d["pasos"][0].update(
        esperar="a_medir")), phase="publish")) - {"unresolved_value"}


def test_the_zone_is_the_first_one_of_the_flow_even_after_always() -> None:
    always = published(with_(flow_def("carrito"), lambda d: d.update(horario="siempre")), 2)
    history = flow("carrito", versions={1: published(flow_def("carrito")), 2: always})
    bogota = draft_of("carrito", lambda d: d.update(horario={**CDMX, "zona": "America/Bogota"}))
    assert "zone_changed" in found(run(bogota, in_world=world(history)))
    assert "zone_changed" not in found(run(draft_of("carrito"), in_world=world(history)))
    # Con siempre no hay zona que mirar.
    assert not found(run(fd.as_draft(always), in_world=world(history))) & {"zone_changed", "zone_invalid"}


def test_always_counts_as_no_zone_in_a_shared_pool() -> None:
    always = with_(flow_def("carrito"), lambda d: d.update(horario="siempre"))
    assert "pool_zone_mismatch" in found(run(always, in_world=world(live("pago_fallido"))))
    both_always = live("pago_fallido", with_(flow_def("pago_fallido"), lambda d: d.update(horario="siempre")))
    assert "pool_zone_mismatch" not in found(run(always, in_world=world(both_always)))


def test_flows_of_another_product_are_named_with_their_product() -> None:
    other = flow("carrito", product="att2", flow_id="0000000b-0000-4000-8000-00000000000b")
    both = FlowWorld([*world().flows, other])
    two = InstanceView("lancemos", "att1", frozenset({"att1", "att2"}), "America/Mexico_City")
    definition = with_(flow_def("descuento"), lambda d: (
        d.update(producto="att1"), d["entra"].update(contesto_a=["carrito", "att2/carrito", "att2/precheckout"])))
    result = check([version_item(definition, "descuento")], in_world=both, instance=two)
    assert [(f.code, f.path) for f in result.blocks] == [
        ("unknown_flow_reference", "/0/definicion/entra/contesto_a/2")]


def test_a_mutual_win_is_seen_with_a_qualified_name_and_inside_the_same_proposal() -> None:
    qualified = with_(flow_def("precheckout"), lambda d: d.update(prioridad={"le_gana_a": ["att1/carrito"]}))
    assert "mutual_le_gana_a" in found(run(flow_def("carrito"), in_world=world(live("precheckout", qualified))))
    items = [version_item(flow_def("carrito")), version_item(qualified, "precheckout")]
    result = check(items)
    assert {(f.code, f.item_index) for f in result.blocks} == {("mutual_le_gana_a", 0), ("mutual_le_gana_a", 1)}


def test_a_definition_of_another_flow_is_invalid_value() -> None:
    renamed = with_(flow_def("carrito"), lambda d: d.update(flujo="carrito_nuevo"))
    assert {(f.code, f.path) for f in check([version_item(renamed)]).blocks} == {
        ("invalid_value", "/0/definicion/flujo")}
    other_product = with_(flow_def("carrito"), lambda d: d.update(producto="att2"))
    two = InstanceView("lancemos", "att1", frozenset({"att1", "att2"}), "America/Mexico_City")
    assert ("invalid_value", "/0/definicion/producto") in {
        (f.code, f.path) for f in check([version_item(other_product)], instance=two).blocks}


def test_the_silence_window_up_to_thirty_days() -> None:
    late = with_(flow_def("descuento"), lambda d: d["dispara"]["silencio"].update(hasta="31d"))
    assert [(f.code, f.path) for f in check([version_item(late, "descuento")]).blocks] == [
        ("silence_window_invalid", "/0/definicion/dispara/silencio/hasta")]
    month = with_(flow_def("descuento"), lambda d: d["dispara"]["silencio"].update(hasta="30d"))
    assert check([version_item(month, "descuento")]).blocks == ()


def test_the_checkout_button_is_the_one_of_the_coupon_followup() -> None:
    from bridge.followup_discount import FOLLOWUP_BUTTON_URL

    assert fv.CHECKOUT_BUTTON_URL == FOLLOWUP_BUTTON_URL


def test_a_template_with_a_media_header_needs_parameters_the_flow_does_not_send() -> None:
    with_header = catalog_with(lambda p: next(t for t in p["message_templates"]
                                              if t["name"] == "att1_carrito_abandonado_01")["components"].insert(
        0, {"type": "HEADER", "format": "IMAGE"}))
    assert [f.code for f in check([version_item(flow_def("carrito"))], catalog=with_header).blocks] == [
        "template_variable_count_mismatch"]


def test_a_named_placeholder_is_a_variable_count_mismatch() -> None:
    named = catalog_with(lambda p: next(t for t in p["message_templates"]
                                        if t["name"] == "att1_reactivacion_01")["components"][0].update(
        text="Hola, {{1}}. {{first_name}}"))
    assert "template_variable_count_mismatch" in found(run(block("5a"), "reactivacion", catalog=named))


# ---------------------------------------------------------------------------
# Un negativo por rama, no uno por codigo: un codigo con varios caminos tiene
# una prueba por camino, con su ruta. Cada una cae con su mutacion (V06, V07,
# V09 a V15 y V26 a V29 de la revision de las pruebas del PR #243).
# ---------------------------------------------------------------------------

def coupon_button_url(url: str) -> TemplateCatalog:
    """El catalogo con el boton de URL de la plantilla del cupon apuntando a ``url``."""
    def change(payload: dict[str, Any]) -> None:
        for component in template_of(payload, "att1_seguimiento_descuento_01")["components"]:
            if component.get("type") == "BUTTONS":
                for button in component["buttons"]:
                    button["url"] = url
    return catalog_with(change)


def test_rule_13_two_entries_with_the_same_name_and_language_are_not_in_the_catalog() -> None:
    twice = catalog_with(lambda p: p["message_templates"].append(
        copy.deepcopy(template_of(p, "att1_carrito_abandonado_01"))))
    assert blocks_at(check([version_item(flow_def("carrito"))], catalog=twice)) == [
        ("template_not_in_catalog", "/0/definicion/pasos/0/plantilla")]


@pytest.mark.parametrize(
    "component",
    [{"type": "HEADER", "format": "TEXT", "text": "Hola {{1}}"}, {"type": "FOOTER", "text": "Hasta {{1}}"}],
    ids=["encabezado", "pie"],
)
def test_rule_13_a_text_header_or_footer_with_a_placeholder_is_a_variable_count_mismatch(
    component: dict[str, Any],
) -> None:
    with_placeholder = catalog_with(lambda p: template_of(p, "att1_carrito_abandonado_01")["components"].insert(
        0, dict(component)))
    assert blocks_at(check([version_item(flow_def("carrito"))], catalog=with_placeholder)) == [
        ("template_variable_count_mismatch", "/0/definicion/pasos/0/plantilla")]
    # Sin marcadores pasa, como el HEADER y el FOOTER de hello_world.
    plain = catalog_with(lambda p: template_of(p, "att1_carrito_abandonado_01")["components"].insert(
        0, {**component, "text": "Hola"}))
    assert check([version_item(flow_def("carrito"))], catalog=plain).blocks == ()


def test_rule_13_a_component_the_flow_cannot_fill_is_a_variable_count_mismatch() -> None:
    carousel = catalog_with(lambda p: template_of(p, "att1_carrito_abandonado_01")["components"].append(
        {"type": "CAROUSEL"}))
    assert blocks_at(check([version_item(flow_def("carrito"))], catalog=carousel)) == [
        ("template_variable_count_mismatch", "/0/definicion/pasos/0/plantilla")]


def test_rule_13_the_checkout_button_is_the_hotmart_checkout() -> None:
    elsewhere = coupon_button_url("https://otro-sitio.example/{{1}}")
    assert blocks_at(check([version_item(flow_def("descuento"), "descuento")], catalog=elsewhere)) == [
        ("template_button_mismatch", "/0/definicion/pasos/0/boton")]


def test_rule_13_a_step_without_a_button_cannot_send_a_template_with_a_url_button() -> None:
    without_button = with_(flow_def("descuento"), lambda d: d["pasos"][0].pop("boton"))
    assert blocks_at(check([version_item(without_button, "descuento")])) == [
        ("template_button_mismatch", "/0/definicion/pasos/0/plantilla")]


def test_rule_13_the_ficha_button_is_a_dynamic_url() -> None:
    ficha = with_(flow_def("ficha"), lambda d: (
        d.update(pasos=[{"plantilla": "att1_seguimiento_descuento_01", "idioma": "es_MX",
                         "variables": ["nombre", "producto"], "boton": {"link": "ficha"}}]),
        d["termina"].update(vence="1d")))
    static = check([version_item(ficha, "ficha")], catalog=coupon_button_url("https://pay.hotmart.com/checkout"))
    assert blocks_at(static) == [("template_button_mismatch", "/0/definicion/pasos/0/boton")]
    dynamic = check([version_item(ficha, "ficha")], catalog=coupon_button_url("https://pay.hotmart.com/{{1}}"))
    assert dynamic.blocks == ()


def coupon_buttons(order: list[str]) -> TemplateCatalog:
    """El catalogo con los botones de la plantilla del cupon en ``order``.

    ``url`` es el boton al checkout y ``rapida`` una respuesta rapida, como
    las plantillas A1 del carrito aprobadas el 18/10 (fixture
    ``chatwoot_message_templates_inbox_11_carrito_20261018.json``).
    """
    def change(payload: dict[str, Any]) -> None:
        for component in template_of(payload, "att1_seguimiento_descuento_01")["components"]:
            if component.get("type") == "BUTTONS":
                [url] = [b for b in component["buttons"] if b.get("type") == "URL"]
                component["buttons"] = [
                    copy.deepcopy(url) if kind == "url" else {"type": "QUICK_REPLY", "text": "No más mensajes"}
                    for kind in order
                ]
    return catalog_with(change)


def test_rule_13_the_checkout_button_may_be_followed_by_quick_replies() -> None:
    descuento = [version_item(flow_def("descuento"), "descuento")]
    assert check(descuento, catalog=coupon_buttons(["url", "rapida"])).blocks == ()
    assert check(descuento, catalog=coupon_buttons(["url", "rapida", "rapida"])).blocks == ()


@pytest.mark.parametrize("order", [["rapida", "url"], ["url", "url"], ["rapida"]],
                         ids=["la-rapida-primero", "dos-links", "sin-link"])
def test_rule_13_the_checkout_button_opens_the_group_and_is_the_only_link(order: list[str]) -> None:
    descuento = [version_item(flow_def("descuento"), "descuento")]
    assert blocks_at(check(descuento, catalog=coupon_buttons(order))) == [
        ("template_button_mismatch", "/0/definicion/pasos/0/boton")]


def test_rule_3_the_purchase_permission_goes_only_with_the_purchase_trigger() -> None:
    on_the_cart = with_(flow_def("carrito"), lambda d: d["entra"].update(permiso="compra"))
    assert blocks_at(check([version_item(on_the_cart)], phase="draft")) == [
        ("permission_trigger_mismatch", "/0/definicion/entra/permiso")]


def test_rule_7_a_condition_with_an_open_and_a_closed_window_is_not_the_open_window() -> None:
    mixed = with_(block("5b"), lambda d: d["pasos"][0]["condicion"].update(es=["abierta", "cerrada"]))
    assert blocks_at(check([version_item(mixed, "reactivacion")], phase="draft")) == [
        ("conversar_outside_open_window_branch", "/0/definicion/pasos/0/entonces/0/conversar")]


def test_rule_17_a_conversar_with_the_purchase_permission_blocks() -> None:
    talk = purchase_flow(pasos=[{"condicion": {"dato": "ventana", "es": ["abierta"]},
                                 "entonces": [{"conversar": "Confirmar la compra."}], "si_no": []}])
    result = check([version_item(talk, "activacion", ok=CLIENT_OK)], catalog=hello_world_catalog())
    assert blocks_at(result) == [
        ("purchase_permission_requires_single_utility_send", "/0/definicion/pasos/0/entonces/0/conversar")]


def test_rule_2_a_silence_whose_from_equals_its_until_is_invalid() -> None:
    flat = with_(flow_def("descuento"), lambda d: d["dispara"]["silencio"].update(desde="24h", hasta="24h"))
    assert blocks_at(check([version_item(flat, "descuento")], phase="draft")) == [
        ("silence_window_invalid", "/0/definicion/dispara/silencio/desde")]


def test_rule_8_a_schedule_whose_from_equals_its_until_is_not_increasing() -> None:
    empty = with_(flow_def("carrito"), lambda d: d["horario"].update(desde="09:00", hasta="09:00"))
    assert blocks_at(check([version_item(empty)], phase="draft")) == [
        ("window_not_increasing", "/0/definicion/horario/hasta")]


def test_rule_9_a_daily_cap_equal_to_the_total_passes() -> None:
    # El contrato bloquea un por_dia mayor que el total (flow-definition-v1.md:987), no uno igual.
    equal = with_(flow_def("carrito"), lambda d: d["topes"].update(por_dia=1000, total=1000))
    assert check([version_item(equal)]).blocks == ()
    over = with_(flow_def("carrito"), lambda d: d["topes"].update(por_dia=1001, total=1000))
    assert blocks_at(check([version_item(over)])) == [("daily_cap_exceeds_total", "/0/definicion/topes/por_dia")]


@pytest.mark.parametrize("actor", fv.ACTORS)
def test_rule_15_taking_the_coupon_out_is_also_a_coupon_change(actor: str) -> None:
    without_coupon = draft_of("descuento", lambda d: d["pasos"][0]["boton"].pop("cupon"))
    result = check([version_item(without_coupon, "descuento")], actor=actor, in_world=world(live("descuento")))
    assert blocks_at(result) == [("coupon_change_not_supported", "/0/definicion/pasos")]


def test_a_new_form_flow_gets_the_recovery_caps_and_their_warning() -> None:
    # El formulario (intencion) es un evento de recuperacion (flow-definition-v1.md:204):
    # sin topes recibe 10 por dia y 150 en total, y con sin_tope avisa.
    minimal = {"flujo": "precheckout", "nombre": "Primer contacto", "dispara": {"evento": "intencion"},
               "pasos": [dict(TEMPLATE_STEP)], "horario": dict(CDMX)}
    result = check([version_item(minimal, "precheckout")])
    assert result.blocks == () and result.warnings == ()
    [item] = result.normalized_items
    assert item.definicion["topes"] == {"bolsa": "att1-precheckout", "por_dia": 10, "total": 150,
                                        "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": "0m"}}
    without_cap = {**minimal, "topes": {"por_dia": "sin_tope"}}
    assert warnings_at(check([version_item(without_cap, "precheckout")])) == [
        ("recovery_without_cap", "/0/definicion/topes")]


# ---------------------------------------------------------------------------
# La regla 17 y la advertencia del horario, en forma positiva: lo unico que
# pasa es una plantilla resuelta cuya categoria es exactamente UTILITY.
# ---------------------------------------------------------------------------

NOT_UTILITY = [
    pytest.param(lambda template: template.pop("category"), id="sin-categoria"),
    pytest.param(lambda template: template.update(category=None), id="nula"),
    pytest.param(lambda template: template.update(category=""), id="vacia"),
    pytest.param(lambda template: template.update(category=" "), id="en-blanco"),
    pytest.param(lambda template: template.update(category=7), id="no-es-texto"),
    pytest.param(lambda template: template.update(category="utility"), id="en-minusculas"),
    pytest.param(lambda template: template.update(category="UTILITY "), id="con-un-espacio"),
    pytest.param(lambda template: template.update(category="AUTHENTICATION"), id="autenticacion"),
    pytest.param(lambda template: template.update(category="MARKETING"), id="marketing"),
]


@pytest.mark.parametrize("change", NOT_UTILITY)
def test_rule_17_blocks_a_template_that_is_not_exactly_utility(change: Callable[[dict[str, Any]], None]) -> None:
    # «algo que no es una plantilla de utilidad» (flow-definition-v1.md:997): sin
    # categoria, no se sabe que lo sea.
    result = check([version_item(purchase_flow(), "activacion", ok=CLIENT_OK)], catalog=hello_world_catalog(change))
    assert blocks_at(result) == [
        ("purchase_permission_requires_single_utility_send", "/0/definicion/pasos/0/plantilla")]


@pytest.mark.parametrize("change", NOT_UTILITY)
def test_switching_on_blocks_a_purchase_flow_whose_template_is_not_exactly_utility(
    change: Callable[[dict[str, Any]], None],
) -> None:
    purchase = live("activacion", purchase_flow(), state="apagado")
    result = check([state_item("prender", "activacion")], in_world=world(purchase),
                   catalog=hello_world_catalog(change))
    assert blocks_at(result) == [("purchase_permission_requires_single_utility_send", "/0")]


def test_the_captured_utility_template_publishes_and_switches_on_a_purchase_flow() -> None:
    utility = hello_world_catalog()
    assert check([version_item(purchase_flow(), "activacion", ok=CLIENT_OK)], catalog=utility).blocks == ()
    purchase = live("activacion", purchase_flow(), state="apagado")
    assert check([state_item("prender", "activacion")], in_world=world(purchase), catalog=utility).blocks == ()


@pytest.mark.parametrize("change", NOT_UTILITY)
def test_always_warns_with_a_template_that_is_not_exactly_utility(change: Callable[[dict[str, Any]], None]) -> None:
    catalog = catalog_with(lambda p: change(template_of(p, "att1_reactivacion_01")))
    result = check([version_item(block("5a"), "reactivacion")], catalog=catalog)
    assert result.blocks == ()
    assert warnings_at(result) == [
        ("always_with_marketing", "/0/definicion/horario"),
        ("silence_without_purchase_exit", "/0/definicion/termina/si"),
    ]


def test_always_does_not_warn_with_a_utility_template() -> None:
    catalog = catalog_with(lambda p: template_of(p, "att1_reactivacion_01").update(category="UTILITY"))
    result = check([version_item(block("5a"), "reactivacion")], catalog=catalog)
    assert warnings_at(result) == [("silence_without_purchase_exit", "/0/definicion/termina/si")]


# ---------------------------------------------------------------------------
# Las advertencias, rama por rama, y la ruta de cada una escrita a mano (V01 a
# V05, V08, V17, V18 y V21).
# ---------------------------------------------------------------------------

WARNING_PATHS = {
    "always_with_marketing": "/0/definicion/horario",
    "empty_branch_with_le_gana_a": "/0/definicion/pasos/0",
    "recovery_without_cap": "/0/definicion/topes",
    "same_trigger_unrelated": "/0/definicion/dispara",
    "silence_window_shorter_than_closed_gap": "/0/definicion/dispara/silencio",
    "silence_without_purchase_exit": "/0/definicion/termina/si",
    "wait_without_contesta": "/0/definicion/pasos",
}


def test_the_hand_written_warning_paths_are_the_seven_warnings() -> None:
    assert set(WARNING_PATHS) == fv.WARNING_CODES


@pytest.mark.parametrize("code", sorted(WARNING_PATHS))
def test_each_warning_points_where_it_has_to(code: str) -> None:
    negative, _ = CASES[code]
    assert [finding.path for finding in negative() if finding.code == code] == [WARNING_PATHS[code]]


def test_recovery_without_cap_also_with_only_the_total_without_cap() -> None:
    total_free = with_(flow_def("carrito"), lambda d: d["topes"].update(por_dia=80, total="sin_tope"))
    result = check([version_item(total_free)])
    assert result.blocks == ()
    assert warnings_at(result) == [("recovery_without_cap", "/0/definicion/topes")]


def test_the_same_trigger_in_another_product_is_not_a_warning() -> None:
    # Con ATT1 y ATT2 en la misma instancia, el carrito de cada producto es su propio flujo.
    two = InstanceView("lancemos", "att1", frozenset({"att1", "att2"}), "America/Mexico_City")
    other_cart = flow("carrito", product="att2", flow_id="0000000b-0000-4000-8000-00000000000b",
                      versions={1: published(with_(flow_def("carrito"), lambda d: d.update(producto="att2")))})
    cart = with_(flow_def("carrito"), lambda d: d.update(producto="att1"))
    result = check([version_item(cart)], instance=two, in_world=FlowWorld([*world().flows, other_cart]))
    assert result.blocks == () and result.warnings == ()


@pytest.mark.parametrize(("until", "warns"), [("720m", False), ("719m", True)])
def test_the_closed_stretch_counts_the_closing_minute(until: str, warns: bool) -> None:
    # De 09:00 a 20:59 con el minuto de cierre: 720 minutos abiertos y 720 cerrados.
    silence = with_(flow_def("descuento"), lambda d: d["dispara"]["silencio"].update(desde="0m", hasta=until))
    result = check([version_item(silence, "descuento")])
    assert result.blocks == ()
    assert warnings_at(result) == (
        [("silence_window_shorter_than_closed_gap", "/0/definicion/dispara/silencio")] if warns else [])


def test_a_flow_that_names_itself_in_le_gana_a_is_not_a_mutual_win() -> None:
    # La regla 10 habla de dos flujos; la autorreferencia no se marca (notas de la implementacion).
    selfish = with_(flow_def("carrito"), lambda d: d["prioridad"].update(le_gana_a=["carrito"]))
    result = check([version_item(selfish)])
    assert "mutual_le_gana_a" not in found(result)
    assert result.blocks == () and result.warnings == ()


def test_a_coupon_without_a_link_is_also_a_coupon_without_the_checkout() -> None:
    no_link = with_(flow_def("descuento"), lambda d: d["pasos"][0].update(boton={"cupon": "TIROIDES10"}))
    assert blocks_at(check([version_item(no_link, "descuento")], phase="draft")) == [
        ("required_key_missing", "/0/definicion/pasos/0/boton/link"),
        ("coupon_requires_checkout_link", "/0/definicion/pasos/0/boton/cupon"),
    ]


def test_a_key_with_a_tilde_and_a_one_keeps_its_pointer_in_the_scope_of_the_agent() -> None:
    # RFC 6901: al leer un puntero, ~1 se decodifica antes que ~0. La clave a~1b va como /a~01b.
    draft = draft_of("carrito", lambda d: d["topes"].update({"a~1b": 1}))
    result = check([version_item(draft)], actor="automation_expert", in_world=world(live("carrito")), phase="draft")
    assert blocks_at(result) == [
        ("unknown_key", "/0/definicion/topes/a~01b"),
        ("change_outside_1b_scope", "/0/definicion/topes/a~01b"),
    ]


# ---------------------------------------------------------------------------
# El tamano se mide primero (C#4): un texto largo sin @ no dispara el chequeo
# cuadratico de identificadores de la 1a.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("where", ["nombre", "conversar", "derivar"])
def test_a_long_text_without_an_at_is_rejected_in_bounded_time(where: str) -> None:
    # Medido antes de la correccion: 63,7 s con 250 000 caracteres en un derivar.
    text = "a" * 250_000
    definition = block("5b")
    if where == "nombre":
        definition["nombre"] = text
    elif where == "conversar":
        definition["pasos"][0]["entonces"][0]["conversar"] = text
    else:
        definition["pasos"][0]["entonces"] = [{"derivar": text}]
    started = time.perf_counter()
    result = check([version_item(definition, "reactivacion")])
    elapsed = time.perf_counter() - started
    assert elapsed < 5, f"{elapsed:.1f} s"
    assert ("invalid_value", "/0/definicion") in blocks_at(result)
    assert "identifier_not_allowed" not in found(result)


# ---------------------------------------------------------------------------
# La forma de lo que cruza a la base (§5.2), con la logica de sus helpers.
# ---------------------------------------------------------------------------

_FREE_TEXT_BAD = re.compile(
    "[\u0001-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb\U000e0000-\U000e007f]")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _free_text_ok(text: object) -> bool:
    return isinstance(text, str) and 1 <= len(text.strip(" ")) <= 500 and not _FREE_TEXT_BAD.search(text)


def findings_are_valid(findings: object) -> bool:
    """``_flow_change_findings_are_valid``, en Python."""
    if not isinstance(findings, list):
        return False
    for finding in findings:
        if not isinstance(finding, dict):
            return False
        keys = set(finding)
        if not (keys <= {"alternative", "code", "kind", "limit", "path", "reason", "rule"}
                and keys >= {"code", "kind", "path", "rule"}):
            return False
        if not (isinstance(finding["code"], str) and re.fullmatch(r"[a-z][a-z0-9_:]{0,79}", finding["code"])):
            return False
        if finding["kind"] not in ("forma", "proteccion", "todavia_no", "ciclo", "advertencia"):
            return False
        path = finding["path"]
        # Un JSON Pointer (RFC 6901): ~ va como ~0 y / como ~1 adentro de una clave.
        if not isinstance(path, str) or len(path) > 512 or not re.fullmatch(r"(/([^/~]|~[01])*)*", path):
            return False
        rule = finding["rule"]
        if not (rule is None or (type(rule) is int and 1 <= rule <= 17)):
            return False
        for key in ("limit", "reason", "alternative"):
            if key in finding and not (isinstance(finding[key], str) and len(finding[key]) <= 300
                                       and _free_text_ok(finding[key])):
                return False
    return True


def result_is_valid(result: object) -> bool:
    """``_flow_change_result_is_valid``, en Python: un aviso rapido.

    La prueba contra la base es el puente (``validator_results_cases.json``, que
    PGlite pasa por la funcion de la migracion en el paso 10 de
    ``validate_flow_store.mjs``).
    """
    return (
        isinstance(result, dict) and set(result) == {"blocks", "warnings"}
        and findings_are_valid(result["blocks"]) and findings_are_valid(result["warnings"])
        # En forma positiva: toda advertencia es advertencia y sin regla, y ningun bloqueo lo es.
        and all(f["kind"] == "advertencia" and f["rule"] is None for f in result["warnings"])
        and all(f["kind"] != "advertencia" for f in result["blocks"])
        and (fd.json_size(result) or 10**9) <= 61440
        and len(result["blocks"]) + len(result["warnings"]) <= 200
    )


def items_are_valid(items: object) -> bool:
    """``_flow_change_items_are_valid``, en Python."""
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        return False
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            return False
        if not (isinstance(item.get("flow_id"), str) and _UUID.fullmatch(item["flow_id"])
                and isinstance(item.get("producto"), str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", item["producto"])
                and isinstance(item.get("flujo"), str) and re.fullmatch(r"[a-z][a-z0-9_]{1,39}", item["flujo"])):
            return False
        keys = set(item)
        if item.get("tipo") == "version":
            if not (keys <= {"definicion", "en_curso", "flow_id", "flujo", "ok_del_cliente", "producto", "tipo"}
                    and keys >= {"definicion", "en_curso", "flow_id", "flujo", "producto", "tipo"}):
                return False
            definition = item["definicion"]
            if not (isinstance(definition, dict) and definition.get("esquema") == "setter-flujo/v1"
                    and definition.get("estado") == "borrador" and "version" not in definition
                    and definition.get("flujo") == item["flujo"] and definition.get("producto") == item["producto"]
                    and (fd.json_size(definition) or 10**9) <= 32768 and item["en_curso"] == "seguir"):
                return False
            if "ok_del_cliente" in item:
                ok = item["ok_del_cliente"]
                if not (isinstance(ok, dict) and set(ok) == {"fecha", "medio", "quien"}
                        and all(isinstance(ok[k], str) and len(ok[k]) <= 120 and _free_text_ok(ok[k])
                                for k in ("quien", "medio"))
                        and isinstance(ok["fecha"], str)
                        and re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])", ok["fecha"])):
                    return False
            kind = "contenido"
        elif item.get("tipo") == "volver":
            if keys != {"en_curso", "flow_id", "flujo", "producto", "tipo", "version"}:
                return False
            if not (type(item["version"]) is int and 1 <= item["version"] <= 999_999_999
                    and item["en_curso"] == "seguir"):
                return False
            kind = "contenido"
        elif item.get("tipo") in ("prender", "apagar"):
            if keys != {"flow_id", "flujo", "producto", "tipo"}:
                return False
            kind = "estado"
        else:
            return False
        if (item["flow_id"], kind) in seen:
            return False
        seen.add((item["flow_id"], kind))
    return True


@pytest.mark.parametrize("code", sorted(CASES))
def test_every_result_has_the_shape_the_base_accepts(code: str) -> None:
    for build in CASES[code]:
        findings = build()
        blocks = [f.to_json() for f in findings if f.kind != "advertencia"]
        warnings = [f.to_json() for f in findings if f.kind == "advertencia"]
        assert result_is_valid({"blocks": blocks, "warnings": warnings}), code


@pytest.mark.parametrize("block_id", ["1", "2", "3", "4", "5a", "5b"])
def test_a_validated_item_has_the_minimal_form_the_base_accepts(block_id: str) -> None:
    definition = block(block_id)
    result = check([version_item(definition, definition["flujo"])])
    assert result.ok and result_is_valid(result.to_json())
    assert items_are_valid(result.items_json())


def test_items_of_every_kind_have_the_minimal_form() -> None:
    two = flow("carrito", versions={1: published(flow_def("carrito")), 2: published(flow_def("carrito"), 2)})
    items = [state_item("volver", version=1), state_item("apagar"),
             version_item(purchase_flow(), "activacion", ok=CLIENT_OK), state_item("prender", "activacion")]
    result = check(items, in_world=world(two), catalog=hello_world_catalog())
    assert result.blocks == ()
    assert items_are_valid(result.items_json())


@pytest.mark.parametrize(
    "change",
    [
        # Encontrado con mutaciones al azar: un valor no hashable en es rompia la regla 7.
        lambda d: d.update(pasos=[{"condicion": {"dato": "ventana", "es": [{}]},
                                   "entonces": [{"conversar": "Retomar."}], "si_no": []}]),
        lambda d: d.update({7: "x", "x" * 600: 1, "a\u0000b": [None]}),
        lambda d: d["pasos"][0].update(plantilla={"a_crear": 1}),
        lambda d: d["prioridad"].update(le_gana_a=[{}, None, ["x"]]),
        lambda d: d["topes"].update(por_dia=1.5, total=True),
        lambda d: d.update(horario={"zona": ["America/Mexico_City"], "desde": {}, "hasta": None}),
        lambda d: d.update(dispara={"evento": "carrito", "silencio": {}}),
        lambda d: d["termina"].update(si={"compra": True}),
        # Un sustituto UTF-16 suelto en un valor, en una clave y adentro de una
        # lista (antes, UnicodeEncodeError), y los separadores de linea y de parrafo.
        lambda d: d.update(nombre="Carrito \ud83d"),
        lambda d: d["topes"].update({"max\udfff": 1}),
        lambda d: d.update(extra=[{"x": "a\udc00"}]),
        lambda d: d["pasos"][0].update(plantilla="att1\u2028carrito"),
        lambda d: d.update(nombre="Carrito\u2029abandonado"),
    ],
)
def test_hostile_values_give_findings_and_never_an_exception(change: Callable[[dict[str, Any]], None]) -> None:
    definition = flow_def("carrito")
    change(definition)
    for phase in ("draft", "publish"):
        for actor in fv.ACTORS:
            result = check([version_item(definition)], phase=phase, actor=actor)
            assert result.blocks
            assert result_is_valid(result.to_json())
            # El resultado se puede escribir como JSON en UTF-8: ninguna ruta se
            # lleva el caracter, se poda al padre.
            assert fd.json_size(result.to_json()) is not None


def test_items_with_a_lone_surrogate_say_that_they_are_not_json() -> None:
    result = check([version_item(with_(flow_def("carrito"), lambda d: d.update(nombre="Carrito \ud83d")))])
    assert blocks_at(result) == [
        ("invalid_value", "/0/definicion"), ("invalid_value", "/0/definicion/nombre"), ("invalid_value", "")]
    assert result.blocks[0].reason == "La definición no es JSON."
    assert result.blocks[2].reason == "Los ítems de la propuesta no se pueden escribir como JSON."


def _many_flows(count: int) -> tuple[FlowWorld, dict[str, str]]:
    keys = [f"flujo_{index:02d}" for index in range(count)]
    ids = {key: f"{index:08x}-0000-4000-8000-0000000000ff" for index, key in enumerate(keys, start=1)}
    return FlowWorld(WorldFlow(flow_id=ids[key], product_ref="att1", flow_key=key) for key in keys), ids


def test_items_over_256_kib_are_invalid_value_at_the_root() -> None:
    many, ids = _many_flows(9)

    def heavy(key: str) -> ProposalItem:
        # Cada definicion queda debajo de 32 KiB, y las nueve pasan de 256 KiB.
        definition = with_(flow_def("carrito"), lambda d: (
            d.update(flujo=key, prioridad={"le_gana_a": [], "no_arranca_mientras": []}),
            d["topes"].update(bolsa=f"bolsa-{key}"), d["pasos"].append({"derivar": "nota " * 5800})))
        return ProposalItem(producto="att1", flujo=key, flow_id=ids[key], tipo="version", definicion=definition,
                            en_curso="seguir")

    items = [heavy(key) for key in ids]
    assert all(fd.json_size(item.definicion) <= fd.DEFINITION_MAX_BYTES for item in items)
    result = validate(items, actor="operador", world=many, instance=INSTANCE, catalog=CATALOG, phase="draft")
    assert [(f.code, f.path, f.reason) for f in result.blocks] == [
        ("invalid_value", "", "Los ítems de la propuesta pasan de 262144 bytes.")]


# ---------------------------------------------------------------------------
# Con que reglas, en que fase y con que actor corrio (C#8): la base lo exige
# en p_validator, con phase publish y el actor de la propuesta.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase", fv.PHASES)
@pytest.mark.parametrize("actor", fv.ACTORS)
def test_the_validator_json_says_the_rules_the_phase_and_the_actor(phase: str, actor: str) -> None:
    result = check([state_item("apagar")], in_world=world(live("carrito")), phase=phase, actor=actor)
    assert result.validator_json() == {
        "contract": "setter-flujo/v1", "rules_version": 1, "phase": phase, "actor": actor}


@pytest.mark.parametrize(("phase", "actor"), [("preview", "operador"), ("publish", "slack")])
def test_a_result_with_another_phase_or_actor_is_an_error_of_the_caller(phase: str, actor: str) -> None:
    with pytest.raises(ValueError):
        ValidationResult(blocks=(), warnings=(), normalized_items=(), phase=phase, actor=actor)


def _many_unknown_keys() -> dict[str, Any]:
    definition = flow_def("carrito")
    for index in range(260):
        definition[f"clave_{index}"] = index
    return definition


def _many_templates_not_in_the_catalog() -> dict[str, Any]:
    # 210 pasos con una plantilla que no esta: cada hallazgo de la 13 lleva limite,
    # razon y alternativa, y 200 de ellos pasan de 61440 bytes.
    return with_(flow_def("carrito"), lambda d: d.update(pasos=[
        {"plantilla": f"att1_no_existe_{index:03d}", "idioma": "es_MX", "variables": ["nombre", "producto"]}
        for index in range(210)]))


def test_the_result_is_capped_at_200_findings() -> None:
    result = check([version_item(_many_unknown_keys())])
    assert len(result.blocks) == 200 and result.warnings == ()
    assert result_is_valid(result.to_json())


def test_the_result_is_capped_at_the_bytes_the_base_accepts() -> None:
    # La base rechaza un resultado de mas de 61440 bytes (flow_change_result_too_large):
    # se quedan los primeros bloqueos que entran.
    definition = _many_templates_not_in_the_catalog()
    assert fd.json_size(definition) <= fd.DEFINITION_MAX_BYTES
    result = check([version_item(definition)])
    size = fd.json_size(result.to_json())
    assert size is not None and 61440 - 500 < size <= 61440
    assert 100 < len(result.blocks) < 200 and result.warnings == ()
    assert blocks_at(result) == [("template_not_in_catalog", f"/0/definicion/pasos/{index}/plantilla")
                                 for index in range(len(result.blocks))]
    assert result_is_valid(result.to_json())


# ---------------------------------------------------------------------------
# El puente con la base (T#11): los resultados de CASES, con su to_json() y su
# validator_json(), van a validator_results_cases.json, y PGlite los pasa por
# _flow_change_result_is_valid y _flow_change_validator_is_valid de la
# migracion (validate_flow_store.mjs, paso 10). Con FLOW_STORE_FIXTURE_DIR se
# escribe; sin la variable, el del repo tiene que ser el de hoy.
# ---------------------------------------------------------------------------

BRIDGE_FILE = "validator_results_cases.json"


def _bridge_document() -> dict[str, Any]:
    recorded: list[ValidationResult] = []
    module = globals()
    plain_check = module["check"]

    def recording_check(*args: Any, **kwargs: Any) -> ValidationResult:
        result = plain_check(*args, **kwargs)
        recorded.append(result)
        return result

    cases: list[dict[str, Any]] = []
    module["check"] = recording_check
    try:
        for code in sorted(CASES):
            for case, build in zip(("negativo", "positivo"), CASES[code]):
                recorded.clear()
                findings = build()
                if recorded:
                    # Lo que dio validate, tal cual: el resultado y con que corrio.
                    [result] = recorded
                    entry = {"result": result.to_json(), "validator": result.validator_json()}
                else:
                    # duplicate_key sale de loads_strict, no de validate: un resultado con ese bloqueo.
                    entry = {"result": {"blocks": [f.to_json() for f in findings], "warnings": []}, "validator": None}
                cases.append({"code": code, "case": case, **entry})
    finally:
        module["check"] = plain_check
    edges = []
    for description, definition in (("200 hallazgos (el tope de cantidad)", _many_unknown_keys()),
                                    ("el tope de bytes, con los hallazgos largos de la 13",
                                     _many_templates_not_in_the_catalog())):
        result = check([version_item(definition)])
        edges.append({"description": description, "result": result.to_json(), "validator": result.validator_json()})
    validators = []
    for phase in fv.PHASES:
        for actor in fv.ACTORS:
            result = check([state_item("apagar")], in_world=world(live("carrito")), phase=phase, actor=actor)
            validators.append(result.validator_json())
    return {
        "_generated": {
            "generator": "tests/test_flow_validator.py (test_the_bridge_json_with_the_base_is_the_one_in_the_repo)",
            "date": datetime.now(timezone.utc).date().isoformat(),
            "from": "los casos negativo y positivo de cada codigo de CASES, dos bordes del resultado y "
                    "validator_json() con las dos fases y los dos actores",
            "regenerate": "FLOW_STORE_FIXTURE_DIR=tests/fixtures/flow_store uv run pytest tests/test_flow_validator.py",
        },
        "validators": validators,
        "cases": cases,
        "edges": edges,
    }


def test_the_bridge_json_with_the_base_is_the_one_in_the_repo() -> None:
    document = _bridge_document()
    assert len(document["cases"]) == 2 * len(CASES)
    assert sum(entry["validator"] is None for entry in document["cases"]) == 1
    for entry in document["cases"]:
        if entry["case"] == "negativo":
            produced = {f["code"] for f in [*entry["result"]["blocks"], *entry["result"]["warnings"]]}
            assert entry["code"] in produced, entry["code"]
    target = os.environ.get("FLOW_STORE_FIXTURE_DIR")
    if target:
        (Path(target) / BRIDGE_FILE).write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n",
                                                encoding="utf-8")
        return
    in_repo = json.loads((FLOW_STORE / BRIDGE_FILE).read_text(encoding="utf-8"))
    generated_on = in_repo.get("_generated", {}).pop("date", None)
    assert isinstance(generated_on, str) and date.fromisoformat(generated_on) >= date(2026, 10, 16)
    document["_generated"].pop("date")
    assert in_repo == document, f"{BRIDGE_FILE} no es el de hoy: regenerarlo con FLOW_STORE_FIXTURE_DIR"


def test_validating_the_normalized_items_gives_the_same_items() -> None:
    keys = ("carrito", "pago_fallido", "precheckout", "descuento", "reactivacion")
    first = check([version_item(flow_def(key), key) for key in keys], in_world=world(keys=keys))
    again = check(list(first.normalized_items), in_world=world(keys=keys))
    assert again.items_json() == first.items_json()
    assert again.to_json() == first.to_json()


# ---------------------------------------------------------------------------
# Lo que llega mal es un error de quien llama, no un hallazgo.
# ---------------------------------------------------------------------------

def test_an_item_of_a_flow_that_is_not_in_the_world_is_flow_not_found() -> None:
    stranger = ProposalItem(producto="att1", flujo="carrito", flow_id="0000000c-0000-4000-8000-00000000000c",
                            tipo="prender")
    with pytest.raises(FlowValidationInputError) as caught:
        check([stranger])
    assert caught.value.code == "flow_not_found"
    renamed = ProposalItem(producto="att1", flujo="pago_fallido", flow_id=IDS["carrito"], tipo="prender")
    with pytest.raises(FlowValidationInputError):
        check([renamed])


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"actor": "slack"}, ValueError),
        ({"phase": "preview"}, ValueError),
    ],
)
def test_the_actor_and_the_phase_are_closed(kwargs: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        check([state_item("prender")], **kwargs)


def test_the_items_are_one_to_ten_with_one_content_and_one_state_per_flow() -> None:
    with pytest.raises(ProposalItemsInvalid):
        check([])
    with pytest.raises(ProposalItemsInvalid):
        check([state_item("prender"), state_item("apagar")])


def test_eleven_items_are_too_many() -> None:
    many, ids = _many_flows(11)
    items = [ProposalItem(producto="att1", flujo=key, flow_id=flow_id, tipo="prender") for key, flow_id in ids.items()]
    with pytest.raises(ProposalItemsInvalid) as caught:
        validate(items, actor="operador", world=many, instance=INSTANCE, catalog=CATALOG, phase="draft")
    assert caught.value.detail == "items_count_invalid"
    ten = validate(items[:10], actor="operador", world=many, instance=INSTANCE, catalog=CATALOG, phase="draft")
    assert {f.code for f in ten.blocks} == {"flow_has_no_version"}


# ---------------------------------------------------------------------------
# Los casos compartidos con la base: los invariantes 3 y 4 (§5.8).
#
# invariant_cases.json tiene el formato fijo de la parte de la base (commit 1,
# validate_flow_store.mjs): instance, catalog_fixture, actor y phase arriba, y
# cada caso con id, description, flujo, previous_versions, definition, base y
# validator (rule, code y path, la ruta adentro de definition). Las
# definiciones vienen completas y normalizadas, en borrador.
# ---------------------------------------------------------------------------

SHARED = json.loads((FLOW_STORE / "invariant_cases.json").read_text(encoding="utf-8"))
# Los siete casos de la tabla del diseno, con el bloqueo del validador que pide
# cada uno: la regla, el codigo y la ruta adentro de la definicion.
SHARED_EXPECTED = {
    "a_medir_en_una_rama": (11, "unresolved_value", "/pasos/0/entonces/0/esperar"),
    "a_crear_en_un_paso": (11, "unresolved_value", "/pasos/0/plantilla"),
    "a_decidir_en_vence": (11, "unresolved_value", "/termina/vence"),
    "zona_inexistente": (8, "zone_invalid", "/horario/zona"),
    "zona_cambia_despues_de_siempre": (8, "zone_changed", "/horario/zona"),
    "horario_siempre": None,
    "misma_zona": None,
}


def _value_at(document: object, pointer: str) -> object:
    """El valor de un JSON Pointer (RFC 6901: ~1 se decodifica antes que ~0)."""
    current = document
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        current = current[int(key)] if isinstance(current, list) else current[key]  # type: ignore[index]
    return current


def _shared_result(case: dict[str, Any]) -> ValidationResult:
    """Lo que dice el archivo: el flujo con sus versiones previas publicadas, y un item version."""
    instance_data = SHARED["instance"]
    instance = InstanceView(instance_data["tenant_ref"], instance_data["ally_ref"],
                            frozenset(instance_data["products"]), instance_data["time_zone"])
    catalog = TemplateCatalog.from_inbox_payload(json.loads(
        (Path(__file__).resolve().parents[1] / SHARED["catalog_fixture"]).read_text(encoding="utf-8")))
    definition = case["definition"]
    versions = {number: fd.as_published(previous, number)
                for number, previous in enumerate(case["previous_versions"], start=1)}
    flow_id = "0000000d-0000-4000-8000-00000000000d"
    shared_world = FlowWorld([WorldFlow(
        flow_id=flow_id, product_ref=definition["producto"], flow_key=case["flujo"],
        active_version=max(versions) if versions else None, generation=len(versions), versions=versions)])
    item = ProposalItem(producto=definition["producto"], flujo=case["flujo"], flow_id=flow_id,
                        tipo="version", definicion=definition, en_curso="seguir")
    return validate([item], actor=SHARED["actor"], world=shared_world, instance=instance,
                    catalog=catalog, phase=SHARED["phase"])


def test_the_shared_file_is_the_seven_cases_of_the_design() -> None:
    assert (SHARED["actor"], SHARED["phase"]) == ("operador", "publish")
    expected = {
        case["id"]: (case["validator"]["rule"], case["validator"]["code"], case["validator"]["path"])
        if case["validator"] else None
        for case in SHARED["cases"]
    }
    assert expected == SHARED_EXPECTED
    # La ruta apunta, en la definicion del caso, a lo que se bloquea.
    for case in SHARED["cases"]:
        if case["validator"] is None:
            continue
        value = _value_at(case["definition"], case["validator"]["path"])
        if case["validator"]["code"] == "unresolved_value":
            assert fd.is_unresolved(value) or (isinstance(value, dict) and "a_crear" in value), case["id"]
        else:
            assert value == case["definition"]["horario"]["zona"], case["id"]


@pytest.mark.parametrize("case", SHARED["cases"], ids=lambda case: case["id"])
def test_a_shared_case_is_judged_the_same_by_the_base_and_the_validator(case: dict[str, Any]) -> None:
    result = _shared_result(case)
    if case["validator"] is None:
        # Ningun bloqueo: la base lo publica.
        assert result.blocks == ()
    else:
        expected = (case["validator"]["rule"], case["validator"]["code"], case["validator"]["path"])
        assert [(f.rule, f.code, f.definition_path) for f in result.blocks] == [expected]
    # Una capa que rechaza y la otra que no hace fallar la suite.
    assert (case["base"] is None) == (case["validator"] is None)
    assert result_is_valid(result.to_json())
    # La definicion ya viene normalizada: lo que se guardaria es la misma.
    assert result.normalized_items[0].definicion == case["definition"]


# ---------------------------------------------------------------------------
# base_for contra la base (§5.2). base_example.json lo escribio PGlite
# (validate_flow_store.mjs, paso 9) con _flow_store_base sobre la cadena
# completa: instance, flows (los de la instancia, con sus versiones),
# flows_of_other_instances y bases.
# ---------------------------------------------------------------------------

BASE_EXAMPLE = json.loads((FLOW_STORE / "base_example.json").read_text(encoding="utf-8"))


def _example_world() -> FlowWorld:
    instance = BASE_EXAMPLE["instance"]
    flows = []
    for flow_data in BASE_EXAMPLE["flows"]:
        assert (flow_data["tenant_ref"], flow_data["ally_ref"]) == (instance["tenant_ref"], instance["ally_ref"])
        assert flow_data["product_ref"] in instance["products"]
        flows.append(WorldFlow(
            flow_id=flow_data["flow_id"], product_ref=flow_data["product_ref"], flow_key=flow_data["flow_key"],
            state=flow_data["state"], active_version=flow_data["active_version"],
            generation=flow_data["generation"],
            versions={version["version"]: version["definition"] for version in flow_data["versions"]},
        ))
    return FlowWorld(flows)


@pytest.mark.parametrize("entry", BASE_EXAMPLE["bases"], ids=lambda entry: entry["description"])
def test_base_for_gives_exactly_what_flow_store_base_gave(entry: dict[str, Any]) -> None:
    example_world = _example_world()
    items = []
    for flow_id in entry["item_flow_ids"]:
        known = example_world.flow(flow_id)
        assert known is not None, flow_id
        items.append(ProposalItem(producto=known.product_ref, flujo=known.flow_key, flow_id=flow_id,
                                  tipo="prender"))
    base = example_world.base_for(items)
    assert base == entry["base"]
    # Los flujos de otra instancia no entran en la base.
    others = {flow_data["flow_id"] for flow_data in BASE_EXAMPLE["flows_of_other_instances"]}
    assert others and not others & set(base)


def test_the_example_world_has_two_products_and_a_flow_without_version() -> None:
    example_world = _example_world()
    assert {known.product_ref for known in example_world.flows} == set(BASE_EXAMPLE["instance"]["products"])
    assert any(known.active_version is None for known in example_world.flows)
    assert len(BASE_EXAMPLE["bases"]) == 3


def test_rule_14_without_a_legacy_kind_does_not_compile_anything() -> None:
    # Sin legacy_kind el flujo no tiene politica del legado: la forma no se mira.
    four = with_(flow_def("carrito"), lambda d: d.update(pasos=[dict(TEMPLATE_STEP), {"esperar": "1h"}] * 3
                                                         + [dict(TEMPLATE_STEP)]))
    assert not {code for code in found(run(four)) if code.startswith("engine_not_supported")}
    assert found(run(four, in_world=legacy())) & {code for code in fv.BLOCK_CODES if code.startswith("engine_not")}


def test_rule_14_points_at_the_step_and_only_at_publish() -> None:
    changed = with_(flow_def("carrito"), lambda d: d.update(pasos=[dict(TEMPLATE_STEP), {"esperar": "24h"},
                                                                   {**TEMPLATE_STEP, "variables": ["nombre"]}]))
    result = check([version_item(changed)], in_world=legacy())
    assert ("engine_not_supported:variables_differ", "/0/definicion/pasos/2/variables") in blocks_at(result)
    draft = check([version_item(changed)], in_world=legacy(), phase="draft")
    assert not [code for code, _ in blocks_at(draft) if code.startswith("engine_not_supported")]

