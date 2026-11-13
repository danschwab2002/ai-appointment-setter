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
  esas lineas esta el comportamiento que nombra (una marca de texto por cita);
* que al codigo Python se lo cita por marca y no por numero de linea (E-318):
  ``archivo#nombre`` es el bloque entre ``# flow-parity: nombre`` y
  ``# flow-parity-fin: nombre``. Mover codigo arriba no rompe la cita; borrar la
  marca, duplicarla o sacar la logica de entre las dos lineas, si. A una
  migracion se la sigue citando por lineas: una migracion aplicada no se edita.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bridge.flow_parity import EXCEPTIONS_V1, FlowException
from bridge.instance_manifest import TEMPLATE_SLOTS

ROOT = Path(__file__).resolve().parents[1]
CART = frozenset({"carrito"})
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
    # Las de los varios toques (T2.1, 2026-10-21): A1, A2 y Z del carrito y el seguimiento
    # del pago rechazado. No son filas de §1.7: van con su nombre.
    ("t2.1", "a1_espera_al_cupon", CART, "despacho"),
    ("t2.1", "a1_cupon_por_flag", CART, "despacho"),
    ("t2.1", "toque_con_cupon_cierra_sin_cupon", CART_AND_PAYMENT, "despacho"),
    ("t2.1", "renovacion_adelanta_el_ultimo_toque", CART, "sql"),
    ("t2.1", "renovacion_sobrevive_a_la_respuesta", CART, "sql"),
    ("t2.1", "boton_con_off_discount", CART_AND_PAYMENT, "despacho"),
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
    "a1_espera_al_cupon": 'CartDiscountFallback("recovery_coupon_not_ready_in_time")',
    "a1_cupon_por_flag": "if settings.recovery_discount_first_touch_enabled:",
    "toque_con_cupon_cierra_sin_cupon": "reason=CART_LAST_TOUCH_COUPON_NOT_READY_IN_TIME, retryable=False",
    "renovacion_adelanta_el_ultimo_toque": "set due_at = least(action.due_at, now())",
    "renovacion_sobrevive_a_la_respuesta": "and not public._recovery_renewal_requested(v_action.id)",
    "boton_con_off_discount": "followup_button_suffix(reservation.checkout_url_final, coupon.code)",
}

_NAME = re.compile(r"[a-z][a-z0-9_]{2,59}")
_CITATION = re.compile(r"(?P<path>[^:#]+):(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?")
_ANCHORED = re.compile(r"(?P<path>[^:#]+\.py)#(?P<anchor>[a-z][a-z0-9_]{2,59})")
# Lo unico que se cita por lineas: una migracion aplicada no se edita, sus lineas no se mueven.
_BY_LINES = re.compile(r"supabase/migrations/[0-9]{14}_[a-z0-9_]+\.sql")
_START = re.compile(r"\s*# flow-parity: (?P<anchor>[a-z][a-z0-9_]{2,59})\s*")
_END = re.compile(r"\s*# flow-parity-fin: (?P<anchor>[a-z][a-z0-9_]{2,59})\s*")
LAYERS = {"plan", "puerta", "despacho", "sql"}


def _anchored_block(lines: list[str], anchor: str, where: str) -> list[str]:
    """Las lineas entre ``# flow-parity: <anchor>`` y ``# flow-parity-fin: <anchor>``, sin las marcas."""
    starts = [i for i, line in enumerate(lines) if (m := _START.fullmatch(line)) and m["anchor"] == anchor]
    ends = [i for i, line in enumerate(lines) if (m := _END.fullmatch(line)) and m["anchor"] == anchor]
    assert len(starts) == 1, f"{where}: la marca de inicio aparece {len(starts)} veces"
    assert len(ends) == 1, f"{where}: la marca de fin aparece {len(ends)} veces"
    [start], [end] = starts, ends
    assert start + 1 < end, f"{where}: el bloque marcado esta vacio o al reves"
    return lines[start + 1:end]


