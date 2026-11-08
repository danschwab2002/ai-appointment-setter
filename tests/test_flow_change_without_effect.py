"""Que el AE no publique un cambio que no cambia ningun envio (E-353; flow-change-cycle-v1 1.11.0).

Dos fuentes de lo que un flujo ejecuta de verdad:

* la ruta, que sale de ``read_flow_store_v1`` (migracion 20261108000100). El fixture
  ``flow_change/store_route_executor_e353.json`` es lo que contesta la base en PGlite con el mundo de ATT1
  (``validate_flow_store_route_executor.mjs --check`` lo compara): el carrito en motor, el pago en legado,
  la bienvenida nativa y un carrito sin ruta;
* lo que corre el bridge de la instancia (``InstanceRuntime``, de su configuracion).

De los flags de hoy, el vault registra al 2026-11-02 (config-viva) solo que Maribel no tiene
``FLOW_ENGINE_NATIVE_INTENT_ENABLED``. El resto es un supuesto de la prueba: en ATT1 el mismo flag apagado, que
es el caso mas estricto, y en las dos el motor prendido y el barredor armado. Nada se midio en el VPS.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_flow_change_api_app import (
    AUTH,
    DB_KEY,
    PREFIX,
    SLACK,
    FakeChatwoot,
    PostgrestEmulator,
    SharedSupabaseSpy,
    _client,
    _settings,
)
from test_flow_approval_writes import PROPOSAL, _done, _page, _post, _publish_fields, _signed
from test_flow_change_new_flow import _new_item, _propose, image  # noqa: F401

from bridge import flow_change_api
from bridge.app import create_app
from bridge.flow_change_operator import OperatorInputError, instance_from_input, world_from_store
from bridge.flow_definition import InstanceRuntime, InstanceView, ProposalItem
from bridge.flow_validator import validate

STORE = json.loads((Path(__file__).parent / "fixtures/flow_change/store_route_executor_e353.json")
                   .read_text(encoding="utf-8"))
BY_KEY = {f"{flow['product_ref']}/{flow['flow_key']}": flow for flow in STORE["flows"]}
# El caso mas estricto de hoy: el primer contacto nativo sin prender; el motor y el barredor, en marcha.
ATT1_TODAY = InstanceRuntime(engine_enabled=True, native_intent_enabled=False, native_silence_sweeper=True,
                             cart_touches=True, main_product="att1")
MARIBEL_TODAY = InstanceRuntime(engine_enabled=True, native_intent_enabled=False, native_silence_sweeper=True,
                                cart_touches=False, main_product="maribel")
NO_EFFECT = {"change_without_effect_legacy_route", "native_engine_not_running", "native_first_contact_not_running",
             "native_silence_not_running", "native_trigger_other_product"}
ALL_ON = InstanceRuntime(engine_enabled=True, native_intent_enabled=True, native_silence_sweeper=True,
                         cart_touches=True, main_product="att1")


def _instance(runtime: InstanceRuntime | None, products: tuple[str, ...] = ("att1", "att9")) -> InstanceView:
    return InstanceView("lancemos", "att1", frozenset(products), "America/Mexico_City", runtime=runtime)


def _blocked(items: list[ProposalItem], *, world: Any = None, runtime: InstanceRuntime | None = ATT1_TODAY,
             actor: str = "automation_expert", phase: str = "draft") -> set[str]:
    products = tuple({item.producto for item in items}) or ("att1",)
    result = validate(items, actor=actor, world=world or world_from_store(STORE),
                      instance=_instance(runtime, products), catalog=None, phase=phase)
    return {finding.code for finding in result.blocks} & NO_EFFECT


def _version(key: str) -> ProposalItem:
    flow = BY_KEY[key]
    definition = copy.deepcopy(flow["versions"][str(flow["active_version"])])
    for field in ("version", "estado"):
        definition.pop(field, None)
    return ProposalItem(producto=flow["product_ref"], flujo=flow["flow_key"], flow_id=flow["flow_id"], tipo="version",
                        definicion=definition, en_curso="seguir")


def _state(key: str, tipo: str) -> ProposalItem:
    flow = BY_KEY[key]
    return ProposalItem(producto=flow["product_ref"], flujo=flow["flow_key"], flow_id=flow["flow_id"], tipo=tipo)


# ------------------------------------------------------------------ la ruta


def test_a_version_of_a_legacy_flow_on_the_legacy_route_is_blocked_with_the_reason_for_the_owner() -> None:
    world = world_from_store(STORE)
    result = validate([_version("att1/pago_fallido")], actor="automation_expert", world=world,
                      instance=_instance(ATT1_TODAY, ("att1",)), catalog=None, phase="draft")
    [finding] = [f for f in result.blocks if f.code == "change_without_effect_legacy_route"]
    assert finding.path == "/0"
    assert "los mensajes que salen no cambiarían" in finding.reason
    assert "Decile al dueño" in finding.alternative and "No se arregla cambiando la propuesta" in finding.alternative


def test_a_version_of_a_flow_with_no_route_is_blocked() -> None:
    assert _blocked([_version("att9/carrito")], runtime=None) == {"change_without_effect_legacy_route"}


def test_the_operator_can_still_prepare_a_version_on_the_legacy_route() -> None:
    assert _blocked([_version("att1/pago_fallido")], actor="operador") == set()


def test_a_version_of_the_cart_on_the_engine_route_is_not_blocked() -> None:
    assert _blocked([_version("att1/carrito")]) == set()


def test_without_the_route_key_it_is_not_blocked_because_it_does_not_know() -> None:
    # La base todavia sin 20261108000100: el bridge nuevo sobre la base vieja publica como hoy.
    stale = {"flows": [{key: value for key, value in flow.items() if key != "route_executor"}
                       for flow in STORE["flows"]]}
    assert _blocked([_version("att1/pago_fallido")], world=world_from_store(stale)) == set()


def test_turning_a_legacy_flow_on_or_off_is_not_blocked_because_it_does_change_what_is_sent() -> None:
    assert _blocked([_state("att1/pago_fallido", "apagar")]) == set()
    paused = copy.deepcopy(STORE)
    paused["flows"][1]["state"] = "pausado"
    assert _blocked([_state("att1/pago_fallido", "prender")], world=world_from_store(paused)) == set()


# ------------------------------------------------------------------ lo que corre la instancia


def _native_flow(trigger: dict[str, Any], state: str, *, product: str = "att1", key: str = "mi_flujo") -> dict[str, Any]:
    return {"flow_id": "0000000a-0000-4000-8000-0000000e3530", "product_ref": product, "flow_key": key, "state": state,
            "active_version": 1, "generation": 1, "native": True, "route_executor": "motor",
            "versions": {"1": {"dispara": trigger, "esquema": "setter-flujo/v1"}}}


INTENT = {"evento": "intencion"}
SILENCE = {"silencio": {"lo_ultimo": "agente", "desde": "24h", "hasta": "72h", "conversaciones": ["abierta"]}}


def _switch(trigger: dict[str, Any], tipo: str = "prender", *, product: str = "att1") -> tuple[ProposalItem, Any]:
    flow = _native_flow(trigger, "pausado", product=product)
    world = world_from_store({"flows": [flow]})
    return ProposalItem(producto=product, flujo="mi_flujo", flow_id=flow["flow_id"], tipo=tipo), world


@pytest.mark.parametrize(("trigger", "runtime", "code"), [
    (INTENT, ATT1_TODAY, "native_first_contact_not_running"),
    (SILENCE, replace(ALL_ON, native_silence_sweeper=False), "native_silence_not_running"),
    (INTENT, replace(ALL_ON, engine_enabled=False), "native_engine_not_running"),
    ({"evento": "compra"}, replace(ALL_ON, engine_enabled=False), "native_engine_not_running"),
])
def test_turning_on_a_native_flow_that_the_instance_does_not_run_is_blocked(trigger, runtime, code) -> None:
    item, world = _switch(trigger)
    assert _blocked([item], world=world, runtime=runtime) == {code}


def test_the_silence_only_runs_on_the_main_product() -> None:
    item, world = _switch(SILENCE, product="att9")
    assert _blocked([item], world=world, runtime=ALL_ON) == {"native_trigger_other_product"}


def test_the_first_contact_of_another_product_runs_with_its_flag() -> None:
    # E-350: con el flag, el formulario de un producto que no es el principal entra a sus flujos nativos.
    item, world = _switch(INTENT, product="att9")
    assert _blocked([item], world=world, runtime=ALL_ON) == set()


@pytest.mark.parametrize("runtime", [replace(ALL_ON, cart_touches=False), replace(ALL_ON, engine_enabled=False)])
def test_a_version_of_the_cart_on_the_engine_route_is_blocked_if_this_bridge_sends_it_by_the_legacy(runtime) -> None:
    assert _blocked([_version("att1/carrito")], runtime=runtime) == {"change_without_effect_legacy_route"}


def test_a_version_of_the_cart_with_an_unknown_runtime_is_not_blocked_on_the_engine_route() -> None:
    assert _blocked([_version("att1/carrito")], runtime=None) == set()


def test_a_new_first_contact_without_the_flag_is_blocked_before_being_saved(image: None) -> None:
    emulator = PostgrestEmulator()
    client, _ = _client(emulator, runtime=ATT1_TODAY)
    definition = copy.deepcopy(_new_item()["definicion"])
    definition.update(dispara=INTENT, entra={"permiso": "formulario"})
    item = {**_new_item(), "flujo": "primer_contacto", "definicion": definition}
    saved = _propose(client, [item])
    assert saved.status_code == 422, saved.text
    blocks = {block["code"]: block for block in saved.json()["data"]["blocks"]}
    assert "native_first_contact_not_running" in blocks
    assert "los formularios seguirían saliendo como hoy" in blocks["native_first_contact_not_running"]["reason"]
    assert "Decile al dueño" in blocks["native_first_contact_not_running"]["alternative"]


def test_the_runtime_comes_from_the_configuration_of_the_bridge(image: None) -> None:
    # Sin inyectar nada: esta app no tiene el motor, asi que el AE no puede crear un flujo nativo.
    emulator = PostgrestEmulator()
    store = flow_change_api.FlowChangeStore(base_url="https://att1-rest.internal", db_key=DB_KEY,
                                            transport=httpx.MockTransport(emulator.handler))
    app = create_app(_settings(), supabase_client=SharedSupabaseSpy(), chatwoot_client=FakeChatwoot(),
                     flow_change_store=store)
    saved = _propose(TestClient(app), [_new_item()])
    assert saved.status_code == 422, saved.text
    assert "native_engine_not_running" in {block["code"] for block in saved.json()["data"]["blocks"]}


def test_a_flag_turned_off_after_validating_does_not_publish_from_the_page(image: None) -> None:
    # La propuesta ya validada es un flujo nativo nuevo: la pagina revalida con lo que corre hoy y no publica.
    off = InstanceRuntime(engine_enabled=False, native_intent_enabled=False, native_silence_sweeper=False,
                          cart_touches=False, main_product="att1")
    client, db = _signed(runtime=off)
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))
    assert _done(response) == {"hecho": "revalidation_blocked"}
    assert db.writes == []


def test_the_same_proposal_publishes_from_the_page_when_the_instance_runs_it(image: None) -> None:
    client, db = _signed()
    response = _post(client, f"/flow-approval/p/{PROPOSAL}/publish", _publish_fields(_page(client)))
    assert _done(response)["hecho"] == "publicada"
    assert ("approver_publish_flow_change_v1" in [name for name, _ in db.writes])


def test_the_runtime_of_the_input_is_strict() -> None:
    base = {"tenant_ref": "lancemos", "ally_ref": "att1", "products": ["att1"], "time_zone": "America/Mexico_City"}
    assert instance_from_input(base).runtime is None
    parsed = instance_from_input({**base, "runtime": ALL_ON.to_json()})
    assert parsed.runtime == ALL_ON
    for broken in ({"engine_enabled": True}, {**ALL_ON.to_json(), "engine_enabled": "si"}, "x"):
        with pytest.raises(OperatorInputError):
            instance_from_input({**base, "runtime": broken})


# ------------------------------------------------------------------ lo que hoy publica y hace efecto


@pytest.mark.parametrize("key", ["att1/carrito", "att1/bienvenida"])
def test_with_todays_flags_of_att1_what_has_effect_keeps_publishing(key) -> None:
    assert _blocked([_version(key)], phase="publish") == set()


def test_with_todays_flags_of_att1_the_new_welcome_and_the_silence_keep_publishing() -> None:
    for trigger in ({"evento": "compra"}, {"evento": "carrito"}, {"evento": "pago_fallido"}, SILENCE):
        item, world = _switch(trigger)
        assert _blocked([item], world=world, runtime=ATT1_TODAY, phase="publish") == set(), trigger


def test_with_todays_flags_of_maribel_what_has_effect_keeps_publishing() -> None:
    for trigger in ({"evento": "compra"}, {"evento": "pago_fallido"}, SILENCE):
        item, world = _switch(trigger, product="maribel")
        assert _blocked([item], world=world, runtime=MARIBEL_TODAY, phase="publish") == set(), trigger


def test_the_real_store_of_att1_of_the_22_oct_without_the_key_changes_nothing() -> None:
    # La foto capturada de produccion, anterior a la migracion, con el carrito publicado: una version de ese
    # carrito se bloquearia si la clave ausente se leyera como «sin ruta».
    real = json.loads((Path(__file__).parent / "fixtures/flow_change/att1_read_flow_store_v1_20261022.json")
                      .read_text(encoding="utf-8"))["response"]
    assert all("route_executor" not in flow for flow in real["flows"])
    cart = next(flow for flow in real["flows"] if flow["flow_key"] == "carrito")
    assert cart["active_version"] == 1
    definition = copy.deepcopy(cart["versions"]["1"])
    for field in ("version", "estado"):
        definition.pop(field, None)
    item = ProposalItem(producto=cart["product_ref"], flujo="carrito", flow_id=cart["flow_id"], tipo="version",
                        definicion=definition, en_curso="seguir")
    result = validate([item], actor="automation_expert", world=world_from_store(real),
                      instance=_instance(ATT1_TODAY, ("att1",)), catalog=None, phase="draft")
    assert not {finding.code for finding in result.blocks} & NO_EFFECT
