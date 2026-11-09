"""template-request-v1 (F-146, parte 2): el validador del borrador, el pedido de Meta y la burbuja.

Con plantillas reales, sin inventar respuestas de Meta:

* tests/fixtures/meta_template_requests/plantillas_reales_att1_maribel_20261102.json:
  las fuentes de ATT1 y Maribel que subio meta_plantillas.py (vault del OS), con
  lo que contesto Meta de cada una (categoria final y estado);
* las capturas del catalogo de la bandeja 11 de ATT1 (2026-10-19 y 2026-10-26),
  para comparar el pedido que arma el bridge con lo que Meta aprobo.

Los casos rojos son los del selftest de meta_plantillas.py, mas los del producto.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bridge.instance_manifest import InstanceManifest
from bridge.template_request import (
    RESOLVED_STATES,
    SEND_ATTEMPTS_MAX,
    resolve_state,
    EXAMPLE_NAME,
    TemplateResult,
    check_for_step,
    OPT_OUT_BUTTON,
    TemplateContext,
    TemplateRequestInvalid,
    bubble,
    check,
    context_from_manifest,
    draft_from_stored,
    meta_request,
    number_body,
    parse_template,
    step_at,
)

FIXTURES = Path(__file__).parent / "fixtures"
REAL = json.loads((FIXTURES / "meta_template_requests" / "plantillas_reales_att1_maribel_20261102.json")
                  .read_text(encoding="utf-8"))
BY_NAME = {row["template"]["nombre"]: row for row in REAL["plantillas"]}
CATALOG_1026 = json.loads((FIXTURES / "chatwoot_message_templates_inbox_11_bienvenida_20261026.json").read_text())
CATALOG_1019 = json.loads((FIXTURES / "chatwoot_message_templates_inbox_11_pago_rechazado_20261019.json").read_text())
# La base del boton de la ficha de OXXO de ATT1, la aprobada en Meta (fuente de ATT1, botones.ficha).
SLIP = ("CASHPAYMENT", "OXXO", "https://customer-local-latam.ebanx.com/print/oxxo?hash={{1}}")


def _context(instance: str, **overrides: Any) -> TemplateContext:
    manifest = InstanceManifest.from_toml_file(FIXTURES / "instances" / instance / "instancia.toml")
    context = context_from_manifest(manifest)
    if overrides:
        context = TemplateContext(**{**context.__dict__, **overrides})
    return context


ATT1 = _context("att1")
MARIBEL = _context("maribel")
CONTEXTS = {"att1": ATT1, "maribel": MARIBEL}


def _codes(findings: list[Any]) -> list[str]:
    return sorted({finding.code for finding in findings})


def _raw(name: str) -> dict[str, Any]:
    return copy.deepcopy(BY_NAME[name]["template"])


def _catalog(name: str, catalog: dict[str, Any]) -> dict[str, Any]:
    return next(t for t in catalog["message_templates"] if t["name"] == name)


# ------------------------------------------------------------------- el contexto, del manifiesto


def test_the_context_comes_from_the_manifest() -> None:
    manifest = InstanceManifest.from_toml_file(FIXTURES / "instances" / "att1" / "instancia.toml")
    context = context_from_manifest(manifest)
    assert (context.ally_ref, context.language, context.product_name) == ("att1", "es_MX", "Alimenta tu Tiroides")
    assert context.hotlink == "D98014973Y" and context.offer_code == "gopi6lh7"
    assert MARIBEL.product_name == "El Protocolo Biohacking +"


# ------------------------------------------------------------------- las plantillas reales


# Las que Meta aprobo y que el AE podria haber escrito: solo datos del vocabulario del flujo.
WRITABLE = [
    "att1_pago_rechazado_cupon_01", "att1_pago_rechazado_dos_veces_01", "att1_pago_rechazado_saldo_01",
    "att1_pago_rechazado_banco_01", "att1_pago_rechazado_descuento_01", "att1_pago_efectivo_confirmado_01",
    "att1_carrito_descuento_01", "att1_carrito_dudas_01", "att1_recordatorio_dia_elegido_01",
    "att1_ultima_renovacion_01", "att1_bienvenida_01", "maribel_retomar_proceso_01", "maribel_bienvenida_01",
]


@pytest.mark.parametrize("name", WRITABLE)
def test_a_real_template_meta_approved_passes_the_rules(name: str) -> None:
    row = BY_NAME[name]
    assert row["en_meta"]["status"] == "APPROVED"
    result = check(parse_template(row["template"]), CONTEXTS[row["instancia"]])
    assert result.blocks == [], [f.to_json() for f in result.blocks]


def test_the_recategorization_that_talks_of_a_discount_is_warned_and_the_other_one_is_not() -> None:
    # maribel_retomar_proceso_01: pedida UTILITY, aprobada MARKETING (captura de la bandeja 13).
    retomar = BY_NAME["maribel_retomar_proceso_01"]
    assert (retomar["template"]["categoria"], retomar["en_meta"]["category"]) == ("UTILITY", "MARKETING")
    warned = check(parse_template(retomar["template"]), MARIBEL)
    assert "template_utility_sounds_promotional" in _codes(warned.warnings)
    # att1_ficha_vence_manana_01: pedida UTILITY, aprobada MARKETING, sin hablar de descuento. El aviso no la
    # ve venir: la comparacion de la categoria final (parte 4) es la que la caza.
    manana = BY_NAME["att1_ficha_vence_manana_01"]
    assert (manana["template"]["categoria"], manana["en_meta"]["category"]) == ("UTILITY", "MARKETING")
    quiet = check(parse_template(manana["template"]), _context("att1", slip_buttons=(SLIP,)))
    assert "template_utility_sounds_promotional" not in _codes(quiet.warnings)


def test_the_slip_templates_of_the_legacy_use_data_the_flow_cannot_fill() -> None:
    # La ficha del legado nombra la tienda y su vencimiento (tienda, vence_ficha): el flujo nativo no los llena.
    for name in ("att1_ficha_efectivo_01", "att1_ficha_vence_hoy_01", "att1_pago_pendiente_01"):
        result = check(parse_template(BY_NAME[name]["template"]), _context("att1", slip_buttons=(SLIP,)))
        assert _codes(result.blocks) == ["template_variable_unknown"], (name, _codes(result.blocks))


def test_a_marketing_one_without_the_opt_out_is_blocked_even_if_meta_took_it() -> None:
    # maribel_seguimiento_descuento_01 se subio sin la baja (el seguimiento con cupon mandaba un solo boton
    # hasta E-272): desde el AE no se puede, y desde el 2026-11-02 no hay excepcion (decision de Dan).
    row = BY_NAME["maribel_seguimiento_descuento_01"]
    result = check(parse_template(row["template"]), MARIBEL)
    assert _codes(result.blocks) == ["template_marketing_without_opt_out"]


def test_a_fixed_url_does_not_depend_on_a_list_of_the_instance() -> None:
    # E-362, decision de Dan del 2026-11-09: las URLs cambian de un flujo a otro; manda la forma.
    draft = parse_template(_raw("att1_bienvenida_01"))
    assert "template_fixed_url_not_listed" not in _codes(check(draft, _context("att1")).blocks)
    assert "template_fixed_url_invalid" not in _codes(check(draft, _context("att1")).blocks)


def _codes_with_fixed_url(url: str) -> list[str]:
    raw = copy.deepcopy(_raw("att1_bienvenida_01"))
    next(b for b in raw["botones"] if b["tipo"] == "url_fija")["url"] = url
    return _codes(check(parse_template(raw), _context("att1")).blocks)


@pytest.mark.parametrize("url", [
    "https://hola", "https://sin-dominio/ruta.con.punto", "ftp://x.example/a", "https://x.example/a b",
    "https://x.example/{{1}}", "https://x.example/%7B%7B1%7D%7D", "https://x.example/%7b%7b1%7d%7d", "https://a..",
    "https://user:clave@x.example/a", "https://forms.gle@evil.example/x", "https://192.168.0.1/admin",
    "https://x.example:puerto/a", "https://-x.example/a", "https://x.example?correo=ana@gmail.com",
    "https://x.example/a?correo=ana%40gmail.com", "https://x.example/a?ana@gmail.com",
])
def test_a_fixed_url_has_to_have_the_form_of_a_link(url: str) -> None:
    assert "template_fixed_url_invalid" in _codes_with_fixed_url(url)


@pytest.mark.parametrize("url", [
    "https://forms.gle/mzopWTpQae8TDBph9", "https://bit.ly/3AbCdEf", "https://chat.whatsapp.com/GrupoDemo",
    "https://wa.me/5215512345678", "https://api.whatsapp.com/send?phone=5215512345678",
    "https://www.ejemplo.com.mx/pago/123456789?precio=1497000&utm_campaign=2026", "https://medium.com/@marca/nota",
    "https://x.example/a?usuario=@marca", "https://xn--andu-hqa.example/a",
])
def test_a_real_link_passes_even_with_digits_or_a_shortener(url: str) -> None:
    assert "template_fixed_url_invalid" not in _codes_with_fixed_url(url)


# ------------------------------------------------------------------- el pedido de Meta, contra lo aprobado


def test_the_request_of_a_template_with_name_and_product_is_what_meta_approved() -> None:
    draft = parse_template(_raw("att1_pago_efectivo_confirmado_01"))
    assert draft.variables == ("nombre", "producto")
    request = meta_request(draft, ATT1)
    approved = _catalog("att1_pago_efectivo_confirmado_01", CATALOG_1026)
    assert (request["name"], request["language"], request["category"]) == (
        approved["name"], approved["language"], approved["category"])
    assert request["parameter_format"] == approved["parameter_format"] == "POSITIONAL"
    assert request["components"] == approved["components"]


def test_the_request_with_a_header_and_a_fixed_button_is_what_meta_approved() -> None:
    request = meta_request(parse_template(_raw("att1_bienvenida_01")), ATT1)
    approved = _catalog("att1_bienvenida_01", CATALOG_1026)
    assert request["components"] == approved["components"]


def test_the_request_with_the_checkout_button_has_the_dynamic_url_and_an_example() -> None:
    request = meta_request(parse_template(_raw("att1_pago_rechazado_banco_01")), ATT1)
    approved = _catalog("att1_pago_rechazado_banco_01", CATALOG_1019)
    assert request["components"][0]["text"] == approved["components"][0]["text"]
    ours, theirs = request["components"][1]["buttons"], approved["components"][1]["buttons"]
    assert [(b["type"], b["text"], b.get("url")) for b in ours] == [(b["type"], b["text"], b.get("url")) for b in theirs]
    assert ours[0]["example"] == ["https://pay.hotmart.com/D98014973Y?off=gopi6lh7&sck=ejemplo"]
    # El ejemplo de cada dato lo pone el bridge: el nombre es el generico fijo.
    assert request["components"][0]["example"]["body_text"][0][0] == EXAMPLE_NAME


def test_the_slip_button_takes_the_base_of_the_payment_method() -> None:
    raw = {"nombre": "att1_ficha_aviso_01", "idioma": "es_MX", "categoria": "UTILITY",
           "cuerpo": "Hola, {{nombre}}. Te dejo tu ficha para pagar {{producto}} en efectivo, para que la tengas.",
           "botones": [{"tipo": "ficha", "texto": "Ver mi ficha"}, {"tipo": "respuesta", "texto": OPT_OUT_BUTTON}]}
    context = _context("att1", slip_buttons=(SLIP,))
    draft = parse_template(raw)
    assert check(draft, context).blocks == []
    button = meta_request(draft, context)["components"][1]["buttons"][0]
    assert button["url"] == SLIP[2]
    assert button["example"] == ["https://customer-local-latam.ebanx.com/print/oxxo?hash="
                                 "0123456789abcdef0123456789abcdef0123456789abcdef"]
    assert _codes(check(draft, ATT1).blocks) == ["template_slip_button_unavailable"]


# ------------------------------------------------------------------- los rojos


BASE = {"nombre": "att1_aviso_prueba_01", "idioma": "es_MX", "categoria": "MARKETING",
        "cuerpo": "Hola, {{nombre}}. Tu {{producto}} vale hasta {{vence}}, con el pago listo para ti.",
        "botones": [{"tipo": "checkout", "texto": "Usar mi descuento"},
                    {"tipo": "respuesta", "texto": OPT_OUT_BUTTON}]}


def _with(**changes: Any) -> dict[str, Any]:
    raw = copy.deepcopy(BASE)
    raw.update(changes)
    return raw


def test_the_base_case_is_clean() -> None:
    assert check(parse_template(BASE), ATT1).blocks == []


@pytest.mark.parametrize(("changes", "code"), [
    # Los del selftest de meta_plantillas.py.
    ({"cuerpo": "{{nombre}} hola que tal todo bien por aca hoy"}, "template_variable_at_edge"),
    ({"cuerpo": "Hola que tal todo bien por aca, {{nombre}}"}, "template_variable_at_edge"),
    ({"cuerpo": "Hola {{nombre}}{{producto}} que tal todo bien hoy."}, "template_variables_adjacent"),
    ({"cuerpo": "Hola {{nombre}}, {{nombre}}: que tal todo bien hoy."}, "template_variable_repeated"),
    ({"cuerpo": "Hola {{1}}, que tal todo bien por aca hoy."}, "template_variable_numbered"),
    ({"botones": [{"tipo": "checkout", "texto": "Usar mi descuento"}]}, "template_marketing_without_opt_out"),
    ({"encabezado": "x" * 61}, "template_header_too_long"),
    ({"encabezado": "Hola {{nombre}}"}, "template_header_has_variable"),
    ({"botones": [{"tipo": "url_fija", "texto": "C", "url": "https://x.example/{{1}}"},
                  {"tipo": "respuesta", "texto": OPT_OUT_BUTTON}]}, "template_fixed_url_invalid"),
    ({"botones": [{"tipo": "url_fija", "texto": "C", "url": "http://x.example"},
                  {"tipo": "respuesta", "texto": OPT_OUT_BUTTON}]}, "template_fixed_url_invalid"),
    ({"cuerpo": "Hola {{nombre}}, que tal.\n\n\nTodo bien con {{producto}} por aca hoy."}, "template_blank_lines"),
    ({"botones": [{"tipo": "checkout", "texto": "x" * 26}, {"tipo": "respuesta", "texto": OPT_OUT_BUTTON}]},
     "template_button_text_too_long"),
    ({"cuerpo": "Hola, " + "a" * 1030 + " {{nombre}} fin."}, "template_body_too_long"),
    # Los del producto.
    ({"cuerpo": "Hola, {{nombre}}. Tu {{curso}} te espera con el pago listo para ti hoy."}, "template_variable_unknown"),
    ({"botones": [{"tipo": "checkout", "texto": "Pagar"}, {"tipo": "ficha", "texto": "Ficha"},
                  {"tipo": "respuesta", "texto": OPT_OUT_BUTTON}]}, "template_more_than_one_url_button"),
    ({"botones": [{"tipo": "respuesta", "texto": OPT_OUT_BUTTON}, {"tipo": "checkout", "texto": "Pagar"}]},
     "template_url_button_not_first"),
    ({"botones": [{"tipo": "checkout", "texto": "Pagar"}, {"tipo": "respuesta", "texto": OPT_OUT_BUTTON},
                  {"tipo": "respuesta", "texto": OPT_OUT_BUTTON}]}, "template_button_text_repeated"),
    ({"cuerpo": "Hola, {{nombre}}. Entra a https://otro.example para ver {{producto}} hoy mismo."},
     "template_text_has_link"),
    ({"cuerpo": "Hola, {{nombre}}. Escribenos al 55 1234 5678 por {{producto}} cuando quieras."},
     "identifier_not_allowed"),
    ({"pie": "Escribenos a equipo@marca.example"}, "identifier_not_allowed"),
    ({"nombre": "maribel_aviso_prueba_01"}, "template_name_pattern"),
    ({"nombre": "att1_aviso_prueba"}, "template_name_pattern"),
    ({"idioma": "es_AR"}, "template_language_not_instance"),
    # De la revision en tres lentes (E-268).
    ({"cuerpo": "Hola, {{nombre completo}}. Tu {{producto}} te espera con el pago listo hoy."},
     "template_variable_malformed"),
    ({"cuerpo": "Hola, {{{nombre}}}. Tu {{producto}} te espera con el pago listo hoy."}, "template_variable_malformed"),
    ({"cuerpo": "Hola, {{nombre}. Tu {{producto}} te espera con el pago listo hoy mismo."},
     "template_variable_malformed"),
    ({"cuerpo": "Hola, {{nombre}}. Entra a pay.hotmart.com/XYZ para {{producto}} cuando quieras."},
     "template_text_has_link"),
    ({"encabezado": "Hola\nListo"}, "template_header_has_line_break"),
    ({"botones": [{"tipo": "checkout", "texto": "Pagar {{nombre}}"}, {"tipo": "respuesta", "texto": OPT_OUT_BUTTON}]},
     "template_button_text_invalid"),
    ({"botones": [{"tipo": "checkout", "texto": "Pagar"}, {"tipo": "respuesta", "texto": "Ver\nmas"},
                  {"tipo": "respuesta", "texto": OPT_OUT_BUTTON}]}, "template_button_text_invalid"),
    ({"nombre": "att1_01"}, "template_name_pattern"),
    ({"nombre": "att1__01"}, "template_name_pattern"),
])
def test_each_rule_blocks(changes: dict[str, Any], code: str) -> None:
    result = check(parse_template(_with(**changes)), ATT1)
    assert code in _codes(result.blocks), [f.to_json() for f in result.blocks]


def test_a_name_already_in_the_catalog_is_blocked_in_any_status() -> None:
    names = [(t["name"], t["language"]) for t in CATALOG_1026["message_templates"]]
    result = check(parse_template(_with(nombre="att1_bienvenida_01", categoria="UTILITY",
                                        cuerpo=BY_NAME["att1_bienvenida_01"]["template"]["cuerpo"],
                                        botones=[])), ATT1, catalog_names=names)
    assert "template_name_in_catalog" in _codes(result.blocks)


def test_the_opt_out_is_recognized_without_its_accent_like_opt_out_does() -> None:
    raw = _with(botones=[{"tipo": "checkout", "texto": "Pagar"}, {"tipo": "respuesta", "texto": "No mas mensajes"}])
    assert check(parse_template(raw), ATT1).blocks == []


def test_a_language_the_instance_does_not_have_once_is_blocked() -> None:
    result = check(parse_template(BASE), _context("att1", language=None))
    assert "template_language_unknown" in _codes(result.blocks)


@pytest.mark.parametrize(("raw", "field"), [
    ("no", "template"),
    ({**BASE, "otra": 1}, "template"),
    ({k: v for k, v in BASE.items() if k != "cuerpo"}, "template"),
    ({**BASE, "nombre": "Con-Mayus"}, "template.nombre"),
    ({**BASE, "categoria": "AUTHENTICATION"}, "template.categoria"),
    ({**BASE, "idioma": "espanol"}, "template.idioma"),
    ({**BASE, "botones": [{"tipo": "imagen", "texto": "x"}]}, "template.botones.0"),
    ({**BASE, "botones": [{"tipo": "checkout", "texto": "x", "url": "https://x.example"}]}, "template.botones.0"),
    ({**BASE, "cuerpo": "   "}, "template.cuerpo"),
])
def test_a_draft_that_does_not_have_the_shape_says_which_field(raw: Any, field: str) -> None:
    with pytest.raises(TemplateRequestInvalid) as caught:
        parse_template(raw)
    assert caught.value.code == "template_request_invalid"
    assert caught.value.reason["field"] == field
    assert "Con-Mayus" not in json.dumps(caught.value.reason)


# ------------------------------------------------------------------- la numeracion, la burbuja y lo guardado


def test_the_numbering_follows_the_order_of_appearance() -> None:
    assert number_body("Hola {{producto}} y {{ nombre }}.") == ("Hola {{1}} y {{2}}.", ("producto", "nombre"))


def test_the_bubble_shows_the_data_in_brackets_and_an_example_with_the_real_product() -> None:
    shown = bubble(parse_template(_raw("maribel_bienvenida_01")), MARIBEL)
    assert shown["encabezado"] == "Notificación"
    assert shown["cuerpo"].startswith("Tu inscripción en [producto] ha sido confirmada.")
    assert shown["cuerpo_con_ejemplo"].startswith("Tu inscripción en El Protocolo Biohacking + ha sido")
    assert shown["botones"] == [{"tipo": "url_fija", "texto": "Completar mi formulario",
                                 "link": "https://forms.gle/1ESdBdR2fLYd5Eqt7"}]
    assert shown["categoria_explicada"].startswith("Utilidad:")
    checkout = bubble(parse_template(BASE), ATT1)
    assert checkout["botones"][0] == {"tipo": "checkout", "texto": "Usar mi descuento", "link": "link de pago"}


def test_what_the_base_keeps_reads_back_the_same_and_a_changed_order_is_caught() -> None:
    draft = parse_template(BASE)
    assert draft_from_stored(draft.to_json()) == draft
    stored = draft.to_json()
    stored["variables"] = list(reversed(stored["variables"]))
    with pytest.raises(TemplateRequestInvalid):
        draft_from_stored(stored)


def test_the_step_of_a_proposal_is_read_by_its_pointer() -> None:
    items = [{"definicion": {"pasos": [{"esperar": "1h"}, {"plantilla": {"a_crear": "att1_aviso_prueba_01"}}]}}]
    assert step_at(items, "/items/0/definicion/pasos/1") == {"plantilla": {"a_crear": "att1_aviso_prueba_01"}}
    assert step_at(items, "/items/0/definicion/pasos/9") is None
    assert step_at(items, "/items/x") is None


# ------------------------------------------------------------------- lo que consume el validador de PGlite


def bridge_fixture() -> dict[str, Any]:
    """La salida del bridge para att1_bienvenida_02, la que registra validate_meta_template_requests.mjs."""
    raw = _raw("att1_bienvenida_01")
    raw["nombre"] = "att1_bienvenida_02"
    draft = parse_template(raw)
    result = check(draft, ATT1)
    assert result.ok
    return {
        "_que_es": ("La salida de template_request.py (parse_template, check, meta_request y bubble) para "
                    "att1_bienvenida_02: el texto real de att1_bienvenida_01 con el numero siguiente. La regenera "
                    "tests/test_template_request.py y la registra validate_meta_template_requests.mjs."),
        "purpose": "la bienvenida de la compra con el formulario del perfil",
        "draft": draft.to_json(),
        "result": result.to_json(),
        "meta_request": meta_request(draft, ATT1),
        "preview": {"burbuja": bubble(draft, ATT1), "catalog_sha256": "0" * 64},
        "validator": {"contract": "template-request-v1", "rules_version": 1, "product_version": "v1.25.0",
                      "git_sha": "069e602e"},
    }


def bridge_extra_cases() -> dict[str, Any]:
    """Mas formas para el cruce con la base: checkout de marketing con la baja y pie, y la ficha con su medio."""
    marketing = parse_template(_with(nombre="att1_carrito_aviso_02", pie="Equipo de la Dra. Nina"))
    slip_context = _context("att1", slip_buttons=(SLIP,))
    slip = parse_template({
        "nombre": "att1_ficha_aviso_01", "idioma": "es_MX", "categoria": "UTILITY",
        "cuerpo": "Hola, {{nombre}}. Te dejo tu ficha para pagar {{producto}} en {{medio_de_pago}}, para tenerla.",
        "botones": [{"tipo": "ficha", "texto": "Ver mi ficha", "medio": "CASHPAYMENT"},
                    {"tipo": "respuesta", "texto": OPT_OUT_BUTTON}]})
    cases = []
    for draft, context in ((marketing, ATT1), (slip, slip_context)):
        result = check(draft, context)
        assert result.ok, result.to_json()
        cases.append({"draft": draft.to_json(), "meta_request": meta_request(draft, context),
                      "result": result.to_json()})
    return {"_que_es": ("Mas salidas de template_request.py para el validador de PGlite (validate_meta_template_"
                        "requests.mjs): las regenera tests/test_template_request.py."), "cases": cases}


@pytest.mark.parametrize(("name", "build"), [("bridge_att1_bienvenida_02.json", bridge_fixture),
                                             ("bridge_extra_cases.json", bridge_extra_cases)])
def test_the_fixture_of_the_sql_validator_is_todays_bridge_output(name: str, build: Any) -> None:
    path = FIXTURES / "meta_template_requests" / name
    expected = json.dumps(build(), ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    assert path.read_text(encoding="utf-8") == expected


def test_precio_without_a_price_in_the_manifest_is_blocked() -> None:
    raw = _with(cuerpo="Hola, {{nombre}}. Tu {{producto}} queda en {{precio}} con el pago listo para ti hoy.")
    assert check(parse_template(raw), ATT1).blocks == []
    assert "template_price_unknown" in _codes(check(parse_template(raw), _context("att1", price=None)).blocks)


def _step_codes(draft_raw: dict[str, Any], step: dict[str, Any], definition: dict[str, Any] | None = None) -> list[str]:
    result = TemplateResult()
    check_for_step(parse_template(draft_raw), definition or {}, step, result)
    return [(f.code, f.path) for f in result.blocks]  # type: ignore[misc]


def test_the_step_that_waits_for_it_has_to_be_able_to_send_it() -> None:
    name = BASE["nombre"]
    ok_step = {"plantilla": {"a_crear": name}, "idioma": "es_MX", "variables": ["nombre", "producto", "vence"],
               "boton": {"link": "checkout", "cupon": "personal"}}
    assert _step_codes(BASE, ok_step) == []
    assert _step_codes(BASE, {**ok_step, "plantilla": {"a_crear": "att1_otra_01"}}) == [
        ("template_flow_step_mismatch", "/for_flow/step")]
    assert ("template_flow_step_mismatch", "/idioma") in _step_codes(BASE, {**ok_step, "idioma": "es_AR"})
    assert ("template_flow_step_mismatch", "/cuerpo") in _step_codes(BASE, {**ok_step, "variables": ["nombre"]})
    # Sin boton en el paso, la plantilla no puede llevar el link de pago; sin cupon, ni vence ni precio.
    without = {k: v for k, v in ok_step.items() if k != "boton"}
    assert [path for _, path in _step_codes(BASE, without)] == ["/botones", "/cuerpo"]
    # Un flujo con permiso de compra manda solo utilidad (la regla 17).
    assert ("template_flow_step_mismatch", "/categoria") in _step_codes(
        BASE, ok_step, {"entra": {"permiso": "compra"}})


def test_more_than_ten_buttons_is_blocked_with_its_reason() -> None:
    buttons = [{"tipo": "respuesta", "texto": f"Opcion {index}"} for index in range(10)]
    raw = _with(categoria="UTILITY", botones=[*buttons, {"tipo": "respuesta", "texto": OPT_OUT_BUTTON}])
    assert "template_too_many_buttons" in _codes(check(parse_template(raw), ATT1).blocks)


def test_the_slip_button_picks_its_payment_method_and_two_bases_need_one() -> None:
    other = ("SPEI", "Banco", "https://pagos.example/ficha?id={{1}}")
    context = _context("att1", slip_buttons=(SLIP, other))
    base = {"nombre": "att1_ficha_aviso_01", "idioma": "es_MX", "categoria": "UTILITY",
            "cuerpo": "Hola, {{nombre}}. Te dejo tu ficha para pagar {{producto}} en {{medio_de_pago}}, para tenerla."}
    ambiguous = parse_template({**base, "botones": [{"tipo": "ficha", "texto": "Ver mi ficha"}]})
    assert "template_slip_button_unavailable" in _codes(check(ambiguous, context).blocks)
    chosen = parse_template({**base, "botones": [{"tipo": "ficha", "texto": "Ver mi ficha", "medio": "SPEI"}]})
    assert check(chosen, context).blocks == []
    request = meta_request(chosen, context)
    assert request["components"][1]["buttons"][0]["url"] == other[2]
    # El ejemplo del medio de pago es la tienda de la ficha.
    assert request["components"][0]["example"]["body_text"][0][2] == "OXXO"


def test_the_warnings_the_contract_promises() -> None:
    assert "template_url_and_quick_replies" in _codes(check(parse_template(BASE), ATT1).warnings)
    sparse = _with(cuerpo="Hola {{nombre}} y {{producto}} ya.")
    assert "template_few_words_for_variables" in _codes(check(parse_template(sparse), ATT1).warnings)
    header = _with(encabezado="*Listo*")
    assert "template_header_formatting" in _codes(check(parse_template(header), ATT1).warnings)


def test_the_phone_rule_starts_at_seven_digits() -> None:
    six = _with(cuerpo="Hola, {{nombre}}. Tu orden 123 456 de {{producto}} quedo lista para pagar hoy.")
    seven = _with(cuerpo="Hola, {{nombre}}. Tu orden 123 4567 de {{producto}} quedo lista para pagar hoy.")
    assert "identifier_not_allowed" not in _codes(check(parse_template(six), ATT1).blocks)
    assert "identifier_not_allowed" in _codes(check(parse_template(seven), ATT1).blocks)


def test_urls_fijas_in_the_manifest_has_no_effect_on_the_validator() -> None:
    # E-362: se sigue aceptando, para no romper manifiestos, y no decide nada.
    manifest = replace(InstanceManifest.from_toml_file(FIXTURES / "instances" / "att1" / "instancia.toml"),
                       template_fixed_urls=("https://ejemplo.com/f",))
    draft = parse_template(_raw("att1_bienvenida_01"))
    assert _codes(check(draft, context_from_manifest(manifest)).blocks) == _codes(check(draft, _context("att1")).blocks)


# ---- E-356: el estado ya resuelto. Lo que el AE le cuenta al duenio sale de aca, nunca del estado crudo.
_DRAFT = {"cuerpo": "Hola {{nombre}}, ya sos parte de {{producto}}."}
_BODY = "Hola {{1}}, ya sos parte de {{2}}."


def _row(status: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "effective_status": status, "template_name": "demo_comunidad_01", "language": "es_MX",
            "category_requested": "UTILITY", "draft": _DRAFT, "send_attempts": 1, "last_send_error": None,
            "recategorized": False, **extra}


def _entry(status: str = "APPROVED", category: str = "UTILITY", body: str = _BODY, name: str = "demo_comunidad_01",
           language: str = "es_MX") -> dict[str, Any]:
    return {"name": name, "status": status, "category": category, "language": language,
            "components": [{"type": "BODY", "text": body}]}


@pytest.mark.parametrize("status,state", [
    ("borrador", "borrador"), ("validada", "esperando_que_la_manden"), ("aprobada", "aprobada_sin_mandar"),
    ("vencida", "vencida"), ("descartada", "descartada"),
])
def test_what_never_reached_meta_does_not_take_the_state_of_the_catalog(status: str, state: str) -> None:
    resolved = resolve_state(_row(status), [_entry()])
    assert resolved["state"] == state and resolved["catalog_status"] is None


def test_an_expired_request_is_expired_whatever_its_status_says() -> None:
    assert resolve_state(_row("aprobada", effective_status="vencida"), [])["state"] == "vencida"


@pytest.mark.parametrize("row,state", [
    (_row("enviada"), "mandandose"),
    (_row("enviada", last_send_error="meta_unreachable"), "no_llego_a_meta"),
    (_row("enviada", last_send_error="meta:100", send_attempts=5), "no_llego_a_meta_sin_reintentos"),
    (_row("enviada", last_send_error="meta_name_taken_other_text"), "nombre_ocupado_en_meta"),
    (_row("enviada", last_send_error="meta_name_taken_unusable", send_attempts=5), "nombre_ocupado_en_meta"),
    (_row("enviada", send_attempts=None), "sin_confirmar"),
    (_row("en_revision"), "en_revision"),
    (_row("sin_respuesta_de_meta"), "sin_respuesta_de_meta"),
    (_row("aprobada_meta"), "aprobada_falta_catalogo"),
    (_row("aprobada_meta", recategorized=True), "aprobada_otro_tipo"),
    (_row("en_catalogo"), "aprobada_lista"),
    (_row("rechazada"), "rechazada"),
    (_row("un_estado_nuevo"), "estado_de_meta_desconocido"),
])
def test_what_was_sent_and_is_not_in_the_catalog(row: dict[str, Any], state: str) -> None:
    assert resolve_state(row, [])["state"] == state
    assert state in RESOLVED_STATES


@pytest.mark.parametrize("entry,state", [
    (_entry(), "aprobada_lista"),
    (_entry(category="MARKETING"), "aprobada_otro_tipo"),
    (_entry("REJECTED"), "rechazada"),
    (_entry("PENDING"), "en_revision"),
    (_entry("IN_APPEAL"), "en_revision"),
    (_entry("PAUSED"), "pausada_en_meta"),
    (_entry("DISABLED"), "desactivada_en_meta"),
    (_entry("PENDING_DELETION"), "estado_de_meta_desconocido"),
])
def test_the_catalog_decides_over_a_request_that_stood_still(entry: dict[str, Any], state: str) -> None:
    resolved = resolve_state(_row("en_revision"), [entry])
    assert resolved["state"] == state and resolved["catalog_checked"] is True
    assert resolved["catalog_status"] == entry["status"]


@pytest.mark.parametrize("entry", [
    _entry(body="Otro texto con el mismo nombre {{1}} aca."), _entry(language="en_US"), _entry(name="otra_cosa_01"),
])
def test_only_the_same_name_language_and_body_lend_their_state(entry: dict[str, Any]) -> None:
    # Con el nombre tomado por otro texto (meta_name_taken_other_text), el catalogo tiene un APPROVED que no es ella.
    row = _row("enviada", last_send_error="meta_name_taken_other_text")
    resolved = resolve_state(row, [entry])
    assert resolved["state"] == "nombre_ocupado_en_meta" and resolved["catalog_status"] is None


def test_a_send_that_timed_out_and_meta_did_take_is_what_the_catalog_says() -> None:
    row = _row("enviada", last_send_error="meta_unreachable")
    assert resolve_state(row, [_entry("PENDING")])["state"] == "en_revision"


@pytest.mark.parametrize("status", ["aprobada_meta", "rechazada", "en_catalogo"])
def test_a_late_pending_of_the_catalog_does_not_undo_an_answer(status: str) -> None:
    assert resolve_state(_row(status), [_entry("PENDING")])["state"] != "en_revision"


def test_without_the_catalog_or_the_draft_it_says_it_did_not_check() -> None:
    assert resolve_state(_row("en_revision"), None) == {"state": "en_revision", "catalog_checked": False,
                                                        "catalog_status": None, "catalog_category": None}
    # La fila de la lista sin releer: tiene nombre pero no borrador, asi que no se le presta el estado.
    resolved = resolve_state(_row("en_revision", draft=None), [_entry()])
    assert resolved == {"state": "en_revision", "catalog_checked": False, "catalog_status": None,
                        "catalog_category": None}


def test_the_attempts_limit_is_the_one_of_the_page() -> None:
    from bridge.template_approval import SEND_ATTEMPTS_MAX as PAGE_MAX  # noqa: PLC0415
    assert SEND_ATTEMPTS_MAX == PAGE_MAX