def _cited_lines(exception: FlowException) -> list[str]:
    if anchored := _ANCHORED.fullmatch(exception.cita):
        # La marca lleva el nombre de la excepcion: una cita no puede tomar prestado el bloque de otra.
        assert anchored["anchor"] == exception.name, exception.cita
        source = ROOT / anchored["path"]
        assert source.is_file(), exception.cita
        return _anchored_block(source.read_text(encoding="utf-8").splitlines(), anchored["anchor"], exception.cita)
    match = _CITATION.fullmatch(exception.cita)
    assert match, exception.cita
    assert _BY_LINES.fullmatch(match["path"]), f"{exception.cita}: al codigo se lo cita por marca, no por linea"
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


def test_every_exception_is_sql_or_the_dispatchers() -> None:
    assert {exception.name: exception.layer for exception in EXCEPTIONS_V1} == {
        name: layer for _, name, _, layer in EXPECTED}
    assert all(exception.layer in LAYERS for exception in EXCEPTIONS_V1)
    # De las de §1.7, solo el nombre inseguro es del despachador; de los varios toques, lo que
    # arma el bridge (el cupon de A1, el toque que espera al cupon 2 y el boton).
    assert [exception.name for exception in EXCEPTIONS_V1 if exception.layer == "despacho"] == [
        "nombre_inseguro_cierra", "a1_espera_al_cupon", "a1_cupon_por_flag", "toque_con_cupon_cierra_sin_cupon",
        "boton_con_off_discount"]


def test_only_the_several_touches_ones_are_by_step() -> None:
    # Las de la T2.1 las aplica codigo que elige por el paso: no las hereda todo flujo del motor.
    assert [exception.name for exception in EXCEPTIONS_V1 if exception.by_step] == [
        name for row, name, _, _ in EXPECTED if row == "t2.1"]
    # Los pasos de cada una: las de A1 van con first_contact, las de Z y el seguimiento con su paso.
    assert {exception.name: exception.step_keys for exception in EXCEPTIONS_V1 if exception.by_step} == {
        "a1_espera_al_cupon": {"first_contact"},
        "a1_cupon_por_flag": {"first_contact"},
        "toque_con_cupon_cierra_sin_cupon": {"cart_last_touch", "payment_failure_followup"},
        "renovacion_adelanta_el_ultimo_toque": {"cart_last_touch"},
        "renovacion_sobrevive_a_la_respuesta": {"cart_last_touch"},
        "boton_con_off_discount": {"first_contact", "cart_last_touch", "payment_failure_followup"},
    }


def test_the_payment_failure_followup_reuses_the_last_touch_of_the_cart() -> None:
    # toque_con_cupon_cierra_sin_cupon y boton_con_off_discount citan la Z del carrito para el pago
    # fallido tambien: su seguimiento la reusa (portable-payment-failure-followup-v1.md, «El envio»).
    source = (ROOT / "src" / "bridge" / "payment_failure_followups.py").read_text(encoding="utf-8")
    assert "compose_cart_last_touch" in source
    assert "CART_LAST_TOUCH_COUPON_NOT_READY_IN_TIME" in source


