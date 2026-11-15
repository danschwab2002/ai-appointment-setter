"""El minimo, el esqueleto y el texto libre de un producto nuevo (product-card-v1, E-569, MP-07).

Datos:

* La ficha de Forever Young (``fixtures/agent_products/forever_young_card_sintetica_20261114.json``):
  la sintetica del arnes A-0 en la forma de product-card-v1, con los IDs sinteticos de E-563 y los
  alias de la decision A-0 de Dan (2026-11-14). Ver su ``_capture``.
* La oferta Black de ATT1, capturada del checkout publico (``hotmart_checkout_offer_black_att1_20261110.json``):
  el producto 5071808, el hotlink y la oferta ``wmkq4ba9`` a USD 47. Es una oferta del principal, asi
  que como producto nuevo choca con el principal.
* Los manifiestos de ATT1 (``instances/att1``) y de Maribel (``instances/maribel-fbad25e-20261113``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from bridge import agent_products as ap
from bridge.instance_manifest import InstanceManifest

FIXTURES = Path(__file__).parent / "fixtures"
FY = json.loads((FIXTURES / "agent_products" / "forever_young_card_sintetica_20261114.json").read_text("utf-8"))
MARIBEL = InstanceManifest.from_toml_file(FIXTURES / "instances" / "maribel-fbad25e-20261113" / "instancia.toml")
ATT1 = InstanceManifest.from_toml_file(FIXTURES / "instances" / "att1" / "instancia.toml")
BLACK = json.loads((FIXTURES / "hotmart_checkout_offer_black_att1_20261110.json").read_text("utf-8"))


def _card(**changes: Any) -> dict[str, Any]:
    card = copy.deepcopy(FY["card"])
    card.update(changes)
    return card


def _maribel(stored: list[dict[str, Any]] | None = None) -> ap.ProductContext:
    return ap.ProductContext.from_manifest(MARIBEL, stored or [])


def _codes(parsed: ap.ParsedCard) -> list[str]:
    return [finding["code"] for finding in parsed.blocks]


def _block(parsed: ap.ParsedCard, code: str) -> dict[str, str]:
    found = [finding for finding in parsed.blocks if finding["code"] == code]
    assert found, (code, parsed.blocks)
    return found[0]


def _stored(**changes: Any) -> dict[str, Any]:
    """Una fila de ``taken`` de list_agent_products_v1 (la forma de la migracion 20261115000100)."""
    row = {"product_id": "4b0c3a6e-0000-4000-8000-000000000001", "product_key": "taller-de-recetas",
           "status": "publicada", "nombre": "Taller de recetas", "alias": ["taller", "recetas en vivo"],
           "hotmart_product_id": 9000020, "offer_codes": ["tallerp1"], "incluye_productos": []}
    row.update(changes)
    return row


# ------------------------------------------------------------------ Forever Young: la ficha que valida


def test_forever_young_validates_clean_with_its_aliases_and_no_warnings() -> None:
    parsed = ap.parse_product_card(_card(), context=_maribel())

    assert parsed.ok and parsed.blocks == () and parsed.warnings == ()
    assert parsed.minimum is not None
    assert parsed.minimum["alias"] == ["forever young", "black forever young", "black de ana lucia"]
    assert parsed.minimum["hotmart"] == {"producto_id": 9000019, "hotlink": "X000FOREVERYOUNGPRUEBA"}
    assert parsed.minimum["ofertas"] == [{"codigo": "fyprueba1", "precio": "199", "moneda": "USD",
                                          "por_defecto": True, "otros_montos": [
                                              {"monto": "69", "moneda": "USD",
                                               "que_es": "cada uno de 3 pagos con tarjeta"}]}]
    assert parsed.minimum["relaciones"] == {"incluye_productos": [], "no_se_vende_si_tiene": [],
                                            "venta_cruzada": []}
    # La forma exacta que exige la base (_agent_product_minimum_is_valid): las siete claves, sin texto_libre.
    assert sorted(parsed.minimum) == ["alias", "clave", "hotmart", "no_se_puede_prometer", "nombre", "ofertas",
                                      "relaciones"]
    assert parsed.free_text == FY["card"]["texto_libre"]


def test_the_render_is_the_header_the_promises_and_the_free_text_as_is() -> None:
    parsed = ap.parse_product_card(_card(), context=_maribel())

    assert parsed.render is not None and parsed.render_bytes is not None
    assert parsed.render.startswith(
        "# Producto: Black Forever Young\n\n"
        "- Oferta por defecto: USD 199 (precio de lista).\n"
        "  - Otro monto: USD 69 (cada uno de 3 pagos con tarjeta).\n\n"
        "No se puede prometer:\n\n- un resultado físico o de edad\n")
    assert parsed.render.endswith(FY["card"]["texto_libre"] + "\n")
    assert parsed.render_bytes <= ap.MAX_RENDER_BYTES
    # Ni el hotlink, ni el codigo de oferta, ni los alias, ni las relaciones llegan al agente.
    for hidden in ("X000FOREVERYOUNGPRUEBA", "fyprueba1", "9000019", "black forever young", "venta_cruzada"):
        assert hidden not in parsed.render
    # Determinista: misma ficha, misma huella.
    assert ap.parse_product_card(_card(), context=_maribel()).render_sha256 == parsed.render_sha256


def test_the_price_in_the_free_text_has_to_be_declared() -> None:
    text = FY["card"]["texto_libre"].replace("USD 199,", "USD 149,")
    parsed = ap.parse_product_card(_card(texto_libre=text), context=_maribel())

    assert not parsed.ok and parsed.minimum is None
    assert _block(parsed, "free_text_amount_not_declared") == {
        "code": "free_text_amount_not_declared", "field": "texto_libre", "detail": "149 USD"}


def test_a_declared_amount_in_another_currency_does_not_count() -> None:
    text = FY["card"]["texto_libre"] + "\n\nEn México, MXN 199."
    parsed = ap.parse_product_card(_card(texto_libre=text), context=_maribel())

    assert _block(parsed, "free_text_amount_not_declared")["detail"] == "199 MXN"


def test_numbers_without_a_currency_are_not_amounts() -> None:
    text = FY["card"]["texto_libre"] + "\n\nSon 8 programas, 30 días de acceso y 10 % de descuento el 19 de noviembre."
    assert ap.parse_product_card(_card(texto_libre=text), context=_maribel()).ok


def test_without_garantia_it_validates_with_a_warning() -> None:
    text = FY["card"]["texto_libre"].replace("## Garantía\n\n7 días desde la compra.\n\n", "")
    parsed = ap.parse_product_card(_card(texto_libre=text), context=_maribel())

    assert parsed.ok and parsed.minimum is not None
    assert parsed.warnings == ({"code": "skeleton_section_missing", "field": "texto_libre", "detail": "garantia"},)


def test_the_skeleton_compares_without_case_or_accents_and_names_every_missing_section() -> None:
    text = FY["card"]["texto_libre"].replace("## Qué es", "## QUE ES")
    assert ap.parse_product_card(_card(texto_libre=text), context=_maribel()).warnings == ()

    bare = ap.parse_product_card(_card(texto_libre="Un texto sin secciones."), context=_maribel())
    assert bare.ok
    assert [finding["detail"] for finding in bare.warnings] == [slug for _, slug in ap.SKELETON]


# ------------------------------------------------------------------ los alias (A-0, 14/11)


@pytest.mark.parametrize(("aliases", "code"), [
    (None, "aliases_missing"),
    ([], "aliases_missing"),
    (["f"], "alias_invalid"),
    (["  "], "alias_invalid"),
    ([7], "alias_invalid"),
    (["linea\notra"], "alias_invalid"),
    (["x" * 61], "alias_invalid"),
    (["fy"], "alias_invalid"),
    (["123"], "alias_invalid"),
    (["principal"], "alias_invalid"),
    (["Ninguno"], "alias_invalid"),
    ("forever young", "alias_invalid"),
    (["llamame al 55 1234 5678"], "free_text_personal_data"),
    (["ventas@ejemplo.com"], "free_text_personal_data"),
    (["xoxb-" + "1" * 12], "free_text_secret"),
    ([f"alias {n}" for n in range(13)], "alias_invalid"),
    (["Forever Young", "forever-young"], "alias_duplicate"),
    (["forever", "FÓREVER"], "alias_duplicate"),
    (["Black de Ana Lucía", "black-de-ana-lucia"], "alias_duplicate"),
])
def test_the_aliases_are_a_short_list_without_normalized_duplicates(aliases: Any, code: str) -> None:
    card = _card()
    if aliases is None:
        del card["alias"]
    else:
        card["alias"] = aliases
    assert code in _codes(ap.parse_product_card(card, context=_maribel()))


def test_an_alias_cannot_be_the_name_of_the_principal() -> None:
    parsed = ap.parse_product_card(_card(alias=["forever young", "el protocolo biohacking"]), context=_maribel())

    assert _block(parsed, "alias_taken") == {"code": "alias_taken", "field": "alias[1]"}


def test_an_alias_cannot_be_the_name_key_or_alias_of_another_product_of_the_base() -> None:
    stored = [_stored()]
    for aliases, where in ((["forever young", "Recetas en vivo"], "alias[1]"), (["Taller-de-Recetas"], "alias[0]"),
                           (["TALLER"], "alias[0]")):
        parsed = ap.parse_product_card(_card(alias=aliases), context=_maribel(stored))
        assert _block(parsed, "alias_taken")["field"] == where
    parsed = ap.parse_product_card(_card(nombre="Taller de recetas"), context=_maribel(stored))
    assert _block(parsed, "alias_taken")["field"] == "nombre"


def test_repeating_its_own_name_or_key_as_an_alias_is_not_an_error() -> None:
    # Contrato §2.1: el nombre y la clave son alias implicitos; repetirlos en la lista se cuenta una vez.
    assert ap.parse_product_card(_card(alias=["Black Forever Young", "forever-young"]), context=_maribel()).ok


def test_containing_another_alias_is_not_a_collision() -> None:
    # §2.1: se compara por igualdad exacta del texto normalizado; «protocolo black» no choca con «protocolo».
    stored = [_stored(alias=["protocolo"])]
    assert ap.parse_product_card(_card(alias=["protocolo black"]), context=_maribel(stored)).ok


def test_the_aliases_of_another_version_of_the_same_product_do_not_collide() -> None:
    stored = [_stored(product_key="forever-young", nombre="Black Forever Young", alias=["black de ana lucia"],
                      hotmart_product_id=9000019, offer_codes=["fyprueba1"])]
    assert ap.parse_product_card(_card(), context=_maribel(stored)).ok


# ------------------------------------------------------------------ lo que ya usa la instancia


def test_the_black_offer_of_att1_as_a_new_product_collides_with_the_principal() -> None:
    measured = BLACK["medido"][0]
    query = parse_qs(urlsplit(measured["url"]).query)
    hotlink = urlsplit(measured["url"]).path.strip("/")
    card = _card(clave="black-november", nombre="Black November", alias=["black november"],
                 hotmart={"producto_id": measured["product_id"], "hotlink": hotlink},
                 ofertas=[{"codigo": query["off"][0], "precio": str(measured["precio_usd"]), "moneda": "USD",
                           "por_defecto": True},
                          {"codigo": BLACK["oferta_por_defecto"], "precio": "47", "moneda": "USD",
                           "por_defecto": False}],
                 texto_libre="## Precio y cómo se paga\n\nUSD 47, con el Bono Black de regalo.")
    parsed = ap.parse_product_card(card, context=ap.ProductContext.from_manifest(ATT1))

    assert "hotmart_product_taken" in _codes(parsed)
    # gopi6lh7 es la oferta por defecto del principal (instances/att1/instancia.toml).
    assert {"code": "offer_code_taken", "field": "ofertas[1].codigo"} in parsed.blocks
    assert parsed.minimum is None


@pytest.mark.parametrize("key", ["principal", "todos", "ninguno", "maribel"])
def test_a_reserved_key_or_the_funnel_of_the_principal_is_taken(key: str) -> None:
    assert _block(ap.parse_product_card(_card(clave=key), context=_maribel()), "product_key_taken") == {
        "code": "product_key_taken", "field": "clave"}


@pytest.mark.parametrize("key", ["Forever", "forever_young", "-fy", "fy-", "a--b", "x" * 41, 7, None])
def test_an_invalid_key(key: Any) -> None:
    assert "product_key_invalid" in _codes(ap.parse_product_card(_card(clave=key), context=_maribel()))


def test_the_hotmart_id_and_the_offers_of_another_product_of_the_base_are_taken() -> None:
    stored = [_stored(hotmart_product_id=9000019, offer_codes=["fyprueba1"])]
    parsed = ap.parse_product_card(_card(), context=_maribel(stored))

    assert {"code": "hotmart_product_taken", "field": "hotmart.producto_id"} in parsed.blocks
    assert {"code": "offer_code_taken", "field": "ofertas[0].codigo"} in parsed.blocks


@pytest.mark.parametrize(("hotmart", "code"), [
    (None, "hotmart_product_invalid"),
    ({"producto_id": "9000019", "hotlink": "X000FOREVERYOUNGPRUEBA"}, "hotmart_product_invalid"),
    ({"producto_id": 0, "hotlink": "X000FOREVERYOUNGPRUEBA"}, "hotmart_product_invalid"),
    ({"producto_id": True, "hotlink": "X000FOREVERYOUNGPRUEBA"}, "hotmart_product_invalid"),
    ({"producto_id": 9000019, "hotlink": "pay.hotmart.com/X0"}, "hotlink_invalid"),
    ({"producto_id": 9000019, "hotlink": "abc"}, "hotlink_invalid"),
])
def test_the_hotmart_block(hotmart: Any, code: str) -> None:
    card = _card(hotmart=hotmart)
    if hotmart is None:
        del card["hotmart"]
    assert code in _codes(ap.parse_product_card(card, context=_maribel()))


# ------------------------------------------------------------------ las ofertas


def _offer(**changes: Any) -> dict[str, Any]:
    offer = copy.deepcopy(FY["card"]["ofertas"][0])
    offer.update(changes)
    return offer


@pytest.mark.parametrize(("offers", "code"), [
    (None, "offers_missing"),
    ([], "offers_missing"),
    ([_offer(por_defecto=False)], "default_offer_count"),
    ([_offer(), _offer(codigo="fyprueba2")], "default_offer_count"),
    ([_offer(), _offer(por_defecto=False)], "offer_code_taken"),
    ([_offer(codigo="ab")], "offer_code_invalid"),
    ([_offer(precio="0")], "offer_price_invalid"),
    ([_offer(precio=199)], "offer_price_invalid"),
    ([_offer(precio="1.397")], "offer_price_invalid"),
    ([_offer(precio="199.999")], "offer_price_invalid"),
    ([_offer(moneda="usd")], "offer_currency_invalid"),
    ([_offer(moneda="GBP")], "offer_currency_invalid"),
    ([_offer(otros_montos=[{"monto": "69", "moneda": "USD", "que_es": ""}])], "other_amount_invalid"),
    ([_offer(otros_montos=[{"monto": "-1", "moneda": "USD", "que_es": "cuota"}])], "other_amount_invalid"),
    ([_offer(otros_montos=[{"monto": "69", "moneda": "XYZ", "que_es": "cuota"}])], "other_amount_invalid"),
])
def test_the_offers(offers: Any, code: str) -> None:
    card = _card(ofertas=offers)
    if offers is None:
        del card["ofertas"]
    assert code in _codes(ap.parse_product_card(card, context=_maribel()))


def test_a_price_is_normalized_and_its_other_amounts_are_allowed_in_the_text() -> None:
    offer = _offer(precio="1397.00", moneda="MXN",
                   otros_montos=[{"monto": "1204.31", "moneda": "MXN", "que_es": "total con el cupón"}])
    text = "## Precio y cómo se paga\n\nMXN 1.397, o MXN 1.204,31 con el cupón. En pesos: $1,397."
    parsed = ap.parse_product_card(_card(ofertas=[offer], texto_libre=text), context=_maribel())

    assert parsed.ok, parsed.blocks
    assert parsed.minimum is not None and parsed.minimum["ofertas"][0]["precio"] == "1397"
    assert "- Oferta por defecto: MXN 1.397 (precio de lista)." in (parsed.render or "")


# ------------------------------------------------------------------ lo que no se puede prometer


@pytest.mark.parametrize(("promises", "code"), [
    (None, "promises_missing"),
    ([], "promises_missing"),
    ([""], "promise_invalid"),
    (["x" * 201], "promise_invalid"),
    (["una\nlinea"], "promise_invalid"),
    ([f"promesa {n}" for n in range(21)], "promise_invalid"),
])
def test_the_promises(promises: Any, code: str) -> None:
    card = _card(no_se_puede_prometer=promises)
    if promises is None:
        del card["no_se_puede_prometer"]
    assert code in _codes(ap.parse_product_card(card, context=_maribel()))


# ------------------------------------------------------------------ el texto libre


@pytest.mark.parametrize(("addition", "code"), [
    ("Escribinos a ventas@ejemplo.com.", "free_text_personal_data"),
    ("Llamá al +52 55 1234 5678.", "free_text_personal_data"),
    ("La clave es sk-" + "a" * 24 + ".", "free_text_secret"),
    ("Compralo en pay.hotmart.com/X000FOREVERYOUNGPRUEBA", "free_text_payment_url"),
    ("El link lleva ?off=fyprueba1 al final.", "free_text_payment_url"),
])
def test_what_cannot_go_in_the_free_text(addition: str, code: str) -> None:
    parsed = ap.parse_product_card(_card(texto_libre=FY["card"]["texto_libre"] + "\n\n" + addition),
                                   context=_maribel())
    assert code in _codes(parsed)


def test_thousands_are_not_a_phone() -> None:
    offer = _offer(precio="1397000", moneda="COP", otros_montos=[])
    text = "## Precio y cómo se paga\n\nCOP 1.397.000 en Colombia."
    assert ap.parse_product_card(_card(ofertas=[offer], texto_libre=text), context=_maribel()).ok


def _filled(size: int) -> str:
    """Un texto libre de exactamente ``size`` bytes, en palabras (una tira de letras parece una clave)."""
    text = "## Qué es\n\n"
    while len(text.encode("utf-8")) + 6 <= size:
        text += "texto "
    return text + "x" * (size - len(text.encode("utf-8")))


def test_the_free_text_size_and_emptiness() -> None:
    assert "free_text_empty" in _codes(ap.parse_product_card(_card(texto_libre="  "), context=_maribel()))
    assert "free_text_empty" in _codes(ap.parse_product_card(_card(texto_libre=None), context=_maribel()))
    big = _filled(ap.MAX_FREE_TEXT_BYTES + 1)
    assert _block(ap.parse_product_card(_card(texto_libre=big), context=_maribel()),
                  "free_text_too_long")["detail"] == str(ap.MAX_FREE_TEXT_BYTES + 1)
    assert "free_text_control_characters" in _codes(
        ap.parse_product_card(_card(texto_libre="## Qué es\n\u0007"), context=_maribel()))


def test_a_free_text_inside_its_cap_can_still_make_the_render_too_long() -> None:
    text = _filled(ap.MAX_FREE_TEXT_BYTES)
    # La cabecera de Forever Young pesa unos 300 bytes: con diez promesas largas pasa de los 500 que quedan.
    promises = [f"promesa larga numero {n} " + "que no se puede hacer " * 6 for n in range(10)]
    parsed = ap.parse_product_card(_card(texto_libre=text, no_se_puede_prometer=promises), context=_maribel())

    assert len(text.encode("utf-8")) <= ap.MAX_FREE_TEXT_BYTES
    assert _codes(parsed) == ["render_too_long"]
    assert int(_block(parsed, "render_too_long")["detail"]) > ap.MAX_RENDER_BYTES


# ------------------------------------------------------------------ las relaciones (D7)


def test_the_relations_point_to_products_that_exist() -> None:
    stored = [_stored()]
    ok = ap.parse_product_card(_card(relaciones={"incluye_productos": ["principal", "taller-de-recetas"],
                                                 "venta_cruzada": [{"producto": "principal",
                                                                    "condicion": "solo si ya es alumna"}]}),
                               context=_maribel(stored))
    assert ok.ok and ok.minimum is not None
    assert ok.minimum["relaciones"] == {"incluye_productos": ["principal", "taller-de-recetas"],
                                        "no_se_vende_si_tiene": [],
                                        "venta_cruzada": [{"producto": "principal",
                                                           "condicion": "solo si ya es alumna"}]}
    assert "venta_cruzada" not in (ok.render or "") and "solo si ya es alumna" not in (ok.render or "")

    for relations in ({"incluye_productos": ["no-existe"]}, {"no_se_vende_si_tiene": ["forever-young"]},
                      {"venta_cruzada": [{"producto": "no-existe"}]}):
        assert "relation_unknown_product" in _codes(
            ap.parse_product_card(_card(relaciones=relations), context=_maribel(stored)))


def test_a_pack_cannot_include_itself_through_another_product() -> None:
    stored = [_stored(incluye_productos=["forever-young"])]
    parsed = ap.parse_product_card(_card(relaciones={"incluye_productos": ["taller-de-recetas"]}),
                                   context=_maribel(stored))
    assert _codes(parsed) == ["relation_cycle"]


# ------------------------------------------------------------------ lo que no es una ficha


@pytest.mark.parametrize("raw", [None, [], "texto", 7])
def test_what_is_not_an_object_is_refused(raw: Any) -> None:
    with pytest.raises(ap.ProductCardInvalid) as caught:
        ap.parse_product_card(raw, context=_maribel())
    assert caught.value.code == "product_card_invalid"


@pytest.mark.parametrize("where", ["top", "hotmart", "oferta", "relaciones"])
def test_unknown_keys_are_refused_by_name(where: str) -> None:
    card = _card()
    if where == "top":
        card["precio"] = "199"
    elif where == "hotmart":
        card["hotmart"]["checkout"] = "x"
    elif where == "oferta":
        card["ofertas"][0]["cupon"] = "x"
    else:
        card["relaciones"]["otra"] = []
    with pytest.raises(ap.ProductCardInvalid) as caught:
        ap.parse_product_card(card, context=_maribel())
    assert caught.value.reason["problem"] == "unknown_keys"
    assert "x" not in json.dumps(caught.value.reason) or where == "top"


def test_every_block_comes_back_at_once() -> None:
    card = _card(clave="Mal", alias=[], ofertas=[], no_se_puede_prometer=[], texto_libre="")
    assert sorted(_codes(ap.parse_product_card(card, context=_maribel()))) == [
        "aliases_missing", "free_text_empty", "offers_missing", "product_key_invalid", "promises_missing"]


def test_the_context_from_a_manifest_with_products() -> None:
    context = ap.ProductContext.from_manifest(ATT1)
    principal = context.products[0]
    assert principal.origin == "principal" and principal.hotmart_product_id == ATT1.hotmart_product_id
    assert "gopi6lh7" in principal.offer_codes
    assert ATT1.funnel_ref in context.reserved_keys


# ------------------------------------------------------------------ lo que salio de la revision (E-569)


@pytest.mark.parametrize("text", [
    "El en vivo: 2026-11-19 20:00 hs.", "Ediciones 2024-2025-2026", "El 19.11.2026 a las 11:00.",
    "Seguila en instagram.com/@ana.lucia",
])
def test_dates_hours_and_handles_are_not_personal_data(text: str) -> None:
    parsed = ap.parse_product_card(_card(texto_libre=FY["card"]["texto_libre"] + "\n\n" + text), context=_maribel())
    assert parsed.ok, parsed.blocks


def test_amounts_with_thousands_are_not_personal_data_outside_the_free_text() -> None:
    offer = _offer(precio="1200000", moneda="COP",
                   otros_montos=[{"monto": "1397000", "moneda": "COP", "que_es": "COP 1.397.000 tachado"}])
    card = _card(nombre="Taller 19.11.2026", ofertas=[offer],
                 no_se_puede_prometer=["descuentos sobre COP 1.200.000"],
                 texto_libre="## Precio y cómo se paga\n\nCOP 1.200.000.")
    assert ap.parse_product_card(card, context=_maribel()).ok


def test_the_own_key_is_an_implicit_alias_too() -> None:
    stored = [_stored(alias=["black de ana lucia"])]
    parsed = ap.parse_product_card(_card(clave="black-de-ana-lucia", alias=["forever young"]),
                                   context=_maribel(stored))
    assert {"code": "alias_taken", "field": "clave"} in parsed.blocks


def test_aliases_that_are_not_a_list_say_so() -> None:
    assert _block(ap.parse_product_card(_card(alias="fy fy"), context=_maribel()), "alias_invalid")["detail"] == (
        "no_es_lista")


def test_the_minimum_always_comes_out_with_the_full_shape_the_base_asks_for() -> None:
    card = _card(ofertas=[{"codigo": "fyprueba1", "precio": "199.00", "moneda": "USD", "por_defecto": True}],
                 texto_libre="## Precio y cómo se paga\n\nUSD 199.")
    del card["relaciones"]
    parsed = ap.parse_product_card(card, context=_maribel())
    assert parsed.minimum is not None
    assert parsed.minimum["ofertas"] == [{"codigo": "fyprueba1", "precio": "199", "moneda": "USD",
                                          "por_defecto": True, "otros_montos": []}]
    assert parsed.minimum["relaciones"] == {"incluye_productos": [], "no_se_vende_si_tiene": [], "venta_cruzada": []}
