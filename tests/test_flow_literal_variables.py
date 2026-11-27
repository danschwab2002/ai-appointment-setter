"""F4: el texto fijo en un marcador de un paso de flujo (``{"texto": "…"}`` en ``variables``).

Contrato: docs/contracts/flow-engine-v2.md §36; la base: 20261127300000. Flag: FLOW_LITERAL_VARIABLES_ENABLED.
Mapa: «Frenos ante Meta y calentamiento» (D15-D17, 2026-11-27).

Datos:

* Los textos son de ficcion, con la forma y el largo de los borradores de utilidad de Maribel del 27/11
  (217 a 321 caracteres), sin datos de nadie.
* El aviso de la plantilla de utilidad que habla de descuento usa ``maribel_retomar_proceso_01``, capturada de la
  bandeja 13 (tests/fixtures/meta_template_requests/plantillas_reales_att1_maribel_20261102.json).
* El envio nativo usa los dobles de test_flow_engine_native_dispatch.py (el inbox 11 de ATT1, capturado el
  10/10); la plantilla de dos marcadores es la bienvenida de ese archivo con otro nombre y otro cuerpo: es lo
  unico inventado del envio.
* Lo que la base le da al bridge en ``template_variables`` es
  tests/fixtures/flow_engine/literal_variables_context_20261127.json: la salida de PGlite (no es una captura de
  produccion: ningun flujo publicado tiene todavia un texto fijo), que comprueba
  validate_flow_engine_literal_variables.mjs.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_definition as fd
from bridge.approved_templates import ApprovedTemplateError, parse_approved_template
from bridge.flow_engine import (
    ENGINE_CAPABILITIES_V1,
    native_not_supported,
    native_variables_fit,
    not_supported,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
CONTEXT = json.loads((FIXTURES / "flow_engine" / "literal_variables_context_20261127.json").read_text(encoding="utf-8"))
TEXT = ("Tu inscripción quedó a medio camino y todavía estás a tiempo: con el acceso empezás hoy mismo, a tu "
        "ritmo, con el acompañamiento del equipo y las clases grabadas para verlas cuando quieras, sin apuro ninguno.")
CDMX = "America/Mexico_City"


def literal(text: str = TEXT) -> dict[str, str]:
    return {"texto": text}


def flow(variables: list[Any], **changes: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "esquema": fd.SCHEMA, "flujo": "pago_rechazado", "nombre": "Pago rechazado", "estado": "borrador",
        "producto": "el-protocolo", "dispara": {"evento": "pago_fallido"}, "entra": {"permiso": "formulario"},
        "pasos": [{"plantilla": "maribel_pago_no_completado_01", "idioma": "es_MX", "variables": variables}],
        "termina": {"si": ["compra"], "vence": "1d"}, "horario": "siempre",
        "topes": {"por_dia": "sin_tope", "total": "sin_tope",
                  "repeticion": {"una_por": "persona", "otra_despues_de": "nunca"}},
        "prioridad": {"le_gana_a": [], "no_arranca_mientras": []},
    }
    definition.update(changes)
    return definition


def codes(definition: dict[str, Any], *, on: bool) -> set[tuple[str, str]]:
    return {(f.code, f.path) for f in fd.check_definition(definition, literal_variables=on)}


# -- La forma (D15, D-F4.1, D-F4.2) ---------------------------------------------

def test_with_the_flag_a_literal_is_a_valid_variable() -> None:
    assert codes(flow(["nombre", literal()]), on=True) == set()
    assert codes(flow([literal(), "producto", literal()]), on=True) == set()
    assert codes(flow([literal("a" * 400)]), on=True) == set()
    assert codes(flow([literal("hola    chau")]), on=True) == set()


def test_without_the_flag_a_literal_is_refused_with_its_reason() -> None:
    findings = fd.check_definition(flow(["nombre", literal()]))
    [finding] = [f for f in findings if f.path == "/pasos/0/variables/1"]
    assert finding.code == "invalid_value"
    assert "texto fijo" in (finding.reason or "")
    # Lo de antes de F4, igual: un nombre que no existe es invalid_value con la lista.
    [name] = fd.check_definition(flow(["nombre", "apellido"]))
    assert (name.code, name.path) == ("invalid_value", "/pasos/0/variables/1")


@pytest.mark.parametrize("value, code, path", [
    (literal(""), "invalid_value", "/pasos/0/variables/0/texto"),
    (literal("   "), "invalid_value", "/pasos/0/variables/0/texto"),
    (literal("a" * 401), "invalid_value", "/pasos/0/variables/0/texto"),
    (literal("hola\nchau"), "invalid_value", "/pasos/0/variables/0/texto"),
    (literal("hola\tchau"), "invalid_value", "/pasos/0/variables/0/texto"),
    (literal("hola     chau"), "invalid_value", "/pasos/0/variables/0/texto"),
    (literal("hola {{1}}"), "invalid_value", "/pasos/0/variables/0/texto"),
    (literal("entrá a https://ejemplo.invalid/x"), "invalid_value", "/pasos/0/variables/0/texto"),
    (literal("entrá a www.ejemplo"), "invalid_value", "/pasos/0/variables/0/texto"),
    (literal("entrá a pay.hotmart.com/X"), "invalid_value", "/pasos/0/variables/0/texto"),
    (literal(7), "invalid_value", "/pasos/0/variables/0/texto"),  # type: ignore[arg-type]
    (literal("escribinos al 5512345678"), "identifier_not_allowed", "/pasos/0/variables/0/texto"),
    (literal("escribinos a alguien@ejemplo.invalid"), "identifier_not_allowed", "/pasos/0/variables/0/texto"),
    ({"texto": "hola", "de": "nombre"}, "unknown_key", "/pasos/0/variables/0/de"),
    ({}, "required_key_missing", "/pasos/0/variables/0/texto"),
])
def test_each_bad_literal_is_found_where_it_is(value: object, code: str, path: str) -> None:
    assert (code, path) in codes(flow([value]), on=True)


def test_a_control_character_in_a_literal_is_found_by_the_rule_of_every_string() -> None:
    assert ("invalid_value", "/pasos/0/variables/0/texto") in codes(flow([literal("hola​chau")]), on=True)


def test_normalize_takes_the_flag_from_the_instance() -> None:
    off = fd.InstanceView(tenant_ref="lancemos", ally_ref="maribel", products=frozenset({"el-protocolo"}))
    on = replace(off, literal_variables=True)
    draft = flow(["nombre", literal()])
    assert any(f.path == "/pasos/0/variables/1" for f in
               fd.normalize_definition(draft, new_flow=False, instance=off).findings)
    assert not any("variables" in f.path for f in
                   fd.normalize_definition(draft, new_flow=False, instance=on).findings)
    with pytest.raises(ValueError, match="instance_literal_variables_invalid"):
        replace(off, literal_variables="true")  # type: ignore[arg-type]


def test_the_runtime_literal_is_the_text_of_a_well_formed_one_only() -> None:
    assert fd.literal_from_variable(literal("Hola")) == fd.TemplateLiteral("Hola")
    assert hash(fd.TemplateLiteral("Hola")) == hash(fd.TemplateLiteral("Hola"))
    for bad in (literal(""), literal("hola\nchau"), literal("al 5512345678"), {"texto": "a", "x": 1}, "nombre"):
        assert fd.literal_from_variable(bad) is None
    assert fd.variable_label(literal("Hola")) == "«Hola»" and fd.variable_label("nombre") == "nombre"


# -- El motor que lo manda (D-F4.3) ----------------------------------------------

@pytest.mark.parametrize("trigger, permission", [
    ({"evento": "pago_fallido"}, "formulario"), ({"evento": "carrito"}, "formulario"),
    ({"evento": "intencion"}, "formulario"), ({"evento": "compra"}, "compra"),
])
def test_the_native_engine_sends_a_literal_in_its_event_flows(trigger: dict[str, str], permission: str) -> None:
    assert native_not_supported(flow(["nombre", literal()], dispara=trigger, entra={"permiso": permission})) == ()


def test_the_native_audience_sends_a_literal_next_to_the_hour() -> None:
    import test_e483_native_audience_source as audience

    definition = audience._audience(evento={"el": "2026-12-01T20:00", "zona": CDMX})
    for step in definition["pasos"]:
        if "plantilla" in step:
            step["variables"] = ["nombre", "hora_evento", literal()]
    assert native_not_supported(definition, audience=True) == ()


def test_the_payment_slip_and_the_legacy_engine_do_not_send_a_literal() -> None:
    slip = flow(["nombre", literal()], dispara={"evento": "ficha_impresa"},
                termina={"si": ["compra", "ficha_vencida"], "vence": "4d"})
    assert "engine_not_supported:variable_texto" in native_not_supported(slip, payment_slip=True)
    assert "engine_not_supported:variables_differ" in native_not_supported(slip, payment_slip=True)
    assert "engine_not_supported:variable_texto" in not_supported(flow([literal()]), ENGINE_CAPABILITIES_V1)


def test_the_silence_follow_up_keeps_its_two_variables() -> None:
    # D17: el tercer parametro del seguimiento por silencio va en un PR aparte.
    silence = flow(["nombre", "producto", literal()], dispara={"silencio": {
        "lo_ultimo": "agente", "desde": "24h", "hasta": "72h", "conversaciones": ["abierta", "resuelta"]}},
        entra={"permiso": "conversacion", "contesto_a": "cualquier_plantilla"},
        termina={"si": ["compra", "contesta"], "vence": "1d"},
        topes={"por_dia": "sin_tope", "total": "sin_tope",
               "repeticion": {"una_por": "conversacion", "otra_despues_de": "nunca"}})
    silence["pasos"][0]["boton"] = {"link": "checkout", "cupon": "PROTOCOLO10"}
    found = native_not_supported(silence)
    assert "engine_not_supported:variables_seguimiento" in found
    assert "engine_not_supported:variable_texto" in found


def test_the_fit_counts_the_names_and_lets_two_equal_literals_go() -> None:
    assert native_variables_fit(["nombre", literal(), literal()], literal=True)
    assert not native_variables_fit(["nombre", literal()])
    assert not native_variables_fit(["nombre", "nombre", literal()], literal=True)
    assert not native_variables_fit([literal(), "precio"], literal=True)
    assert not native_variables_fit([], literal=True)


# -- Lo que lee el bridge de la base (D-F4.4) --------------------------------------

def test_the_context_of_the_step_decodes_the_literal_in_its_position() -> None:
    from bridge.supabase import _step_variable

    decoded = tuple(_step_variable(item) for item in CONTEXT["template_variables"])
    expected = tuple(fd.TemplateLiteral(item["texto"]) if isinstance(item, dict) else item
                     for item in CONTEXT["definition_variables"])
    assert decoded == expected
    # Lo que no es el objeto queda como estaba: el envio lo cierra como antes.
    assert _step_variable("{no es json") == "{no es json"
    assert _step_variable('{"de": "nombre"}') == '{"de": "nombre"}'


def test_a_branch_of_the_refused_payment_brings_the_literal_as_an_object() -> None:
    from bridge.supabase import SupabaseError, _native_branches

    step = {"plantilla": "maribel_pago_no_completado_01", "idioma": "es_MX", "variables": ["nombre", literal()]}
    [first, last] = _native_branches([
        {"cuando": {"dato": "rechazos", "es": ["uno"]}, "paso": step}, {"cuando": None, "paso": step}],
        operation="flow_engine_execution_context_v1")
    assert first.template_variables == ("nombre", fd.TemplateLiteral(TEXT)) == last.template_variables
    with pytest.raises(SupabaseError):
        _native_branches([{"cuando": {"dato": "rechazos", "es": ["uno"]}, "paso": {**step, "variables": [{"de": 1}]}},
                          {"cuando": None, "paso": step}], operation="flow_engine_execution_context_v1")


# -- El envio nativo ---------------------------------------------------------------

import test_flow_engine_native_dispatch as native  # noqa: E402

from bridge.worker import FLOW_ENGINE_STEP_UNSUPPORTED  # noqa: E402

TWO_MARKS = "att1_pago_no_completado_01"
TWO_MARKS_TEMPLATE = {
    **copy.deepcopy(native.WELCOME_TEMPLATE), "name": TWO_MARKS,
    "components": [{"type": "BODY", "text": "Hola {{1}}, {{2}}",
                    "example": {"body_text": [["Edith", "tu pago no se completó"]]}}],
}


def _send(tmp_path: Path, variables: tuple[object, ...], *, on: bool) -> tuple[Any, Any]:
    authority = native._NativeAuthority(step=native._step(template_name=TWO_MARKS, template_variables=variables))
    chatwoot = native._NativeChatwoot()
    chatwoot.catalog.append(copy.deepcopy(TWO_MARKS_TEMPLATE))
    dispatcher = native._dispatcher(authority, chatwoot, tmp_path)
    dispatcher._native_literal_variables_enabled = on  # app.py: FLOW_LITERAL_VARIABLES_ENABLED
    native._run(dispatcher)
    return authority, chatwoot


def test_with_the_flag_the_literal_goes_in_its_marker_as_written(tmp_path: Path) -> None:
    authority, chatwoot = _send(tmp_path, ("nombre", fd.TemplateLiteral(TEXT)), on=True)
    [message] = native._templates(chatwoot)
    assert message["template_params"]["name"] == TWO_MARKS
    assert message["content"] == f"Hola Edith García Pérez, {TEXT}"
    assert message["template_params"]["processed_params"]["body"] == {"1": "Edith García Pérez", "2": TEXT}


def test_the_literal_keeps_its_spaces(tmp_path: Path) -> None:
    # Los nombres que arma el bridge se colapsan; el texto fijo va como lo aprobo Dan.
    text = "Te esperamos    hoy"
    _, chatwoot = _send(tmp_path, ("nombre", fd.TemplateLiteral(text)), on=True)
    [message] = native._templates(chatwoot)
    assert message["template_params"]["processed_params"]["body"]["2"] == text


def test_without_the_flag_a_step_with_a_literal_closes_for_good(tmp_path: Path) -> None:
    authority, chatwoot = _send(tmp_path, ("nombre", fd.TemplateLiteral(TEXT)), on=False)
    assert native._templates(chatwoot) == []
    [closed] = authority.finalizations
    assert closed["reason_code"] == FLOW_ENGINE_STEP_UNSUPPORTED and closed["next_attempt_at"] is None


def test_a_template_braked_by_f1_does_not_go_out_with_a_literal(tmp_path: Path) -> None:
    # F1 (20261127100000): una plantilla retirada por recategorizacion queda marcada en el catalogo y el envio
    # nativo la rechaza igual que antes; el texto fijo no abre otro camino.
    from bridge.approved_templates import BRAKE_KEY, BRAKED_STATUS

    authority = native._NativeAuthority(step=native._step(
        template_name=TWO_MARKS, template_variables=("nombre", fd.TemplateLiteral(TEXT))))
    chatwoot = native._NativeChatwoot()
    chatwoot.catalog.append({**copy.deepcopy(TWO_MARKS_TEMPLATE), "status": BRAKED_STATUS,
                             BRAKE_KEY: "template_recategorized"})
    dispatcher = native._dispatcher(authority, chatwoot, tmp_path)
    dispatcher._native_literal_variables_enabled = True
    native._run(dispatcher)
    assert native._templates(chatwoot) == []
    assert authority.finalizations[0]["reason_code"] == "template_recategorized"


def test_what_meta_would_refuse_is_stopped_before_sending(tmp_path: Path) -> None:
    # Un texto que la base dejo pasar y Meta rechazaria (error 132018) no sale.
    authority, chatwoot = _send(tmp_path, ("nombre", fd.TemplateLiteral("hola     chau")), on=True)
    assert native._templates(chatwoot) == []
    assert authority.finalizations[0]["reason_code"] == "template_parameters_missing"


def test_a_body_longer_than_1024_is_stopped_before_sending(tmp_path: Path) -> None:
    authority, chatwoot = _send(tmp_path, ("nombre", fd.TemplateLiteral("a" * 1020)), on=True)
    assert native._templates(chatwoot) == []
    assert authority.finalizations[0]["reason_code"] == "approved_template_mismatch"


def test_render_stops_the_bad_values_and_the_long_body() -> None:
    inbox = {"message_templates": [TWO_MARKS_TEMPLATE]}
    template = parse_approved_template(inbox, template_name=TWO_MARKS, expected_language="es_MX",
                                       expected_category="UTILITY", parameter_count=2,
                                       parameterless_buttons_only=True)
    assert template.render({"1": "Edith", "2": TEXT}) == f"Hola Edith, {TEXT}"
    for bad, detail in (("hola\nchau", "invalid_parameter_whitespace"), ("a\tb", "invalid_parameter_whitespace"),
                        ("a     b", "invalid_parameter_whitespace"), (" ", "empty_parameter"),
                        ("a" * 1020, "rendered_body_too_long")):
        with pytest.raises(ApprovedTemplateError) as error:
            template.render({"1": "Edith", "2": bad})
        assert error.value.detail == detail


# -- Lo que ve quien aprueba (D16) -------------------------------------------------

def test_the_preview_puts_the_literal_in_its_marker_and_lists_it_whole() -> None:
    from bridge.flow_change_preview import message_as_seen

    seen = message_as_seen(TWO_MARKS_TEMPLATE, {"variables": ["nombre", literal()]})
    assert seen["cuerpo"] == f"Hola [nombre], {TEXT}"
    assert seen["textos_fijos"] == [{"marcador": 2, "texto": TEXT}]
    assert "textos_fijos" not in message_as_seen(TWO_MARKS_TEMPLATE, {"variables": ["nombre", "producto"]})


def test_the_page_shows_the_literal_whole_and_escaped() -> None:
    from bridge.flow_approval import _message_bubble

    text = "Tu acceso <b>te espera</b> & no se vence"
    step = {"plantilla": TWO_MARKS, "variables": ["nombre", literal(text)]}
    html = _message_bubble(None, step)
    assert "Texto fijo en el marcador {{2}}" in html
    assert "Tu acceso &lt;b&gt;te espera&lt;/b&gt; &amp; no se vence" in html and "<b>" not in html
    long_step = {"plantilla": TWO_MARKS, "variables": [literal("a" * 400)]}
    assert "a" * 400 in _message_bubble(None, long_step)
    assert "Texto fijo" not in _message_bubble(None, {"plantilla": TWO_MARKS, "variables": ["nombre"]})


def test_the_operator_and_the_page_carry_the_flag() -> None:
    from bridge.flow_change_operator import OperatorInputError, instance_from_input

    base = {"tenant_ref": "lancemos", "ally_ref": "maribel", "products": ["el-protocolo"], "time_zone": CDMX}
    assert instance_from_input(base).literal_variables is False
    assert instance_from_input({**base, "literal_variables": True}).literal_variables is True
    with pytest.raises(OperatorInputError):
        instance_from_input({**base, "literal_variables": "true"})
    source = (ROOT / "src" / "bridge" / "flow_approval.py").read_text(encoding="utf-8")
    assert '**({"literal_variables": True} if literal_variables else {})' in source


# -- El aviso de la plantilla de utilidad (D16) -------------------------------------

def test_the_promotional_utility_warning_recommends_the_literal() -> None:
    from test_template_request import BY_NAME, MARIBEL, check, parse_template

    retomar = BY_NAME["maribel_retomar_proceso_01"]
    [warning] = [w for w in check(parse_template(retomar["template"]), MARIBEL).warnings
                 if w.code == "template_utility_sounds_promotional"]
    assert "variable de texto del flujo" in warning.reason and "{texto: …}" in warning.reason


# -- El cableado y el AE -----------------------------------------------------------

def test_the_flag_is_off_by_default_and_portable() -> None:
    import bridge.app as app_module

    assert app_module.Settings.__dataclass_fields__["flow_literal_variables_enabled"].default is False
    assert "flow_literal_variables_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    source = (ROOT / "src" / "bridge" / "app.py").read_text(encoding="utf-8")
    assert source.count("literal_variables=settings.flow_literal_variables_enabled") == 2
    assert "native_literal_variables_enabled=settings.flow_literal_variables_enabled" in source


def test_the_ae_knows_the_literal() -> None:
    skills = ROOT / "profiles" / "automation-expert" / "skills" / "flujos"
    create = (skills / "crear-un-flujo" / "SKILL.md").read_text(encoding="utf-8")
    assert "{texto: \"…\"}" in create and "FLOW_LITERAL_VARIABLES_ENABLED" in create
    assert "no en la ficha ni en el seguimiento a 24 h" in create
    template = (skills / "pedir-plantilla-a-meta" / "SKILL.md").read_text(encoding="utf-8")
    assert "template_utility_sounds_promotional" in template
    plugin = ROOT / "profiles" / "automation-expert" / "plugins" / "automation-expert-change"
    assert 'version: "0.16.0"' in (plugin / "plugin.yaml").read_text(encoding="utf-8")
    assert "{texto: \\\"…\\\"}" in (plugin / "schemas.py").read_text(encoding="utf-8")
