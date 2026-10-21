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
* ``att1_flow_config_20261021.json`` (T2.1): la misma consulta el 2026-10-21 a
  las 15:43:39Z, con el carrito en ``att1-recuperacion`` v7 (tres toques) y el
  pago fallido en ``att1-pago-rechazado`` v2. Las plantillas de los toques salen
  del parametro ``ATT1_TOUCHES_20261021`` del traductor, y su catalogo de las
  capturas de Chatwoot del 18 y el 19/10 (``chatwoot_message_templates_inbox_11_*``).
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
         "automatic_messages_and_steps_differ"),
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


# ---------------------------------------------------------------------------
# 5. La captura del 21/10 (T2.1): los varios toques.
# ---------------------------------------------------------------------------

CONFIG_21: dict[str, Any] = json.loads(
    (FLOW_STORE / "att1_flow_config_20261021.json").read_text(encoding="utf-8"))
TRANSLATED_21 = TRANSLATOR.translate_att1(CONFIG_21, MANIFEST, BLOCKS, TRANSLATOR.ATT1_TOUCHES_20261021)
DEFINITIONS_21 = {key: normalize(translated.definition) for key, translated in TRANSLATED_21.items()}
EVENT_FLOWS = ("carrito", "pago_fallido", "precheckout")


def _templates_of(name: str) -> list[dict[str, Any]]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return list(data["message_templates"] if isinstance(data, dict) else data)


# El catalogo de la bandeja 11 con las plantillas de los toques: el del 10/10 mas las capturas
# del carrito (18/10) y del seguimiento del pago rechazado (19/10). Una plantilla repetida
# (att1_ultima_renovacion_01 esta en las dos) entra una vez.
_CATALOG_21_TEMPLATES: dict[str, dict[str, Any]] = {}
for _name in ("chatwoot_inbox_11_message_templates_20261010.json",
              "chatwoot_message_templates_inbox_11_carrito_20261018.json",
              "chatwoot_message_templates_inbox_11_pago_rechazado_seguimiento_20261019.json"):
    for _template in _templates_of(_name):
        _CATALOG_21_TEMPLATES.setdefault(f"{_template['name']}/{_template['language']}", _template)
CATALOG_21 = TemplateCatalog.from_inbox_payload({"message_templates": list(_CATALOG_21_TEMPLATES.values())})


def _scope_21(scope_key: str) -> dict[str, Any]:
    [control] = [c for c in CONFIG_21["pilot_runtime_controls"] if c["scope_key"] == scope_key]
    [scope] = [s for s in CONFIG_21["pilot_scope_versions"]
               if s["scope_key"] == scope_key and s["version"] == control["scope_version"]]
    return scope


def test_the_translation_of_21_10_is_the_cart_v7_and_the_payment_failure_v2() -> None:
    recovery, failure = _scope_21("att1-recuperacion"), _scope_21("att1-pago-rechazado")
    assert (recovery["version"], recovery["policy_key"]) == (7, "att1-carrito-tres-toques")
    assert (failure["version"], failure["policy_key"]) == (2, "att1-pago-rechazado-dos-toques")
    cart, payment = TRANSLATED_21["carrito"].definition, TRANSLATED_21["pago_fallido"].definition
    assert [step.get("plantilla", step.get("esperar")) for step in cart["pasos"]] == [
        "att1_carrito_descuento_01", "24h", "att1_carrito_dudas_01", "48h", "att1_ultima_renovacion_01"]
    assert [step.get("boton") for step in cart["pasos"][::2]] == [
        {"link": "checkout", "cupon": "personal"}, None, {"link": "checkout", "cupon": "personal"}]
    assert cart["termina"]["vence"] == "5d"
    assert (cart["topes"]["bolsa"], cart["topes"]["por_dia"], cart["topes"]["total"]) == (
        "att1-recuperacion", 240, 5000)
    assert [step.get("plantilla", step.get("esperar")) for step in payment["pasos"]] == [
        "15m", "att1_compra_fallida_01", "24h", "att1_pago_rechazado_descuento_01"]
    assert payment["termina"]["vence"] == "3d"
    # Ya no comparten bolsa: el pago fallido cuenta en su scope.
    assert (payment["topes"]["bolsa"], payment["topes"]["por_dia"], payment["topes"]["total"]) == (
        "att1-pago-rechazado", 10, 200)
    # att1-recuperacion v7 sigue nombrando PURCHASE_CANCELED, pero el pago fallido sale de su
    # scope propio (src/bridge/app.py:254-257).
    assert recovery["additional_source_event_types"] == ["PURCHASE_CANCELED"]
    assert failure["source_event_type"] == "PURCHASE_CANCELED"
    assert TRANSLATED_21["pago_fallido"].sources["/dispara/evento"] == (
        "dato real: pilot_scope_versions.source_event_type")
    # El primer contacto, como el 16/10.
    assert TRANSLATED_21["precheckout"].definition == TRANSLATED["precheckout"].definition


