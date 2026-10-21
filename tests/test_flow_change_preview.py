"""La vista previa de una propuesta (src/bridge/flow_change_preview.py).

Las definiciones son las de ATT1 generadas el 2026-10-16
(tests/fixtures/flow_store/att1_definitions_20261016.json), publicadas como v1.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from bridge.flow_change_preview import (
    GAP_AUDIENCE,
    GAP_DIFF_TRUNCATED,
    GAP_IN_FLIGHT,
    GAP_SIMULATION,
    PREVIEW_MAX_BYTES,
    build_preview,
)
from bridge.flow_definition import FlowWorld, ProposalItem, WorldFlow

ROOT = Path(__file__).resolve().parents[1]
DEFINITIONS = {d["flujo"]: d for d in json.loads(
    (ROOT / "tests/fixtures/flow_store/att1_definitions_20261016.json").read_text(encoding="utf-8"))["definitions"]}
CART = "00000001-0000-4000-8000-0000000a7701"


def published(definition: dict, version: int) -> dict:
    out = copy.deepcopy(definition)
    out.update(version=version, estado="publicada")
    return out


def world(state: str = "prendido") -> FlowWorld:
    v1 = published(DEFINITIONS["carrito"], 1)
    v2 = published(DEFINITIONS["carrito"], 2)
    v2["termina"]["vence"] = "2d"
    return FlowWorld([WorldFlow(CART, "att1", "carrito", state, 2, 7, {1: v1, 2: v2})])


def wait_item(wait: str = "2h") -> ProposalItem:
    definition = copy.deepcopy(DEFINITIONS["carrito"])
    definition["termina"]["vence"] = "2d"
    definition["pasos"] = [{"esperar": wait}, *definition["pasos"]]
    return ProposalItem("att1", "carrito", CART, "version", definition, en_curso="seguir")


def test_a_wait_before_the_cart_template_is_a_diff_against_the_active_version() -> None:
    preview = build_preview([wait_item()], world(), purpose="que el carrito espere 2 horas")
    [change] = preview["cambios"]
    assert change["base_version"] == 2 and change["tipo"] == "version" and change["en_curso"] == "seguir"
    assert {"ruta": "/pasos/0/esperar", "antes": None, "despues": "2h"} in change["diff"]
    assert {"ruta": "/pasos/0/plantilla", "antes": "att1_carrito_abandonado_01", "despues": None} in change["diff"]
    # estado y version los pone la base al publicar: no son un cambio.
    assert not [c for c in change["diff"] if c["ruta"] in ("/estado", "/version")]
    assert preview["volver_atras"] == [{"producto": "att1", "flujo": "carrito", "a_version": 2}]
    assert preview["para_que"] == "que el carrito espere 2 horas"


def test_what_the_engine_does_not_build_yet_goes_as_gaps_with_their_code() -> None:
    preview = build_preview([wait_item()], world(), purpose="x")
    assert [gap["code"] for gap in preview["huecos"]] == [GAP_SIMULATION, GAP_IN_FLIGHT, GAP_AUDIENCE]
    assert preview["simulacion"] is None and preview["a_quien"] is None
    assert preview["en_curso"] == {"politica": "seguir", "recorridos": None}


def test_going_back_diffs_against_the_target_version() -> None:
    item = ProposalItem("att1", "carrito", CART, "volver", version=1, en_curso="seguir")
    [change] = build_preview([item], world(), purpose="volver")["cambios"]
    assert change["a_version"] == 1
    assert change["diff"] == [{"ruta": "/termina/vence", "antes": "2d", "despues": "1d"}]


def test_a_rollback_to_a_version_that_does_not_exist_is_an_error() -> None:
    item = ProposalItem("att1", "carrito", CART, "volver", version=9, en_curso="seguir")
    with pytest.raises(ValueError, match="flow_change_rollback_target_invalid"):
        build_preview([item], world(), purpose="volver")


@pytest.mark.parametrize(("tipo", "state", "after"), [("apagar", "prendido", "apagado"), ("prender", "pausado", "prendido")])
def test_turning_on_or_off_shows_the_state_before_and_after(tipo: str, state: str, after: str) -> None:
    item = ProposalItem("att1", "carrito", CART, tipo)
    preview = build_preview([item], world(state), purpose="x")
    assert preview["cambios"] == [{"producto": "att1", "flujo": "carrito", "tipo": tipo, "base_version": 2,
                                   "estado": {"antes": state, "despues": after}}]
    assert preview["volver_atras"] == []


def test_warnings_go_by_code_and_path_without_their_text() -> None:
    warning = {"code": "wait_without_contesta", "path": "/0/definicion/pasos", "reason": "texto largo"}
    preview = build_preview([wait_item()], world(), purpose="x", warnings=[warning])
    assert preview["advertencias"] == [{"code": "wait_without_contesta", "path": "/0/definicion/pasos"}]


def test_a_preview_that_would_not_fit_in_the_base_drops_the_diff_and_says_so() -> None:
    item = wait_item()
    definition = copy.deepcopy(dict(item.definicion or {}))
    definition["nombre"] = "x" * (PREVIEW_MAX_BYTES + 10)
    preview = build_preview([item.with_definition(definition)], world(), purpose="x")
    assert len(json.dumps(preview).encode()) <= PREVIEW_MAX_BYTES
    assert preview["cambios"][0]["diff"] == [] and preview["cambios"][0]["diff_count"] > 0
    assert {"code": GAP_DIFF_TRUNCATED, "items": "all"} in preview["huecos"]
