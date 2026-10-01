"""Manifiesto de instancia v2 (docs/referencia-manifiesto.md).

Fixtures: tests/fixtures/instances/att1 (datos de ATT1 medidos el 2026-09-28) y
tests/fixtures/instances/johanna (valores de Johanna que hoy estan en el codigo).
"""

from __future__ import annotations

import copy
import re
import tomllib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from bridge.commercial_ally import JOHANNA_COMMERCIAL_ALLY
from bridge.instance_manifest import (
    DEFAULT_TEMPLATE_PARAMETERS,
    FLOWS,
    InstanceManifest,
    ManifestError,
)
from bridge.lead_precheckout import _JOHANNA_LANDING_OFFERS

FIXTURES = Path(__file__).parent / "fixtures" / "instances"


def _payload(name: str) -> dict:
    return tomllib.loads((FIXTURES / name / "instancia.toml").read_text(encoding="utf-8"))


def _load(payload: dict) -> InstanceManifest:
    return InstanceManifest.from_mapping(payload)


def test_johanna_manifest_produces_the_binding_in_code_plus_its_other_landing_offers() -> None:
    manifest = InstanceManifest.from_toml_file(FIXTURES / "johanna" / "instancia.toml")

    binding = manifest.to_commercial_ally_config()

    # Johanna corre hoy desde el codigo, con una sola oferta en el binding. Lo unico
    # que el manifiesto suma son sus otras cinco landings: sus ofertas (F2c) y su
    # sitio, host y ruta (A6). Es lo que gana al mudarse al manifiesto (F5). Nada
    # mas cambia.
    assert binding.additional_offer_codes == (
        "mgbgpp19",
        "s1qfxm7m",
        "jtt6fcsm",
        "ecyu87q0",
        "ulhzpw9a",
    )
    assert [
        (landing.offer_code, landing.site, landing.landing_id, landing.page_host, landing.page_path)
        for landing in binding.additional_offer_landings
    ] == [
        (code, "psicologajohanna", landing_id, "psicologajohanna.com", f"/ldla/evg/vsl/{landing_id}")
        for landing_id, code in _JOHANNA_LANDING_OFFERS.items()
        if landing_id != "ads-a"
    ]
    assert (
        replace(binding, additional_offer_codes=(), additional_offer_landings=())
        == JOHANNA_COMMERCIAL_ALLY
    )


def test_att1_binding_accepts_the_three_landing_offers() -> None:
    manifest = InstanceManifest.from_toml_file(FIXTURES / "att1" / "instancia.toml")

    binding = manifest.to_commercial_ally_config()

    assert binding.offer_code == "gopi6lh7"
    assert binding.additional_offer_codes == ("bmaztyhg", "2uafw5bg")
    assert binding.accepted_offer_codes == ("gopi6lh7", "bmaztyhg", "2uafw5bg")


def test_johanna_manifest_carries_the_six_landing_offers_in_code() -> None:
    manifest = InstanceManifest.from_toml_file(FIXTURES / "johanna" / "instancia.toml")

    assert {offer.landing_id: offer.code for offer in manifest.offers} == dict(
        _JOHANNA_LANDING_OFFERS
    )


def test_att1_manifest_loads_with_its_measured_values() -> None:
    manifest = InstanceManifest.from_toml_file(FIXTURES / "att1" / "instancia.toml")

    assert (manifest.tenant_ref, manifest.ally_ref) == ("lancemos", "att1")
    assert (manifest.hotmart_product_id, manifest.hotlink) == (5071808, "D98014973Y")
    assert manifest.price == Decimal("47")
    assert (manifest.chatwoot_account_id, manifest.chatwoot_inbox_id) == (2, 11)
    assert manifest.default_offer.code == "gopi6lh7"
    assert [offer.code for offer in manifest.offers] == ["gopi6lh7", "bmaztyhg", "2uafw5bg"]
    assert manifest.templates["carrito"].language == "es_MX"
    assert not any(manifest.flows.values())


def test_att1_excludes_the_ghl_recovery_offer() -> None:
    manifest = InstanceManifest.from_toml_file(FIXTURES / "att1" / "instancia.toml")

    assert "83utgyow" not in {offer.code for offer in manifest.offers}


