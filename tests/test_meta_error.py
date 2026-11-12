"""E-486 (v1.37.0, item 6): el codigo de Meta tambien cuando viene como JSON.

La QA del 12/11 leyo los ``failed`` de Chatwoot con ``external_error`` vacio:
el error venia como JSON adentro de un string. Las formas de aca son las de
Chatwoot y la Cloud API, con el texto de Meta reemplazado.
"""

from __future__ import annotations

import json

import pytest

from bridge.automation_expert_read import _meta_error_code as ae_meta_error_code
from bridge.meta_delivery import meta_error_code
from bridge.meta_error import meta_error_code_of


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("131049: This message was not delivered", "131049"),
        ('{"code": 131050, "title": "Unable to deliver"}', "131050"),
        ('{"error": {"code": 130472, "message": "experiment"}}', "130472"),
        ('[{"code": 131026, "title": "Message undeliverable"}]', "131026"),
        ('"131042: Business eligibility payment issue"', "131042"),
        ({"code": "131000"}, "131000"),
        (131049, "131049"),
    ],
)
def test_the_code_in_every_shape(value: object, code: str) -> None:
    assert meta_error_code_of(value) == code


@pytest.mark.parametrize(
    "value",
    [None, "", "Template not found", "{no es json", '{"title": "sin codigo"}', [], True, 12],
)
def test_no_code_is_none(value: object) -> None:
    assert meta_error_code_of(value) is None


def test_never_the_text_even_with_a_phone() -> None:
    value = json.dumps({"code": 131026, "title": "No llega a +5215512345678"})
    assert meta_error_code_of(value) == "131026"


def test_the_sweeper_and_the_ae_reader_see_the_json_code() -> None:
    message = {"content_attributes": {"external_error": '{"code": 131050, "title": "x"}'}}
    assert meta_error_code(message) == "131050"
    assert ae_meta_error_code(message) == "131050"
    # La base guarda content_attributes como texto.
    as_text = {"content_attributes": json.dumps(message["content_attributes"])}
    assert meta_error_code(as_text) == "131050"
