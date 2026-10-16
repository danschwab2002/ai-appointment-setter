"""Los flujos de ATT1 como definiciones, con el dato real del 2026-10-16.

Diseno del almacen, §7.5. Fixtures (``tests/fixtures/flow_store/``):

* ``att1_flow_config_20261016.json``: la configuracion de los flujos de
  att1-db (stack setter-att1) el 2026-10-16 a las 13:15:51Z, leida en una
  sesion de solo lectura (``default_transaction_read_only``) con
  ``att1_flow_config_query.sql``, que esta tal cual se corrio. Solo filas de
  configuracion: sin ``channel_account_ref`` y sin los eventos de una persona
  (``_captured.redactions``). Se regenera con el comando de solo lectura del
  README de los anexos del diseno.
* ``att1_instancia_f01fbcc.toml``: ``[instancia]`` y ``[plantillas]`` del
  manifiesto de ATT1 en ``f01fbcc`` (2026-10-15), con las cinco plantillas.
* ``chatwoot_inbox_11_message_templates_20261010.json``: el catalogo de la
  bandeja 11 de ATT1, capturado de Chatwoot el 2026-10-10.
* ``schema_examples_v1.json``: los bloques del esquema.
* ``att1_definitions_20261016.json`` y ``att1_validation_result_20261016.json``:
  los genera esta prueba (la 4) y los publica PGlite
  (``validate_flow_store.mjs``, paso 7). No son capturas: ``_generated`` dice
  el generador, el dia en que se escribieron y de que salen. Para
  regenerarlos:
  ``FLOW_STORE_FIXTURE_DIR=tests/fixtures/flow_store uv run pytest tests/test_flow_validator_att1.py``.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import tomllib
from collections.abc import Callable, Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_definition as fd
from bridge.flow_validator import (
    RULES_VERSION,
    VALIDATOR_CONTRACT,
    FlowWorld,
    InstanceView,
    ProposalItem,
    TemplateCatalog,
    ValidationResult,
    WorldFlow,
    validate,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
FLOW_STORE = FIXTURES / "flow_store"
SPEC = importlib.util.spec_from_file_location("flow_store_translator", ROOT / "tests" / "flow_store_translator.py")
assert SPEC and SPEC.loader
TRANSLATOR = importlib.util.module_from_spec(SPEC)
# Una dataclass busca su modulo en sys.modules al definirse.
sys.modules[SPEC.name] = TRANSLATOR
SPEC.loader.exec_module(TRANSLATOR)

CONFIG: dict[str, Any] = json.loads((FLOW_STORE / "att1_flow_config_20261016.json").read_text(encoding="utf-8"))
MANIFEST: dict[str, Any] = tomllib.loads((FLOW_STORE / "att1_instancia_f01fbcc.toml").read_text(encoding="utf-8"))
BLOCKS: list[dict[str, Any]] = json.loads(
    (FLOW_STORE / "schema_examples_v1.json").read_text(encoding="utf-8"))["blocks"]
BLOCK = {block["block"]: block["definition"] for block in BLOCKS}
CATALOG = TemplateCatalog.from_inbox_payload(json.loads(
    (FIXTURES / "chatwoot_inbox_11_message_templates_20261010.json").read_text(encoding="utf-8")))
INSTANCE = InstanceView(
    MANIFEST["instancia"]["tenant_ref"], MANIFEST["instancia"]["ally_ref"],
    frozenset({MANIFEST["instancia"]["funnel_ref"]}), MANIFEST["instancia"]["zona_horaria"],
)
ORDER = TRANSLATOR.ORDER
IDS = {key: f"{index:08x}-0000-4000-8000-0000000a7701" for index, key in enumerate(ORDER, start=1)}


def translate(config: dict[str, Any] = CONFIG) -> dict[str, Any]:
    return TRANSLATOR.translate_att1(config, MANIFEST, BLOCKS)


TRANSLATED = translate()


def normalize(definition: dict[str, Any]) -> dict[str, Any]:
    result = fd.normalize_definition(definition, new_flow=True, instance=INSTANCE, catalog=CATALOG)
    assert result.findings == (), [(f.code, f.path) for f in result.findings]
    return result.definition


DEFINITIONS = {key: normalize(translated.definition) for key, translated in TRANSLATED.items()}


def _scope(scope_key: str, version: int) -> dict[str, Any]:
    [scope] = [s for s in CONFIG["pilot_scope_versions"] if s["scope_key"] == scope_key and s["version"] == version]
    return scope


def _leaves(value: object, prefix: str = "") -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _leaves(child, f"{prefix}/{key}")
    elif isinstance(value, list) and value and all(isinstance(child, dict) for child in value):
        for index, child in enumerate(value):
            yield from _leaves(child, f"{prefix}/{index}")
    else:
        yield prefix


# ---------------------------------------------------------------------------
# 1. El traductor.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("flow_key", "block_id"), [("carrito", "1"), ("pago_fallido", "2"), ("precheckout", "3")])
def test_the_translation_is_the_schema_block_of_today_field_by_field(flow_key: str, block_id: str) -> None:
    changes = fd.diff(normalize(BLOCK[block_id]), DEFINITIONS[flow_key])
    assert changes == (), [(c.path, c.before, c.after) for c in changes]


@pytest.mark.parametrize("flow_key", ["carrito", "pago_fallido", "precheckout"])
def test_every_translated_field_says_where_it_comes_from(flow_key: str) -> None:
    translated = TRANSLATED[flow_key]
    assert set(_leaves(translated.definition)) == set(translated.sources)
    for source in translated.sources.values():
        assert source == TRANSLATOR.SOURCE_CODE or source.startswith(
            (TRANSLATOR.SOURCE_REAL + ": ", TRANSLATOR.SOURCE_MANIFEST + ": ")), source


def test_what_comes_from_att1_db_is_the_active_version_of_each_scope() -> None:
    # Los controles armados: la recuperacion en la v5 y el primer contacto en la v2.
    recovery = _scope("att1-recuperacion", 5)
    first_contact = _scope("att1-primer-contacto", 2)
    cart, payment, precheckout = (TRANSLATED[key].definition for key in ("carrito", "pago_fallido", "precheckout"))
    for definition in (cart, payment):
        assert definition["topes"]["por_dia"] == recovery["max_outbound_request_starts_per_day"] == 80
        assert definition["topes"]["total"] == recovery["max_outbound_request_starts_total"] == 1000
        assert definition["topes"]["bolsa"] == recovery["scope_key"]
        assert definition["entra"]["permiso"] == "formulario" and recovery["audience_mode"] == "consented_intent"
        assert definition["termina"]["vence"] == "1d"
        assert definition["horario"] == {"zona": "America/Mexico_City", "desde": "09:00", "hasta": "21:00"}
        # La gracia de la recuperacion es 00:00:00: sin espera.
        assert "esperar" not in definition["pasos"][0]
    assert cart["dispara"] == {"evento": "carrito"} and recovery["source_event_type"] == "PURCHASE_OUT_OF_SHOPPING_CART"
    assert payment["dispara"] == {"evento": "pago_fallido"}
    assert recovery["additional_source_event_types"] == ["PURCHASE_CANCELED"]
    assert precheckout["topes"]["por_dia"] == first_contact["max_outbound_request_starts_per_day"] == 10
    assert precheckout["topes"]["total"] == first_contact["max_outbound_request_starts_total"] == 150
    assert precheckout["topes"]["bolsa"] == "att1-primer-contacto"
    assert precheckout["pasos"][0] == {"esperar": "60m"}
    assert precheckout["dispara"] == {"evento": "intencion"}
    sources = TRANSLATED["precheckout"].sources
    assert sources["/pasos/0/esperar"] == "dato real: followup_policy_versions.grace_period"
    assert sources["/topes/por_dia"] == "dato real: pilot_scope_versions.max_outbound_request_starts_per_day"
    assert TRANSLATED["pago_fallido"].sources["/dispara/evento"] == (
        "dato real: pilot_scope_versions.additional_source_event_types")


def test_what_comes_from_the_manifest_is_the_template_and_its_language() -> None:
    for flow_key in ("carrito", "pago_fallido", "precheckout"):
        translated = TRANSLATED[flow_key]
        step_at = len(translated.definition["pasos"]) - 1
        step = translated.definition["pasos"][step_at]
        assert (step["plantilla"], step["idioma"]) == (
            MANIFEST["plantillas"][flow_key]["nombre"], MANIFEST["plantillas"][flow_key]["idioma"])
        assert translated.sources[f"/pasos/{step_at}/plantilla"] == f"manifiesto: [plantillas].{flow_key}.nombre"


def test_the_coupon_and_the_reactivation_come_whole_from_the_schema_blocks() -> None:
    assert TRANSLATED["descuento"].definition == BLOCK["4"]
    assert TRANSLATED["reactivacion"].definition == BLOCK["5a"]
    assert TRANSLATED["reactivacion"].sources == {
        "": "bloque del esquema 5a (docs/contracts/flow-definition-v1.md:638-661)"}


def _policy(config: dict[str, Any], policy_key: str) -> dict[str, Any]:
    [policy] = [p for p in config["followup_policy_versions"] if p["policy_key"] == policy_key]
    return policy


def _active(config: dict[str, Any], scope_key: str) -> dict[str, Any]:
    [control] = [c for c in config["pilot_runtime_controls"] if c["scope_key"] == scope_key]
    [scope] = [s for s in config["pilot_scope_versions"]
               if s["scope_key"] == scope_key and s["version"] == control["scope_version"]]
    return scope


def test_the_translation_follows_the_data_when_the_data_changes() -> None:
    # Las constantes de la prueba de arriba coinciden hoy con el dato; esta mueve
    # el dato y exige que la salida lo siga, flujo por flujo.
    config = copy.deepcopy(CONFIG)
    recovery = _policy(config, "att1-recuperacion-un-toque")
    recovery["business_windows"][0].update(start="10:00", end="18:00")
    recovery["expires_after"] = "2 days"
    _active(config, "att1-recuperacion")["max_outbound_request_starts_per_day"] = 81
    first_contact = _policy(config, "att1-primer-contacto-formulario")
    first_contact["business_windows"][0].update(start="08:00", end="20:00")
    first_contact["expires_after"] = "3 days"
    first_contact["grace_period"] = "00:30:00"
    _active(config, "att1-primer-contacto")["max_outbound_request_starts_total"] = 151
    translated = translate(config)
    for flow_key in ("carrito", "pago_fallido"):
        definition = translated[flow_key].definition
        assert definition["horario"] == {"zona": "America/Mexico_City", "desde": "10:00", "hasta": "18:00"}
        assert definition["termina"]["vence"] == "2d"
        assert (definition["topes"]["por_dia"], definition["topes"]["total"]) == (81, 1000)
    precheckout = translated["precheckout"].definition
    assert precheckout["horario"] == {"zona": "America/Mexico_City", "desde": "08:00", "hasta": "20:00"}
    assert precheckout["termina"]["vence"] == "3d"
    assert precheckout["pasos"][0] == {"esperar": "30m"}
    assert (precheckout["topes"]["por_dia"], precheckout["topes"]["total"]) == (10, 151)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda c: _policy(c, "att1-recuperacion-un-toque")["business_windows"][0].update(days=[1, 2, 3, 4, 5]),
         "business_window_not_every_day"),
        (lambda c: _policy(c, "att1-recuperacion-un-toque")["business_windows"].append(
            {"start": "10:00", "end": "12:00", "days": [1, 2, 3, 4, 5, 6, 7]}), "business_windows_not_one_window"),
        (lambda c: _policy(c, "att1-recuperacion-un-toque")["business_windows"][0].update(start="9:00"),
         "business_window_hour_shape"),
        (lambda c: _policy(c, "att1-primer-contacto-formulario").update(grace_period="1 hour"), "grace_period_shape"),
        (lambda c: _policy(c, "att1-primer-contacto-formulario").update(grace_period="00:00:30"), "grace_period_shape"),
        (lambda c: _policy(c, "att1-recuperacion-un-toque").update(expires_after="1 mon"), "expires_after_shape"),
        (lambda c: _policy(c, "att1-recuperacion-un-toque").update(max_automatic_messages=2),
         "more_than_one_automatic_message"),
        (lambda c: _active(c, "att1-recuperacion").update(audience_mode="manual_cohort"), "audience_mode_unknown"),
        (lambda c: _active(c, "att1-recuperacion").update(additional_source_event_types=["PURCHASE_BILLET_PRINTED"]),
         "event_type_unknown"),
        (lambda c: _active(c, "att1-recuperacion").update(timezone="America/Bogota"),
         "scope_and_policy_time_zones_differ"),
        (lambda c: c["pilot_runtime_controls"][1].update(scope_version=9), "active_scope_missing"),
    ],
)
def test_the_translator_fails_loudly_on_a_shape_it_does_not_know(
    change: Callable[[dict[str, Any]], None], error: str,
) -> None:
    config = copy.deepcopy(CONFIG)
    change(config)
    with pytest.raises(TRANSLATOR.TranslationError) as caught:
        translate(config)
    assert str(caught.value).startswith(error)


# ---------------------------------------------------------------------------
# 2. Los cuatro flujos de hoy y la reactivacion como esta construida.
# ---------------------------------------------------------------------------

def _registered_world() -> FlowWorld:
    return FlowWorld(WorldFlow(flow_id=IDS[key], product_ref="att1", flow_key=key) for key in ORDER)


def _first_versions() -> list[ProposalItem]:
    return [ProposalItem(producto="att1", flujo=key, flow_id=IDS[key], tipo="version",
                         definicion=TRANSLATED[key].definition, en_curso="seguir") for key in ORDER]


FIRST_VERSIONS: ValidationResult = validate(
    _first_versions(), actor="operador", world=_registered_world(), instance=INSTANCE, catalog=CATALOG,
    phase="publish")


def test_the_five_flows_publish_together_with_the_three_warnings_of_the_contract() -> None:
    # Las que el contrato dice que dio su validador de prueba (flow-definition-v1.md:476-479):
    # hasta esta prueba, una prediccion sin medir. La reactivacion es el quinto bloque, apagada.
    assert FIRST_VERSIONS.blocks == ()
    # Las rutas, escritas a mano: el golden de abajo las escribe el mismo
    # codigo, y no puede ser la unica defensa.
    assert (ORDER[2], ORDER[4]) == ("precheckout", "reactivacion")
    assert [(f.code, f.path) for f in FIRST_VERSIONS.warnings] == [
        ("wait_without_contesta", "/2/definicion/pasos"),
        ("always_with_marketing", "/4/definicion/horario"),
        ("silence_without_purchase_exit", "/4/definicion/termina/si"),
    ]
    assert [item.definicion for item in FIRST_VERSIONS.normalized_items] == [DEFINITIONS[key] for key in ORDER]


# ---------------------------------------------------------------------------
# 3. Cambios desde el dato real, con los cinco publicados como v1.
# ---------------------------------------------------------------------------

def _published_world() -> FlowWorld:
    return FlowWorld(
        WorldFlow(flow_id=IDS[key], product_ref="att1", flow_key=key,
                  state="apagado" if key == "reactivacion" else "prendido",
                  active_version=1, generation=1, versions={1: fd.as_published(DEFINITIONS[key], 1)})
        for key in ORDER
    )


def _change(flow_key: str, change: Callable[[dict[str, Any]], None], actor: str = "operador") -> ValidationResult:
    draft = fd.as_draft(fd.as_published(DEFINITIONS[flow_key], 1))
    change(draft)
    item = ProposalItem(producto="att1", flujo=flow_key, flow_id=IDS[flow_key], tipo="version",
                        definicion=draft, en_curso="seguir")
    return validate([item], actor=actor, world=_published_world(), instance=INSTANCE, catalog=CATALOG,
                    phase="publish")


def _codes(result: ValidationResult) -> list[tuple[str, str | None]]:
    return [(f.code, f.definition_path) for f in result.blocks]


@pytest.mark.parametrize("actor", ["automation_expert", "operador"])
def test_the_cart_daily_cap_from_80_to_90_passes_with_both_actors(actor: str) -> None:
    assert _codes(_change("carrito", lambda d: d["topes"].update(por_dia=90), actor)) == []


def test_the_cart_daily_cap_at_2000_goes_over_the_total() -> None:
    assert _codes(_change("carrito", lambda d: d["topes"].update(por_dia=2000))) == [
        ("daily_cap_exceeds_total", "/topes/por_dia")]


def test_the_automation_expert_adds_a_two_hour_wait_to_the_cart() -> None:
    result = _change("carrito", lambda d: d["pasos"].insert(0, {"esperar": "2h"}), "automation_expert")
    assert _codes(result) == []


def test_the_cart_pool_is_outside_the_1b_scope_for_the_agent_and_a_version_for_the_operator() -> None:
    def other_pool(definition: dict[str, Any]) -> None:
        definition["topes"]["bolsa"] = "att1-carrito"

    assert _codes(_change("carrito", other_pool, "automation_expert")) == [
        ("change_outside_1b_scope", "/topes/bolsa")]
    assert _codes(_change("carrito", other_pool, "operador")) == []


def test_another_coupon_code_is_not_changed_by_the_cycle() -> None:
    result = _change("descuento", lambda d: d["pasos"][0]["boton"].update(cupon="OTRO10"))
    assert _codes(result) == [("coupon_change_not_supported", "/pasos/0/boton/cupon")]


def test_a_cart_version_in_bogota_changes_the_zone_of_the_flow() -> None:
    result = _change("carrito", lambda d: d["horario"].update(zona="America/Bogota"))
    # Ademas de zone_changed: el carrito comparte la bolsa att1-recuperacion con el
    # pago fallido, que sigue en CDMX.
    assert _codes(result) == [("zone_changed", "/horario/zona"), ("pool_zone_mismatch", "/horario")]


def test_the_payment_failure_template_on_the_cart_passes_in_this_piece() -> None:
    # Aprobada, en es_MX y con dos variables: pasa. La frena la regla 16 (la plantilla
    # de [plantillas] para los flujos de hoy), que llega con el motor (pieza 2).
    result = _change("carrito", lambda d: d["pasos"][0].update(plantilla="att1_compra_fallida_01"))
    assert _codes(result) == []


# ---------------------------------------------------------------------------
# 4. El JSON que comparte con PGlite (validate_flow_store.mjs, paso 7).
# ---------------------------------------------------------------------------

def _generated(source: str) -> dict[str, str]:
    """De donde sale un JSON que escribe esta prueba: lo genera el codigo, no es una captura.

    ``date`` es el dia (UTC) en que se escribio; la comparacion con el del repo
    mira que sea una fecha, no que sea hoy.
    """
    return {
        "generator": (
            "tests/test_flow_validator_att1.py (test_the_json_shared_with_pglite_is_the_one_in_the_repo), "
            "con tests/flow_store_translator.py, bridge.flow_definition y bridge.flow_validator"
        ),
        "date": datetime.now(timezone.utc).date().isoformat(),
        "from": source,
        "regenerate": "FLOW_STORE_FIXTURE_DIR=tests/fixtures/flow_store uv run pytest tests/test_flow_validator_att1.py",
    }


def _definitions_document() -> dict[str, Any]:
    return {
        "_generated": _generated(
            "tests/flow_store_translator.py sobre att1_flow_config_20261016.json (att1-db, 2026-10-16T13:15:51Z), "
            "att1_instancia_f01fbcc.toml y schema_examples_v1.json, normalizado con bridge.flow_definition"
        ),
        "instance": {"tenant_ref": INSTANCE.tenant_ref, "ally_ref": INSTANCE.ally_ref,
                     "products": sorted(INSTANCE.products), "time_zone": INSTANCE.time_zone},
        "definitions": [item.definicion for item in FIRST_VERSIONS.normalized_items],
    }


def _validation_document() -> dict[str, Any]:
    return {
        "_generated": _generated(
            "la prueba 2: las cinco definiciones de att1_definitions_20261016.json como una propuesta de "
            "primeras versiones del operador, en ese orden, con el catalogo de la bandeja 11 del 2026-10-10"
        ),
        # p_validator sin product_version ni git_sha, que suma quien llama.
        "validator": FIRST_VERSIONS.validator_json(),
        "actor": FIRST_VERSIONS.actor,
        "phase": FIRST_VERSIONS.phase,
        "catalog_sha256": CATALOG.catalog_sha256,
        "result": FIRST_VERSIONS.to_json(),
    }


def test_the_json_shared_with_pglite_is_the_one_in_the_repo() -> None:
    documents = {
        "att1_definitions_20261016.json": _definitions_document(),
        "att1_validation_result_20261016.json": _validation_document(),
    }
    target = os.environ.get("FLOW_STORE_FIXTURE_DIR")
    if target:
        for name, document in documents.items():
            (Path(target) / name).write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n",
                                             encoding="utf-8")
        return
    for name, document in documents.items():
        in_repo = json.loads((FLOW_STORE / name).read_text(encoding="utf-8"))
        generated_on = in_repo.get("_generated", {}).pop("date", None)
        assert isinstance(generated_on, str) and date.fromisoformat(generated_on) >= date(2026, 10, 16), name
        document["_generated"].pop("date")
        assert in_repo == document, f"{name} no es el de hoy: regenerarlo con FLOW_STORE_FIXTURE_DIR"
    assert documents["att1_validation_result_20261016.json"]["validator"] == {
        "contract": VALIDATOR_CONTRACT, "rules_version": RULES_VERSION, "phase": "publish", "actor": "operador"}