@pytest.mark.parametrize("flow_key", EVENT_FLOWS)
def test_every_field_of_21_10_says_where_it_comes_from(flow_key: str) -> None:
    translated = TRANSLATED_21[flow_key]
    assert set(_leaves(translated.definition)) == set(translated.sources)
    for source in translated.sources.values():
        assert source in (TRANSLATOR.SOURCE_CODE, TRANSLATOR.TOUCHES_SOURCE) or source.startswith(
            (TRANSLATOR.SOURCE_REAL + ": ", TRANSLATOR.SOURCE_MANIFEST + ": ", TRANSLATOR.SOURCE_BRIDGE + ": ")), source
    if flow_key == "carrito":
        assert translated.sources["/pasos/3/esperar"] == (
            "dato real: followup_policy_versions.steps[cart_last_touch].delay")
        assert translated.sources["/pasos/4/boton/cupon"] == (
            "dato real: followup_policy_versions.steps[cart_last_touch].coupon_ordinal")


def test_without_the_touches_the_translation_of_21_10_fails_loudly() -> None:
    with pytest.raises(TRANSLATOR.TranslationError, match="touch_templates_missing:carrito"):
        TRANSLATOR.translate_att1(CONFIG_21, MANIFEST, BLOCKS)


def test_a_touch_whose_coupon_is_not_the_policys_fails_loudly() -> None:
    touches = dict(TRANSLATOR.ATT1_TOUCHES_20261021)
    a1, a2, z = touches["carrito"]
    touches["carrito"] = (a1, a2, TRANSLATOR.Touch(z.template, z.language, False, z.variables, z.variables_from))
    with pytest.raises(TRANSLATOR.TranslationError, match="touch_coupon_and_policy_differ:cart_last_touch"):
        TRANSLATOR.translate_att1(CONFIG_21, MANIFEST, BLOCKS, touches)


def _first_versions_21(keys: tuple[str, ...] = EVENT_FLOWS) -> list[ProposalItem]:
    return [ProposalItem(producto="att1", flujo=key, flow_id=IDS[key], tipo="version",
                         definicion=TRANSLATED_21[key].definition, en_curso="seguir") for key in keys]


def test_the_v7_validates_without_blocks_as_a_draft_and_the_engine_runs_it() -> None:
    from bridge.flow_engine import ENGINE_CAPABILITIES_V1, not_supported

    result = validate(_first_versions_21(), actor="operador", world=_registered_world(), instance=INSTANCE,
                      catalog=CATALOG_21, phase="draft")
    assert result.blocks == ()
    # La regla 14, con las capacidades de la T2.1: tres envios, la espera entre ellos y el cupon.
    for key in EVENT_FLOWS:
        assert not_supported(DEFINITIONS_21[key], ENGINE_CAPABILITIES_V1) == (), key


