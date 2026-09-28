"""Conocimiento comercial por instancia (docs/contracts/commercial-knowledge-v1.md).

Fixture: tests/fixtures/instances/att1/conocimiento/knowledge-v1.toml, armado el
2026-09-28 desde la landing publicada de ATT1 y su Brand Voice V1.
"""

from __future__ import annotations

import copy
import datetime
import tomllib
from pathlib import Path

import pytest

from bridge.commercial_knowledge import (
    MAX_RENDERED_BYTES,
    CommercialKnowledge,
    KnowledgeError,
)

FIXTURE = Path(__file__).parent / "fixtures" / "instances" / "att1" / "conocimiento" / "knowledge-v1.toml"


def _payload() -> dict:
    return tomllib.loads(FIXTURE.read_text(encoding="utf-8"))


def _approved(payload: dict) -> dict:
    payload = copy.deepcopy(payload)
    payload["cabecera"].update(
        estado="aprobado", aprobado_por="dan", aprobado_el=datetime.date(2026, 9, 28)
    )
    return payload


def test_draft_is_refused_when_the_bridge_requires_approval() -> None:
    with pytest.raises(KnowledgeError, match="borrador"):
        CommercialKnowledge.from_toml_file(FIXTURE)


def test_draft_loads_for_validation() -> None:
    knowledge = CommercialKnowledge.from_toml_file(FIXTURE, require_approved=False)

    assert knowledge.ally_ref == "att1"
    assert knowledge.status == "borrador"
    assert not knowledge.approved


def test_approved_knowledge_loads_and_renders_in_fixed_order() -> None:
    knowledge = CommercialKnowledge.from_mapping(_approved(_payload()))
    rendered = knowledge.render()

    headings = [line for line in rendered.splitlines() if line.startswith("#")]
    assert headings == [
        "# Conocimiento aprobado: Método Raizana",
        "## Identidad",
        "## Voz",
        "## Oferta",
        "## Contenido del programa",
        "## No confirmado",
        "## Promesas prohibidas",
        "## Limites sensibles: salud tiroidea (hipotiroidismo, Hashimoto) y alimentación",
        "## Preguntas frecuentes aprobadas",
    ]
    assert "Precio: USD 47." in rendered
    assert "Garantia: no confirmada." in rendered


def test_render_is_deterministic() -> None:
    first = CommercialKnowledge.from_mapping(_approved(_payload()))
    second = CommercialKnowledge.from_mapping(_approved(_payload()))

    assert first.render() == second.render()
    assert first.rendered_sha256 == second.rendered_sha256


def test_empty_component_list_tells_the_agent_to_hand_off() -> None:
    rendered = CommercialKnowledge.from_mapping(_approved(_payload())).render()

    assert "No hay lista de componentes confirmada" in rendered


def test_source_comments_never_reach_the_agent() -> None:
    rendered = CommercialKnowledge.from_mapping(_approved(_payload())).render()

    assert "LANDING" not in rendered
    assert "intake-v1.json" not in rendered


def test_approval_requires_who_and_when() -> None:
    payload = _payload()
    payload["cabecera"]["estado"] = "aprobado"

    with pytest.raises(KnowledgeError, match="aprobado_por y aprobado_el"):
        CommercialKnowledge.from_mapping(payload)


def test_approval_date_must_be_a_date_not_a_datetime() -> None:
    payload = _approved(_payload())
    payload["cabecera"]["aprobado_el"] = datetime.datetime(2026, 9, 28, 12, 0)

    with pytest.raises(KnowledgeError, match="fecha"):
        CommercialKnowledge.from_mapping(payload)


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnopqrstuvwxyz0123",
        "xoxb-1234567890-abcdefghij",
        "ghp_abcdefghijklmnopqrstuvwxyz0123",
        "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZSJ9",
        "0123456789abcdef0123456789abcdef01234567",
    ],
)
def test_secret_shaped_values_are_refused(secret) -> None:
    payload = _approved(_payload())
    payload["oferta"]["notas"].append(f"clave {secret}")

    with pytest.raises(KnowledgeError, match="credencial"):
        CommercialKnowledge.from_mapping(payload)


def test_oversized_block_is_refused() -> None:
    payload = _approved(_payload())
    payload["no_confirmado"]["items"].append("x" * (MAX_RENDERED_BYTES + 1))

    with pytest.raises(KnowledgeError, match="maximo"):
        CommercialKnowledge.from_mapping(payload)


@pytest.mark.parametrize("section", ["no_confirmado", "promesas_prohibidas"])
def test_guardrail_sections_cannot_be_empty(section) -> None:
    payload = _approved(_payload())
    payload[section]["items"] = []

    with pytest.raises(KnowledgeError, match="vacia"):
        CommercialKnowledge.from_mapping(payload)


def test_unknown_section_is_rejected() -> None:
    payload = _approved(_payload())
    payload["bonos"] = {"items": ["x"]}

    with pytest.raises(KnowledgeError, match="bonos"):
        CommercialKnowledge.from_mapping(payload)


def test_faq_entries_need_source_and_date() -> None:
    payload = _approved(_payload())
    del payload["faq"][0]["fuente"]

    with pytest.raises(KnowledgeError, match="fuente"):
        CommercialKnowledge.from_mapping(payload)
