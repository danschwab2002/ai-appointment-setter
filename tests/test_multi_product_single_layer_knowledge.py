"""Un conocimiento de una sola capa con [[productos]] en el manifiesto (E-563, MP-05).

El carril A de Forever Young (plan del multiproducto, MP-05): Maribel vende un segundo
producto, el combo de Hotmart de Forever Young, con el conocimiento firmado de siempre.
Hasta esta version, con [[productos]] el bridge no arrancaba si el conocimiento no tenia
una seccion por producto, y el v9 de Maribel es de una sola capa (``[oferta]`` arriba) y
no tiene lugar para otra seccion (30.300 de 32.000 bytes). Ahora:

- un conocimiento de una sola capa cuenta como el del producto principal;
- un producto que no es el principal puede no tener seccion: el agente sabe de el lo que
  le diga una campana vigente que lo nombre en ``oferta.producto`` (``validate`` lo avisa);
- lo que sigue frenando: una seccion de un producto no declarado, y un conocimiento por
  producto sin la seccion del principal;
- sin [[productos]] (ATT1 y Maribel hoy) nada cambia.

Fixtures: ``tests/fixtures/instances/maribel-fbad25e-20261113`` (el manifiesto y el v9 de
Maribel tal cual, ver su ORIGEN.txt). El segundo producto es SINTETICO: su ID de Hotmart,
su hotlink, su oferta y su landing son inventados (el combo real todavia no existe).
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bridge.agent_campaigns import CampaignContext, instance_blocks, load_campaign_segment, parse_campaign
from test_product_label import _Chatwoot, _client

from bridge.app import Settings, agent_products_context, create_app
from bridge.commercial_knowledge import CommercialKnowledge
from bridge.instance_cli import validate_instance
from bridge.instance_manifest import InstanceManifest
from bridge.segment_knowledge import SegmentCatalog, knowledge_product_names

MARIBEL = Path(__file__).parent / "fixtures" / "instances" / "maribel-fbad25e-20261113"
V9_SHA = "078a9fab3b12595ea529326ad2bef29ad78922e846726a471f914678887bfe7d"
V9_OFFER = "El Protocolo Biohacking +"

# SINTETICO (no es el combo real de Hotmart): marcado en cada valor.
FY_ID, FY_HOTLINK, FY_OFFER, FY_OTHER = 9000019, "X000FOREVERYOUNGPRUEBA", "fyprueba1", "fyprueba2"
FY_NAME = "Forever Young de prueba"
FY_LABEL, FY_MACRO = "forever-young", 9901
PRODUCTS = f"""
# --- E-563: el principal y un segundo producto SINTETICO, solo para las pruebas ---
[[productos]]
clave = "maribel"

[[productos]]
clave = "forever-young"
funnel_ref = "forever-young"   # = la clave: los flujos nativos y la siembra van por el funnel_ref
binding_version = 1
etiqueta = "{FY_LABEL}"
macro_etiqueta = {FY_MACRO}

[productos.hotmart]
product_id = {FY_ID}
hotlink = "{FY_HOTLINK}"
product_name = "{FY_NAME}"
moneda = "USD"
precio = "199"

[[productos.hotmart.ofertas]]
codigo = "{FY_OFFER}"
site = "elprotocolo"
landing_id = "black-forever-young-prueba"
url = "https://www.elprotocolo.net/black/forever-young-prueba"
origen = "pauta"
por_defecto = true