def test_the_v7_publishes_without_blocks_against_the_captured_catalog() -> None:
    # Con las variables de la 1.1 (cuando, precio y vence), cada toque declara las de su plantilla:
    # A1 cinco, A2 dos, y Z y B2 tres. La regla 13 las cuenta contra el catalogo capturado.
    result = validate(_first_versions_21(), actor="operador", world=_registered_world(), instance=INSTANCE,
                      catalog=CATALOG_21, phase="publish")
    assert result.blocks == ()
    cart, payment = TRANSLATED_21["carrito"].definition, TRANSLATED_21["pago_fallido"].definition
    assert [step["variables"] for step in cart["pasos"][::2]] == [
        ["nombre", "producto", "cuando", "precio", "vence"], ["nombre", "producto"], ["nombre", "producto", "vence"]]
    assert payment["pasos"][3]["variables"] == ["nombre", "producto", "vence"]
    # Las variables salen de las constantes del bridge, que existen con esos valores.
    from bridge.cart_first_touch import CART_DISCOUNT_VARIABLES
    from bridge.cart_followups import CART_LAST_TOUCH_VARIABLES, CART_SECOND_TOUCH_VARIABLES
    from bridge.payment_failure_followups import PAYMENT_FAILURE_FOLLOWUP_VARIABLES

    assert (CART_DISCOUNT_VARIABLES, CART_SECOND_TOUCH_VARIABLES, CART_LAST_TOUCH_VARIABLES,
            PAYMENT_FAILURE_FOLLOWUP_VARIABLES) == (
        TRANSLATOR.A1_VARIABLES, TRANSLATOR.A2_VARIABLES, TRANSLATOR.LAST_TOUCH_VARIABLES,
        TRANSLATOR.LAST_TOUCH_VARIABLES)


def test_the_red_of_vence_without_the_personal_coupon() -> None:
    # Z sin el cupon personal pero con vence: el bridge no tiene que vencimiento poner.
    result = _change_21(lambda d: d["pasos"][4].pop("boton"))
    assert ("variable_needs_personal_coupon", "/pasos/4/variables/2") in _codes(result)


def _published_world_21() -> FlowWorld:
    return FlowWorld(
        WorldFlow(flow_id=IDS[key], product_ref="att1", flow_key=key,
                  state="apagado" if key == "reactivacion" else "prendido",
                  active_version=1, generation=1, versions={1: fd.as_published(
                      (DEFINITIONS_21 if key in EVENT_FLOWS else DEFINITIONS)[key], 1)})
        for key in ORDER
    )


def _change_21(change: Callable[[dict[str, Any]], None]) -> ValidationResult:
    draft = fd.as_draft(fd.as_published(DEFINITIONS_21["carrito"], 1))
    change(draft)
    item = ProposalItem(producto="att1", flujo="carrito", flow_id=IDS["carrito"], tipo="version",
                        definicion=draft, en_curso="seguir")
    return validate([item], actor="operador", world=_published_world_21(), instance=INSTANCE,
                    catalog=CATALOG_21, phase="publish")


def test_a2_at_36_hours_only_changes_the_waits() -> None:
    def later(definition: dict[str, Any]) -> None:
        definition["pasos"][1]["esperar"] = "36h"
        definition["pasos"][3]["esperar"] = "36h"

    assert _codes(_change_21(later)) == []


def test_the_red_of_a_third_personal_coupon() -> None:
    def third(definition: dict[str, Any]) -> None:
        definition["pasos"][2]["boton"] = {"link": "checkout", "cupon": "personal"}

    # A2 no tiene boton de URL en el catalogo (13) y es el tercer cupon personal (15).
    assert ("coupon_personal_limit", "/pasos/4/boton/cupon") in _codes(_change_21(third))


def test_the_red_of_a_fixed_code_instead_of_the_personal_one() -> None:
    result = _change_21(lambda d: d["pasos"][4]["boton"].update(cupon="TIROIDES10"))
    assert ("coupon_change_not_supported", "/pasos/4/boton/cupon") in _codes(result)
    # Y al reves: un codigo fijo que pasa a personal en el seguimiento con cupon.
    back = _change("descuento", lambda d: d["pasos"][0]["boton"].update(cupon="personal"))
    assert _codes(back) == [("coupon_change_not_supported", "/pasos/0/boton/cupon")]


def test_the_red_of_a_fourth_send() -> None:
    from bridge.flow_engine import not_supported

    def fourth(definition: dict[str, Any]) -> None:
        definition["pasos"].extend([{"esperar": "24h"}, copy.deepcopy(definition["pasos"][2])])

    draft = copy.deepcopy(DEFINITIONS_21["carrito"])
    fourth(draft)
    assert not_supported(draft) == ("engine_not_supported:demasiados_envios",)

