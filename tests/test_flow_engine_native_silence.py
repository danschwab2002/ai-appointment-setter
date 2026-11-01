"""El seguimiento a 24 h con cupon, nativo: el esquema y el validador (P22-1 de M16, flow-engine-v2 2.12.0).

El diseno es el del seguimiento y la ficha nativos (2026-11-01, en el vault), §1.2 (a). Este PR no
tiene migracion: la base sigue sin abrir un silencio nativo (P22-2) y no hay barrido nativo (P22-3).

Datos capturados:

* ``chatwoot_inbox_11_message_templates_20261010.json``: el catalogo de la bandeja 11 de ATT1, leido de
  Chatwoot el 2026-10-10. Trae la plantilla del seguimiento con cupon de ATT1,
  ``att1_seguimiento_descuento_01`` (APPROVED, MARKETING, es_MX, un solo boton URL al checkout). La de
  Maribel (``maribel_seguimiento_descuento_01``) no esta capturada: la definicion de Maribel se valida
  aca con la de ATT1, que tiene la misma forma.
* ``chatwoot_followup_candidate_inbox_11_20261010.json``: la conversacion 21 de ATT1, una candidata real
  del seguimiento. La persona contesto la plantilla **del carrito** y el agente le mando el link: la
  conversacion adoptada que pide el legado viene de una plantilla de otro flujo, y eso es lo que
  ``contesto_a: cualquier_plantilla`` escribe.

Lo que fijan:

* la regla 14 nativa deja pasar el seguimiento de Maribel escrito en ``setter-flujo/v1`` (§1.1 del
  diseno), y frena cada cosa que el seguimiento de hoy no hace: el silencio de la persona, otro
  permiso, otro ``contesto_a``, sin boton o sin cupon, el cupon personal, una espera, dos envios y
  otra repeticion;
* ``contesto_a: cualquier_plantilla`` es un texto del esquema (regla 1), va solo con
  ``permiso: conversacion`` (regla 3) y no depende de que flujos estan registrados (regla 10);
* la regla 13 pide un solo boton en un nativo con el checkout, y en el legado no cambia;
* el cupon con ``+`` (``PROTOCOLO10+``, el del link del agente) sigue frenado por la forma;
* los flujos de hoy validan igual.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_flow_engine_native_cart import CART
from test_flow_engine_native_payment_failure import PAYMENT
from test_flow_validator import (
    CATALOG,
    INBOX_11,
    catalog_with,
    check,
    flow,
    flow_def,
    found,
    run,
    template_of,
    version_item,
    world,
)

from bridge import flow_definition as fd
from bridge.flow_definition import ANY_TEMPLATE, ProposalItem, check_definition
from bridge.flow_engine import (
    ENGINE_CAPABILITIES_NATIVE,
    ENGINE_CAPABILITIES_NATIVE_SILENCE,
    NATIVE_CAPABILITIES_BY_TRIGGER,
    NATIVE_PERMISSION_BY_TRIGGER,
    NATIVE_SILENCE_TRIGGER,
    native_not_supported,
    native_trigger_key,
)
from bridge.followup_discount import parse_followup_template

FIXTURES = Path(__file__).parent / "fixtures"
CANDIDATE = json.loads((FIXTURES / "chatwoot_followup_candidate_inbox_11_20261010.json").read_text(encoding="utf-8"))
TEMPLATE = "att1_seguimiento_descuento_01"

# El seguimiento de Maribel con los valores de hoy (§1.1 del diseno): el silencio de 24 a 72 h con lo
# ultimo del agente, en conversaciones abiertas y resueltas; la conversacion adoptada de cualquier
# plantilla; un mensaje con el boton del checkout y PROTOCOLO10; 09:00-20:59 en CDMX; sin tope; una vez
# por conversacion. La plantilla es la capturada de ATT1 (la de Maribel no esta capturada).
FOLLOWUP: dict[str, Any] = {
    "esquema": "setter-flujo/v1", "flujo": "seguimiento_cupon", "producto": "att1", "estado": "borrador",
    "nombre": "Seguimiento con cupón",
    "dispara": {"silencio": {"lo_ultimo": "agente", "desde": "24h", "hasta": "72h",
                             "conversaciones": ["abierta", "resuelta"]}},
    "entra": {"permiso": "conversacion", "contesto_a": ANY_TEMPLATE},
    "pasos": [{"plantilla": TEMPLATE, "idioma": "es_MX", "variables": ["nombre", "producto"],
               "boton": {"link": "checkout", "cupon": "PROTOCOLO10"}}],
    "termina": {"si": ["compra", "contesta"]},
    "horario": {"zona": "America/Mexico_City", "desde": "09:00", "hasta": "20:59"},
    "topes": {"bolsa": "seguimiento-cupon", "por_dia": "sin_tope", "total": "sin_tope",
              "repeticion": {"una_por": "conversacion", "otra_despues_de": "nunca"}},
    "prioridad": {"le_gana_a": [], "no_arranca_mientras": []},
}


def _followup(**changes: Any) -> dict[str, Any]:
    definition = copy.deepcopy(FOLLOWUP)
    definition.update(copy.deepcopy(changes))
    return definition


def _new(definition: dict[str, Any], *, key: str = "seguimiento_cupon") -> ProposalItem:
    return ProposalItem(producto="att1", flujo=key, flow_id=None, tipo="nuevo", definicion=definition,
                        prender="cohorte")


def _blocks(definition: dict[str, Any], **kwargs: Any) -> set[str]:
    kwargs.setdefault("catalog", CATALOG)
    result = check([_new(definition)], actor="automation_expert", draft_base={}, **kwargs)
    return {finding.code for finding in result.blocks}


# -- Los fixtures --------------------------------------------------------------


def test_the_fixtures_are_the_captured_followup_template_and_an_adopted_cart_conversation() -> None:
    template = template_of(INBOX_11, TEMPLATE)
    assert (template["status"], template["category"], template["language"]) == ("APPROVED", "MARKETING", "es_MX")
    [buttons] = [component["buttons"] for component in template["components"] if component["type"] == "BUTTONS"]
    assert [(button["type"], button["url"]) for button in buttons] == [("URL", "https://pay.hotmart.com/{{1}}")]
    # Lo que manda el barrido de hoy: exactamente esa forma.
    parsed = parse_followup_template(INBOX_11, template_name=TEMPLATE, expected_language="es_MX")
    assert parsed.name == TEMPLATE
    # La candidata adopto la plantilla del carrito, no la del seguimiento: la adopcion es de otro flujo.
    # El primer mensaje es el cuerpo de la plantilla del carrito (con el nombre anonimizado), la persona
    # contesta y el agente manda el link.
    cart = next(c["text"] for c in template_of(INBOX_11, "att1_carrito_abandonado_01")["components"]
                if c["type"] == "BODY")
    first, reply, link = CANDIDATE["conversation"]["messages"]
    assert first["content"].startswith(cart.split("{{2}}", 1)[0].replace("{{1}}", "NOMBRE"))
    assert [message["message_type"] for message in (first, reply, link)] == [1, 0, 1]
    assert "att1_carrito_abandonado_01" in CANDIDATE["_capture"]["caso"]
    assert "inbound_adopted_template_conversation" in CANDIDATE["_capture"]["caso"]


# -- La regla 14 nativa ----------------------------------------------------------


def test_the_capabilities_are_per_trigger() -> None:
    assert dict(NATIVE_CAPABILITIES_BY_TRIGGER) == {
        "compra": ENGINE_CAPABILITIES_NATIVE, "carrito": ENGINE_CAPABILITIES_NATIVE,
        "pago_fallido": ENGINE_CAPABILITIES_NATIVE, NATIVE_SILENCE_TRIGGER: ENGINE_CAPABILITIES_NATIVE_SILENCE}
    assert NATIVE_PERMISSION_BY_TRIGGER[NATIVE_SILENCE_TRIGGER] == "conversacion"
    silence = ENGINE_CAPABILITIES_NATIVE_SILENCE
    assert (silence.max_sends_per_path, silence.waits_between_sends) == (1, False)
    assert silence.step_types == {"plantilla"} and silence.buttons == {"checkout"}
    assert silence.exits == {"compra", "contesta"} and silence.variables == {"nombre", "producto"}
    # Los eventos no cambian: el silencio no esta en sus capacidades.
    assert ENGINE_CAPABILITIES_NATIVE.triggers == {"compra", "carrito", "pago_fallido"}
    assert ENGINE_CAPABILITIES_NATIVE.buttons == frozenset() and ENGINE_CAPABILITIES_NATIVE.exits == {"compra"}


@pytest.mark.parametrize(("dispara", "key"), [
    ({"evento": "compra"}, "compra"),
    ({"evento": "pago_fallido"}, "pago_fallido"),
    ({"silencio": {"lo_ultimo": "agente"}}, NATIVE_SILENCE_TRIGGER),
    ({"silencio": {"lo_ultimo": "persona"}}, None),
    ({"evento": "ficha_impresa"}, None),
    ({"evento": "compra", "silencio": {"lo_ultimo": "agente"}}, None),
    ("silencio", None),
    # Un evento con el nombre del silencio no es el silencio.
    ({"evento": "silencio:agente"}, None),
])
def test_the_native_trigger_key(dispara: object, key: str | None) -> None:
    assert native_trigger_key({"dispara": dispara}) == key


def test_the_native_engine_takes_the_maribel_followup() -> None:
    # Los valores de Maribel (§1.1 del diseno) con la plantilla y el producto de ATT1: la de Maribel no esta
    # capturada (tests/fixtures/instances/maribel/instancia.toml sigue con <COMPLETAR>).
    assert native_not_supported(FOLLOWUP) == ()


@pytest.mark.parametrize(("change", "code"), [
    # La reactivacion queda fuera de M16.
    ({"dispara": {"silencio": {"lo_ultimo": "persona", "desde": "24h", "hasta": "72h"}}},
     "motor_nativo:disparador"),
    ({"entra": {"permiso": "formulario", "contesto_a": ANY_TEMPLATE}}, "motor_nativo:permiso"),
    ({"entra": {"permiso": "conversacion", "contesto_a": []}}, "engine_not_supported:contesto_a"),
    ({"entra": {"permiso": "conversacion", "contesto_a": ["carrito"]}}, "engine_not_supported:contesto_a"),
    ({"entra": {"permiso": "conversacion"}}, "engine_not_supported:contesto_a"),
    ({"pasos": [{"plantilla": TEMPLATE, "idioma": "es_MX", "variables": ["nombre", "producto"]}]},
     "engine_not_supported:boton_sin_cupon"),
    ({"pasos": [{"plantilla": TEMPLATE, "idioma": "es_MX", "variables": ["nombre", "producto"],
                 "boton": {"link": "checkout"}}]}, "engine_not_supported:boton_sin_cupon"),
    ({"pasos": [{"plantilla": TEMPLATE, "idioma": "es_MX", "variables": ["nombre", "producto"],
                 "boton": {"link": "checkout", "cupon": "personal"}}]}, "engine_not_supported:cupon_personal"),
    ({"pasos": [{"plantilla": TEMPLATE, "idioma": "es_MX", "variables": ["nombre", "producto"],
                 "boton": {"link": "ficha"}}]}, "engine_not_supported:boton_ficha"),
    ({"pasos": [{"esperar": "1h"}, copy.deepcopy(FOLLOWUP["pasos"][0])]}, "engine_not_supported:esperar"),
    ({"pasos": [copy.deepcopy(FOLLOWUP["pasos"][0]), {"esperar": "24h"}, copy.deepcopy(FOLLOWUP["pasos"][0])]},
     "engine_not_supported:demasiados_envios"),
    ({"pasos": [{**copy.deepcopy(FOLLOWUP["pasos"][0]), "variables": ["nombre", "precio"]}]},
     "engine_not_supported:variables_differ"),
    ({"topes": {**FOLLOWUP["topes"], "repeticion": {"una_por": "persona", "otra_despues_de": "nunca"}}},
     "motor_nativo:repeticion"),
    ({"topes": {**FOLLOWUP["topes"], "repeticion": {"una_por": "conversacion", "otra_despues_de": "24h"}}},
     "motor_nativo:repeticion"),
    ({"termina": {"si": ["compra", "ficha_vencida"]}}, "engine_not_supported:salida_ficha_vencida"),
    # El barrido corta con la compra y con la respuesta: la definicion lo dice.
    ({"termina": {"si": ["compra"]}}, "engine_not_supported:salida_compra_y_contesta"),
    ({"termina": {}}, "engine_not_supported:salida_compra_y_contesta"),
    # FollowupTemplate.params manda nombre y producto, en ese orden.
    ({"pasos": [{**copy.deepcopy(FOLLOWUP["pasos"][0]), "variables": ["producto", "nombre"]}]},
     "engine_not_supported:variables_seguimiento"),
    ({"pasos": [{**copy.deepcopy(FOLLOWUP["pasos"][0]), "variables": ["nombre"]}]},
     "engine_not_supported:variables_seguimiento"),
])
def test_what_the_native_followup_does_not_run(change: dict[str, Any], code: str) -> None:
    assert code in native_not_supported(_followup(**change))


@pytest.mark.parametrize("definition", [CART, PAYMENT], ids=["carrito", "pago_rechazado"])
def test_the_native_events_run_as_before(definition: dict[str, Any]) -> None:
    assert native_not_supported(definition) == ()
    # Una conversacion por repeticion sigue siendo solo del silencio.
    once = copy.deepcopy(definition)
    once["topes"]["repeticion"] = {"una_por": "conversacion", "otra_despues_de": "nunca"}
    assert "motor_nativo:repeticion" in native_not_supported(once)
    # Y el boton del checkout, solo del silencio.
    with_button = copy.deepcopy(definition)
    step = next(step for step in with_button["pasos"] if "plantilla" in step)
    step["boton"] = {"link": "checkout", "cupon": "PROTOCOLO10"}
    assert "engine_not_supported:boton_checkout" in native_not_supported(with_button)


# -- El esquema: cualquier_plantilla y el cupon -------------------------------------


def test_contesto_a_takes_the_any_template_text_and_nothing_else() -> None:
    assert check_definition(FOLLOWUP) == ()
    [finding] = check_definition(_followup(entra={"permiso": "conversacion", "contesto_a": "carrito"}))
    assert (finding.code, finding.path) == (fd.INVALID_VALUE.code, "/entra/contesto_a")
    assert "cualquier_plantilla" in (finding.reason or "")
    # La lista sigue siendo de flujos.
    assert check_definition(_followup(entra={"permiso": "conversacion", "contesto_a": ["carrito"]})) == ()


def test_the_coupon_with_a_plus_is_still_refused_by_the_shape() -> None:
    # PROTOCOLO10+ es el del link del agente; el seguimiento usa PROTOCOLO10 (generar.py de Maribel).
    step = {**copy.deepcopy(FOLLOWUP["pasos"][0]), "boton": {"link": "checkout", "cupon": "PROTOCOLO10+"}}
    findings = check_definition(_followup(pasos=[step]))
    assert [(finding.code, finding.path) for finding in findings] == [
        (fd.INVALID_VALUE.code, "/pasos/0/boton/cupon")]


def test_the_cycle_validates_the_native_followup_without_the_legacy_registered() -> None:
    # Maribel: el legado no esta registrado (D1). cualquier_plantilla no nombra flujos.
    assert _blocks(FOLLOWUP, in_world=world(keys=())) == set()


def test_a_list_cannot_name_an_unregistered_legacy_flow() -> None:
    # Lo que cualquier_plantilla evita: con una lista, el carrito del legado tiene que estar registrado.
    blocks = _blocks(_followup(entra={"permiso": "conversacion", "contesto_a": ["carrito"]}),
                     in_world=world(keys=()))
    assert {"unknown_flow_reference", "motor_nativo:forma"} <= blocks


def test_any_template_needs_the_conversation_permission() -> None:
    result = check([_new(_followup(dispara={"evento": "carrito"},
                                   entra={"permiso": "formulario", "contesto_a": ANY_TEMPLATE}))],
                   actor="automation_expert", draft_base={})
    assert ("contesto_a_requires_conversation", "/0/definicion/entra/contesto_a") in [
        (finding.code, finding.path) for finding in result.blocks]


def test_the_native_followup_shape_blocks_where_it_is_written() -> None:
    result = check([_new(_followup(entra={"permiso": "conversacion", "contesto_a": []}))],
                   actor="automation_expert", draft_base={}, catalog=CATALOG, in_world=world(keys=()))
    [block] = result.blocks
    assert (block.code, block.path) == ("motor_nativo:forma", "/0/definicion/entra/contesto_a")
    assert "contesto_a" in (block.reason or "")


def test_the_silence_of_the_person_stays_out_of_the_native_engine() -> None:
    blocks = _blocks(_followup(dispara={"silencio": {"lo_ultimo": "persona", "desde": "24h", "hasta": "72h"}}),
                     in_world=world(keys=()))
    assert "motor_nativo:disparador" in blocks


# -- La regla 13: un solo boton --------------------------------------------------


def _with_quick_reply(payload: dict[str, Any]) -> None:
    template = template_of(payload, TEMPLATE)
    buttons = next(c for c in template["components"] if c["type"] == "BUTTONS")["buttons"]
    buttons.append({"type": "QUICK_REPLY", "text": "No más mensajes"})


def test_rule_13_wants_a_single_checkout_button_in_the_native_followup() -> None:
    catalog = catalog_with(_with_quick_reply)
    assert "template_button_mismatch" in _blocks(FOLLOWUP, in_world=world(keys=()), catalog=catalog)
    # Lo que manda el barrido: con la respuesta rapida, la plantilla no sale.
    payload = copy.deepcopy(INBOX_11)
    _with_quick_reply(payload)
    with pytest.raises(Exception, match="followup_template_button_missing"):
        parse_followup_template(payload, template_name=TEMPLATE, expected_language="es_MX")


def test_rule_13_of_the_legacy_followup_does_not_change() -> None:
    # El seguimiento del legado de ATT1 (bloque 4) con la misma plantilla y la respuesta rapida: la regla 13
    # del legado sigue aceptando respuestas rapidas despues del checkout.
    catalog = catalog_with(_with_quick_reply)
    assert "template_button_mismatch" not in found(run(flow_def("descuento"), "descuento", catalog=catalog))
    assert "template_button_mismatch" not in found(run(flow_def("descuento"), "descuento"))


# -- Lo que no cambia ---------------------------------------------------------------


def test_the_legacy_followup_validates_as_before() -> None:
    # El bloque 4 (el seguimiento de ATT1, contesto_a como lista) no pasa por la regla 14 nativa.
    assert not {code for code in found(run(flow_def("descuento"), "descuento"))
                if code.startswith(("motor_nativo:", "engine_not_supported:"))}
    legacy = flow("descuento", legacy_kind=None)
    assert legacy.is_native is False


# -- El lienzo -------------------------------------------------------------------------


def _armar():
    path = (Path(__file__).resolve().parents[1] / "profiles" / "automation-expert" / "plugins"
            / "automation-expert-read" / "lienzo" / "armar.py")
    spec = importlib.util.spec_from_file_location("lienzo_armar_e248", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_canvas_says_any_template_in_words() -> None:
    armar = _armar()
    nodos, _ = armar.traducir(copy.deepcopy(FOLLOWUP), {}, "propuesto")
    texto = json.dumps(nodos, ensure_ascii=False)
    assert "cualquier plantilla del recuperador" in texto
    assert "c, u, a" not in texto


@pytest.mark.parametrize("button", [
    {"type": "URL", "text": "Ver", "url": "https://forms.gle/mzopWTpQae8TDBph9"},
    {"type": "QUICK_REPLY", "text": "Usar mi descuento"},
], ids=["url fija", "respuesta rapida"])
def test_rule_13_native_followup_wants_the_checkout_url_button(button: dict[str, Any]) -> None:
    def only(payload: dict[str, Any]) -> None:
        template = template_of(payload, TEMPLATE)
        next(c for c in template["components"] if c["type"] == "BUTTONS")["buttons"] = [copy.deepcopy(button)]

    assert "template_button_mismatch" in _blocks(FOLLOWUP, in_world=world(keys=()), catalog=catalog_with(only))
    payload = copy.deepcopy(INBOX_11)
    only(payload)
    with pytest.raises(Exception, match="followup_template_button"):
        parse_followup_template(payload, template_name=TEMPLATE, expected_language="es_MX")


def test_an_unknown_trigger_is_measured_with_the_event_capabilities() -> None:
    # El silencio de la persona no es nativo: se mide con las capacidades de los eventos, como antes.
    codes = native_not_supported(_followup(dispara={"silencio": {"lo_ultimo": "persona", "desde": "24h",
                                                                 "hasta": "72h"}}))
    assert "motor_nativo:disparador" in codes
    assert "engine_not_supported:boton_checkout" in codes
    assert "engine_not_supported:disparo_silencio" in codes


def test_the_canvas_lists_the_flows_of_contesto_a() -> None:
    armar = _armar()
    nodos, _ = armar.traducir(_followup(entra={"permiso": "conversacion", "contesto_a": ["carrito", "pago_fallido"]}),
                              {}, "propuesto")
    assert "carrito, pago_fallido" in json.dumps(nodos, ensure_ascii=False)
