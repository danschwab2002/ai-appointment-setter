"""Las reglas 2 y 3 del disparo por audiencia (E-484), todavia fuera de flow_validator.

Contrato: docs/contracts/flow-audience-trigger-v1.md. El enganche en flow_validator es de
E-483 en adelante; aca se prueban los codigos y que tengan la forma del catalogo.
"""

from __future__ import annotations

from typing import Any

import pytest

from bridge import flow_definition as fd
from bridge import flow_validator_audiencia as fva

from test_flow_audience import difusion


def codes(definition: dict[str, Any]) -> list[tuple[str, str]]:
    return [(finding.code, finding.path) for finding in fva.audience_findings(definition)]


def test_a_well_formed_audience_has_no_findings() -> None:
    assert codes(difusion()) == []
    assert codes(difusion(dispara={"manual": {}}, para={"es": "clienta"}, entra={"permiso": "compra"})) == []


def test_an_audience_without_para() -> None:
    definition = difusion()
    definition.pop("para")
    assert codes(definition) == [("audience_requires_para", "/para")]


def test_para_and_ritmo_without_an_audience_trigger() -> None:
    definition = difusion(dispara={"evento": "carrito"}, ritmo={"por_hora": 10})
    assert codes(definition) == [("para_requires_audience_trigger", "/para"),
                                 ("para_requires_audience_trigger", "/ritmo")]


@pytest.mark.parametrize("status, permission, blocked", [
    ("prospecta", "formulario", False), ("clienta", "compra", False),
    ("prospecta", "compra", True), ("clienta", "formulario", True),
    ("prospecta", "conversacion", True), ("clienta", "conversacion", True),
])
def test_the_permission_that_goes_with_each_audience(status: str, permission: str, blocked: bool) -> None:
    found = codes(difusion(para={"es": status}, entra={"permiso": permission}))
    assert found == ([("audience_permission_mismatch", "/entra/permiso")] if blocked else [])


def test_two_triggers_are_left_to_trigger_not_exactly_one() -> None:
    assert codes(difusion(dispara={"manual": {}, "evento": "compra"})) == []


def test_the_base_path_of_an_item_is_kept() -> None:
    definition = difusion()
    definition.pop("para")
    found = fva.audience_findings(definition, (0, "definicion"))
    assert [finding.path for finding in found] == ["/0/definicion/para"]


def test_the_codes_have_the_shape_of_the_closed_catalog() -> None:
    for spec in fva.AUDIENCE_CODES:
        assert spec.kind == fd.KIND_FORM and spec.rule in (2, 3)
        assert fd.is_clean_text(spec.reason, 300) and fd.is_clean_text(spec.alternative, 300)
        finding = spec.at("/para").to_json()
        assert finding["code"] == spec.code and finding["rule"] == spec.rule