def test_no_legacy_kind_is_outside_the_template_slots() -> None:
    assert TEMPLATE_SLOTS == (
        "precheckout", "carrito", "pago_fallido", "reactivacion", "descuento", "ficha",
        "ficha_vence_manana", "ficha_vence_hoy", "identidad",
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
    # (template_greeting_name_is_safe, worker.py:1322). Que D1 haga lo que dice lo fija D1:
    # mutar esa linea la pone en rojo (el rojo 4 de §5.4).
    from test_flow_engine_dispatcher_legacy import test_d1_a_refused_name_closes_the_action_without_a_retry as d1

    from bridge.lead_first_name import template_greeting_name_is_safe

    [unsafe] = [exception for exception in EXCEPTIONS_V1 if exception.name == "nombre_inseguro_cierra"]
    assert unsafe.cita == "src/bridge/worker.py#nombre_inseguro_cierra"
    assert "template_greeting_name_is_safe(name_value)" in _cited_lines(unsafe)[0]
    [names] = [mark.args[1] for mark in d1.pytestmark if mark.name == "parametrize" and mark.args[0] == "name"]
    values = [name.values[0] if hasattr(name, "values") else name for name in names]
    assert values and not any(template_greeting_name_is_safe(value) for value in values)
    # Y el filtro no rechaza todo: un nombre de verdad pasa.
    assert template_greeting_name_is_safe("Maria")


def _source_anchors() -> dict[str, list[str]]:
    # Cada marca de inicio o de fin que hay en src/, con el archivo en que esta.
    found: dict[str, list[str]] = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if m := (_START.fullmatch(line) or _END.fullmatch(line)):
                found.setdefault(m["anchor"], []).append(path.relative_to(ROOT).as_posix())
    return found


def test_every_anchor_in_the_code_is_cited_and_every_anchored_citation_has_its_pair() -> None:
    # Una marca que ninguna excepcion cita es una cita que se borro sin limpiar el codigo (o una
    # excepcion renombrada): queda colgando y engana al que lee. Cada marca va en un solo archivo,
    # una vez de inicio y una de fin.
    cited = {
        anchored["anchor"]: anchored["path"]
        for exception in EXCEPTIONS_V1 if (anchored := _ANCHORED.fullmatch(exception.cita))}
    found = _source_anchors()
    assert set(found) == set(cited)
    assert {anchor: paths for anchor, paths in found.items()} == {
        anchor: [path, path] for anchor, path in cited.items()}


def test_no_python_code_is_cited_by_line_number() -> None:
    # El mal que arreglo E-318: una cita por linea a app.py se rompia con cualquier PR que tocara
    # app.py mas arriba. Ninguna cita de excepcion, y ningun comentario de flow_parity.py, nombra
    # un archivo .py con un numero de linea.
    assert not [exception.cita for exception in EXCEPTIONS_V1 if re.search(r"\.py:[0-9]", exception.cita)]
    source = (ROOT / "src" / "bridge" / "flow_parity.py").read_text(encoding="utf-8")
    assert not re.findall(r"[A-Za-z_]+\.py:[0-9]+", source)


@pytest.mark.parametrize(("lines", "problem"), [
    (["# flow-parity: a1_cupon_por_flag", "x = 1"], "la marca de fin aparece 0 veces"),
    (["x = 1", "# flow-parity-fin: a1_cupon_por_flag"], "la marca de inicio aparece 0 veces"),
    (["# flow-parity: a1_cupon_por_flag", "# flow-parity-fin: a1_cupon_por_flag"], "vacio o al reves"),
    (["# flow-parity-fin: a1_cupon_por_flag", "x = 1", "# flow-parity: a1_cupon_por_flag"], "vacio o al reves"),
    (["# flow-parity: a1_cupon_por_flag", "x = 1", "# flow-parity-fin: a1_cupon_por_flag",
      "    # flow-parity: a1_cupon_por_flag"], "la marca de inicio aparece 2 veces"),
])
def test_a_broken_anchor_is_red(lines: list[str], problem: str) -> None:
    with pytest.raises(AssertionError, match=problem):
        _anchored_block(lines, "a1_cupon_por_flag", "app.py#a1_cupon_por_flag")


def test_moving_the_logic_out_of_the_anchor_is_red() -> None:
    # Las marcas se quedan pero la linea que hace lo que la excepcion nombra se va de entre las dos:
    # el bloque sigue existiendo y ya no la tiene.
    name = "toque_con_cupon_cierra_sin_cupon"
    [touch] = [exception for exception in EXCEPTIONS_V1 if exception.name == name]
    lines = (ROOT / "src" / "bridge" / "cart_followups.py").read_text(encoding="utf-8").splitlines()
    assert MARKS[name] in "\n".join(_anchored_block(lines, name, touch.cita))
    [doing] = [i for i, line in enumerate(lines) if MARKS[name] in line]
    end = next(i for i, line in enumerate(lines) if (m := _END.fullmatch(line)) and m["anchor"] == name)
    moved = lines[:doing] + lines[doing + 1:end + 1] + [lines[doing]] + lines[end + 1:]
    block = _anchored_block(moved, name, touch.cita)
    assert block and MARKS[name] not in "\n".join(block)
