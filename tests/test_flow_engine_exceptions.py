"""El catalogo de excepciones del motor (T1): las «E» de §1.7 del carrito y el pago fallido.

Diseno del motor de flujos, §1.7, §2.8 y §5.3. ``EXCEPTIONS_V1`` vive en
``flow_parity`` desde T1 porque la paridad lo usa, y pasa a ``flow_engine`` en
T3. Esta prueba fija:

* que cada nombre aparece una vez y tiene la forma del contrato;
* que hay una entrada por cada «E» de §1.7 del carrito y el pago fallido (la
  1, 2, 2b, 3, 4, 4b, 4c, 5, 5b y 6), en ese orden, con sus flujos;
* la capa de cada una: todas ``sql`` salvo la 5, que es del despachador y la
  respalda D1 (``tests/test_flow_engine_dispatcher_legacy.py``);
* que ningun ``legacy_kind`` queda fuera de ``TEMPLATE_SLOTS``
  (``src/bridge/instance_manifest.py:69``);
* que cada cita apunta a un archivo del repo, a lineas que existen, y que en
  esas lineas esta el comportamiento que nombra (una marca de texto por cita).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bridge.flow_parity import EXCEPTIONS_V1, FlowException
from bridge.instance_manifest import TEMPLATE_SLOTS

ROOT = Path(__file__).resolve().parents[1]
CART_AND_PAYMENT = frozenset({"carrito", "pago_fallido"})
WITH_FIRST_CONTACT = frozenset({"carrito", "pago_fallido", "precheckout"})

# Las «E» de §1.7 del carrito y el pago fallido, en su orden: la fila, el
# nombre, los flujos de la columna «Flujo» y la capa (§2.8).
EXPECTED = (
    ("1", "toma_humana_en_su_conversacion", CART_AND_PAYMENT, "sql"),
    ("2", "caso_hotmart_pausado_frena", CART_AND_PAYMENT, "sql"),
    ("2b", "compra_ambigua_pausa", WITH_FIRST_CONTACT, "sql"),
    ("3", "sin_horario_escala", WITH_FIRST_CONTACT, "sql"),
    ("4", "intencion_viva", CART_AND_PAYMENT, "sql"),
    ("4b", "compra_conocida_antes_del_plan", WITH_FIRST_CONTACT, "sql"),
    ("4c", "conflicto_de_compra_frena_todo", WITH_FIRST_CONTACT, "sql"),
    ("5", "nombre_inseguro_cierra", WITH_FIRST_CONTACT, "despacho"),
    ("5b", "fallo_permanente_deja_el_caso_abierto", WITH_FIRST_CONTACT, "sql"),
    ("6", "persona_es_el_contacto", WITH_FIRST_CONTACT, "sql"),
)

# Lo que tiene que estar en las lineas citadas de cada una: el codigo de hoy
# que hace lo que la excepcion nombra.
MARKS = {
    "toma_humana_en_su_conversacion": "human_takeover_active",
    "caso_hotmart_pausado_frena": "'grace_period', 'active', 'paused'",
    "compra_ambigua_pausa": "fail_closed_ambiguous_known_purchase",
    "sin_horario_escala": "no_business_window_before_expiry",
    "intencion_viva": "pilot_audience_allowed",
    "compra_conocida_antes_del_plan": "stop_cart_recovery_for_known_purchase",
    "conflicto_de_compra_frena_todo": "unresolved_purchase_semantic_conflict",
    "nombre_inseguro_cierra": "retryable=False",
    "fallo_permanente_deja_el_caso_abierto": "permanent_failed",
    "persona_es_el_contacto": "rc.contact_id = p_contact_id",
}

_NAME = re.compile(r"[a-z][a-z0-9_]{2,59}")
_CITATION = re.compile(r"(?P<path>[^:]+):(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?")
LAYERS = {"plan", "puerta", "despacho", "sql"}


def _cited_lines(exception: FlowException) -> list[str]:
    match = _CITATION.fullmatch(exception.cita)
    assert match, exception.cita
    source = ROOT / match["path"]
    assert source.is_file(), exception.cita
    lines = source.read_text(encoding="utf-8").splitlines()
    start, end = int(match["start"]), int(match["end"] or match["start"])
    assert start <= end <= len(lines), exception.cita
    return lines[start - 1:end]


def test_the_names_are_unique_and_have_the_contracts_form() -> None:
    names = [exception.name for exception in EXCEPTIONS_V1]
    assert len(names) == len(set(names))
    assert all(_NAME.fullmatch(name) for name in names)


def test_there_is_one_entry_for_each_e_of_the_cart_and_the_payment_failure_in_order() -> None:
    assert [(exception.name, exception.legacy_kinds) for exception in EXCEPTIONS_V1] == [
        (name, kinds) for _, name, kinds, _ in EXPECTED]


def test_every_exception_is_sql_except_the_unsafe_name_which_is_the_dispatchers() -> None:
    assert {exception.name: exception.layer for exception in EXCEPTIONS_V1} == {
        name: layer for _, name, _, layer in EXPECTED}
    assert all(exception.layer in LAYERS for exception in EXCEPTIONS_V1)
    assert [exception.name for exception in EXCEPTIONS_V1 if exception.layer == "despacho"] == [
        "nombre_inseguro_cierra"]


def test_no_legacy_kind_is_outside_the_template_slots() -> None:
    assert TEMPLATE_SLOTS == (
        "precheckout", "carrito", "pago_fallido", "reactivacion", "descuento", "ficha",
    )
    for exception in EXCEPTIONS_V1:
        assert exception.legacy_kinds and exception.legacy_kinds <= set(TEMPLATE_SLOTS), exception.name


@pytest.mark.parametrize("exception", EXCEPTIONS_V1, ids=lambda exception: exception.name)
def test_each_citation_points_at_the_code_that_does_it(exception: FlowException) -> None:
    cited = "\n".join(_cited_lines(exception))
    assert MARKS[exception.name] in cited
    assert exception.descripcion.strip()


def test_the_unsafe_name_is_backed_by_d1_of_the_dispatcher() -> None:
    # D1 corre el despachador real con nombres que no son un nombre y ve que cierra sin
    # reintento. Aca no se busca su texto: se toma la prueba como objeto, y se mira que sus
    # nombres son los que rechaza el filtro real que usa la linea citada
    # (template_greeting_name_is_safe, worker.py:1210). Que D1 haga lo que dice lo fija D1:
    # mutar la linea 1195 la pone en rojo (el rojo 4 de §5.4).
    from test_flow_engine_dispatcher_legacy import test_d1_a_refused_name_closes_the_action_without_a_retry as d1

    from bridge.lead_first_name import template_greeting_name_is_safe

    [unsafe] = [exception for exception in EXCEPTIONS_V1 if exception.name == "nombre_inseguro_cierra"]
    assert unsafe.cita == "src/bridge/worker.py:1210-1226"
    assert "template_greeting_name_is_safe(name_value)" in _cited_lines(unsafe)[0]
    [names] = [mark.args[1] for mark in d1.pytestmark if mark.name == "parametrize" and mark.args[0] == "name"]
    values = [name.values[0] if hasattr(name, "values") else name for name in names]
    assert values and not any(template_greeting_name_is_safe(value) for value in values)
    # Y el filtro no rechaza todo: un nombre de verdad pasa.
    assert template_greeting_name_is_safe("Maria")
