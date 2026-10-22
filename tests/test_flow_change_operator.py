"""El modulo que corre el validador para el comando del operador (src/bridge/flow_change_operator.py).

Fixtures:

* las definiciones de ATT1 generadas el 2026-10-16
  (tests/fixtures/flow_store/att1_definitions_20261016.json);
* el catalogo de la bandeja 11 de ATT1 leido de la base de Chatwoot
  (``channel_whatsapp.message_templates``) el 2026-10-21 en una sesion de
  solo lectura (tests/fixtures/flow_change_operator/), que es de donde lo lee
  el comando: tiene la misma forma que ``GET /inboxes/11``.
"""

from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from bridge import flow_change_operator as op
from bridge.flow_definition import TemplateCatalog

ROOT = Path(__file__).resolve().parents[1]
DEFINITIONS = {d["flujo"]: d for d in json.loads(
    (ROOT / "tests/fixtures/flow_store/att1_definitions_20261016.json").read_text(encoding="utf-8"))["definitions"]}
CATALOG = json.loads((ROOT / "tests/fixtures/flow_change_operator/chatwoot_db_message_templates_inbox_11_20261021.json")
                     .read_text(encoding="utf-8"))
KEYS = ("carrito", "pago_fallido", "precheckout")
IDS = {key: f"0000000{index}-0000-4000-8000-0000000a7701" for index, key in enumerate(KEYS, start=1)}
IMAGE = {"SETTER_VERSION": "1.15.5", "GIT_SHA": "b163669f57bfdafbac6cb19d82dd357c83c3c1e8"}
INSTANCE = {"tenant_ref": "lancemos", "ally_ref": "att1", "products": ["att1"], "time_zone": "America/Mexico_City"}


def store(*, published: bool = True) -> dict:
    flows = []
    for key in KEYS:
        versions = {}
        if published:
            definition = copy.deepcopy(DEFINITIONS[key])
            definition.update(version=1, estado="publicada")
            versions = {"1": definition}
        flows.append({"flow_id": IDS[key], "product_ref": "att1", "flow_key": key,
                      "state": "prendido" if published else "apagado",
                      "active_version": 1 if published else None, "generation": 3 if published else 0,
                      "versions": versions})
    return {"flows": flows}


def cart_wait_item() -> dict:
    definition = copy.deepcopy(DEFINITIONS["carrito"])
    definition["pasos"] = [{"esperar": "2h"}, *definition["pasos"]]
    return {"producto": "att1", "flujo": "carrito", "flow_id": IDS["carrito"], "tipo": "version",
            "definicion": definition, "en_curso": "seguir"}


def payload(items: list[dict], *, kind: str = "automation_expert", catalog: object = CATALOG) -> dict:
    return {"instance": copy.deepcopy(INSTANCE), "store": store(), "catalog": catalog, "proposal": {
        "id": "11111111-0000-4000-8000-000000000001", "revision": 1, "items": items,
        "draft_base": {IDS["carrito"]: {"active_version": 1}}, "purpose": "que el carrito espere 2 horas",
        "requested_by_kind": kind}}


def run(args: list[str], body: object, environ: dict | None = None) -> tuple[int, dict]:
    out = io.StringIO()
    code = op.main(args, stdin=io.StringIO(json.dumps(body)), stdout=out, environ=IMAGE if environ is None else environ)
    return code, json.loads(out.getvalue())


def test_a_wait_in_the_cart_validates_with_what_the_base_asks_to_record() -> None:
    code, out = run(["validar"], payload([cart_wait_item()]))
    assert code == op.EXIT_OK and out["ok"] is True and out["blocks"] == 0
    # p_validated_base: todos los flujos, con la generacion solo de los de la propuesta.
    assert out["validated_base"] == {IDS["carrito"]: {"active_version": 1, "generation": 3},
                                     IDS["pago_fallido"]: {"active_version": 1},
                                     IDS["precheckout"]: {"active_version": 1}}
    # Las seis claves que exige _flow_change_validator_is_valid, con la identidad de la imagen.
    assert out["validator"] == {"contract": "setter-flujo/v1", "rules_version": 1, "phase": "publish",
                                "actor": "automation_expert", "product_version": "1.15.5",
                                "git_sha": IMAGE["GIT_SHA"]}
    assert out["catalog_sha256"] == TemplateCatalog.from_inbox_payload(CATALOG).catalog_sha256
    assert [w["code"] for w in out["result"]["warnings"]] == ["wait_without_contesta"]
    assert {"ruta": "/pasos/0/esperar", "antes": None, "despues": "2h"} in out["preview"]["cambios"][0]["diff"]