def test_att1_and_johanna_do_not_share_any_customer_value() -> None:
    att1 = InstanceManifest.from_toml_file(FIXTURES / "att1" / "instancia.toml")
    johanna = InstanceManifest.from_toml_file(FIXTURES / "johanna" / "instancia.toml")

    assert att1.hotmart_product_id != johanna.hotmart_product_id
    assert att1.hotlink != johanna.hotlink
    assert (att1.chatwoot_account_id, att1.chatwoot_inbox_id) != (
        johanna.chatwoot_account_id,
        johanna.chatwoot_inbox_id,
    )
    assert not {o.code for o in att1.offers} & {o.code for o in johanna.offers}
    assert not {t.name for t in att1.templates.values()} & {
        t.name for t in johanna.templates.values()
    }
    assert att1.inbound_scope_key != johanna.inbound_scope_key


def test_offer_lookup_by_landing_and_checkout_url() -> None:
    manifest = InstanceManifest.from_toml_file(FIXTURES / "att1" / "instancia.toml")

    offer = manifest.offer_for_landing("metodoraizana", "org-a")
    assert offer is not None and offer.code == "bmaztyhg"
    assert manifest.checkout_url(offer) == "https://pay.hotmart.com/D98014973Y?off=bmaztyhg"
    assert manifest.offer_for_landing("metodoraizana", "no-existe") is None
    assert manifest.checkout_url() == "https://pay.hotmart.com/D98014973Y?off=gopi6lh7"


def test_flow_blockers_name_the_missing_event_and_template() -> None:
    manifest = InstanceManifest.from_toml_file(FIXTURES / "att1" / "instancia.toml")

    assert manifest.flow_blockers("carrito") == ()
    assert manifest.flow_blockers("precheckout") == ("el evento 'intencion' no esta en eventos",)
    assert manifest.flow_blockers("reactivacion") == ("falta la plantilla 'reactivacion'",)


def test_a_flow_cannot_be_on_without_its_event_or_template() -> None:
    payload = _payload("att1")
    payload["flujos"]["reactivacion"] = True

    with pytest.raises(ManifestError, match="flujos.reactivacion"):
        _load(payload)


def test_unknown_keys_are_rejected_at_every_level() -> None:
    for path in ([], ["instancia"], ["hotmart"], ["chatwoot"], ["guardas"]):
        payload = _payload("att1")
        table = payload
        for key in path:
            table = table[key]
        table["clave_inventada"] = "x"
        with pytest.raises(ManifestError, match="clave_inventada"):
            _load(payload)


def test_missing_required_key_is_named() -> None:
    payload = _payload("att1")
    del payload["chatwoot"]["inbox_id"]

    with pytest.raises(ManifestError, match="chatwoot: faltan inbox_id"):
        _load(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p.__setitem__("schema", "setter-instancia/v1"), "schema"),
        (lambda p: p.__setitem__("producto", "1.0.0"), "producto"),
        (lambda p: p["instancia"].__setitem__("ally_ref", "ATT1"), "slug"),
        (lambda p: p["instancia"].__setitem__("zona_horaria", "Mexico/Nowhere"), "zona IANA"),
        (lambda p: p["hotmart"].__setitem__("precio", 47.0), "precio"),
        (lambda p: p["hotmart"].__setitem__("precio", "-1"), "positivo"),
        (lambda p: p["hotmart"].__setitem__("moneda", "usd"), "moneda"),
        (lambda p: p["chatwoot"].__setitem__("inbox_id", True), "entero positivo"),
        (lambda p: p["chatwoot"].__setitem__("inbox_id", 0), "entero positivo"),
        (lambda p: p["plantillas"].__setitem__("carrito", {"nombre": "Carrito", "idioma": "es_MX"}), "nombre de Meta"),
        (lambda p: p["plantillas"].__setitem__("carrito", {"nombre": "att1_x", "idioma": "espanol"}), "idioma"),
        (lambda p: p.__setitem__("eventos", ["carrito", "clic"]), "eventos no soportados"),
        (lambda p: p["flujos"].__setitem__("inbound", "false"), "true o false"),
        (lambda p: p["agente"].__setitem__("conocimiento", "../otra/knowledge.toml"), "ruta relativa"),
        (lambda p: p["guardas"].__setitem__("terminos_sensibles", ["yodo", "yodo"]), "repetidos"),
    ],
)
def test_invalid_values_are_rejected_with_a_readable_reason(mutate, message) -> None:
    payload = _payload("att1")
    mutate(payload)

    with pytest.raises(ManifestError, match=message):
        _load(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda offers: offers[0].__setitem__("por_defecto", False), "exactamente una"),
        (lambda offers: offers[1].__setitem__("por_defecto", True), "exactamente una"),
        (lambda offers: offers[1].__setitem__("codigo", "gopi6lh7"), "codigos repetidos"),
        (lambda offers: offers[1].__setitem__("landing_id", "ads-a"), "misma landing"),
        (lambda offers: offers[0].__setitem__("url", "http://www.metodoraizana.com/att1"), "https"),
        (lambda offers: offers[0].__setitem__("url", "https://www.metodoraizana.com/att1?x=1"), "query"),
        (lambda offers: offers[0].__setitem__("origen", "meta"), "origen"),
        (lambda offers: offers.clear(), "al menos una"),
    ],
)
def test_offer_catalog_rules(mutate, message) -> None:
    payload = _payload("att1")
    mutate(payload["hotmart"]["ofertas"])

    with pytest.raises(ManifestError, match=message):
        _load(payload)


