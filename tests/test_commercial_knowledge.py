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


# La seccion [pagos] (1.6.0). Los valores son los que Dan miro en el panel de
# Hotmart del producto 5071808 el 2026-10-12 (decision «como se resuelve cada
# pago que no se completa»), y el motivo con el texto con que llego en el
# PURCHASE_CANCELED del 2026-10-11 23:50Z de ATT1.
def _with_payments(payload: dict) -> dict:
    payload = _approved(payload)
    payload["pagos"] = {
        "pais_principal": "México",
        "medios": ["tarjeta de crédito", "PayPal", "transferencia bancaria", "efectivo en tienda"],
        "por_pais": [
            {"pais": "México", "transferencia": "SPEI", "efectivo": "OXXO"},
            {"pais": "Chile", "efectivo": "Sencillito"},
        ],
        "pasos": [{"medio": "SPEI", "pasos": ["Elige transferencia en la página de pago.", "Confirma la compra."]}],
        "rechazos": [
            {
                "motivo": "Saldo insuficiente",
                "como_llega": ["Saldo insuficiente para o cartão informado. Entre em contato com a operadora."],
                "que_hacer": "Sugiere otra tarjeta u otro medio de la lista.",
            },
            {"motivo": "Rechazo sin detalle", "que_hacer": "Sugiere otro medio de la lista."},
        ],
        "reglas": ["Si la compra quedó en análisis, no la intentes de nuevo."],
    }
    return payload


def test_payments_section_renders_after_the_offer() -> None:
    rendered = CommercialKnowledge.from_mapping(_with_payments(_payload())).render()

    headings = [line for line in rendered.splitlines() if line.startswith("## ")]
    assert headings[2:5] == ["## Oferta", "## Pagos", "## Contenido del programa"]
    assert "Pais principal del negocio: México." in rendered
    assert "- México: transferencia SPEI; efectivo OXXO." in rendered
    assert "- Chile: efectivo Sencillito." in rendered
    assert "Paso a paso para pagar con SPEI:\n\n1. Elige transferencia en la página de pago.\n2. Confirma la compra." in rendered
    assert (
        "- Saldo insuficiente (llega como «Saldo insuficiente para o cartão informado. "
        "Entre em contato com a operadora.»): Sugiere otra tarjeta u otro medio de la lista."
    ) in rendered
    assert "- Rechazo sin detalle: Sugiere otro medio de la lista." in rendered
    assert "- Si la compra quedó en análisis, no la intentes de nuevo." in rendered


def test_knowledge_without_payments_renders_as_before() -> None:
    rendered = CommercialKnowledge.from_mapping(_approved(_payload())).render()

    assert "## Pagos" not in rendered
    assert CommercialKnowledge.from_mapping(_approved(_payload())).payments is None


def test_payments_need_the_active_methods() -> None:
    payload = _with_payments(_payload())
    payload["pagos"]["medios"] = []

    with pytest.raises(KnowledgeError, match="pagos.medios"):
        CommercialKnowledge.from_mapping(payload)


def test_a_country_entry_names_at_least_one_local_method() -> None:
    payload = _with_payments(_payload())
    payload["pagos"]["por_pais"].append({"pais": "Panamá"})

    with pytest.raises(KnowledgeError, match="transferencia, efectivo o los dos"):
        CommercialKnowledge.from_mapping(payload)


def test_unknown_payment_key_is_rejected() -> None:
    payload = _with_payments(_payload())
    payload["pagos"]["cuotas"] = ["3 cuotas"]

    with pytest.raises(KnowledgeError, match="cuotas"):
        CommercialKnowledge.from_mapping(payload)


def test_refusal_entries_need_what_to_do() -> None:
    payload = _with_payments(_payload())
    del payload["pagos"]["rechazos"][0]["que_hacer"]

    with pytest.raises(KnowledgeError, match="que_hacer"):
        CommercialKnowledge.from_mapping(payload)


