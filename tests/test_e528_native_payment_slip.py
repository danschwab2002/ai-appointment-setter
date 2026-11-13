"""E-528 (C4 del mapa «el AE maneja todos los flujos», P25 de M16): la ficha de OXXO nativa.

La definicion objetivo es la del diseno (``2026-11-01-m16-diseno-seguimiento-y-ficha-nativos.md`` §2.2): C1 a
la hora con nombre, producto, la tienda y el vencimiento; la espera anclada al vencimiento; C3 el dia que vence
a las 09:00. Aca: el esquema (la espera anclada y la variable ``vencimiento``), la regla 5 de la ficha, la regla
14 nativa con ``FLOW_ENGINE_NATIVE_PAYMENT_SLIP_ENABLED`` y el flag en el runtime de la instancia.

Las plantillas son las capturadas de la bandeja 11 de ATT1
(``tests/fixtures/chatwoot_message_templates_inbox_11_ficha_recordatorios_20261019.json``, 2026-10-19):
``att1_ficha_efectivo_01`` ({{1}} nombre, {{2}} producto, {{3}} la tienda, {{4}} el vencimiento, el boton con
la URL de la ficha) y ``att1_ficha_vence_hoy_01`` ({{1}} nombre, {{2}} la tienda, {{3}} producto).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from bridge.flow_definition import (
    FICHA_DUE_DAYS,
    InstanceView,
    due_anchor_max_minutes,
    parse_due_anchor,
    check_definition,
    touch_times,
)
from bridge.flow_engine import (
    ENGINE_CAPABILITIES_NATIVE_PAYMENT_SLIP,
    NATIVE_CAPABILITIES_BY_TRIGGER,
    NATIVE_PAYMENT_SLIP_TRIGGER,
    native_not_supported,
    native_trigger_key,
)
from bridge.flow_validator import NativeRuntime, runtime_runs_payment_slip

ROOT = Path(__file__).resolve().parents[1]
SLIP_TEMPLATES = ROOT / "tests" / "fixtures" / "chatwoot_message_templates_inbox_11_ficha_recordatorios_20261019.json"

ANCHOR = {"vencimiento": {"dias_antes": 0, "a_las": "09:00"}}
C1 = {"plantilla": "att1_ficha_efectivo_01", "idioma": "es_MX",
      "variables": ["nombre", "producto", "medio_de_pago", "vencimiento"], "boton": {"link": "ficha"}}
C3 = {"plantilla": "att1_ficha_vence_hoy_01", "idioma": "es_MX",
      "variables": ["nombre", "medio_de_pago", "producto"], "boton": {"link": "ficha"}}
FICHA: dict[str, Any] = {
    "nombre": "Ficha de pago sin pagar", "dispara": {"evento": "ficha_impresa"},
    "entra": {"permiso": "formulario"},
    "pasos": [{"esperar": "60m"}, C1, {"esperar": ANCHOR}, C3],
    "termina": {"si": ["compra", "ficha_vencida"], "vence": "4d"},
    "horario": {"zona": "America/Mexico_City", "desde": "09:00", "hasta": "21:00"},
    "topes": {"bolsa": "att1-ficha-nativa", "por_dia": 20, "total": 1000,
              "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": "0m"}},
}


def _ficha(**changes: Any) -> dict[str, Any]:
    return {**copy.deepcopy(FICHA), **changes}


# -- El esquema --------------------------------------------------------------------------------------


def test_the_anchored_wait_reads_only_its_exact_shape() -> None:
    assert parse_due_anchor(ANCHOR) == (0, 9 * 60)
    assert parse_due_anchor({"vencimiento": {"dias_antes": 1, "a_las": "10:00"}}) == (1, 10 * 60)
    for wrong in ({"vencimiento": {"dias_antes": 2, "a_las": "09:00"}},
                  {"vencimiento": {"dias_antes": 0}},
                  {"vencimiento": {"dias_antes": 0, "a_las": "9:00"}},
                  {"vencimiento": {"dias_antes": True, "a_las": "09:00"}},
                  {"vencimiento": {"dias_antes": 0, "a_las": "09:00", "zona": "UTC"}},
                  {"vence": {"dias_antes": 0, "a_las": "09:00"}}, "3d", None):
        assert parse_due_anchor(wrong) is None, wrong
    # Lo mas que espera: hasta el dia del vencimiento (impresion + 3) a las 09:00.
    assert due_anchor_max_minutes(ANCHOR) == FICHA_DUE_DAYS * 24 * 60 + 9 * 60
    assert due_anchor_max_minutes("60m") is None


def _form_codes(definition: dict[str, Any]) -> set[tuple[str, str]]:
    return {(finding.code, finding.path) for finding in check_definition(definition)}


def test_the_schema_accepts_the_target_definition_and_rejects_a_bad_anchor() -> None:
    # Antes de E-528 la espera anclada era invalid_value (no es una duracion) y vencimiento no era una variable.
    assert not {path for code, path in _form_codes(_ficha()) if code == "invalid_value"}
    bad = _ficha(pasos=[{"esperar": "60m"}, C1, {"esperar": {"vencimiento": {"dias_antes": 3, "a_las": "09:00"}}}, C3])
    assert ("invalid_value", "/pasos/2/esperar") in _form_codes(bad)


def test_touch_times_stop_at_the_anchored_wait() -> None:
    assert touch_times(_ficha()) == [
        {"paso": 2, "plantilla": "att1_ficha_efectivo_01", "horas_desde_el_primero": 0.0},
        {"paso": 3, "sigue": "depende_del_vencimiento"},
    ]


# -- La regla 14 nativa ------------------------------------------------------------------------------


def test_without_the_flag_the_native_slip_is_not_a_native_trigger() -> None:
    assert native_trigger_key(_ficha()) is None
    assert "motor_nativo:disparador" in native_not_supported(_ficha())


def test_with_the_flag_the_target_definition_runs_natively() -> None:
    assert native_trigger_key(_ficha(), payment_slip=True) == NATIVE_PAYMENT_SLIP_TRIGGER
    assert NATIVE_CAPABILITIES_BY_TRIGGER[NATIVE_PAYMENT_SLIP_TRIGGER] is ENGINE_CAPABILITIES_NATIVE_PAYMENT_SLIP
    assert native_not_supported(_ficha(), payment_slip=True) == ()
    # Un solo toque (C1) tambien.
    assert native_not_supported(_ficha(pasos=[{"esperar": "60m"}, C1]), payment_slip=True) == ()


@pytest.mark.parametrize(("change", "code"), [
    # Contestar no corta C3: la salida contesta no la ejecuta la ficha.
    ({"termina": {"si": ["compra", "ficha_vencida", "contesta"], "vence": "4d"}}, "engine_not_supported:salida_contesta"),
    # Entre C1 y C3, la espera anclada; una duracion no.
    ({"pasos": [{"esperar": "60m"}, C1, {"esperar": "3d"}, C3]}, "engine_not_supported:espera_anclada"),
    # Solo el ancla de C3 (D-P25-2): otra hora, o C2, no.
    ({"pasos": [{"esperar": "60m"}, C1, {"esperar": {"vencimiento": {"dias_antes": 0, "a_las": "10:00"}}}, C3]},
     "engine_not_supported:espera_anclada"),
    ({"pasos": [{"esperar": "60m"}, C1, {"esperar": {"vencimiento": {"dias_antes": 1, "a_las": "10:00"}}}, C3]},
     "engine_not_supported:espera_anclada"),
    # El ancla antes de C1 no.
    ({"pasos": [{"esperar": ANCHOR}, C1]}, "engine_not_supported:espera_anclada"),
    # Tres envios (C2) no.
    ({"pasos": [{"esperar": "60m"}, C1, {"esperar": ANCHOR}, C3, {"esperar": "1h"}, C3]},
     "engine_not_supported:demasiados_envios"),
    # El boton es el link a la ficha, sin cupon.
    ({"pasos": [{"esperar": "60m"}, {**C1, "boton": {"link": "checkout"}}]}, "engine_not_supported:boton_checkout"),
    # Las variables, de las de la ficha.
    ({"pasos": [{"esperar": "60m"}, {**C1, "variables": ["nombre", "precio"]}]}, "engine_not_supported:variable_precio"),
    # El permiso es el formulario.
    ({"entra": {"permiso": "conversacion"}}, "motor_nativo:permiso"),
])
def test_what_the_native_slip_still_cannot_do(change: dict[str, Any], code: str) -> None:
    assert code in native_not_supported(_ficha(**change), payment_slip=True)


def test_the_flag_does_not_change_the_other_native_triggers() -> None:
    cart = _ficha(dispara={"evento": "carrito"})
    assert native_not_supported(cart) == native_not_supported(cart, payment_slip=True)


# -- La regla 5 y el validador entero ----------------------------------------------------------------


def _runtime(on: bool) -> NativeRuntime:
    return NativeRuntime(engine_enabled=True, native_intent_enabled=True, native_silence_sweeper=True,
                         cart_touches=True, main_product="att1", native_payment_slip=on)


def _findings(definition: dict[str, Any], runtime: NativeRuntime | None) -> set[str]:
    import test_flow_validator as tfv

    slips = json.loads(SLIP_TEMPLATES.read_text(encoding="utf-8"))["message_templates"]
    catalog = tfv.catalog_with(lambda payload: payload["message_templates"].extend(copy.deepcopy(slips)))
    instance = InstanceView("lancemos", "att1", frozenset({"att1"}), "America/Mexico_City", runtime=runtime)
    return {finding.code for finding in tfv.run_new(definition, "ficha_oxxo", actor="automation_expert",
                                                    instance=instance, catalog=catalog)}


def test_the_ae_publishes_the_native_slip_only_with_the_flag() -> None:
    off = _findings(_ficha(), _runtime(False))
    assert {"motor_nativo:disparador", "native_multitouch_not_running"} & off
    on = _findings(_ficha(), _runtime(True))
    # La espera de gracia antes de C1 deja la advertencia de siempre; nada bloquea.
    assert on <= {"wait_without_contesta"}


def test_vencimiento_and_the_anchor_go_only_with_the_printed_slip() -> None:
    cart_step = {"plantilla": "att1_ficha_efectivo_01", "idioma": "es_MX", "variables": ["nombre", "vencimiento"]}
    cart = _ficha(dispara={"evento": "carrito"}, pasos=[cart_step, {"esperar": ANCHOR}, cart_step],
                  termina={"si": ["compra", "contesta"], "vence": "4d"})
    codes = _findings(cart, _runtime(True))
    assert {"vencimiento_requires_ficha_trigger", "due_anchor_requires_ficha_trigger"} <= codes
    # La hora del ancla, dentro del horario.
    late = _ficha(horario={"zona": "America/Mexico_City", "desde": "10:00", "hasta": "21:00"})
    assert "due_anchor_requires_ficha_trigger" in _findings(late, _runtime(True))


def test_the_runtime_carries_the_flag_and_the_operator_input_defaults_it_off() -> None:
    from bridge.flow_change_operator import instance_from_input

    on = _runtime(True)
    assert on.to_json()["native_payment_slip_enabled"] is True
    base = {"tenant_ref": "lancemos", "ally_ref": "att1", "products": ["att1"], "time_zone": "America/Mexico_City"}
    assert instance_from_input({**base, "runtime": on.to_json()}).runtime == on
    without = {key: value for key, value in on.to_json().items() if key != "native_payment_slip_enabled"}
    assert runtime_runs_payment_slip(instance_from_input({**base, "runtime": without}).runtime) is False
    with pytest.raises(Exception):
        instance_from_input({**base, "runtime": {**on.to_json(), "native_payment_slip_enabled": "si"}})


# -- La vista previa de la ficha nativa (E-531, lo que quedo de C9 para la ficha) ---------------------------

# Los conteos que daria read_flow_engine_native_trigger_counts_v1 con ficha_impresa (20261113200000) sobre las
# 13 fichas capturadas de ATT1 (una por persona y por transaccion).
SLIP_FIXTURE = ROOT / "tests" / "fixtures" / "hotmart_payment_slip_printed_att1_20261018.json"
_SLIPS = json.loads(SLIP_FIXTURE.read_text(encoding="utf-8"))["eventos"]
SLIP_COUNTS = {"contract": "flow-engine-v2", "trigger": "ficha_impresa", "product_ref": "att1",
               "since": "2026-10-11T19:40:00Z", "until": "2026-10-18T19:40:00Z",
               "events": len({e["payload"]["data"]["purchase"]["transaction"] for e in _SLIPS}),
               "people": len({e["payload"]["data"]["buyer"]["email"] for e in _SLIPS})}


def _slip_item(definition: dict[str, Any]) -> Any:
    from bridge.flow_definition import parse_items

    [item] = parse_items([{"producto": "att1", "flujo": "ficha_nativa", "tipo": "nuevo", "definicion": definition,
                          "prender": "cohorte"}])
    return item


def test_the_preview_of_the_slip_says_c3_waits_for_the_due_day_and_has_no_basis() -> None:
    from bridge.flow_change_simulation import (
        TOUCH_AT_MOST,
        TOUCH_NO_BASIS,
        TOUCH_SLIP_NEXT,
        build_new_flow_simulation,
    )

    assert SLIP_COUNTS["events"] == 13 and SLIP_COUNTS["people"] == 13
    built = build_new_flow_simulation(SLIP_COUNTS, [_slip_item(FICHA)])
    assert built["status"] == "built", built
    summary = built["summary"]
    assert summary["disparador"] == "ficha_impresa" and summary["personas"] == 13
    assert summary["toques"] == [
        {"toque": 1, "espera": "60m", "plantilla": "att1_ficha_efectivo_01", "llegan": 13, "cota": TOUCH_AT_MOST},
        {"toque": 2, "espera": None, "espera_vencimiento": {"dias_antes": 0, "a_las": "09:00"},
         "plantilla": "att1_ficha_vence_hoy_01", "llegan": None, "cota": TOUCH_NO_BASIS, "por_que": TOUCH_SLIP_NEXT},
    ]
    # La cota de mensajes: dos toques por persona, como maximo.
    assert summary["mensajes_si_fuera_para_todos"] == 26 and summary["mensajes_cota"] == TOUCH_AT_MOST
    # Con solo C1, el resumen de antes: sin toques.
    one = build_new_flow_simulation(SLIP_COUNTS, [_slip_item(_ficha(pasos=[{"esperar": "60m"}, C1]))])
    assert "toques" not in one["summary"] and one["summary"]["mensajes_si_fuera_para_todos"] == 13


def test_the_approval_page_says_the_due_day_of_c3_in_words() -> None:
    from bridge.flow_approval import _simulation_summary
    from bridge.flow_change_simulation import build_new_flow_simulation

    built = build_new_flow_simulation(SLIP_COUNTS, [_slip_item(FICHA)])
    html = _simulation_summary(built)
    assert "Mensaje 1 (60 minutos después): como máximo <strong>13</strong> personas" in html
    assert "Mensaje 2 (el día que vence la ficha, a las 09:00): sin base para estimarlo: depende de quién paga " \
           "la ficha antes de que venza" in html
