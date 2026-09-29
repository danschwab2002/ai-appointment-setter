"""SOUL comun del agente comercial (setter-producto-instalable-v1 §4.4).

El SOUL comun es el de Johanna sin lo que es de Johanna. Las secciones que no
dependen del cliente tienen que seguir siendo las mismas, byte a byte: si una
cambia en un lado y no en el otro, las dos instancias dejan de comportarse igual.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON = (ROOT / "profiles" / "agente-comercial-comun" / "SOUL.md").read_text(encoding="utf-8")
JOHANNA = (ROOT / "profiles" / "agente-comercial" / "SOUL.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    start = lines.index(heading)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = re.match(r"^(#+) ", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end]).rstrip()


@pytest.mark.parametrize(
    "value",
    ["Johanna", "Libre de Ansiedad", "USD 49", "Ecuador", "Cuenca", "7 días", "Nina", "tiroid", "Raizana"],
)
def test_common_soul_has_no_customer_value(value: str) -> None:
    assert value.casefold() not in COMMON.casefold()


@pytest.mark.parametrize(
    "heading",
    [
        "### 1. Inbound regular",
        "### 2. Carrito abandonado",
        "### 3. Compra fallida",
        "### Cuándo no pedirlo",
        "### Comunicación de la derivación",
        "## Transparencia operacional del chat",
        "## Entrada",
    ],
)
def test_invariant_sections_are_identical_to_johanna(heading: str) -> None:
    assert _section(COMMON, heading) == _section(JOHANNA, heading)


def test_output_contract_differs_only_in_the_neutral_reason_code() -> None:
    johanna = _section(JOHANNA, "## Salida obligatoria").replace(
        "johanna_e2e_response", "commercial_response"
    )

    assert _section(COMMON, "## Salida obligatoria") == johanna


def test_resolution_policy_differs_only_in_where_the_facts_live() -> None:
    johanna = _section(JOHANNA, "## Política comercial de resolución y derivación humana")
    johanna = johanna.replace(
        "todos los datos necesarios están confirmados en este documento",
        "todos los datos necesarios están confirmados en el bloque «Conocimiento aprobado»",
    )
    common = _section(COMMON, "## Política comercial de resolución y derivación humana")

    # La subseccion de comunicacion se compara aparte.
    assert common.split("### Comunicación de la derivación")[0] == johanna.split(
        "### Comunicación de la derivación"
    )[0]


def test_common_soul_names_the_knowledge_block_the_bridge_renders() -> None:
    from bridge.commercial_knowledge import CommercialKnowledge

    assert "«Conocimiento aprobado»" in COMMON
    assert CommercialKnowledge.render.__doc__ is not None
    # El render arranca con "# Conocimiento aprobado: <marca>".
    source = Path(__import__("bridge.commercial_knowledge", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    assert 'f"# Conocimiento aprobado: {self.brand}"' in source


def test_forbidden_missing_data_phrases_survive_in_the_common_soul() -> None:
    for phrase in ("No tengo información sobre…", "No tengo detalles sobre…", "Ese\n  dato todavía no lo tengo confirmado"):
        assert phrase in COMMON