def test_offer_code_that_yaml_would_read_as_boolean_stays_a_string() -> None:
    payload = _payload("att1")
    payload["hotmart"]["ofertas"][1]["codigo"] = "offf"

    manifest = _load(payload)

    assert manifest.offers[1].code == "offf"


def test_manifest_holds_no_secret_shaped_values() -> None:
    for name in ("att1", "johanna"):
        text = (FIXTURES / name / "instancia.toml").read_text(encoding="utf-8")
        assert not re.search(r"(?i)(token|secret|password|hottok|api_key)\s*=", text)


def test_flows_are_reported_in_a_fixed_order() -> None:
    manifest = InstanceManifest.from_toml_file(FIXTURES / "att1" / "instancia.toml")

    assert tuple(manifest.flows) == FLOWS


def test_loading_does_not_mutate_the_input_mapping() -> None:
    payload = _payload("att1")
    before = copy.deepcopy(payload)

    _load(payload)

    assert payload == before


# ----------------------------------------------------- parametros de plantilla


def test_templates_without_parametros_send_the_two_variables_of_today() -> None:
    for name in ("att1", "johanna"):
        manifest = InstanceManifest.from_toml_file(FIXTURES / name / "instancia.toml")

        assert DEFAULT_TEMPLATE_PARAMETERS == ("nombre", "producto")
        assert {slot: t.parameters for slot, t in manifest.templates.items()} == {
            slot: ("nombre", "producto") for slot in manifest.templates
        }


def test_first_contact_templates_declare_their_variables_in_order() -> None:
    payload = _payload("att1")
    payload["plantillas"]["precheckout"]["parametros"] = ["nombre"]
    payload["plantillas"]["carrito"]["parametros"] = ["nombre"]
    payload["plantillas"]["pago_fallido"]["parametros"] = ["producto", "nombre"]

    manifest = _load(payload)

    assert manifest.templates["precheckout"].parameters == ("nombre",)
    assert manifest.templates["carrito"].parameters == ("nombre",)
    assert manifest.templates["pago_fallido"].parameters == ("producto", "nombre")
    # El nombre y el idioma no cambian por declarar las variables.
    assert manifest.templates["carrito"].name == "att1_carrito_abandonado_01"
    assert manifest.templates["carrito"].language == "es_MX"


@pytest.mark.parametrize("slot", ["reactivacion", "descuento"])
def test_parametros_is_refused_outside_first_contact_templates(slot: str) -> None:
    payload = _payload("att1")
    # att1_descuento_10_post_respuesta_01 esta APPROVED en "en" (comentario del
    # fixture); sirve como plantilla declarada, el flujo sigue apagado.
    payload["plantillas"][slot] = {
        "nombre": "att1_descuento_10_post_respuesta_01",
        "idioma": "en",
        "parametros": ["nombre"],
    }

    with pytest.raises(ManifestError, match=f"plantillas.{slot}.parametros no se admite"):
        _load(payload)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "de 1 a 2"),
        (["nombre", "producto", "nombre"], "de 1 a 2"),
        ("nombre", "de 1 a 2"),
        (["nombre", "nombre"], "repetidas"),
        (["apellido"], "no soportadas apellido"),
        (["Nombre"], "no soportadas Nombre"),
        ([1], "no soportadas 1"),
    ],
)
def test_parametros_rules(value: object, message: str) -> None:
    payload = _payload("att1")
    payload["plantillas"]["carrito"]["parametros"] = value

    with pytest.raises(ManifestError, match=f"plantillas.carrito.parametros.*{message}"):
        _load(payload)


