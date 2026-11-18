"""E-664: el saludo sin nombre cuando el nombre del caso no pasa el filtro.

El dato real: ``tests/fixtures/lead_first_name_unsafe_name_maribel_20261117.json``
(maribel_db, solo lectura, 2026-11-18). Un lead de Maribel puso su email en el
campo del nombre; ``template_greeting_name_is_safe`` lo rechazo y el primer
contacto del formulario se cerro con ``template_parameters_missing`` sin
reintento: el caso quedo ``unreachable`` y la persona no recibio nada.

Desde E-664 la plantilla sale con ``TEMPLATE_NEUTRAL_GREETING`` («qué tal») en
el lugar del nombre: «Hola qué tal, revisamos tu intento de compra» en Maribel y
«Hola, qué tal. Soy del equipo de Dra. Nina.» en ATT1. Con un nombre que pasa
el filtro no cambia nada; con ``neutral_greeting=None`` vuelve el cierre de antes.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_durable_dispatcher_approved_template import (
    ATT1_CATALOG,
    FORM_ANCHOR,
    NOW,
    TEMPLATE,
    WITH_FORM_TEMPLATE,
    _Authority,
    _Chatwoot,
    _dispatcher,
    _run,
)
from test_flow_engine_native_cart import _CartAuthority
from test_flow_engine_native_dispatch import _dispatcher as _native_dispatcher
from test_flow_engine_native_dispatch import _templates
from test_flow_engine_native_quick_replies import MARIBEL, _MaribelChatwoot

from bridge.lead_first_name import (
    TEMPLATE_NEUTRAL_GREETING,
    template_greeting_name_is_safe,
)

FIXTURE = json.loads(
    (
        Path(__file__).parent / "fixtures" / "lead_first_name_unsafe_name_maribel_20261117.json"
    ).read_text(encoding="utf-8")
)
NAME = FIXTURE["nombre_sintetico"]


def test_the_fixture_is_the_closed_case_of_maribel() -> None:
    case, action = FIXTURE["caso"], FIXTURE["accion"]
    assert case["status"] == "unreachable"
    assert case["context"]["trigger_kind"] == FORM_ANCHOR
    assert action["terminal_reason"] == "template_parameters_missing"
    assert action["execution_attempt_count"] == 1
    assert FIXTURE["inferencia_del_nombre"]["result"] == "uncertain"
    # El nombre sintetico tiene la forma del real: el filtro lo rechaza igual.
    shape = FIXTURE["forma_del_nombre"]
    assert len(NAME) == shape["largo"] and "@" in NAME and " " not in NAME
    assert not any(char.isdigit() for char in NAME)
    assert not template_greeting_name_is_safe(NAME)


def test_the_neutral_greeting_passes_the_filter_and_is_the_default() -> None:
    assert TEMPLATE_NEUTRAL_GREETING == "qué tal"
    assert template_greeting_name_is_safe(TEMPLATE_NEUTRAL_GREETING)
    assert TEMPLATE.neutral_greeting == TEMPLATE_NEUTRAL_GREETING


@pytest.mark.parametrize(
    "bad", ["", " qué tal", "qué  tal", "hola@evil.example", "x" * 61, "5512345678"]
)
def test_a_neutral_greeting_that_would_not_go_out_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid_template_neutral_greeting"):
        replace(TEMPLATE, neutral_greeting=bad)


@pytest.mark.parametrize("greeting", [True, False], ids=["con saludo", "sin saludo"])
def test_the_form_first_contact_of_the_fixture_goes_out_without_the_name(
    tmp_path: Path, greeting: bool
) -> None:
    # El mismo disparo del caso (precheckout_intent, primer contacto) con el
    # nombre del fixture: antes se cerraba, ahora sale.
    authority = _Authority(offer_code="gopi6lh7", anchor_type=FORM_ANCHOR, buyer_name=NAME)
    chatwoot = _Chatwoot(ATT1_CATALOG)

    decisions = _run(
        _dispatcher(authority, chatwoot, tmp_path, greeting=greeting, template=WITH_FORM_TEMPLATE)
    )

    assert decisions[-1].decision == "execute"
    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["processed_params"]["body"]["1"] == "qué tal"
    assert message["content"].startswith("Hola, qué tal. Soy del equipo de Dra. Nina.")
    assert NAME not in message["content"]
    assert authority.events[-1] == "accepted"


def test_a_safe_name_is_exactly_today(tmp_path: Path) -> None:
    authority = _Authority(
        offer_code="gopi6lh7", anchor_type=FORM_ANCHOR, buyer_name="Edith García Pérez"
    )
    chatwoot = _Chatwoot(ATT1_CATALOG)

    _run(_dispatcher(authority, chatwoot, tmp_path, greeting=True, template=WITH_FORM_TEMPLATE))

    [message] = chatwoot.posts("/conversations/200/messages")
    assert message["template_params"]["processed_params"]["body"]["1"] == "Edith"


def test_an_empty_name_still_closes(tmp_path: Path) -> None:
    # Un valor del caso que falta no es un nombre que no puede salir: sigue
    # cerrando como antes (D3).
    authority = _Authority(offer_code="gopi6lh7", anchor_type=FORM_ANCHOR, buyer_name="  ")
    chatwoot = _Chatwoot(ATT1_CATALOG)

    _run(_dispatcher(authority, chatwoot, tmp_path, greeting=True, template=WITH_FORM_TEMPLATE))

    assert chatwoot.posts("/conversations/200/messages") == []
    [finalization] = authority.finalizations
    assert finalization["reason_code"] == "template_parameters_missing"


def test_the_native_cart_of_maribel_greets_without_the_name(tmp_path: Path) -> None:
    # El paso nativo con la plantilla real de Maribel (maribel_retomar_proceso_01,
    # «Hola {{1}}, revisamos tu intento de compra»): se lee bien sin el nombre.
    authority = _CartAuthority("formulario")
    authority.context = replace(authority.context, buyer_name=NAME)
    chatwoot = _MaribelChatwoot(MARIBEL)

    asyncio.run(_native_dispatcher(authority, chatwoot, tmp_path).dispatch_due(now=NOW))

    [message] = _templates(chatwoot)
    assert message["template_params"]["processed_params"]["body"]["1"] == "qué tal"
    assert message["content"].startswith("Hola qué tal, revisamos tu intento de compra")
    assert NAME not in message["content"]
    assert authority.acceptances


def test_the_native_cart_of_maribel_without_the_neutral_greeting_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = _CartAuthority("formulario")
    authority.context = replace(authority.context, buyer_name=NAME)
    chatwoot = _MaribelChatwoot(MARIBEL)
    dispatcher = _native_dispatcher(authority, chatwoot, tmp_path)
    monkeypatch.setattr(
        dispatcher, "_waba_template", replace(dispatcher._waba_template, neutral_greeting=None)
    )

    asyncio.run(dispatcher.dispatch_due(now=NOW))

    assert _templates(chatwoot) == []
    assert authority.finalizations[0]["reason_code"] == "template_parameters_missing"
