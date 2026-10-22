"""``bridge.flow_route_revalidation``: la revalidacion de mover una ruta a ``motor`` (E-020).

Corre sobre la lectura real del almacen de ATT1 del 22/10
(``tests/fixtures/flow_change/att1_read_flow_store_v1_20261022.json``) y el
catalogo real de la bandeja 11 del 21/10. La forma de la salida es la que
exige ``_flow_engine_revalidation_is_valid`` (20261021000200:339): seis claves
exactas, ``rules_version`` 2 o mas, ``capabilities`` ``v1``, ``blocks`` vacia
para pasar y el validador con sus seis claves.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path

from bridge import flow_route_revalidation as module

ROOT = Path(__file__).resolve().parents[1]
STORE = json.loads((ROOT / "tests/fixtures/flow_change/att1_read_flow_store_v1_20261022.json").read_text())["response"]
CATALOG = json.loads((ROOT / "tests/fixtures/flow_change_operator/"
                      "chatwoot_db_message_templates_inbox_11_20261021.json").read_text())["message_templates"]
ENV = {"SETTER_VERSION": "1.16.3", "GIT_SHA": "b4d6027"}
SHA = "e" * 64


def payload(**changes: object) -> dict:
    base = {"instance": {"tenant_ref": "lancemos", "ally_ref": "att1", "products": ["att1"],
                         "time_zone": "America/Mexico_City"},
            "store": {"flows": copy.deepcopy(STORE["flows"])},
            "flow": {"product_ref": "att1", "flow_key": "carrito", "legacy_kind": "carrito", "definition_sha256": SHA},
            "catalog": {"message_templates": CATALOG}}
    base.update(changes)
    return base


def call(data: object, environ: dict | None = None) -> tuple[int, dict]:
    out = io.StringIO()
    code = module.main(["revalidar"], stdin=io.StringIO(json.dumps(data)), stdout=out,
                       environ=ENV if environ is None else environ)
    return code, json.loads(out.getvalue())


def cart_of(data: dict) -> dict:
    return next(flow for flow in data["store"]["flows"] if flow["flow_key"] == "carrito")["versions"]["1"]


def test_the_active_cart_of_att1_passes_with_the_exact_shape_the_base_asks() -> None:
    code, out = call(payload())
    assert code == module.EXIT_OK, out
    revalidation = out["revalidation"]
    assert set(revalidation) == {"version", "definition_sha256", "rules_version", "capabilities", "blocks",
                                 "validator"}
    assert revalidation["version"] == 1 and revalidation["definition_sha256"] == SHA
    assert revalidation["rules_version"] == module.ENGINE_RULES_VERSION >= 2
    assert revalidation["capabilities"] == "v1" and revalidation["blocks"] == []
    assert set(revalidation["validator"]) == {"contract", "rules_version", "phase", "actor", "product_version",
                                              "git_sha"}
    assert revalidation["validator"]["phase"] == "publish" and revalidation["validator"]["actor"] == "operador"
    assert (revalidation["validator"]["product_version"], revalidation["validator"]["git_sha"]) == ("1.16.3", "b4d6027")


def test_what_the_engine_does_not_run_blocks() -> None:
    data = payload()
    # Un conversar: la regla 14 del lado del motor (not_supported) y del validador.
    cart_of(data)["pasos"].insert(1, {"conversar": {"objetivo": "resolver dudas"}})
    code, out = call(data)
    assert code == module.EXIT_BLOCKED and out["ok"] is False
    assert out["revalidation"]["blocks"], out


def test_the_legacy_shape_is_checked_with_the_legacy_kind_of_the_route() -> None:
    data = payload()
    # Un cuarto envio: el carrito del legado tiene tres pasos (regla 14, que compile a su politica).
    cart_of(data)["pasos"] += [{"esperar": "24h"}, {"idioma": "es_MX", "plantilla": "att1_carrito_dudas_01",
                                                    "variables": ["nombre", "producto"]}]
    code, out = call(data)
    assert code == module.EXIT_BLOCKED
    assert any(code.startswith("engine_not_supported:") for code in out["revalidation"]["blocks"]), out


def test_a_template_outside_the_catalog_blocks() -> None:
    data = payload()
    cart_of(data)["pasos"][2]["plantilla"] = "att1_plantilla_que_no_existe"
    code, out = call(data)
    assert code == module.EXIT_BLOCKED and out["revalidation"]["blocks"]


def test_what_does_not_serve_is_an_input_error() -> None:
    cases = [
        (payload(catalog=None), ENV, "template_catalog_unavailable"),
        (payload(flow={"product_ref": "att1", "flow_key": "carrito", "legacy_kind": "carrito",
                       "definition_sha256": "x"}), ENV, "route_flow_invalid"),
        (payload(flow={"product_ref": "att1", "flow_key": "carrito", "legacy_kind": "otro",
                       "definition_sha256": SHA}), ENV, "route_flow_invalid"),
        (payload(flow={"product_ref": "att1", "flow_key": "no_existe", "legacy_kind": "carrito",
                       "definition_sha256": SHA}), ENV, "flow_not_found"),
        (payload(flow={"product_ref": "att1", "flow_key": "pago_fallido", "legacy_kind": "pago_fallido",
                       "definition_sha256": SHA}), ENV, "route_flow_without_active_version"),
        (payload(), {}, "operator_image_identity_missing"),
    ]
    for data, environ, reason in cases:
        code, out = call(data, environ)
        assert (code, out) == (module.EXIT_INPUT, {"ok": False, "reason_code": reason})


def test_only_revalidar_is_a_subcommand() -> None:
    out = io.StringIO()
    assert module.main(["validar"], stdin=io.StringIO("{}"), stdout=out, environ=ENV) == module.EXIT_INPUT
    assert json.loads(out.getvalue()) == {"ok": False, "reason_code": "operator_usage"}
