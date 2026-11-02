"""Las cuatro ramas del pago rechazado en el esquema, el validador y la regla 14 (P-16, E-306, fase A del plan
de E-302: A1 a A3; esquema 1.4, flow-engine-v2 §27).

Por que: Dan decidio el 2026-11-02 que el pago rechazado de Maribel lleve las cuatro versiones de ATT1 (D1
«Las 4»), por el motor nativo (D4), con el boton de nuestro link (D5) y el cupon fijo PROTOCOLO10 (D10). El
motor nativo mandaba una sola plantilla sin boton, y el esquema no tenia como escribir «si ya tenia el
descuento, A; si no, si van dos rechazos, B; si no, si fue sin saldo, C; si no, D».

Datos:

* Las cuatro plantillas son las de Maribel (``maribel_pago_rechazado_{cupon,dos_veces,saldo,banco}_01``),
  RECONSTRUIDAS del pedido que ``meta_plantillas.py`` sube a Meta desde la fuente del OS
  (``tests/fixtures/chatwoot_message_templates_inbox_13_maribel_pago_rechazado_reconstruida_20261102.json``):
  el checkout con variable primero y «No más mensajes» despues, MARKETING. Hay que reemplazarlas por la
  captura del inbox 13.
* El resto del flujo es el pago rechazado de Maribel con los valores de hoy
  (``test_flow_engine_native_payment_failure.PAYMENT``, medidos en maribel_db el 2026-10-30).

Lo que fijan: la definicion de Maribel normaliza, se publica igual y pasa la regla 14 nativa y el validador
del ciclo con su catalogo; los datos nuevos valen solo con ``pago_fallido``; la cadena solo en el ``si_no`` y
hasta tres niveles; la regla 14 nativa frena lo que la base y el bridge no ejecutan (otro dato, la cadena que
no va al final, una rama con mas de una plantilla, el cupon personal), y la compra, el carrito y el primer
contacto nativos no cambian.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import test_flow_change_api_app as app_tests
import test_flow_engine_native_template_shape as shape
from test_flow_engine_native_cart import CART
from test_flow_engine_native_payment_failure import PAYMENT

from bridge.flow_definition import (
    CONDITION_CHAIN_MAX_LEVELS,
    CONDITION_DATA,
    PAYMENT_FAILURE_CONDITION_DATA,
    as_published,
    check_definition,
    condition_chain,
)
from bridge.flow_engine import (
    ENGINE_CAPABILITIES_NATIVE,
    NATIVE_CAPABILITIES_BY_TRIGGER,
    native_not_supported,
    native_payment_failure_branches,
)

FIXTURES = Path(__file__).parent / "fixtures"
RECONSTRUCTED = json.loads((FIXTURES / (
    "chatwoot_message_templates_inbox_13_maribel_pago_rechazado_reconstruida_20261102.json")).read_text(
        encoding="utf-8"))
TEMPLATES = {template["name"]: template for template in RECONSTRUCTED["message_templates"]}
BRANCH_NAMES = ("cupon", "dos_veces", "saldo", "banco")
BUTTON = {"link": "checkout", "cupon": "PROTOCOLO10"}


def _send(branch: str, **extra: Any) -> dict[str, Any]:
    return {"plantilla": f"maribel_pago_rechazado_{branch}_01", "idioma": "es_MX",
            "variables": ["nombre", "producto"], "boton": dict(BUTTON), **extra}


def _chain(*, last: list | None = None) -> dict[str, Any]:
    """La cadena del plan de E-302 (§5.1), en el orden de ATT1."""
    return {
        "condicion": {"dato": "descuento_previo", "es": ["si"]},
        "entonces": [_send("cupon")],
        "si_no": [{
            "condicion": {"dato": "rechazos", "es": ["varios"]},
            "entonces": [_send("dos_veces")],
            "si_no": [{
                "condicion": {"dato": "tipo_de_rechazo", "es": ["sin_saldo"]},
                "entonces": [_send("saldo")],
                "si_no": [_send("banco")] if last is None else last,
            }],
        }],
    }


def _maribel(**change: Any) -> dict[str, Any]:
    definition = copy.deepcopy(PAYMENT)
    definition["pasos"] = [{"esperar": "15m"}, _chain()]
    definition.update(change)
    return definition


MARIBEL = _maribel()


def _codes(findings: Any) -> set[str]:
    return {finding.code for finding in findings}


def test_the_fixture_is_the_four_templates_of_maribel_with_the_checkout_first() -> None:
    assert RECONSTRUCTED["_capture"]["reconstruida_el"] == "2026-11-02"
    assert set(TEMPLATES) == {f"maribel_pago_rechazado_{branch}_01" for branch in BRANCH_NAMES}
    for template in TEMPLATES.values():
        assert (template["status"], template["category"], template["language"]) == ("APPROVED", "MARKETING", "es_MX")
        [buttons] = [c["buttons"] for c in template["components"] if c["type"] == "BUTTONS"]
        assert [(b["type"], b.get("url")) for b in buttons] == [
            ("URL", "https://pay.hotmart.com/{{1}}"), ("QUICK_REPLY", None)]
        assert buttons[1]["text"] == "No más mensajes"


# -- A1: el esquema 1.4 ----------------------------------------------------------------------------


def test_the_three_payment_failure_data_are_condition_data() -> None:
    assert PAYMENT_FAILURE_CONDITION_DATA == ("tipo_de_rechazo", "rechazos", "descuento_previo")
    assert set(PAYMENT_FAILURE_CONDITION_DATA) <= set(CONDITION_DATA)
    assert {"ventana", "medio_de_pago"} <= set(CONDITION_DATA)
    assert CONDITION_CHAIN_MAX_LEVELS == 3


def test_the_maribel_definition_normalizes_and_publishes_the_same() -> None:
    # La normalizacion del ciclo (con el mundo y la instancia) la fija el test del ciclo, mas abajo.
    assert check_definition(MARIBEL) == ()
    published = as_published(copy.deepcopy(MARIBEL), 1)
    assert published["pasos"] == MARIBEL["pasos"]
    assert check_definition(published, published=True) == ()


def test_the_chain_reads_in_the_order_of_att1() -> None:
    chain = condition_chain(_chain())
    assert chain is not None
    assert [condition and condition["dato"] for condition, _ in chain] == [
        "descuento_previo", "rechazos", "tipo_de_rechazo", None]
    assert [steps[0]["plantilla"] for _, steps in chain] == [
        f"maribel_pago_rechazado_{branch}_01" for branch in BRANCH_NAMES]
    assert condition_chain({"esperar": "15m"}) is None
    # Sin si_no, la ultima rama es vacia.
    assert condition_chain({"condicion": {"dato": "rechazos", "es": ["uno"]}, "entonces": []})[-1] == (None, [])


@pytest.mark.parametrize(("datum", "value"), [
    ("tipo_de_rechazo", "otro"), ("tipo_de_rechazo", "insufficient_funds"), ("rechazos", "dos"),
    ("descuento_previo", "true"),
])
def test_a_value_that_does_not_exist_is_a_form_error(datum: str, value: str) -> None:
    definition = _maribel()
    definition["pasos"][1]["condicion"] = {"dato": datum, "es": [value]}
    findings = check_definition(definition)
    assert [(f.code, f.path) for f in findings] == [("invalid_value", "/pasos/1/condicion/es/0")]


# -- A2: el validador -----------------------------------------------------------------------------


@pytest.fixture
def pago_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SETTER_VERSION", "1.28.0")
    monkeypatch.setenv("GIT_SHA", "bc1e63b0bc1e63b0bc1e63b0bc1e63b0bc1e63b0")
    templates = [t for t in app_tests.CATALOG["message_templates"] if t.get("name") not in TEMPLATES]
    monkeypatch.setitem(app_tests.CATALOG, "message_templates", [*templates, *copy.deepcopy(list(TEMPLATES.values()))])


def _validate(monkeypatch: pytest.MonkeyPatch, pasos: list, *, dispara: dict | None = None,
              catalog_change: Any = None) -> tuple[dict, dict]:
    def item(steps: list) -> dict:
        definition = {key: copy.deepcopy(PAYMENT[key]) for key in ("nombre", "dispara", "entra", "termina", "horario")}
        if dispara is not None:
            definition["dispara"] = dispara
        return {"producto": "att1", "flujo": "pago_rechazado", "tipo": "nuevo", "prender": "cohorte",
                "definicion": {**definition, "pasos": steps}}

    monkeypatch.setattr(shape, "_item", item)
    if catalog_change is not None:
        catalog_change(app_tests.CATALOG["message_templates"])
    return shape._propose_and_validate(copy.deepcopy(pasos))


def test_the_cycle_validates_the_four_branches_of_maribel(pago_catalog: None, monkeypatch: pytest.MonkeyPatch) -> None:
    definition, result = _validate(monkeypatch, MARIBEL["pasos"])

    assert definition["pasos"] == MARIBEL["pasos"]
    assert result["blocks"] == [], result["blocks"]
    assert result["status"] == "validada"


def test_each_branch_is_checked_against_the_catalog(pago_catalog: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def drop_saldo(templates: list) -> None:
        templates[:] = [t for t in templates if t.get("name") != "maribel_pago_rechazado_saldo_01"]

    _, result = _validate(monkeypatch, MARIBEL["pasos"], catalog_change=drop_saldo)

    assert result["status"] == "borrador"
    assert ("template_not_in_catalog", "/0/definicion/pasos/1/si_no/0/si_no/0/entonces/0/plantilla") in \
        shape._blocks(result)


def test_a_branch_template_without_the_checkout_first_is_blocked(pago_catalog: None,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    def quick_reply_first(templates: list) -> None:
        for template in templates:
            if template.get("name") == "maribel_pago_rechazado_banco_01":
                [group] = [c for c in template["components"] if c["type"] == "BUTTONS"]
                group["buttons"].reverse()

    _, result = _validate(monkeypatch, MARIBEL["pasos"], catalog_change=quick_reply_first)

    assert ("template_button_mismatch", "/0/definicion/pasos/1/si_no/0/si_no/0/si_no/0/boton") in \
        shape._blocks(result)


@pytest.mark.parametrize("datum", PAYMENT_FAILURE_CONDITION_DATA)
def test_a_payment_failure_datum_goes_only_with_the_payment_failure_trigger(datum: str) -> None:
    from bridge.flow_validator import PAYMENT_FAILURE_DATUM_SPECS

    value = {"tipo_de_rechazo": "banco", "rechazos": "uno", "descuento_previo": "no"}[datum]
    cart = copy.deepcopy(CART)
    cart["pasos"] = [{"esperar": "1h"}, {"condicion": {"dato": datum, "es": [value]},
                                         "entonces": [cart["pasos"][-1]], "si_no": []}]
    emitter = _rule_emitter()
    _validator()._rule5(emitter, cart)
    assert (PAYMENT_FAILURE_DATUM_SPECS[datum].code, ("pasos", 1, "condicion", "dato")) in emitter.found
    emitter = _rule_emitter()
    _validator()._rule5(emitter, MARIBEL)
    assert emitter.found == []


class _Emitter:
    def __init__(self) -> None:
        self.found: list[tuple[str, tuple]] = []

    def add(self, spec: Any, segments: Any = (), *, reason: str | None = None) -> None:
        self.found.append((spec.code, tuple(segments)))


def _rule_emitter() -> _Emitter:
    return _Emitter()


def _validator() -> Any:
    # Las reglas 5 y 7 no leen nada del validador: se llaman sobre la definicion sola.
    from bridge.flow_validator import _Run

    return object.__new__(_Run)


def _rule7(definition: dict) -> list[tuple[str, tuple]]:
    emitter = _rule_emitter()
    _validator()._rule7(emitter, definition)
    return [found for found in emitter.found if found[0] == "nested_condition"]


def test_the_chain_goes_only_in_the_si_no_and_up_to_three_levels() -> None:
    assert _rule7(MARIBEL) == []
    # Dos condiciones en un entonces.
    inside_then = _maribel()
    inside_then["pasos"][1]["entonces"] = [{"condicion": {"dato": "rechazos", "es": ["varios"]},
                                            "entonces": [_send("dos_veces")], "si_no": [_send("cupon")]}]
    assert _rule7(inside_then) == [("nested_condition", ("pasos", 1, "entonces", 0, "condicion"))]
    # La condicion no es el unico paso del si_no.
    not_alone = _maribel()
    not_alone["pasos"][1]["si_no"].append(_send("banco"))
    assert _rule7(not_alone) == [("nested_condition", ("pasos", 1, "si_no", 0, "condicion"))]
    # Cuatro niveles.
    four = _maribel()
    four["pasos"][1] = _chain(last=[{"condicion": {"dato": "rechazos", "es": ["uno"]},
                                     "entonces": [_send("banco")], "si_no": [_send("banco")]}])
    assert _rule7(four) == [("nested_condition", ("pasos", 1, "si_no", 0, "si_no", 0, "si_no", 0, "condicion"))]


def test_the_chain_is_still_a_nested_condition_with_another_trigger() -> None:
    cart = copy.deepcopy(CART)
    cart["pasos"] = [{"esperar": "1h"}, _chain()]
    assert _rule7(cart) == [("nested_condition", ("pasos", 1, "si_no", 0, "condicion")),
                            ("nested_condition", ("pasos", 1, "si_no", 0, "si_no", 0, "condicion"))]


# -- A3: la regla 14 y el motor ---------------------------------------------------------------------


def test_the_native_engine_runs_the_four_branches() -> None:
    assert native_not_supported(MARIBEL) == ()
    branches = native_payment_failure_branches(MARIBEL)
    assert branches is not None
    assert [step["plantilla"] for _, step in branches] == [
        f"maribel_pago_rechazado_{branch}_01" for branch in BRANCH_NAMES]
    assert all(step["boton"] == BUTTON for _, step in branches)
    # Un solo paso con el boton y sin condicion tambien: es el boton del checkout del paso nativo (D5).
    single = _maribel(pasos=[{"esperar": "15m"}, _send("banco")])
    assert native_not_supported(single) == ()
    assert native_payment_failure_branches(single) is None
    # Y el pago rechazado de hoy, sin boton, igual.
    assert native_not_supported(PAYMENT) == ()


@pytest.mark.parametrize(("change", "code"), [
    # Otro dato: el motor nativo no sabe la ventana ni el medio de pago del rechazo.
    (lambda d: d["pasos"][1].update(condicion={"dato": "ventana", "es": ["abierta"]}),
     "engine_not_supported:condicion_dato"),
    # La cadena antes de otro paso.
    (lambda d: d["pasos"].append({"esperar": "1h"}), "engine_not_supported:condicion_no_al_final"),
    # Una rama con una espera y la plantilla: la gracia es una sola, antes de elegir.
    (lambda d: d["pasos"][1].update(entonces=[{"esperar": "1h"}, _send("cupon")]),
     "engine_not_supported:rama_de_un_envio"),
    # Una rama vacia no manda nada: el motor nativo manda un envio por camino.
    (lambda d: d["pasos"][1].update(entonces=[]), "engine_not_supported:rama_de_un_envio"),
    # El cupon personal es la fase B.
    (lambda d: d["pasos"][1]["entonces"][0].update(boton={"link": "checkout", "cupon": "personal"}),
     "engine_not_supported:cupon_personal"),
    # El link de la ficha no es de este disparador (la 5) ni del motor.
    (lambda d: d["pasos"][1]["entonces"][0].update(boton={"link": "ficha"}), "engine_not_supported:boton_ficha"),
])
def test_what_the_native_engine_does_not_run_with_the_branches(change: Any, code: str) -> None:
    definition = _maribel()
    change(definition)
    assert code in native_not_supported(definition)


def test_the_purchase_the_cart_and_the_first_contact_do_not_change() -> None:
    for event in ("compra", "carrito", "intencion"):
        assert NATIVE_CAPABILITIES_BY_TRIGGER[event] is ENGINE_CAPABILITIES_NATIVE
    assert "condicion" not in ENGINE_CAPABILITIES_NATIVE.step_types
    assert ENGINE_CAPABILITIES_NATIVE.buttons == frozenset()
    cart = copy.deepcopy(CART)
    cart["pasos"] = [{"esperar": "1h"}, {"condicion": {"dato": "rechazos", "es": ["uno"]},
                                         "entonces": [cart["pasos"][-1]], "si_no": []}]
    assert "engine_not_supported:condicion" in native_not_supported(cart)
    with_button = copy.deepcopy(CART)
    with_button["pasos"][-1] = {**with_button["pasos"][-1], "boton": {"link": "checkout"}}
    assert "engine_not_supported:boton_checkout" in native_not_supported(with_button)