def test_rendered_limit_is_a_cost_guard_that_fits_two_products() -> None:
    # Hasta la 1.8.0 eran 20.000 bytes, porque se suponia que Hermes cortaba el
    # system del pedido como corta el SOUL. El 2026-10-16 se leyo en la imagen de
    # produccion (v2026.8.31) que no lo corta. El tope es por costo y tiene que
    # alcanzar para dos productos de ATT1: unos 26 KB, estimados sobre el v7.
    assert 26_000 < MAX_RENDERED_BYTES <= 32_000


# La forma de un solo producto no cambia: el v7 de ATT1, el que corre desde el
# 2026-10-15 20:07:28Z, da el mismo hash que su /ready (setter-instancia-att1 48d77f4).
ATT1_V7 = Path(__file__).parent / "fixtures" / "commercial_knowledge" / "att1-knowledge-v7.toml"
ATT1_V7_SHA256 = "ac5cc64fbcadb1b1602b8d76e524d74b1bcc86425e44c9b56b48e77ad420d565"


def test_single_product_form_keeps_the_hash_that_runs_in_att1() -> None:
    knowledge = CommercialKnowledge.from_toml_file(ATT1_V7)

    assert knowledge.by_product is False
    assert len(knowledge.render().encode("utf-8")) == 19_992
    assert knowledge.rendered_sha256 == ATT1_V7_SHA256


# La forma de varios productos (2026-10-16, decision de Dan: el agente recibe todo
# el conocimiento de la aliada, lo comun una vez y una seccion por producto, y
# deduce de que producto se habla). El segundo producto es inventado.
def _by_product(payload: dict, *more: dict) -> dict:
    payload = _approved(payload)
    first = {"clave": "att1", "oferta": payload.pop("oferta"), "contenido": payload.pop("contenido")}
    payload["productos"] = [first, *more]
    return payload


def _second_product() -> dict:
    return {
        "clave": "att2",
        "oferta": {
            "nombre": "Producto de prueba",
            "descripcion": "Un segundo producto, inventado para la prueba.",
            "precio": "27",
            "moneda": "USD",
        },
        "contenido": {"componentes": ["Una guía de prueba."]},
        "no_confirmado": {"items": ["cuántas guías trae"]},
        "promesas_prohibidas": {"items": ["Precios distintos de USD 27."]},
        "faq": [
            {"pregunta": "¿Cuánto cuesta?", "respuesta": "Cuesta USD 27.", "fuente": "prueba", "vigencia": "2026-10-16"}
        ],
    }


def test_by_product_form_puts_the_common_part_first_and_one_section_per_product() -> None:
    knowledge = CommercialKnowledge.from_mapping(_by_product(_payload(), _second_product()))
    rendered = knowledge.render()
    first_name = knowledge.products[0].offer_name

    assert knowledge.by_product is True
    assert [product.key for product in knowledge.products] == ["att1", "att2"]
    assert [line for line in rendered.splitlines() if line.startswith("# ")] == [
        f"# Conocimiento aprobado: {knowledge.brand}",
        f"# Producto: {first_name}",
        "# Producto: Producto de prueba",
    ]
    common, first, second = rendered.split("\n# Producto: ")
    assert f"sus 2 productos ({first_name} y Producto de prueba)" in common
    assert "## Oferta" not in common
    assert "## No confirmado" in common
    assert "## Limites sensibles" in common
    assert "## Oferta" in first and "## Contenido del programa" in first
    assert "- Precio: USD 27." in second
    assert "- cuántas guías trae" in second and "cuántas guías trae" not in first
    assert "- Precios distintos de USD 27." in second
    assert "- P: ¿Cuánto cuesta?\n  R: Cuesta USD 27." in second


