"""La plantilla aprobada que el dispatcher manda sin Hermes (A4).

Catalogos capturados de Chatwoot, sin tocar:

* ``chatwoot_inbox_9_message_templates_20260923.json``: la respuesta de
  ``GET /inboxes/9`` del 23/09 (``johanna_reactivacion_01`` con ``{{1}}``,
  ``johanna_interes_precheckout_01`` con ``{{1}}`` y ``{{2}}``, ``hello_world``).
* ``chatwoot_followup_candidates_inbox_9_20260928.json``: el catalogo del mismo
  inbox el 28/09, con ``johanna_carrito_abandonado_01`` y
  ``johanna_compra_fallida_01`` (tres botones QUICK_REPLY cada una) y un
  ``hello_world`` con HEADER y FOOTER.

No hay captura del catalogo del inbox 11 de ATT1 (paso A0). Las ramas de
rechazo se ejercitan mutando una copia de la captura, como
``test_followup_discount.py``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from bridge.approved_templates import (
    APPROVED_TEMPLATE_BODY_MAX_CHARS,
    ApprovedTemplateError,
    parse_approved_template,
)

FIXTURES = Path(__file__).parent / "fixtures"
INBOX_0923: dict[str, Any] = json.loads(
    (FIXTURES / "chatwoot_inbox_9_message_templates_20260923.json").read_text(
        encoding="utf-8"
    )
)
CATALOG_0928: list[dict[str, Any]] = json.loads(
    (FIXTURES / "chatwoot_followup_candidates_inbox_9_20260928.json").read_text(
        encoding="utf-8"
    )
)["inbox_9_message_templates"]
LEAD_NAMES: list[dict[str, Any]] = json.loads(
    (FIXTURES / "lead_names_inbox9_template_params_20260928.json").read_text(
        encoding="utf-8"
    )
)["cases"]

CART = "johanna_carrito_abandonado_01"


def _inbox_0928(catalog: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"message_templates": copy.deepcopy(CATALOG_0928 if catalog is None else catalog)}


def _parse(payload: object, name: str = CART, *, count: int = 2, language: str = "es_EC",
           category: str = "MARKETING"):
    return parse_approved_template(
        payload,
        template_name=name,
        expected_language=language,
        expected_category=category,
        parameter_count=count,
    )


def _body(payload: dict[str, Any], name: str) -> str:
    [template] = [t for t in payload["message_templates"] if t["name"] == name]
    return next(c["text"] for c in template["components"] if c["type"] == "BODY")


# ------------------------------------------------------------ what it accepts


@pytest.mark.parametrize(
    "name", ["johanna_carrito_abandonado_01", "johanna_compra_fallida_01",
             "johanna_interes_precheckout_01"],
)
def test_captured_first_contact_templates_with_quick_replies_are_accepted(name: str) -> None:
    payload = _inbox_0928()

    template = _parse(payload, name)

    assert (template.name, template.language, template.category) == (name, "es_EC", "MARKETING")
    assert template.body == _body(payload, name)
    assert template.parameter_count == 2


def test_the_full_inbox_response_of_the_23rd_is_read_as_is() -> None:
    reactivation = _parse(INBOX_0923, "johanna_reactivacion_01", count=1)
    precheckout = _parse(INBOX_0923, "johanna_interes_precheckout_01", count=2)

    assert reactivation.render({"1": "Edith"}).startswith("Hola, Edith. ")
    assert precheckout.render({"1": "Edith", "2": "Libre de Ansiedad"}) == (
        "Hola, Edith. Vi que te intereso Libre de Ansiedad y quedo pendiente tu compra."
    )


@pytest.mark.parametrize(
    "case", [c for c in LEAD_NAMES if c["deterministic"]], ids=lambda c: c["pattern"]
)
def test_render_fills_every_marker_with_the_captured_names(case: dict[str, Any]) -> None:
    payload = _inbox_0928()
    template = _parse(payload, CART)

    rendered = template.render({"1": case["deterministic"], "2": "Alimenta Tu Tiroides"})

    assert "{{" not in rendered
    assert rendered == (
        _body(payload, CART)
        .replace("{{1}}", case["deterministic"])
        .replace("{{2}}", "Alimenta Tu Tiroides")
    )
    assert len(rendered) <= APPROVED_TEMPLATE_BODY_MAX_CHARS


def test_a_static_header_and_footer_need_no_parameter() -> None:
    # hello_world del 28/09 trae HEADER de texto y FOOTER; se le agrega un
    # marcador al cuerpo para ejercitar la rama (el original no tiene variables).
    catalog = copy.deepcopy(CATALOG_0928)
    [hello] = [t for t in catalog if t["name"] == "hello_world"]
    next(c for c in hello["components"] if c["type"] == "BODY")["text"] += " {{1}}"

    template = _parse(_inbox_0928(catalog), "hello_world", count=1, language="en_US",
                      category="UTILITY")

    assert template.render({"1": "Edith"}).endswith(" Edith")


def test_a_value_is_not_substituted_twice() -> None:
    template = _parse(_inbox_0928(), CART)

    rendered = template.render({"1": "{{2}}", "2": "Alimenta Tu Tiroides"})

    assert rendered.startswith("Hola, {{2}}. ")


# ------------------------------------------------------ what it fails closed


def _mutated(mutate: Callable[[dict[str, Any]], None], name: str = CART) -> dict[str, Any]:
    catalog = copy.deepcopy(CATALOG_0928)
    for template in catalog:
        if template["name"] == name:
            mutate(template)
    return _inbox_0928(catalog)


def _body_component(template: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in template["components"] if c["type"] == "BODY")


def _buttons(template: dict[str, Any]) -> list[dict[str, Any]]:
    return next(c for c in template["components"] if c["type"] == "BUTTONS")["buttons"]


@pytest.mark.parametrize(
    ("payload", "reason", "detail"),
    [
        pytest.param(None, "approved_template_unavailable", "invalid_inbox_payload", id="no payload"),
        pytest.param({"id": 11}, "approved_template_unavailable", "invalid_inbox_payload",
                     id="no catalog"),
        pytest.param(
            _inbox_0928([t for t in CATALOG_0928 if t["name"] != CART]),
            "approved_template_unavailable", "not_found", id="not in the catalog",
        ),
        pytest.param(
            _mutated(lambda t: t.update(status="PENDING")),
            "approved_template_unavailable", "not_approved", id="pending",
        ),
        pytest.param(
            _mutated(lambda t: t.update(language="es_MX")),
            "approved_template_mismatch", "language_mismatch", id="other language",
        ),
        pytest.param(
            _inbox_0928(CATALOG_0928 + [copy.deepcopy(next(t for t in CATALOG_0928 if t["name"] == CART))]),
            "approved_template_mismatch", "ambiguous_template", id="same name and language twice",
        ),
        pytest.param(
            _mutated(lambda t: t.update(category="UTILITY")),
            "approved_template_mismatch", "category_mismatch", id="other category",
        ),
        pytest.param(
            _mutated(lambda t: t["components"].append(copy.deepcopy(_body_component(t)))),
            "approved_template_mismatch", "invalid_body", id="two bodies",
        ),
        pytest.param(
            _mutated(lambda t: _body_component(t).update(text=_body_component(t)["text"] + " {{3}}")),
            "approved_template_mismatch", "unexpected_placeholders", id="a third variable",
        ),
        pytest.param(
            _mutated(lambda t: _body_component(t).update(
                text=_body_component(t)["text"].replace("{{2}}", "el programa"))),
            "approved_template_mismatch", "unexpected_placeholders", id="one variable less",
        ),
        pytest.param(
            _mutated(lambda t: _body_component(t).update(
                text=_body_component(t)["text"] + " {{codigo}}")),
            "approved_template_mismatch", "unexpected_placeholders", id="a named variable",
        ),
        pytest.param(
            _mutated(lambda t: _buttons(t)[0].update(type="URL", url="https://x.test/{{1}}")),
            "approved_template_mismatch", "unsupported_button", id="url button",
        ),
        pytest.param(
            _mutated(lambda t: t["components"].insert(0, {"type": "HEADER", "format": "IMAGE"})),
            "approved_template_mismatch", "component_requires_parameters", id="image header",
        ),
        pytest.param(
            _mutated(lambda t: t["components"].insert(
                0, {"type": "HEADER", "format": "TEXT", "text": "Hola {{1}}"})),
            "approved_template_mismatch", "component_requires_parameters", id="header variable",
        ),
        pytest.param(
            _mutated(lambda t: t["components"].append({"type": "CAROUSEL", "cards": []})),
            "approved_template_mismatch", "unsupported_component", id="unknown component",
        ),
    ],
)
def test_a_template_that_is_not_exactly_what_the_runtime_sends_fails_closed(
    payload: object, reason: str, detail: str
) -> None:
    with pytest.raises(ApprovedTemplateError) as raised:
        _parse(payload)

    assert (raised.value.reason, raised.value.detail) == (reason, detail)


def test_the_count_comes_from_the_declared_variables() -> None:
    # johanna_reactivacion_01 tiene un solo marcador: declarada con dos, no cierra.
    with pytest.raises(ApprovedTemplateError) as raised:
        _parse(INBOX_0923, "johanna_reactivacion_01", count=2)

    assert raised.value.detail == "unexpected_placeholders"


@pytest.mark.parametrize(
    ("values", "reason", "detail"),
    [
        ({"1": "Edith"}, "approved_template_mismatch", "parameter_count_mismatch"),
        ({"1": "Edith", "2": "  "}, "template_parameters_missing", "empty_parameter"),
        (
            {"1": "E" * APPROVED_TEMPLATE_BODY_MAX_CHARS, "2": "Alimenta Tu Tiroides"},
            "approved_template_mismatch",
            "rendered_body_too_long",
        ),
    ],
    ids=["one value short", "blank value", "past the Meta limit"],
)
def test_render_refuses_values_meta_would_reject(
    values: dict[str, str], reason: str, detail: str
) -> None:
    template = _parse(_inbox_0928(), CART)

    with pytest.raises(ApprovedTemplateError) as raised:
        template.render(values)

    assert (raised.value.reason, raised.value.detail) == (reason, detail)


def test_the_limit_is_meta_s_not_the_500_of_an_agent_draft() -> None:
    template = _parse(_inbox_0928(), CART)
    base = len(template.render({"1": "E", "2": "P"}))

    rendered = template.render({"1": "E" * (600 - base), "2": "P"})

    assert 500 < len(rendered) <= APPROVED_TEMPLATE_BODY_MAX_CHARS


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"template_name": " "}, "approved_template_name_missing"),
     ({"parameter_count": 0}, "approved_template_parameter_count_invalid")],
)
def test_a_wrong_call_is_a_programming_error(kwargs: dict[str, Any], message: str) -> None:
    arguments: dict[str, Any] = {
        "template_name": CART,
        "expected_language": "es_EC",
        "expected_category": "MARKETING",
        "parameter_count": 2,
        **kwargs,
    }
    with pytest.raises(ValueError, match=message):
        parse_approved_template(_inbox_0928(), **arguments)