[[productos.hotmart.ofertas]]
codigo = "{FY_OTHER}"
site = "elprotocolo"
landing_id = "black-forever-young-prueba-b"
url = "https://www.elprotocolo.net/black/forever-young-prueba-b"
origen = "organico"
"""
NOW = datetime(2026, 11, 20, 18, tzinfo=UTC)


def _instance(tmp_path: Path, *, products: bool = True) -> Path:
    target = tmp_path / "setter-instancia-maribel"
    shutil.copytree(MARIBEL, target)
    if products:
        path = target / "instancia.toml"
        path.write_text(path.read_text(encoding="utf-8") + PRODUCTS, encoding="utf-8")
    return target


def _knowledge() -> CommercialKnowledge:
    return CommercialKnowledge.from_toml_file(MARIBEL / "conocimiento" / "knowledge-v9.toml")


def _settings(instance: Path, tmp_path: Path) -> Settings:
    manifest = InstanceManifest.from_toml_file(instance / "instancia.toml")
    return Settings(
        webhook_secret="webhook-secret",
        allowed_jid=None,
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        agent_bot_id=1,
        chatwoot_account_id=manifest.chatwoot_account_id,
        chatwoot_inbox_id=manifest.chatwoot_inbox_id,
        commercial_ally_config=manifest.to_commercial_ally_config(),
        commercial_ally_manifest_path=instance / "instancia.toml",
        instance_manifest=manifest,
        hermes_model_name=manifest.agent_model_name,
        commercial_knowledge=_knowledge(),
    )


def _context(manifest: InstanceManifest, *, offer_link: bool = True) -> CampaignContext:
    base = _knowledge()
    return CampaignContext(
        manifest=manifest,
        base=base,
        manifest_catalog=SegmentCatalog((), len(base.render().encode("utf-8"))),
        offer_link_enabled=offer_link,
    )


def _campaign(**oferta: str) -> dict:
    return parse_campaign({
        "clave": "forever-young-2026", "nombre": "Black Forever Young", "etiqueta": "forever-young-2026",
        "macro_etiqueta": 9902, "desde": "2026-11-19T10:00:00-06:00", "hasta": "2026-12-01T00:00:00-06:00",
        "palabras_clave": ["forever young"],
        "oferta": dict(oferta),
        "asesoramiento": ["El agente habla siempre como el equipo de El Protocolo."],
    })


# ------------------------------------------------------- el conocimiento sigue igual


def test_the_fixture_is_the_signed_v9_of_maribel() -> None:
    knowledge = _knowledge()

    assert knowledge.by_product is False and knowledge.approved
    assert [product.key for product in knowledge.products] == [None]
    assert knowledge.products[0].offer_name == V9_OFFER
    assert knowledge.rendered_sha256 == V9_SHA
    assert len(knowledge.render().encode("utf-8")) == 30_300


# ----------------------------------------------------------------- el arranque


def test_maribel_with_a_second_product_and_the_v9_validates_with_a_warning(tmp_path: Path) -> None:
    report = validate_instance(_instance(tmp_path), now=NOW)

    assert report["valida"] is True, report["errores"]
    assert [entry["clave"] for entry in report["manifiesto"]["productos"]] == ["maribel", "forever-young"]
    assert report["conocimiento"]["sha256"] == V9_SHA
    [warning] = [aviso for aviso in report["avisos"] if "forever-young" in aviso]
    assert "no tiene seccion en el conocimiento" in warning and "oferta.producto" in warning


def test_maribel_with_a_second_product_and_the_v9_starts(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings(_instance(tmp_path), tmp_path)

    with caplog.at_level("WARNING", logger="bridge.app"):
        # El Chatwoot de control es de mentira: la etiqueta de forever-young lo pide.
        assert create_app(settings, chatwoot_client=_client(_Chatwoot())) is not None

    assert "instance_products_without_knowledge products=forever-young" in caplog.text


def test_maribel_without_products_validates_and_starts_as_before(tmp_path: Path) -> None:
    instance = _instance(tmp_path, products=False)

    report = validate_instance(instance, now=NOW)
    settings = _settings(instance, tmp_path)

    assert report["valida"] is True
    assert "productos" not in report["manifiesto"]
    assert not any("seccion en el conocimiento" in aviso for aviso in report["avisos"])
    assert settings.instance_manifest is not None and settings.instance_manifest.products == ()
    assert create_app(settings) is not None


@pytest.mark.parametrize(("keys", "error"), [
    # El conocimiento por producto sin la seccion del principal.
    (("forever-young",), "no tiene la seccion del producto principal (maribel)"),
    # Una seccion de un producto que el manifiesto no declara.
    (("maribel", "otro"), "productos que el manifiesto no declara (otro)"),
])
def test_what_still_stops_the_bridge(tmp_path: Path, keys: tuple[str, ...], error: str) -> None:
    manifest = InstanceManifest.from_toml_file(_instance(tmp_path) / "instancia.toml")

    assert error in (manifest.knowledge_products_error(keys) or "")


def test_a_knowledge_by_product_may_leave_out_a_product_that_is_not_the_principal(tmp_path: Path) -> None:
    manifest = InstanceManifest.from_toml_file(_instance(tmp_path) / "instancia.toml")

    assert manifest.knowledge_products_error(("maribel",)) is None
    assert manifest.products_without_knowledge(("maribel",)) == ("forever-young",)
    assert manifest.knowledge_products_error(("maribel", "forever-young")) is None
    assert manifest.products_without_knowledge(("maribel", "forever-young")) == ()
    assert manifest.products_without_knowledge((None,)) == ("forever-young",)


# ------------------------------------------------- los nombres que ve el agente


def test_the_names_of_the_products_come_from_the_offer_and_from_hotmart(tmp_path: Path) -> None:
    manifest = InstanceManifest.from_toml_file(_instance(tmp_path) / "instancia.toml")

    assert knowledge_product_names(manifest, _knowledge()) == {
        "maribel": V9_OFFER,
        "forever-young": FY_NAME,
    }


def test_the_agent_gets_both_products_to_ask_for_a_link(tmp_path: Path) -> None:
    # Sin esto la lista quedaba vacia y, con dos productos, ningun link sin `product`
    # pasaba la validacion (link_without_product): ni el de El Protocolo.
    settings = _settings(_instance(tmp_path), tmp_path)

    assert agent_products_context(settings) == [
        {"key": "maribel", "name": V9_OFFER},
        {"key": "forever-young", "name": FY_NAME},
    ]


def test_without_products_the_agent_input_and_the_names_do_not_change(tmp_path: Path) -> None:
    settings = _settings(_instance(tmp_path, products=False), tmp_path)

    assert agent_products_context(settings) == []
    assert knowledge_product_names(settings.instance_manifest, _knowledge()) == {}
    assert knowledge_product_names(None, _knowledge()) == {}


# ---------------------------------------------- la campana que nombra al producto


def test_a_campaign_of_the_second_product_is_valid_and_names_it(tmp_path: Path) -> None:
    # Sin codigo_oferta: el link sale con la oferta por defecto del producto que nombra
    # el agente (`product`), sin AGENT_CAMPAIGN_OFFER_LINK_ENABLED.
    manifest = InstanceManifest.from_toml_file(_instance(tmp_path) / "instancia.toml")
    draft = _campaign(producto="forever-young", precio="199", moneda="USD")

    blocks, warnings, loaded = instance_blocks(draft, context=_context(manifest, offer_link=False), now=NOW)

    assert blocks == [], blocks
    assert warnings == []
    assert loaded is not None
    assert f"- Producto: {FY_NAME}." in loaded.rendered
    assert "- Precio en esta campaña: USD 199." in loaded.rendered
    assert V9_OFFER not in loaded.rendered


def test_a_campaign_with_another_offer_of_the_second_product_is_valid(tmp_path: Path) -> None:
    manifest = InstanceManifest.from_toml_file(_instance(tmp_path) / "instancia.toml")
    draft = _campaign(producto="forever-young", codigo_oferta=FY_OTHER)

    blocks, warnings, loaded = instance_blocks(draft, context=_context(manifest), now=NOW)

    assert blocks == [], blocks
    assert [warning["code"] for warning in warnings] == ["campaign_offer_not_verified"]
    assert loaded is not None and f"- Producto: {FY_NAME}." in loaded.rendered


def test_the_default_offer_of_the_second_product_is_not_a_campaign_offer(tmp_path: Path) -> None:
    # La regla de E-410 (hallazgo 3) vale por producto: la por defecto ya es la del link.
    manifest = InstanceManifest.from_toml_file(_instance(tmp_path) / "instancia.toml")
    draft = _campaign(producto="forever-young", codigo_oferta=FY_OFFER)

    blocks, _, _ = instance_blocks(draft, context=_context(manifest), now=NOW)

    assert [block["code"] for block in blocks] == ["campaign_offer_is_default"]


def test_a_campaign_of_the_principal_is_named_with_its_offer(tmp_path: Path) -> None:
    manifest = InstanceManifest.from_toml_file(_instance(tmp_path) / "instancia.toml")

    loaded = load_campaign_segment(_campaign(producto="maribel"), version=1, context=_context(manifest))

    assert f"- Producto: {V9_OFFER}." in loaded.rendered


def test_a_campaign_of_an_undeclared_product_is_refused(tmp_path: Path) -> None:
    manifest = InstanceManifest.from_toml_file(_instance(tmp_path) / "instancia.toml")

    blocks, _, loaded = instance_blocks(_campaign(producto="otro"), context=_context(manifest), now=NOW)

    assert loaded is None
    assert [block["code"] for block in blocks] == ["campaign_segment_invalid"]
    assert "no es un producto del conocimiento base (forever-young, maribel)" in blocks[0]["detail"]


def test_without_products_a_campaign_product_is_named_as_the_principal_like_before(tmp_path: Path) -> None:
    # La regla de un solo producto (diseno E-310 §2.1), sin cambios: cualquier clave se
    # renderiza con la oferta del base, y el codigo de oferta no es de ningun producto.
    manifest = InstanceManifest.from_toml_file(_instance(tmp_path, products=False) / "instancia.toml")
    context = _context(manifest)

    loaded = load_campaign_segment(_campaign(producto="forever-young"), version=1, context=context)
    blocks, _, _ = instance_blocks(
        _campaign(producto="forever-young", codigo_oferta=FY_OFFER), context=context, now=NOW
    )

    assert context.product_names == {}
    assert f"- Producto: {V9_OFFER}." in loaded.rendered
    assert "campaign_offer_unknown" in {block["code"] for block in blocks}


def test_the_offer_link_flag_still_gates_the_campaign_offer(tmp_path: Path) -> None:
    manifest = InstanceManifest.from_toml_file(_instance(tmp_path) / "instancia.toml")
    draft = _campaign(producto="forever-young", codigo_oferta=FY_OTHER)

    blocks, _, _ = instance_blocks(draft, context=_context(manifest, offer_link=False), now=NOW)

    assert [block["code"] for block in blocks] == ["campaign_offer_link_disabled"]


def test_replace_keeps_the_rest_of_the_manifest(tmp_path: Path) -> None:
    # La forma de Maribel con productos no toca nada de lo de arriba: mismo binding del
    # principal, mismas ofertas, mismo conocimiento.
    with_products = InstanceManifest.from_toml_file(_instance(tmp_path) / "instancia.toml")
    without = InstanceManifest.from_toml_file(MARIBEL / "instancia.toml")

    assert replace(with_products, products=()) == without
    assert with_products.to_commercial_ally_config() == without.to_commercial_ally_config()
