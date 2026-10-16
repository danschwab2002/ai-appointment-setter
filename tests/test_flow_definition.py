"""El esquema de un flujo y su normalizacion (setter-flujo/v1).

Contrato: docs/contracts/flow-definition-v1.md («La definicion formal» y «Las
reglas del validador», regla 1). Los flujos de prueba salen de
``tests/fixtures/flow_store/schema_examples_v1.json``, los ocho bloques del
contrato sin ``version`` ni ``estado``; el catalogo, de la bandeja 11 de ATT1
capturada el 2026-10-10.

Los caracteres de control y de direccion se escriben con escapes ``\\u``: el
archivo no tiene ninguno de verdad.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from bridge import flow_definition as fd
from bridge.flow_definition import (
    Finding,
    FlowWorld,
    InstanceView,
    ProposalItem,
    ProposalItemsInvalid,
    StrictJSONError,
    TemplateCatalog,
    WorldFlow,
)

FIXTURES = Path(__file__).parent / "fixtures"
BLOCKS: dict[str, dict[str, Any]] = {
    block["block"]: block
    for block in json.loads(
        (FIXTURES / "flow_store" / "schema_examples_v1.json").read_text(encoding="utf-8")
    )["blocks"]
}
INBOX_11 = json.loads(
    (FIXTURES / "chatwoot_inbox_11_message_templates_20261010.json").read_text(encoding="utf-8")
)
CATALOG = TemplateCatalog.from_inbox_payload(INBOX_11)
INSTANCE = InstanceView("lancemos", "att1", frozenset({"att1"}), "America/Mexico_City")
CDMX_09_21 = {"zona": "America/Mexico_City", "desde": "09:00", "hasta": "21:00"}
FLOW_ID = "0f3c2b1a-6d5e-4f70-8a9b-0c1d2e3f4a5b"


def block(block_id: str) -> dict[str, Any]:
    return copy.deepcopy(BLOCKS[block_id]["definition"])


def normalize(definition: dict[str, Any], *, new_flow: bool = True, instance: InstanceView = INSTANCE,
              other_schedules: tuple[object, ...] = (CDMX_09_21,), catalog: TemplateCatalog | None = CATALOG,
              ) -> fd.NormalizedDefinition:
    return fd.normalize_definition(
        definition, new_flow=new_flow, instance=instance, other_schedules=other_schedules,
        catalog=catalog, path=(0, "definicion"),
    )


def published(definition: dict[str, Any], version: int = 1) -> dict[str, Any]:
    normalized = normalize(definition).definition
    normalized["version"] = version
    normalized["estado"] = "publicada"
    return normalized


def codes(findings: tuple[Finding, ...]) -> list[tuple[int | None, str, str]]:
    return [(finding.rule, finding.code, finding.path) for finding in findings]


# ---------------------------------------------------------------------------
# El parseo estricto.
# ---------------------------------------------------------------------------

def test_a_repeated_key_is_duplicate_key_with_its_path_in_the_document() -> None:
    raw = '{"items": [{"definicion": {"topes": {"bolsa": "a", "por_dia": 1, "bolsa": "b"}}}]}'
    with pytest.raises(StrictJSONError) as caught:
        fd.loads_strict(raw)
    assert caught.value.code == "duplicate_key"
    assert caught.value.path == "/items/0/definicion/topes/bolsa"
    finding = caught.value.finding
    assert finding is not None
    assert (finding.rule, finding.code, finding.kind) == (1, "duplicate_key", "forma")
    assert finding.to_json()["path"] == "/items/0/definicion/topes/bolsa"


def test_the_first_repeated_key_in_document_order_wins() -> None:
    raw = '{"a": {"x": 1, "x": 2}, "b": {"y": 1, "y": 2}}'
    with pytest.raises(StrictJSONError) as caught:
        fd.loads_strict(raw.encode("utf-8"))
    assert caught.value.path == "/a/x"


def test_a_repeated_key_at_the_root_and_inside_a_list() -> None:
    with pytest.raises(StrictJSONError) as caught:
        fd.loads_strict('{"k": 1, "k": 1}')
    assert caught.value.path == "/k"
    with pytest.raises(StrictJSONError) as caught:
        fd.loads_strict('[0, [{"a/b": 1, "a/b": 2}]]')
    assert caught.value.path == "/1/0/a~1b"


@pytest.mark.parametrize(
    "raw",
    [
        '{"por_dia": NaN}',
        '{"por_dia": Infinity}',
        '{"por_dia": -Infinity}',
        '{"por_dia": 1e400}',
        '{"por_dia": 1',
        "",
        b"\xff\xfe",
        "[" * 200_000 + "]" * 200_000,
    ],
)
def test_what_is_not_strict_json_is_invalid_json(raw: str | bytes) -> None:
    with pytest.raises(StrictJSONError) as caught:
        fd.loads_strict(raw)
    assert caught.value.code == "invalid_json"
    assert caught.value.finding is None


def test_strict_json_keeps_integers_booleans_and_text() -> None:
    value = fd.loads_strict('{"por_dia": 80, "flag": true, "nombre": "Ni\\u00f1a", "x": 1.5}')
    assert value == {"por_dia": 80, "flag": True, "nombre": "Niña", "x": 1.5}
    assert type(value["por_dia"]) is int and type(value["flag"]) is bool


@pytest.mark.parametrize(
    "raw",
    [
        # json.dumps escribe el sustituto suelto como un escape JSON, y json.loads lo decodifica.
        pytest.param(json.dumps({"nombre": "Carrito \ud83d"}), id="escapado-en-un-valor"),
        pytest.param(json.dumps({"topes": {"max\udfff": 1}}), id="escapado-en-una-clave"),
        pytest.param(json.dumps({"pasos": [{"derivar": ["x", "\udc00"]}]}), id="escapado-en-una-lista"),
        pytest.param('{"nombre": "Carrito \ud83d"}', id="crudo-en-el-texto"),
        pytest.param('{"a": 1, "a": 2, "b": "\ud83d"}', id="antes-que-una-clave-repetida"),
    ],
)
def test_a_lone_surrogate_is_invalid_json(raw: str) -> None:
    with pytest.raises(StrictJSONError) as caught:
        fd.loads_strict(raw)
    assert caught.value.code == "invalid_json"
    assert caught.value.finding is None


def test_a_surrogate_pair_is_one_character_and_passes() -> None:
    raw = json.dumps({"nombre": "Carrito \U0001f600"})
    assert "\\ud83d\\ude00" in raw
    assert fd.loads_strict(raw) == {"nombre": "Carrito \U0001f600"}


def test_the_size_of_a_value_that_is_not_utf8_json_is_none() -> None:
    assert fd.json_size({"nombre": "Carrito \ud83d"}) is None
    assert fd.json_size({"max\udfff": 1}) is None
    assert fd.json_size({"por_dia": float("nan")}) is None
    assert fd.json_size({"nombre": "Carrito"}) == len('{"nombre": "Carrito"}')


# ---------------------------------------------------------------------------
# Las duraciones y las horas.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "minutes"),
    [("0m", 0), ("60m", 60), ("24h", 1440), ("1d", 1440), ("30d", 43200), ("99999m", 99999)],
)
def test_a_duration_is_an_integer_and_a_unit(text: str, minutes: int) -> None:
    assert fd.parse_duration(text) == minutes


@pytest.mark.parametrize(
    "text",
    ["00m", "-1m", "1.5h", "1w", "60", 60, "60 m", "100000m", "1D", "\u0663m", "60m\n", None, True],
)
def test_a_malformed_duration_is_none(text: object) -> None:
    assert fd.parse_duration(text) is None


@pytest.mark.parametrize(("text", "minutes"), [("00:00", 0), ("09:00", 540), ("23:59", 1439)])
def test_an_hour_is_hh_mm(text: str, minutes: int) -> None:
    assert fd.parse_hour(text) == minutes


@pytest.mark.parametrize("text", ["24:00", "9:00", "09:60", "09:00\n", 900, "21h", "\u0660\u0669:00"])
def test_a_malformed_hour_is_none(text: object) -> None:
    assert fd.parse_hour(text) is None


# ---------------------------------------------------------------------------
# El texto: sin controles ni bidi (Cc y Cf), sin un sustituto UTF-16 suelto
# (Cs) y sin separadores de linea ni de parrafo (Zl y Zp), en cualquier cadena
# del item.
# ---------------------------------------------------------------------------

CONTROL_AND_FORMAT = [
    pytest.param("\u001b", id="ESC"),
    pytest.param("\u202e", id="RLO"),
    pytest.param("\u2066", id="LRI"),
    pytest.param("\u200b", id="ZWSP"),
    pytest.param("\u200d", id="ZWJ"),
    pytest.param("\ufeff", id="BOM"),
    pytest.param("\u00ad", id="SHY"),
    pytest.param("\u0085", id="NEL"),
    pytest.param("\n", id="LF"),
    pytest.param("\t", id="TAB"),
    pytest.param("\U000e0041", id="TAG"),
    # Un sustituto suelto: json.loads lo decodifica de un escape JSON, y no se
    # puede escribir en UTF-8 (validate explotaba con UnicodeEncodeError).
    pytest.param("\ud83d", id="SUSTITUTO-ALTO"),
    pytest.param("\udc00", id="SUSTITUTO-BAJO"),
    pytest.param("\u2028", id="LS"),
    pytest.param("\u2029", id="PS"),
]


@pytest.mark.parametrize("character", CONTROL_AND_FORMAT)
def test_a_control_or_format_character_in_the_name_is_invalid_value(character: str) -> None:
    definition = block("1")
    definition["nombre"] = f"Carrito{character}abandonado"
    findings = normalize(definition).findings
    assert (1, "invalid_value", "/0/definicion/nombre") in codes(findings)


@pytest.mark.parametrize("character", CONTROL_AND_FORMAT)
def test_a_control_character_anywhere_is_invalid_value_with_its_path(character: str) -> None:
    definition = block("5b")
    definition["pasos"][0]["entonces"][0]["conversar"] = f"Retomar{character}la charla"
    definition["entra"]["contesto_a"] = [f"carrito{character}"]
    definition["extra"] = {"deep": [f"x{character}"]}
    found = codes(normalize(definition).findings)
    assert (1, "invalid_value", "/0/definicion/pasos/0/entonces/0/conversar") in found
    assert (1, "invalid_value", "/0/definicion/entra/contesto_a/0") in found
    assert (1, "invalid_value", "/0/definicion/extra/deep/0") in found
    assert (1, "unknown_key", "/0/definicion/extra") in found


@pytest.mark.parametrize("character", CONTROL_AND_FORMAT)
def test_a_control_character_in_the_client_ok_is_invalid_value(character: str) -> None:
    ok = {"quien": f"Juan{character}", "medio": "WhatsApp", "fecha": "2026-10-15"}
    assert (1, "invalid_value", "/0/ok_del_cliente/quien") in codes(
        fd.check_client_ok(ok, path=(0, "ok_del_cliente")))


def test_accents_ene_punctuation_and_a_simple_emoji_pass() -> None:
    definition = block("1")
    definition["nombre"] = "¿Ñandú? Carrito \U0001f600 \u2764\ufe0f"
    assert normalize(definition).findings == ()
    # El selector de variante U+FE0F es Mn: pasa. El ZWJ de un emoji compuesto es Cf: no.
    assert not fd.has_control_or_format("\u2764\ufe0f")
    assert fd.has_control_or_format("\U0001f469\u200d\U0001f4bb")


@pytest.mark.parametrize(
    "character",
    ["\u2028", "\u2029", "\ud800", "\udbff", "\udc00", "\udfff"],
    ids=["LS", "PS", "D800", "DBFF", "DC00", "DFFF"],
)
def test_a_line_separator_or_a_lone_surrogate_is_not_text(character: str) -> None:
    assert fd.has_control_or_format(f"a{character}b")
    assert not fd.is_clean_text(f"a{character}b", 300)
    # El espacio que no corta (Zs) si es texto.
    assert not fd.has_control_or_format("a\u00a0b")


@pytest.mark.parametrize("character", ["\u2028", "\ud83d", "\udfff"], ids=["LS", "D83D", "DFFF"])
def test_a_separator_or_a_lone_surrogate_in_an_unknown_key_moves_the_path_to_its_parent(character: str) -> None:
    definition = block("1")
    definition["topes"][f"max{character}"] = 5
    found = codes(normalize(definition).findings)
    assert (1, "unknown_key", "/0/definicion/topes") in found


def test_a_control_character_in_an_unknown_key_moves_the_path_to_its_parent() -> None:
    definition = block("1")
    definition["topes"]["max\u202e"] = 5
    found = codes(normalize(definition).findings)
    assert (1, "unknown_key", "/0/definicion/topes") in found


# ---------------------------------------------------------------------------
# Sin un telefono ni un mail: identifier_not_allowed, la regla de la 1a.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("where", "text"),
    [
        ("nombre", "Carrito 55 1234 5678"),
        ("nombre", "Carrito de ana@example.test"),
        ("conversar", "Escribile al +52 (55) 1234-5678"),
        ("derivar", "Llamar al 5512345678"),
    ],
)
def test_a_phone_or_a_mail_in_a_text_is_identifier_not_allowed(where: str, text: str) -> None:
    definition = block("5b")
    if where == "nombre":
        definition["nombre"] = text
        path = "/0/definicion/nombre"
    elif where == "conversar":
        definition["pasos"][0]["entonces"][0]["conversar"] = text
        path = "/0/definicion/pasos/0/entonces/0/conversar"
    else:
        definition["pasos"][0]["entonces"] = [{"derivar": text}]
        path = "/0/definicion/pasos/0/entonces/0/derivar"
    findings = [f for f in normalize(definition).findings if f.code == "identifier_not_allowed"]
    assert [(f.rule, f.kind, f.path) for f in findings] == [(None, "ciclo", path)]


def test_digits_split_by_words_are_not_a_phone() -> None:
    definition = block("1")
    definition["nombre"] = "Carrito 2 veces y 123 mas 4567"
    assert normalize(definition).findings == ()


def test_an_iso_date_reads_as_a_phone_as_the_design_says() -> None:
    # Conocido y aceptado (diseno, §5.3): se resuelve donde entra texto libre largo, en la pieza 4.
    definition = block("1")
    definition["nombre"] = "Carrito 2026-10-16"
    assert [f.code for f in normalize(definition).findings] == ["identifier_not_allowed"]


# ---------------------------------------------------------------------------
# La normalizacion de un flujo nuevo.
# ---------------------------------------------------------------------------

def test_a_minimal_event_flow_gets_every_default_of_the_schema() -> None:
    minimal = {
        "flujo": "carrito",
        "nombre": "Carrito abandonado",
        "dispara": {"evento": "carrito"},
        "pasos": [{"plantilla": "att1_carrito_abandonado_01"}],
    }
    result = normalize(minimal)
    assert result.findings == ()
    assert result.definition == {
        "esquema": "setter-flujo/v1",
        "flujo": "carrito",
        "producto": "att1",
        "nombre": "Carrito abandonado",
        "estado": "borrador",
        "dispara": {"evento": "carrito"},
        "entra": {"permiso": "formulario", "contesto_a": []},
        "pasos": [{"plantilla": "att1_carrito_abandonado_01", "idioma": "es_MX",
                   "variables": ["nombre", "producto"]}],
        "termina": {"si": ["compra"], "vence": "1d"},
        "horario": {"zona": "America/Mexico_City", "desde": "09:00", "hasta": "21:00"},
        "topes": {"bolsa": "att1-carrito", "por_dia": 10, "total": 150,
                  "repeticion": {"una_por": "persona_y_oferta", "otra_despues_de": "0m"}},
        "prioridad": {"le_gana_a": [], "no_arranca_mientras": []},
    }
    assert list(result.definition) == [
        "esquema", "flujo", "producto", "nombre", "estado", "dispara", "entra", "pasos",
        "termina", "horario", "topes", "prioridad",
    ]


def test_a_minimal_silence_flow_gets_the_silence_defaults() -> None:
    minimal = {
        "flujo": "descuento",
        "nombre": "Seguimiento",
        "dispara": {"silencio": {"lo_ultimo": "agente", "desde": "24h", "hasta": "72h"}},
        "pasos": [{"plantilla": "att1_seguimiento_descuento_01",
                   "boton": {"link": "checkout", "cupon": "TIROIDES10"}}],
    }
    definition = normalize(minimal).definition
    assert definition["dispara"]["silencio"]["conversaciones"] == ["abierta"]
    assert definition["entra"] == {"permiso": "conversacion", "contesto_a": []}
    assert definition["termina"] == {"si": ["compra"]}
    assert definition["topes"] == {"bolsa": "att1-descuento", "por_dia": "sin_tope", "total": "sin_tope",
                                   "repeticion": {"una_por": "conversacion", "otra_despues_de": "nunca"}}
    assert definition["pasos"][0] == {"plantilla": "att1_seguimiento_descuento_01", "idioma": "es_MX",
                                      "variables": ["nombre", "producto"],
                                      "boton": {"link": "checkout", "cupon": "TIROIDES10"}}


def test_a_minimal_purchase_flow_gets_the_purchase_defaults() -> None:
    minimal = {
        "flujo": "activacion",
        "nombre": "Bienvenida",
        "dispara": {"evento": "compra"},
        "pasos": [{"plantilla": {"a_crear": "att1_bienvenida_01"}, "idioma": "es_MX"}],
        "horario": "siempre",
    }
    result = normalize(minimal)
    assert result.findings == ()
    assert result.definition["entra"]["permiso"] == "formulario"
    assert result.definition["termina"] == {"si": [], "vence": "1d"}
    assert result.definition["topes"]["por_dia"] == "sin_tope"
    assert result.definition["topes"]["repeticion"] == {"una_por": "persona", "otra_despues_de": "nunca"}


def test_the_schema_blocks_only_gain_what_they_left_out() -> None:
    reactivation = normalize(block("5a")).definition
    assert reactivation["entra"] == {"permiso": "conversacion", "contesto_a": []}
    assert reactivation["topes"]["bolsa"] == "att1-reactivacion"
    assert reactivation["prioridad"] == {"le_gana_a": [], "no_arranca_mientras": []}
    assert reactivation["horario"] == "siempre"
    payment = normalize(block("2")).definition
    assert payment["prioridad"] == {"le_gana_a": ["precheckout"], "no_arranca_mientras": []}
    cart = normalize(block("1")).definition
    expected = block("1")
    expected["entra"]["contesto_a"] = []
    assert {k: v for k, v in cart.items() if k not in ("esquema", "producto", "estado")} == expected


def test_without_catalog_a_new_template_step_needs_its_language() -> None:
    minimal = {"flujo": "carrito", "nombre": "Carrito", "dispara": {"evento": "carrito"},
               "pasos": [{"plantilla": "att1_carrito_abandonado_01"}]}
    found = codes(normalize(minimal, catalog=None).findings)
    assert found == [(1, "required_key_missing", "/0/definicion/pasos/0/idioma")]
    to_create = {"flujo": "carrito", "nombre": "Carrito", "dispara": {"evento": "carrito"},
                 "pasos": [{"plantilla": {"a_crear": "att1_nueva_01"}}]}
    assert codes(normalize(to_create).findings) == [
        (1, "required_key_missing", "/0/definicion/pasos/0/idioma")]


@pytest.mark.parametrize(
    ("others", "time_zone", "expected"),
    [
        ((CDMX_09_21, CDMX_09_21), "America/Mexico_City", CDMX_09_21),
        (({"zona": "America/Bogota", "desde": "09:00", "hasta": "21:00"},), "America/Mexico_City", CDMX_09_21),
        (("siempre", "siempre"), "America/Mexico_City", "siempre"),
        ((CDMX_09_21, {"zona": "America/Mexico_City", "desde": "09:00", "hasta": "20:59"}), "America/Mexico_City", None),
        ((CDMX_09_21, "siempre"), "America/Mexico_City", None),
        ((), "America/Mexico_City", None),
        ((CDMX_09_21,), None, None),
    ],
)
def test_the_default_schedule_is_the_zone_and_the_common_franja(
    others: tuple[object, ...], time_zone: str | None, expected: object,
) -> None:
    minimal = {"flujo": "carrito", "nombre": "Carrito", "dispara": {"evento": "carrito"},
               "pasos": [{"plantilla": "att1_carrito_abandonado_01"}]}
    instance = InstanceView("lancemos", "att1", frozenset({"att1"}), time_zone)
    result = normalize(minimal, instance=instance, other_schedules=others)
    if expected is None:
        assert codes(result.findings) == [(1, "required_key_missing", "/0/definicion/horario")]
        assert "horario" not in result.definition
    else:
        assert result.findings == ()
        assert result.definition["horario"] == expected


def test_a_schedule_object_without_zone_takes_the_zone_of_the_instance() -> None:
    minimal = {"flujo": "carrito", "nombre": "Carrito", "dispara": {"evento": "carrito"},
               "pasos": [{"plantilla": "att1_carrito_abandonado_01"}],
               "horario": {"desde": "10:00", "hasta": "18:00"}}
    result = normalize(minimal)
    assert result.definition["horario"] == {"zona": "America/Mexico_City", "desde": "10:00", "hasta": "18:00"}
    without_zone = InstanceView("lancemos", "att1", frozenset({"att1"}), None)
    assert codes(normalize(minimal, instance=without_zone).findings) == [
        (1, "required_key_missing", "/0/definicion/horario/zona")]


def test_with_several_products_the_product_is_not_completed() -> None:
    two = InstanceView("lancemos", "att1", frozenset({"att1", "att2"}), "America/Mexico_City")
    result = normalize(block("1"), instance=two)
    assert "producto" not in result.definition
    # La bolsa del carrito esta escrita: no hace falta el producto para armarla.
    assert result.findings == ()
    without_pool = block("3")
    del without_pool["topes"]["bolsa"]
    assert "bolsa" not in normalize(without_pool, instance=two).definition["topes"]


def test_without_a_trigger_the_trigger_defaults_wait() -> None:
    definition = block("1")
    del definition["dispara"]
    del definition["entra"]["permiso"]
    del definition["termina"]
    del definition["topes"]["por_dia"]
    result = normalize(definition)
    assert codes(result.findings) == [(1, "required_key_missing", "/0/definicion/dispara")]
    assert "permiso" not in result.definition["entra"]
    assert result.definition["termina"] == {}
    assert "por_dia" not in result.definition["topes"]


# ---------------------------------------------------------------------------
# La bolsa por defecto y default_pool_invalid.
# ---------------------------------------------------------------------------

def test_the_default_pool_is_product_dash_flow() -> None:
    definition = block("4")
    assert "bolsa" not in definition["topes"]
    assert normalize(definition).definition["topes"]["bolsa"] == "att1-descuento"


@pytest.mark.parametrize(
    "product",
    [pytest.param("1producto", id="empieza-con-digito"), pytest.param("p" * 64, id="64-caracteres")],
)
def test_a_default_pool_that_breaks_the_pool_shape_is_blocked(product: str) -> None:
    instance = InstanceView("lancemos", "att1", frozenset({product}), "America/Mexico_City")
    definition = block("4")
    result = normalize(definition, instance=instance)
    [finding] = result.findings
    assert (finding.rule, finding.code, finding.kind, finding.path) == (
        1, "default_pool_invalid", "forma", "/0/definicion/topes/bolsa")
    assert finding.alternative == "Escribir topes.bolsa."
    assert "bolsa" not in result.definition["topes"]
    definition["topes"]["bolsa"] = "att1-descuento"
    assert normalize(definition, instance=instance).findings == ()


# ---------------------------------------------------------------------------
# Un flujo con version recibe la definicion completa.
# ---------------------------------------------------------------------------

def test_a_versioned_flow_with_the_full_definition_passes() -> None:
    draft = fd.as_draft(published(block("1")))
    assert normalize(draft, new_flow=False).findings == ()


@pytest.mark.parametrize(
    ("remove", "path"),
    [
        (("topes", "repeticion"), "/0/definicion/topes/repeticion"),
        (("topes", "bolsa"), "/0/definicion/topes/bolsa"),
        (("topes",), "/0/definicion/topes"),
        (("horario",), "/0/definicion/horario"),
        (("prioridad", "no_arranca_mientras"), "/0/definicion/prioridad/no_arranca_mientras"),
        (("entra", "contesto_a"), "/0/definicion/entra/contesto_a"),
        (("pasos", 0, "idioma"), "/0/definicion/pasos/0/idioma"),
        (("pasos", 0, "variables"), "/0/definicion/pasos/0/variables"),
    ],
)
def test_a_versioned_flow_missing_a_defaultable_key_is_required_key_missing_without_rule(
    remove: tuple[str | int, ...], path: str,
) -> None:
    draft = fd.as_draft(published(block("1")))
    parent: Any = draft
    for key in remove[:-1]:
        parent = parent[key]
    del parent[remove[-1]]
    result = normalize(draft, new_flow=False)
    findings = [f for f in result.findings if f.code == "required_key_missing"]
    assert [(f.rule, f.kind, f.path) for f in findings] == [(None, "ciclo", path)]
    assert findings[0].reason is not None and "versión publicada" in findings[0].reason
    # No se completa: lo que se guarda es lo que llego.
    parent_after: Any = result.definition
    for key in remove[:-1]:
        parent_after = parent_after[key]
    assert remove[-1] not in parent_after


def test_a_versioned_flow_without_a_name_is_required_key_missing_of_rule_1() -> None:
    draft = fd.as_draft(published(block("1")))
    del draft["nombre"]
    assert codes(normalize(draft, new_flow=False).findings) == [
        (1, "required_key_missing", "/0/definicion/nombre")]


def test_a_versioned_flow_still_gets_schema_state_and_product() -> None:
    draft = fd.as_draft(published(block("1")))
    for key in ("esquema", "estado", "producto"):
        del draft[key]
    result = normalize(draft, new_flow=False)
    assert result.findings == ()
    assert (result.definition["esquema"], result.definition["estado"], result.definition["producto"]) == (
        "setter-flujo/v1", "borrador", "att1")


# ---------------------------------------------------------------------------
# La idempotencia.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("block_id", sorted(BLOCKS))
def test_normalizing_a_normalized_definition_gives_the_same(block_id: str) -> None:
    once = normalize(block(block_id))
    twice = normalize(once.definition)
    assert twice.definition == once.definition
    assert list(twice.definition) == list(once.definition)
    assert twice.findings == once.findings
    versioned = normalize(once.definition, new_flow=False)
    assert versioned.definition == once.definition and versioned.findings == once.findings


def test_normalizing_does_not_touch_the_input() -> None:
    definition = block("3")
    before = copy.deepcopy(definition)
    normalize(definition)
    assert definition == before


# ---------------------------------------------------------------------------
# La regla 1 sobre la definicion.
# ---------------------------------------------------------------------------

def test_version_and_published_state_in_a_draft_are_set_by_the_cycle() -> None:
    definition = block("1")
    definition["version"] = 1
    definition["estado"] = "publicada"
    found = codes(normalize(definition).findings)
    assert (1, "version_set_by_cycle", "/0/definicion/version") in found
    assert (1, "version_set_by_cycle", "/0/definicion/estado") in found
    definition["estado"] = "activo"
    del definition["version"]
    assert codes(normalize(definition).findings) == [(1, "invalid_value", "/0/definicion/estado")]


def test_a_published_definition_is_checked_as_published() -> None:
    definition = published(block("1"), version=3)
    assert fd.check_definition(definition, published=True) == ()
    assert [f.code for f in fd.check_definition(definition)] == [
        "version_set_by_cycle", "version_set_by_cycle"]
    definition["version"] = 0
    assert [f.code for f in fd.check_definition(definition, published=True)] == ["invalid_value"]


@pytest.mark.parametrize(
    ("change", "path", "code"),
    [
        (lambda d: d["topes"].__setitem__("max", 5), "/0/definicion/topes/max", "unknown_key"),
        (lambda d: d.__setitem__("puerta", {}), "/0/definicion/puerta", "unknown_key"),
        (lambda d: d["pasos"][0].__setitem__("texto", "hola"), "/0/definicion/pasos/0/texto", "unknown_key"),
        (lambda d: d.__setitem__("esquema", "setter-flujo/v2"), "/0/definicion/esquema", "invalid_value"),
        (lambda d: d.__setitem__("flujo", "Carrito"), "/0/definicion/flujo", "invalid_value"),
        (lambda d: d.__setitem__("nombre", "x" * 61), "/0/definicion/nombre", "invalid_value"),
        (lambda d: d.__setitem__("nombre", "   "), "/0/definicion/nombre", "invalid_value"),
        (lambda d: d["dispara"].__setitem__("evento", "abandono"), "/0/definicion/dispara/evento", "invalid_value"),
        (lambda d: d["entra"].__setitem__("permiso", "todos"), "/0/definicion/entra/permiso", "invalid_value"),
        (lambda d: d.__setitem__("pasos", []), "/0/definicion/pasos", "invalid_value"),
        (lambda d: d["pasos"].append({"esperar": "2h", "plantilla": "x"}), "/0/definicion/pasos/1", "invalid_value"),
        (lambda d: d["pasos"].append({"esperar": "dos horas"}), "/0/definicion/pasos/1/esperar", "invalid_value"),
        (lambda d: d["pasos"][0].__setitem__("idioma", "espanol"), "/0/definicion/pasos/0/idioma", "invalid_value"),
        (lambda d: d["pasos"][0].__setitem__("variables", ["email"]), "/0/definicion/pasos/0/variables/0", "invalid_value"),
        (lambda d: d["pasos"][0].__setitem__("plantilla", "Att1 Carrito"), "/0/definicion/pasos/0/plantilla", "invalid_value"),
        (lambda d: d["pasos"][0].__setitem__("boton", {"cupon": "X"}), "/0/definicion/pasos/0/boton/link", "required_key_missing"),
        (lambda d: d["termina"].__setitem__("si", ["baja"]), "/0/definicion/termina/si/0", "invalid_value"),
        (lambda d: d["termina"].__setitem__("vence", 1), "/0/definicion/termina/vence", "invalid_value"),
        (lambda d: d.__setitem__("horario", "nunca"), "/0/definicion/horario", "invalid_value"),
        (lambda d: d["topes"].__setitem__("bolsa", "Att1"), "/0/definicion/topes/bolsa", "invalid_value"),
        (lambda d: d["topes"]["repeticion"].__setitem__("otra_despues_de", "siempre"), "/0/definicion/topes/repeticion/otra_despues_de", "invalid_value"),
        (lambda d: d["prioridad"].__setitem__("le_gana_a", ["Carrito"]), "/0/definicion/prioridad/le_gana_a/0", "invalid_value"),
        (lambda d: d.__setitem__("prioridad", ["precheckout"]), "/0/definicion/prioridad", "invalid_value"),
    ],
)
def test_rule_1_sees_keys_and_values_outside_the_schema(change: Any, path: str, code: str) -> None:
    definition = block("1")
    change(definition)
    assert (1, code, path) in codes(normalize(definition).findings)


def test_unresolved_values_are_accepted_in_a_draft() -> None:
    assert normalize(block("6")).findings == ()
    assert normalize(block("7")).findings == ()
    definition = block("1")
    definition["horario"] = "a_decidir"
    definition["topes"]["por_dia"] = "a_medir"
    definition["pasos"][0]["variables"] = ["nombre", "a_decidir"]
    assert normalize(definition).findings == ()


def test_a_definition_deeper_than_the_schema_is_rejected_whole() -> None:
    definition = block("1")
    deep: Any = []
    for _ in range(200):
        deep = [deep]
    definition["extra"] = deep
    result = normalize(definition)
    assert codes(result.findings) == [(1, "invalid_value", "/0/definicion")]


def test_a_definition_over_32_kib_is_invalid_value_at_its_root() -> None:
    definition = block("5b")
    definition["pasos"][0]["entonces"] = [{"derivar": "nota " * 7000}]
    assert (1, "invalid_value", "/0/definicion") in codes(normalize(definition).findings)


def test_an_oversized_definition_is_not_checked_string_by_string() -> None:
    # El tamano se mide primero: lo que pasa de 32 KiB da ese hallazgo, en la raiz
    # y primero, y no corre los chequeos por cadena (los identificadores de la 1a
    # son cuadraticos en un tramo sin @).
    definition = block("5b")
    definition["pasos"][0]["entonces"] = [{"derivar": "Llamar al 5512345678\u202e " + "nota " * 7000}]
    assert codes(normalize(definition).findings) == [(1, "invalid_value", "/0/definicion")]
    # El mismo texto, corto: los dos chequeos por cadena corren.
    definition["pasos"][0]["entonces"] = [{"derivar": "Llamar al 5512345678\u202e"}]
    assert codes(normalize(definition).findings) == [
        (1, "invalid_value", "/0/definicion/pasos/0/entonces/0/derivar"),
        (None, "identifier_not_allowed", "/0/definicion/pasos/0/entonces/0/derivar"),
    ]


def test_a_definition_with_a_lone_surrogate_is_not_json_and_says_where() -> None:
    definition = block("1")
    definition["nombre"] = "Carrito \ud83d"
    assert codes(normalize(definition).findings) == [
        (1, "invalid_value", "/0/definicion"), (1, "invalid_value", "/0/definicion/nombre")]


def test_a_definition_that_is_not_an_object_is_invalid_value() -> None:
    assert codes(fd.normalize_definition(["x"], new_flow=True, instance=INSTANCE).findings) == [
        (1, "invalid_value", "")]


# ---------------------------------------------------------------------------
# El OK del cliente.
# ---------------------------------------------------------------------------

def test_the_client_ok_has_exactly_who_how_and_when() -> None:
    assert fd.check_client_ok({"quien": "Juan", "medio": "WhatsApp", "fecha": "2026-10-15"},
                              path=(0, "ok_del_cliente")) == ()
    found = codes(fd.check_client_ok({"quien": "Juan", "fecha": "2026-02-30", "extra": 1},
                                     path=(0, "ok_del_cliente")))
    assert (1, "unknown_key", "/0/ok_del_cliente/extra") in found
    assert (1, "required_key_missing", "/0/ok_del_cliente/medio") in found
    assert (1, "invalid_value", "/0/ok_del_cliente/fecha") in found
    assert codes(fd.check_client_ok({"quien": "J" * 121, "medio": "x", "fecha": "2026-10-15"},
                                    path=(0, "ok_del_cliente"))) == [
        (1, "invalid_value", "/0/ok_del_cliente/quien")]


# ---------------------------------------------------------------------------
# Los items de una propuesta, con la forma minima de la base.
# ---------------------------------------------------------------------------

def _item_json(tipo: str, **extra: Any) -> dict[str, Any]:
    return {"producto": "att1", "flujo": "carrito", "flow_id": FLOW_ID, "tipo": tipo, **extra}


@pytest.mark.parametrize(
    "value",
    [
        _item_json("version", definicion={"flujo": "carrito"}, en_curso="seguir"),
        _item_json("version", definicion={"flujo": "carrito"}, en_curso="seguir",
                   ok_del_cliente={"quien": "Juan", "medio": "WhatsApp", "fecha": "2026-10-15"}),
        _item_json("volver", version=4, en_curso="seguir"),
        _item_json("prender"),
        _item_json("apagar"),
    ],
)
def test_an_item_round_trips_with_exactly_its_keys(value: dict[str, Any]) -> None:
    item = ProposalItem.from_json(value)
    assert item.to_json() == value
    assert list(item.to_json()) == list(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        _item_json("pausar"),
        _item_json("prender", en_curso="seguir"),
        _item_json("version", definicion={}),
        _item_json("version", definicion=[], en_curso="seguir"),
        _item_json("version", definicion={}, en_curso="esperar"),
        _item_json("version", definicion={}, en_curso="seguir", version=2),
        _item_json("version", definicion={}, en_curso="seguir", ok_del_cliente="Juan"),
        _item_json("volver", version=0, en_curso="seguir"),
        _item_json("volver", version=True, en_curso="seguir"),
        _item_json("volver", version="4", en_curso="seguir"),
        _item_json("volver", version=4),
        {**_item_json("prender"), "flow_id": FLOW_ID.upper()},
        {**_item_json("prender"), "producto": "ATT1"},
        {**_item_json("prender"), "flujo": "c"},
    ],
)
def test_an_item_outside_the_minimal_form_is_flow_change_items_invalid(value: object) -> None:
    with pytest.raises(ProposalItemsInvalid) as caught:
        ProposalItem.from_json(value)
    assert caught.value.code == "flow_change_items_invalid"


def test_items_are_one_to_ten_with_one_content_and_one_state_per_flow() -> None:
    content = _item_json("version", definicion={}, en_curso="seguir")
    items = fd.parse_items([content, _item_json("prender")])
    assert [item.tipo for item in items] == ["version", "prender"]
    for value in ([], [_item_json("prender")] * 11, [content, _item_json("volver", version=1, en_curso="seguir")],
                  [_item_json("prender"), _item_json("apagar")], {"items": []}):
        with pytest.raises(ProposalItemsInvalid):
            fd.parse_items(value)


def test_as_draft_drops_the_version_and_sets_the_draft_state() -> None:
    draft = fd.as_draft(published(block("1"), version=5))
    assert "version" not in draft and draft["estado"] == "borrador"


def test_as_published_is_the_form_the_publication_stores() -> None:
    normalized = normalize(block("1")).definition
    stored = fd.as_published(normalized, 3)
    assert stored == published(block("1"), version=3)
    assert list(stored)[:6] == ["esquema", "flujo", "producto", "nombre", "version", "estado"]
    assert fd.check_definition(stored, published=True) == ()
    assert fd.as_draft(stored) == normalized
    assert normalized["estado"] == "borrador" and "version" not in normalized
    for version in (0, True, "3"):
        with pytest.raises(ValueError):
            fd.as_published(normalized, version)


# ---------------------------------------------------------------------------
# El mundo, la instancia y el catalogo.
# ---------------------------------------------------------------------------

def test_base_for_has_every_flow_and_the_generation_of_the_items() -> None:
    cart = "11111111-1111-4111-8111-111111111111"
    payment = "22222222-2222-4222-8222-222222222222"
    other = "33333333-3333-4333-8333-333333333333"
    world = FlowWorld([
        WorldFlow(flow_id=cart, product_ref="att1", flow_key="carrito", state="prendido",
                  active_version=2, generation=7, versions={1: published(block("1")), 2: published(block("1"), 2)}),
        WorldFlow(flow_id=payment, product_ref="att1", flow_key="pago_fallido", generation=0),
        WorldFlow(flow_id=other, product_ref="att2", flow_key="carrito", state="pausado",
                  active_version=1, generation=3, versions={1: published(block("1"))}),
    ])
    items = [ProposalItem(producto="att1", flujo="carrito", flow_id=cart, tipo="prender")]
    assert world.base_for(items) == {
        cart: {"active_version": 2, "generation": 7},
        payment: {"active_version": None},
        other: {"active_version": 1},
    }
    assert world.draft_base_for(items) == {cart: {"active_version": 2}}
    assert world.find("att2", "carrito") is world.flow(other)
    assert world.flow(cart).fixed_zone == "America/Mexico_City"


def test_the_fixed_zone_is_the_oldest_object_schedule() -> None:
    flow_id = "44444444-4444-4444-8444-444444444444"
    always = published(block("1"), 2)
    always["horario"] = "siempre"
    flow = WorldFlow(flow_id=flow_id, product_ref="att1", flow_key="carrito", state="prendido",
                     active_version=2, generation=2, versions={1: published(block("1")), 2: always})
    assert flow.fixed_zone == "America/Mexico_City"
    only_always = WorldFlow(flow_id=flow_id, product_ref="att1", flow_key="carrito", state="prendido",
                            active_version=2, generation=2, versions={2: always},
                            declared_fixed_zone="America/Mexico_City")
    assert only_always.fixed_zone == "America/Mexico_City"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"flow_id": "nope"},
        {"state": "armed"},
        {"state": "prendido"},
        {"active_version": 2, "versions": {}},
        {"generation": -1},
        {"versions": {0: {}}},
    ],
)
def test_a_world_flow_out_of_shape_is_an_error_of_the_caller(kwargs: dict[str, Any]) -> None:
    base = {"flow_id": FLOW_ID, "product_ref": "att1", "flow_key": "carrito"}
    with pytest.raises(ValueError):
        WorldFlow(**{**base, **kwargs})


def test_a_world_with_two_flows_of_one_key_is_an_error_of_the_caller() -> None:
    with pytest.raises(ValueError):
        FlowWorld([
            WorldFlow(flow_id=FLOW_ID, product_ref="att1", flow_key="carrito"),
            WorldFlow(flow_id="55555555-5555-4555-8555-555555555555", product_ref="att1", flow_key="carrito"),
        ])


def test_the_instance_needs_its_refs_and_at_least_one_product() -> None:
    for kwargs in ({"products": frozenset()}, {"products": frozenset({"ATT1"})}, {"tenant_ref": "-x"},
                   {"products": {"att1"}}):
        with pytest.raises(ValueError):
            InstanceView(**{"tenant_ref": "lancemos", "ally_ref": "att1",
                            "products": frozenset({"att1"}), **kwargs})


def test_the_catalog_reads_the_inbox_and_has_its_fingerprint() -> None:
    assert len(CATALOG.templates) == 5
    assert CATALOG.languages("att1_carrito_abandonado_01") == ("es_MX",)
    assert CATALOG.languages("no_existe") == ()
    assert len(CATALOG.catalog_sha256) == 64
    assert TemplateCatalog.from_inbox_payload(copy.deepcopy(INBOX_11)).catalog_sha256 == CATALOG.catalog_sha256
    changed = copy.deepcopy(INBOX_11)
    changed["message_templates"][0]["status"] = "PAUSED"
    assert TemplateCatalog.from_inbox_payload(changed).catalog_sha256 != CATALOG.catalog_sha256
    for payload in (None, [], {"message_templates": {}}):
        with pytest.raises(ValueError):
            TemplateCatalog.from_inbox_payload(payload)


def test_a_catalog_that_cannot_be_written_in_utf8_is_unavailable() -> None:
    payload = copy.deepcopy(INBOX_11)
    payload["message_templates"][0]["nota"] = "Hola \ud83d"
    with pytest.raises(ValueError) as caught:
        TemplateCatalog.from_inbox_payload(payload)
    # El codigo de siempre, y no un UnicodeEncodeError (que tambien es un ValueError).
    assert type(caught.value) is ValueError
    assert str(caught.value) == "template_catalog_unavailable"


# ---------------------------------------------------------------------------
# JSON Pointer, el diff y los hallazgos.
# ---------------------------------------------------------------------------

def test_json_pointer_escapes_tilde_and_slash() -> None:
    assert fd.json_pointer(["a/b", "c~d", 0]) == "/a~1b/c~0d/0"
    assert fd.json_pointer([]) == ""


def test_the_diff_is_field_by_field() -> None:
    before = published(block("1"))
    after = copy.deepcopy(before)
    after["pasos"].insert(0, {"esperar": "2h"})
    after["topes"]["por_dia"] = 90
    del after["prioridad"]["no_arranca_mientras"]
    changes = {change.path: (change.before, change.after) for change in fd.diff(before, after)}
    assert changes["/topes/por_dia"] == (80, 90)
    assert changes["/pasos/0/esperar"] == (None, "2h")
    assert changes["/prioridad/no_arranca_mientras"] == (["pago_fallido"], None)
    assert fd.diff(before, copy.deepcopy(before)) == ()
    assert [c.path for c in fd.diff({"a": 1}, {"a": True})] == ["/a"]
    assert [c.path for c in fd.diff({"a": 1}, {"a": 1.0})] == ["/a"]


def test_a_finding_has_the_shape_the_base_accepts() -> None:
    finding = fd.UNKNOWN_KEY.at("/2/definicion/topes/max")
    assert finding.to_json() == {
        "rule": 1, "code": "unknown_key", "kind": "forma", "path": "/2/definicion/topes/max",
        "reason": fd.UNKNOWN_KEY.reason, "alternative": fd.UNKNOWN_KEY.alternative,
    }
    assert finding.item_index == 2
    assert finding.definition_path == "/topes/max"
    assert Finding(rule=None, code="flow_change_stale_base", kind="ciclo", path="/0").definition_path is None
    for kwargs in ({"rule": 18}, {"rule": True}, {"code": "Bad"}, {"kind": "error"},
                   {"path": "topes"}, {"path": "/" + "x" * 512}, {"path": "/a~b"}, {"path": "/a~2"},
                   {"path": "/a~"}, {"reason": "a\nb"}, {"reason": ""}):
        with pytest.raises(ValueError):
            Finding(**{"rule": 1, "code": "unknown_key", "kind": "forma", "path": "", **kwargs})
    # La base acepta un codigo con dos puntos (la forma de los motivos) y una ruta con ~0 y ~1.
    assert Finding(rule=None, code="le_gano:carrito", kind="ciclo", path="/0/a~0b~1c").path == "/0/a~0b~1c"


def test_a_key_with_tilde_or_slash_gives_a_pointer_the_base_accepts() -> None:
    definition = block("1")
    definition["topes"]["a/b~c"] = 1
    [finding] = normalize(definition).findings
    assert finding.path == "/0/definicion/topes/a~1b~0c"


def test_the_texts_of_the_codes_are_clean_single_lines() -> None:
    for spec in fd.DEFINITION_CODES:
        for text in (spec.reason, spec.limit, spec.alternative):
            assert text is None or fd.is_clean_text(text, 300), spec.code