def test_by_product_form_keeps_the_section_names_the_common_soul_reads() -> None:
    knowledge = CommercialKnowledge.from_mapping(_by_product(_with_payments(_payload())))
    rendered = knowledge.render()
    headings = [line for line in rendered.splitlines() if line.startswith("## ")]

    assert f"la seccion de su producto, {knowledge.products[0].offer_name}," in rendered
    # El SOUL comun nombra estas secciones (profiles/agente-comercial-comun/SOUL.md).
    for heading in (
        "## Voz",
        "## Pagos",
        "## No confirmado",
        "## Promesas prohibidas",
        "## Oferta",
        "## Contenido del programa",
        "## Preguntas frecuentes aprobadas",
    ):
        assert heading in headings
    assert rendered.count("## Pagos") == 1
    assert rendered.index("## Pagos") < rendered.index("# Producto:")


def test_by_product_form_renders_the_common_statements_in_the_common_part() -> None:
    payload = _by_product(_payload())
    payload["se_puede_decir"] = {"items": ["La doctora es médica cirujana."]}
    rendered = CommercialKnowledge.from_mapping(payload).render()

    common = rendered.split("\n# Producto: ")[0]
    assert "## Lo que si se puede decir\n\n- La doctora es médica cirujana." in common


def test_by_product_render_is_deterministic() -> None:
    first = CommercialKnowledge.from_mapping(_by_product(_payload(), _second_product()))
    second = CommercialKnowledge.from_mapping(_by_product(_payload(), _second_product()))

    assert first.render() == second.render()
    assert first.rendered_sha256 == second.rendered_sha256


def test_single_product_form_refuses_common_statements_on_top() -> None:
    payload = _approved(_payload())
    payload["se_puede_decir"] = {"items": ["x"]}

    with pytest.raises(KnowledgeError, match="se_puede_decir"):
        CommercialKnowledge.from_mapping(payload)


def test_by_product_form_refuses_an_offer_on_top() -> None:
    payload = _by_product(_payload())
    payload["oferta"] = payload["productos"][0]["oferta"]

    with pytest.raises(KnowledgeError, match="adentro de cada producto"):
        CommercialKnowledge.from_mapping(payload)


def test_by_product_form_still_requires_the_common_guardrails() -> None:
    payload = _by_product(_payload())
    del payload["no_confirmado"]

    with pytest.raises(KnowledgeError, match="faltan no_confirmado"):
        CommercialKnowledge.from_mapping(payload)


def test_products_cannot_be_empty() -> None:
    payload = _by_product(_payload())
    payload["productos"] = []

    with pytest.raises(KnowledgeError, match="lista no vacia"):
        CommercialKnowledge.from_mapping(payload)


@pytest.mark.parametrize("key", ["ATT1", "att 1", "-att1", ""])
def test_product_key_is_a_lowercase_slug(key) -> None:
    payload = _by_product(_payload())
    payload["productos"][0]["clave"] = key

    with pytest.raises(KnowledgeError, match=r"productos\[0\]\.clave"):
        CommercialKnowledge.from_mapping(payload)


def test_two_products_cannot_share_a_key() -> None:
    second = _second_product()
    second["clave"] = "att1"

    with pytest.raises(KnowledgeError, match="ya la usa otro producto"):
        CommercialKnowledge.from_mapping(_by_product(_payload(), second))


def test_two_products_cannot_share_a_name() -> None:
    payload = _by_product(_payload(), _second_product())
    payload["productos"][1]["oferta"]["nombre"] = payload["productos"][0]["oferta"]["nombre"]

    with pytest.raises(KnowledgeError, match="oferta.nombre ya lo usa otro producto"):
        CommercialKnowledge.from_mapping(payload)


def test_unknown_product_key_is_rejected() -> None:
    second = _second_product()
    second["bonos"] = {"items": ["x"]}

    with pytest.raises(KnowledgeError, match="bonos"):
        CommercialKnowledge.from_mapping(_by_product(_payload(), second))


@pytest.mark.parametrize("section", ["no_confirmado", "promesas_prohibidas"])
def test_own_guardrail_lists_cannot_be_empty(section) -> None:
    second = _second_product()
    second[section]["items"] = []

    with pytest.raises(KnowledgeError, match="vacia"):
        CommercialKnowledge.from_mapping(_by_product(_payload(), second))