def test_a_template_that_is_not_in_the_catalog_blocks_and_exits_3() -> None:
    item = cart_wait_item()
    item["definicion"]["pasos"][1]["plantilla"] = "att1_no_existe_01"
    code, out = run(["validar"], payload([item]))
    assert code == op.EXIT_BLOCKED and out["ok"] is False and out["blocks"] >= 1


def test_without_a_catalog_it_validates_but_there_is_nothing_to_publish_with() -> None:
    code, out = run(["validar"], payload([cart_wait_item()], catalog=None))
    assert out["catalog_sha256"] is None
    assert code == op.EXIT_BLOCKED and out["ok"] is False


def test_the_image_identity_comes_from_the_image_not_from_the_caller() -> None:
    code, out = run(["validar"], payload([cart_wait_item()]), environ={})
    assert code == op.EXIT_INPUT and out == {"ok": False, "reason_code": "operator_image_identity_missing"}


@pytest.mark.parametrize(("mutate", "reason"), [
    (lambda body: body["proposal"].update(requested_by_kind="slack"), "operator_proposal_invalid"),
    (lambda body: body["proposal"].update(items=[]), "flow_change_items_invalid"),
    (lambda body: body["store"]["flows"][0].update(state="raro"), "operator_store_invalid"),
    (lambda body: body["instance"].update(products=[]), "operator_instance_invalid"),
    (lambda body: body["proposal"]["items"][0].update(flow_id="99999999-0000-4000-8000-000000000009"),
     "flow_not_found"),
])
def test_input_that_does_not_fit_is_a_closed_code(mutate, reason: str) -> None:
    body = payload([cart_wait_item()])
    mutate(body)
    code, out = run(["validar"], body)
    assert code == op.EXIT_INPUT and out == {"ok": False, "reason_code": reason}


def test_normalizar_gives_the_items_the_base_accepts_for_a_first_version() -> None:
    items = [{"producto": "att1", "flujo": key, "flow_id": IDS[key], "tipo": "version",
              "definicion": DEFINITIONS[key], "en_curso": "seguir"} for key in KEYS]
    body = {"instance": INSTANCE, "store": store(published=False), "items": items, "catalog": CATALOG}
    code, out = run(["normalizar"], body, environ={})
    assert code == op.EXIT_OK and out["blocks"] == 0
    assert out["draft_base"] == {IDS[key]: {"active_version": None} for key in KEYS}
    assert [item["flujo"] for item in out["items"]] == list(KEYS)


def test_unknown_subcommand_is_usage() -> None:
    code, out = run(["publicar"], {})
    assert code == op.EXIT_INPUT and out["reason_code"] == "operator_usage"


def test_it_runs_as_a_module_like_in_the_fresh_container() -> None:
    result = subprocess.run([sys.executable, "-m", "bridge.flow_change_operator", "validar"],
                            input=json.dumps(payload([cart_wait_item()])), text=True, capture_output=True,
                            env={"PYTHONPATH": str(ROOT / "src"), **IMAGE}, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True


def test_volver_to_a_version_the_engine_read_does_not_bring_validates_with_the_simulation_built() -> None:
    # ATT1, 22/10 19:45Z: ``volver att1/carrito 1`` con la v2 activa daba ``simulation_failed`` con
    # ``proposal_version_unknown``. La foto del almacen que ya lee quien valida trae la v1, y la simulacion sale de ahi.
    case = json.loads((ROOT / "tests/fixtures/flow_simulation/att1_volver_carrito_20261022.json")
                      .read_text(encoding="utf-8"))
    history = json.loads((ROOT / "tests/fixtures/flow_engine/att1_recovery_capture_v3_20261021.json")
                         .read_text(encoding="utf-8"))
    cart_id = case["item"]["flow_id"]
    body = {"instance": copy.deepcopy(INSTANCE), "store": case["store"], "catalog": CATALOG,
            "engine": case["engine"], "history": history, "proposal": {
                "id": "33ceb395-63b2-4c22-a728-e9feef1b95cd", "revision": 1, "items": [case["item"]],
                "draft_base": {cart_id: {"active_version": 2}}, "purpose": "volver el carrito a la v1",
                "requested_by_kind": "operador"}}
    code, out = run(["validar"], body)
    assert code == op.EXIT_OK and out["ok"] is True, out["result"]
    assert out["preview"]["simulacion"]["status"] == "built"
    assert out["preview"]["simulacion"]["summary"]["cambios"] == 52