# ------------------------------------------------------- adaptador de GHL
# El fixture de ATT1 es copia de la instancia y no tiene la seccion todavia: los
# casos la arman por mutacion. Los dos ids son los formularios medidos el 2026-09-29
# (tests/fixtures/ghl/): EgDq en ads-a y Om5F en la landing -d de Mexico.

_GHL_ADS_A_FORM = "EgDqRl2xWc59YjVW1q8W"
_GHL_LANDING_D_FORM = "Om5FpIg5Sr5ce7nSkuPy"


def _with_ghl_adapter(payload: dict, forms: object) -> dict:
    payload["eventos"] = [*payload["eventos"], "intencion"]
    payload["adaptadores"] = {"ghl": {"formularios": forms}}
    return payload


def test_ghl_adapter_section_loads_its_forms_in_order() -> None:
    manifest = _load(_with_ghl_adapter(_payload("att1"), [_GHL_ADS_A_FORM, _GHL_LANDING_D_FORM]))

    assert manifest.ghl_form_ids == (_GHL_ADS_A_FORM, _GHL_LANDING_D_FORM)
    assert "intencion" in manifest.events


def test_without_the_ghl_adapter_section_there_are_no_forms() -> None:
    for name in ("att1", "johanna"):
        manifest = InstanceManifest.from_toml_file(FIXTURES / name / "instancia.toml")

        assert manifest.ghl_form_ids == ()


def test_an_empty_adaptadores_table_means_no_adapter() -> None:
    payload = _payload("att1")
    payload["adaptadores"] = {}

    assert _load(payload).ghl_form_ids == ()


def test_the_ghl_adapter_does_not_change_the_binding() -> None:
    plain = InstanceManifest.from_toml_file(FIXTURES / "att1" / "instancia.toml")
    with_adapter = _load(_with_ghl_adapter(_payload("att1"), [_GHL_ADS_A_FORM]))

    assert with_adapter.to_commercial_ally_config() == plain.to_commercial_ally_config()


@pytest.mark.parametrize(
    ("forms", "message"),
    [
        ([], "al menos un formulario"),
        ("EgDqRl2xWc59YjVW1q8W", "lista de textos"),
        ([_GHL_ADS_A_FORM[:19]], "no es un id de formulario de GHL"),
        ([_GHL_ADS_A_FORM + "X"], "no es un id de formulario de GHL"),
        (["EgDqRl2xWc59YjVW1q8-"], "no es un id de formulario de GHL"),
        ([_GHL_ADS_A_FORM, _GHL_ADS_A_FORM], "repetidos"),
        ([" " + _GHL_ADS_A_FORM], "lista de textos"),
    ],
)
def test_ghl_form_ids_rules(forms: object, message: str) -> None:
    payload = _with_ghl_adapter(_payload("att1"), forms)

    with pytest.raises(ManifestError, match=f"adaptadores.ghl.formularios.*{message}"):
        _load(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p["adaptadores"].__setitem__("hubspot", {}), "adaptadores: claves no soportadas hubspot"),
        (lambda p: p["adaptadores"]["ghl"].__setitem__("token", "x"), "adaptadores.ghl: claves no soportadas token"),
        (lambda p: p["adaptadores"]["ghl"].pop("formularios"), "adaptadores.ghl: faltan formularios"),
        (lambda p: p["adaptadores"].__setitem__("ghl", ["EgDqRl2xWc59YjVW1q8W"]), "adaptadores.ghl debe ser una tabla"),
        (lambda p: p.__setitem__("adaptadores", "ghl"), "adaptadores debe ser una tabla"),
    ],
)
def test_ghl_adapter_section_shape(mutate, message: str) -> None:
    payload = _with_ghl_adapter(_payload("att1"), [_GHL_ADS_A_FORM])
    mutate(payload)

    with pytest.raises(ManifestError, match=message):
        _load(payload)


def test_ghl_adapter_requires_the_intencion_event() -> None:
    payload = _payload("att1")
    assert "intencion" not in payload["eventos"]
    payload["adaptadores"] = {"ghl": {"formularios": [_GHL_ADS_A_FORM]}}

    with pytest.raises(ManifestError, match="adaptadores.ghl exige el evento 'intencion' en eventos"):
        _load(payload)


def test_loading_the_ghl_adapter_does_not_mutate_the_input_mapping() -> None:
    payload = _with_ghl_adapter(_payload("att1"), [_GHL_ADS_A_FORM])
    before = copy.deepcopy(payload)

    _load(payload)

    assert payload == before
