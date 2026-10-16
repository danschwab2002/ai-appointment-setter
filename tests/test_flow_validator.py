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
* ``flow_store/invariant_cases.json``: los siete casos de los invariantes 3 y
  4 que comparten la base y el validador, con el formato fijo de la parte de
  la base (``validate_flow_store.mjs``, commit 1).
* ``flow_store/base_example.json``: el mundo de prueba y las bases que dio
  ``_flow_store_base`` en PGlite (``validate_flow_store.mjs``, paso 9).

Las funciones ``*_are_valid`` de abajo son las de la migracion del almacen,
escritas en Python con sus mismas formas.

Los caracteres de control se escriben con escapes ``\\u``.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
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
         flow_id: str | None = None) -> WorldFlow:
    versions = versions or {}
    if active is None and versions:
        active = max(versions)
    return WorldFlow(
        flow_id=flow_id or IDS[key], product_ref=product, flow_key=key,
        state=state or ("prendido" if active is not None else "apagado"),
        active_version=active, generation=generation if generation is not None else len(versions),
        versions=versions,
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


def utility_template(payload: dict[str, Any]) -> None:
    payload["message_templates"].append({
        "name": "att1_bienvenida_01", "status": "APPROVED", "category": "UTILITY", "language": "es_MX",
        "components": [{"type": "BODY", "text": "Hola, {{1}}. Tu compra de {{2}} está confirmada."}],
    })


def purchase_flow(**changes: Any) -> dict[str, Any]:
    definition = block("7")
    definition["entra"]["permiso"] = "compra"
    definition["pasos"][0]["plantilla"] = "att1_bienvenida_01"
    definition["termina"]["vence"] = "1d"
    definition.update(changes)
    return definition


def with_(definition: dict[str, Any], change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    change(definition)
    return definition


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
    # Regla 15.
    "coupon_change_not_supported": (
        lambda: run(draft_of("descuento", lambda d: d["pasos"][0]["boton"].update(cupon="OTRO10")), "descuento",
                    in_world=world(live("descuento"))),
        lambda: run(draft_of("descuento"), "descuento", in_world=world(live("descuento"))),
    ),
    # Regla 17.
    "purchase_permission_requires_single_utility_send": (
        lambda: check([version_item(with_(purchase_flow(), lambda d: d["pasos"][0].update(
            plantilla="att1_carrito_abandonado_01")), "activacion", ok=CLIENT_OK)]).blocks,
        lambda: check([version_item(purchase_flow(), "activacion", ok=CLIENT_OK)],
                      catalog=catalog_with(utility_template)).blocks,
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
        lambda: check([version_item(purchase_flow(), "activacion")], catalog=catalog_with(utility_template)).blocks,
        lambda: check([version_item(purchase_flow(), "activacion", ok=CLIENT_OK)],
                      catalog=catalog_with(utility_template)).blocks,
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
            "ficha_link_requires_ficha_trigger", "coupon_requires_checkout_link"},
        6: {"event_requires_vence", "silence_forbids_vence"},
        7: {"conversar_outside_open_window_branch", "terminal_step_not_last", "nested_condition"},
        8: {"hour_invalid", "window_not_increasing", "zone_invalid", "zone_changed", "pool_zone_mismatch"},
        9: {"cap_invalid", "daily_cap_exceeds_total"},
        10: {"unknown_flow_reference", "mutual_le_gana_a", "product_not_declared", "product_required",
             "duplicate_flow_key"},
        11: {"unresolved_value"},
        12: {"vence_not_after_waits"},
        13: {"template_not_in_catalog", "template_not_approved", "template_language_mismatch",
             "template_variable_count_mismatch", "template_button_mismatch", "template_catalog_unavailable"},
        15: {"coupon_change_not_supported"},
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
        "purchase_permission_requires_single_utility_send",
    }
    assert {code for code, pairs in fv.CODE_RULES.items() if any(k == "todavia_no" for _, k in pairs)} == {
        "coupon_change_not_supported"}
    cycle = {code for code, pairs in fv.CODE_RULES.items() if (None, "ciclo") in pairs}
    assert cycle == {
        "flow_creation_not_enabled", "change_outside_1b_scope", "trigger_change_is_another_flow",
        "required_key_missing", "flow_change_stale_base", "rollback_target_not_found",
        "rollback_target_is_active", "state_transition_invalid", "flow_has_no_version",
        "en_curso_not_supported", "identifier_not_allowed", "client_purchase_ok_missing",
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


@pytest.mark.parametrize("actor", fv.ACTORS)
@pytest.mark.parametrize(
    ("key", "change"),
    [
        ("descuento", lambda d: d["dispara"]["silencio"].update(lo_ultimo="persona")),
        ("carrito", lambda d: d["dispara"].update(evento="pago_fallido")),
        ("carrito", lambda d: d.update(dispara={"silencio": {
            "lo_ultimo": "agente", "desde": "24h", "hasta": "72h", "conversaciones": ["abierta"]}})),
    ],
    ids=["otro-lo_ultimo", "otro-evento", "evento-a-silencio"],
)
def test_another_event_or_another_last_speaker_is_another_flow(
    key: str, change: Callable[[dict[str, Any]], None], actor: str,
) -> None:
    result = check([version_item(draft_of(key, change), key)], actor=actor, in_world=world(live(key)),
                   phase="draft")
    assert ("trigger_change_is_another_flow", "/dispara") in {
        (f.code, f.definition_path) for f in result.blocks}


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
    marketing = catalog_with(lambda p: (utility_template(p), p["message_templates"][-1].update(category="MARKETING")))
    blocked = check([state_item("prender", "activacion")], in_world=world(purchase), catalog=marketing)
    assert [f.code for f in blocked.blocks] == ["purchase_permission_requires_single_utility_send"]


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
    utility = catalog_with(utility_template)
    already = live("activacion", purchase_flow())
    draft = fd.as_draft(published(purchase_flow()))
    assert check([version_item(draft, "activacion")], in_world=world(already), catalog=utility).blocks == ()
    assert check([version_item(purchase_flow(), "activacion")], catalog=utility, phase="draft").blocks == ()
    result = check([version_item(purchase_flow(), "activacion")], catalog=utility)
    assert [(f.code, f.path) for f in result.blocks] == [("client_purchase_ok_missing", "/0/ok_del_cliente")]


def test_a_malformed_client_ok_is_rule_1() -> None:
    result = check([version_item(purchase_flow(), "activacion", ok={"quien": "Juan\u202e", "medio": "x"})],
                   catalog=catalog_with(utility_template), phase="draft")
    assert {(f.code, f.path) for f in result.blocks} == {
        ("invalid_value", "/0/ok_del_cliente/quien"), ("required_key_missing", "/0/ok_del_cliente/fecha")}


# ---------------------------------------------------------------------------
# Precisiones de las reglas.
# ---------------------------------------------------------------------------

def test_rule_17_counts_sends_per_path() -> None:
    utility = catalog_with(utility_template)
    one_per_branch = purchase_flow(pasos=[{
        "condicion": {"dato": "ventana", "es": ["abierta"]},
        "entonces": [{"plantilla": "att1_bienvenida_01", "idioma": "es_MX"}],
        "si_no": [{"plantilla": "att1_bienvenida_01", "idioma": "es_MX"}]}])
    assert check([version_item(one_per_branch, "activacion", ok=CLIENT_OK)], catalog=utility).blocks == ()
    two_in_a_row = purchase_flow(pasos=[{"plantilla": "att1_bienvenida_01", "idioma": "es_MX"},
                                        {"esperar": "1h"}, {"plantilla": "att1_bienvenida_01", "idioma": "es_MX"}])
    result = check([version_item(two_in_a_row, "activacion", ok=CLIENT_OK)], catalog=utility)
    assert [(f.code, f.path) for f in result.blocks] == [
        ("purchase_permission_requires_single_utility_send", "/0/definicion/pasos")]
    above_and_below = purchase_flow(pasos=[{"plantilla": "att1_bienvenida_01", "idioma": "es_MX"}, {
        "condicion": {"dato": "ventana", "es": ["abierta"]},
        "entonces": [], "si_no": [{"plantilla": "att1_bienvenida_01", "idioma": "es_MX"}]}])
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
# La forma de lo que cruza a la base (§5.2), con la logica de sus helpers.
# ---------------------------------------------------------------------------

_FREE_TEXT_BAD = re.compile(
    "[\u0001-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
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
    """``_flow_change_result_is_valid``, en Python."""
    return (
        isinstance(result, dict) and set(result) == {"blocks", "warnings"}
        and findings_are_valid(result["blocks"]) and findings_are_valid(result["warnings"])
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
    result = check(items, in_world=world(two), catalog=catalog_with(utility_template))
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


def test_the_result_is_capped_at_200_findings() -> None:
    definition = flow_def("carrito")
    for index in range(260):
        definition[f"clave_{index}"] = index
    result = check([version_item(definition)])
    assert len(result.blocks) == 200 and result.warnings == ()
    assert result_is_valid(result.to_json())


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


# ---------------------------------------------------------------------------
# Los casos compartidos con la base: los invariantes 3 y 4 (§5.8).
#
# invariant_cases.json tiene el formato fijo de la parte de la base (commit 1,
# validate_flow_store.mjs): instance, catalog_fixture, actor y phase arriba, y
# cada caso con id, description, flujo, previous_versions, definition, base y
# validator. Las definiciones vienen completas y normalizadas, en borrador.
# ---------------------------------------------------------------------------

SHARED = json.loads((FLOW_STORE / "invariant_cases.json").read_text(encoding="utf-8"))
# Los siete casos de la tabla del diseno, con el bloqueo del validador que pide cada uno.
SHARED_EXPECTED = {
    "a_medir_en_una_rama": (11, "unresolved_value"),
    "a_crear_en_un_paso": (11, "unresolved_value"),
    "a_decidir_en_vence": (11, "unresolved_value"),
    "zona_inexistente": (8, "zone_invalid"),
    "zona_cambia_despues_de_siempre": (8, "zone_changed"),
    "horario_siempre": None,
    "misma_zona": None,
}


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
        case["id"]: (case["validator"]["rule"], case["validator"]["code"]) if case["validator"] else None
        for case in SHARED["cases"]
    }
    assert expected == SHARED_EXPECTED


@pytest.mark.parametrize("case", SHARED["cases"], ids=lambda case: case["id"])
def test_a_shared_case_is_judged_the_same_by_the_base_and_the_validator(case: dict[str, Any]) -> None:
    result = _shared_result(case)
    if case["validator"] is None:
        # Ningun bloqueo: la base lo publica.
        assert result.blocks == ()
    else:
        expected = (case["validator"]["rule"], case["validator"]["code"])
        assert {(f.rule, f.code) for f in result.blocks} == {expected}
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
