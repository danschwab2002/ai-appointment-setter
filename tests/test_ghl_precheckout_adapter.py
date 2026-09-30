"""Traductor puro del webhook de formulario de GHL a lead.precheckout 1.1.0.

Contrato: docs/contracts/ghl-precheckout-adapter-v1.md.

Fixtures: tests/fixtures/ghl/, las dos capturas del 2026-09-29 del receptor
ghl-capture-att1 (anonimizadas, con fecha y origen en su `_capture`). Son los unicos
payloads reales de GHL: toda variante de esta suite se DERIVA de ellos (cambiar
mediumId, quitar un campo, mover un objeto de atribucion real, repetir una clave).
Ninguna esta escrita de cero. Goldens: tests/fixtures/ghl/expected/.

Vectores del sck: tests/fixtures/lancemos_core_sck_*.json, capturados del core.
"""

from __future__ import annotations

import copy
import json
import re
import tomllib
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import phonenumbers
import pytest

from bridge.checkout_issuance import generate_issuance_ulid
from bridge.commercial_ally import OfferLanding
from bridge.ghl_precheckout_adapter import (
    GHL_SETTER_TOKEN_FIELD,
    GhlAdapterRejection,
    compose_lancemos_sck,
    ghl_body_token,
    parse_ghl_body,
    translate_ghl_form_submission,
)
from bridge.instance_manifest import InstanceManifest
from bridge.lead_precheckout import parse_lead_precheckout

FIXTURES = Path(__file__).parent / "fixtures"
GHL = FIXTURES / "ghl"
EXPECTED = GHL / "expected"
ATT1_TOML = FIXTURES / "instances" / "att1" / "instancia.toml"

ADS_A_FORM = "EgDqRl2xWc59YjVW1q8W"
LANDING_D_FORM = "Om5FpIg5Sr5ce7nSkuPy"
NOW = datetime(2026, 9, 29, 16, 0, tzinfo=UTC)
ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
LANDING_D_SCK = (
    "meta-ads~ig~drn_att_pay_evg_capt_Ad155_sept26~Mx-Tiroides-con-Hambre"
    "~30-SR-ATT-Test-Ads-MX-Ciudades-2809~120250442190930484"
)


def _manifest(forms: list[str]) -> InstanceManifest:
    # El fixture de ATT1 es copia de la instancia y todavia no tiene el adaptador:
    # la seccion se arma por mutacion, como en test_instance_manifest.
    payload = tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))
    payload["eventos"] = [*payload["eventos"], "intencion"]
    payload["adaptadores"] = {"ghl": {"formularios": forms}}
    return InstanceManifest.from_mapping(payload)


# ATT1 tal como arranca (D4, confirmado por E14: Om5F todavia no entra) y la lista
# con los dos formularios medidos, para cubrir la landing -d.
ATT1_ONLY_EGDQ = _manifest([ADS_A_FORM])
ATT1_BOTH_FORMS = _manifest([ADS_A_FORM, LANDING_D_FORM])
CONFIG = ATT1_BOTH_FORMS.to_commercial_ally_config()


def _capture(name: str) -> dict:
    return json.loads((GHL / name).read_text(encoding="utf-8"))


def _ads_a() -> dict:
    return copy.deepcopy(_capture("ghl_form_webhook_att1_ads_a_20260929.json")["payload"])


def _editor() -> dict:
    return copy.deepcopy(
        _capture("ghl_form_webhook_editor_test_landing_d_20260929.json")["payload"]
    )


def _landing_d(from_key: str = "lastAttributionSource") -> dict:
    """La prueba del editor con un objeto de atribucion real del mismo contacto
    movido al primer nivel: la forma de un envio real del formulario Om5F."""

    body = _editor()
    body["attributionSource"] = copy.deepcopy(body["contact"][from_key])
    return body


def _golden(name: str) -> dict:
    return json.loads((EXPECTED / name).read_text(encoding="utf-8"))


def _translate(
    body: dict,
    *,
    manifest: InstanceManifest = ATT1_BOTH_FORMS,
    now: datetime = NOW,
    config=None,
):
    return translate_ghl_form_submission(
        body,
        config=config or manifest.to_commercial_ally_config(),
        allowed_forms=frozenset(manifest.ghl_form_ids),
        now=now,
    )


def _rejection(body: dict, **kwargs) -> GhlAdapterRejection:
    with pytest.raises(GhlAdapterRejection) as caught:
        _translate(body, **kwargs)
    return caught.value


def _without_id(event: dict) -> dict:
    return {key: value for key, value in event.items() if key != "id"}


def _assert_matches_golden(event: dict, golden_name: str) -> None:
    golden = _golden(golden_name)
    golden_id = golden["raw_payload"]["id"]
    # Salvo el id aleatorio, el evento es el golden, clave por clave.
    assert {**event, "id": golden_id} == golden["raw_payload"]
    submission = parse_lead_precheckout(event, config=CONFIG)
    assert submission is not None
    assert {
        **submission.as_canonical_payload(),
        "external_submission_id": golden_id,
    } == golden["canonical_payload"]
    # El golden, tal cual, es lo que el parser acepta (C7 lo admite en PGlite).
    golden_submission = parse_lead_precheckout(golden["raw_payload"], config=CONFIG)
    assert golden_submission is not None
    assert golden_submission.as_canonical_payload() == golden["canonical_payload"]


# ------------------------------------------------------------- envio real ads-a


def test_ads_a_real_submission_translates_to_the_ads_a_offer() -> None:
    translation = _translate(_ads_a(), manifest=ATT1_ONLY_EGDQ)
    event = translation.event

    assert (translation.site, translation.landing_id, translation.offer_code) == (
        "metodoraizana",
        "ads-a",
        "gopi6lh7",
    )
    assert translation.form_id == ADS_A_FORM
    assert event["version"] == "1.1.0" and event["event"] == "lead.precheckout"
    assert event["created_at"] == "2026-09-29T16:00:00.000Z"
    # ?test=yes no pasa: page_url es la de la oferta del manifiesto.
    assert event["source"]["page_url"] == "https://www.metodoraizana.com/att1/evg/vsl/ads-a"
    assert event["source"]["system"] == "landing"
    assert event["source"]["aliado"] == "Dra. Nina Garza"
    # Trafico directo: sin UTM, sin sck, sin fbclid, sin referrer.
    assert event["data"]["attribution"] == {
        "utm_source": "",
        "utm_medium": "",
        "utm_campaign": "",
        "utm_content": "",
        "utm_term": "",
        "sck": "",
        "fbclid": "",
        "referrer": "",
    }
    assert translation.has_utm is False and translation.has_fbclid is False
    # AR sin el 9: se manda como lo guardo GHL (D8), valido de linea fija.
    assert event["data"]["buyer"]["phone"] == "+542944440101"
    assert event["data"]["buyer"]["phone_country_code"] == "54"
    assert event["data"]["buyer"]["phone_national"] == "2944440101"
    assert event["data"]["checkout_country"] == {"iso": "AR", "source": "phone_country_code"}
    assert translation.phone_region == "AR"
    assert event["data"]["checkout_url"] == "https://pay.hotmart.com/D98014973Y?off=gopi6lh7"
    assert event["data"]["consent"] == {
        "marketing_optin": True,
        "whatsapp_contact": True,
        "copy_version": "att1-whatsapp-contact-v1",
    }
    assert event["dedupe_key"] == "metodoraizana:gopi6lh7:lead.anonimo.1@example.com"

    submission = parse_lead_precheckout(event, config=ATT1_ONLY_EGDQ.to_commercial_ally_config())
    assert submission is not None
    assert submission.phone_valid is True
    assert submission.whatsapp_contact_authorized is True
    assert submission.normalized_phone == "542944440101"
    _assert_matches_golden(event, "ghl_form_webhook_att1_ads_a_20260929.lead_precheckout.json")


def test_the_id_is_a_new_random_ulid_on_every_translation() -> None:
    # E1: dos entregas del mismo envio (un reintento de GHL) son dos submissions con
    # ids distintos; nunca el mismo id con otro created_at, que dejaria un conflicto
    # que envenena el consentimiento para siempre.
    first = _translate(_ads_a()).event
    second = _translate(_ads_a()).event

    assert first["id"] != second["id"]
    for event in (first, second):
        assert isinstance(event["id"], str) and len(event["id"]) == 26
        assert ULID.fullmatch(event["id"])
        assert parse_lead_precheckout(event, config=CONFIG) is not None
    assert _without_id(first) == _without_id(second)


def test_the_id_time_and_created_at_come_from_the_bridge_clock() -> None:
    later = NOW + timedelta(minutes=7, milliseconds=250)
    event = _translate(_ads_a(), now=later).event
    other_zone = _translate(
        _ads_a(), now=later.astimezone(timezone(timedelta(hours=-3)))
    ).event
    later_ms = (later - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)

    assert event["created_at"] == "2026-09-29T16:07:00.250Z"
    assert other_zone["created_at"] == "2026-09-29T16:07:00.250Z"
    # El tiempo del ULID es el de la traduccion, no date_created (el alta del contacto).
    assert event["id"][:10] == generate_issuance_ulid(timestamp_ms=later_ms)[:10]


def test_a_naive_clock_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _translate(_ads_a(), now=datetime(2026, 9, 29, 16, 0))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["attributionSource"].__setitem__(
            "fbEventId", body["contact"]["attributionSource"]["fbEventId"]
        ),
        lambda body: body["attributionSource"].pop("fbEventId"),
        lambda body: body.__setitem__("date_created", "2026-09-30T09:00:00.000Z"),
        lambda body: body["location"].__setitem__("id", "anonymizedLocation02"),
    ],
    ids=[
        "fbEventId-del-primer-toque",
        "sin-fbEventId",
        "otro-date_created",
        "otra-location",
    ],
)
def test_fb_event_id_date_created_and_location_do_not_touch_the_event(mutate) -> None:
    body = _ads_a()
    mutate(body)

    assert _without_id(_translate(body).event) == _without_id(_translate(_ads_a()).event)


# ------------------------------------------------- la landing -d (prueba del editor)


def test_the_editor_test_is_not_a_form_submission() -> None:
    # attributionSource de primer nivel {}: no hubo disparo de formulario (D3). No se
    # cae al ultimo toque.
    rejection = _rejection(_editor())

    assert (rejection.reason, rejection.status_code) == ("not_a_form_submission", 422)
    assert str(rejection) == "not_a_form_submission"


def test_landing_d_submission_translates_with_the_composed_sck() -> None:
    translation = _translate(_landing_d())
    event = translation.event
    attribution = event["data"]["attribution"]
    url = _editor()["contact"]["lastAttributionSource"]["url"]

    assert (translation.site, translation.landing_id, translation.offer_code) == (
        "metodoraizana-mx",
        "alimenta-tu-tiroides-d",
        "2uafw5bg",
    )
    assert translation.form_id == LANDING_D_FORM
    assert event["source"]["page_url"] == "https://site.metodoraizana.com.mx/alimenta-tu-tiroides-d"
    assert attribution["sck"] == LANDING_D_SCK
    assert len(attribution["sck"]) == 123
    # La UTM sale decodificada de la URL, no del campo estructurado de GHL (que en el
    # primer toque la guarda en minusculas).
    assert attribution["utm_campaign"] == "30-SR-ATT-Test Ads-MX-Ciudades-28/09"
    assert attribution["utm_medium"] == "Mx-Tiroides con Hambre"
    assert attribution["utm_source"] == "meta-ads"
    assert attribution["utm_content"] == "drn_att_pay_evg_capt_Ad155_sept26"
    assert attribution["utm_term"] == "ig"
    assert len(attribution["fbclid"]) == 165
    assert f"fbclid={attribution['fbclid']}" in url
    assert attribution["referrer"] == "https://instagram.com"
    # country dice US: el pais sale del telefono.
    assert event["data"]["checkout_country"] == {"iso": "MX", "source": "phone_country_code"}
    assert event["data"]["buyer"]["phone"] == "+524755550102"
    assert translation.has_utm is True and translation.has_fbclid is True

    submission = parse_lead_precheckout(event, config=CONFIG)
    assert submission is not None and submission.whatsapp_contact_authorized is True
    _assert_matches_golden(
        event, "ghl_form_webhook_landing_d_derived_20260929.lead_precheckout.json"
    )


def test_the_first_touch_url_with_the_encoded_slash_composes_the_same_sck() -> None:
    # contact.attributionSource trae la misma campana con 28%2F09.
    first_touch = _translate(_landing_d("attributionSource")).event
    last_touch = _translate(_landing_d()).event

    assert "28%2F09" in _editor()["contact"]["attributionSource"]["url"]
    assert first_touch["data"]["attribution"] == last_touch["data"]["attribution"]
    assert first_touch["data"]["attribution"]["sck"] == LANDING_D_SCK


def test_landing_d_is_not_allowed_with_the_att1_form_list() -> None:
    rejection = _rejection(_landing_d(), manifest=ATT1_ONLY_EGDQ)

    assert (rejection.reason, rejection.status_code) == ("form_not_allowed", 422)


def test_structured_utm_fields_are_never_read() -> None:
    # El objeto real de -d (con utmSource, campaign, etc. llenos) en la URL de ads-a
    # sin query: sin query no hay UTM, aunque GHL tenga los campos estructurados.
    body = _ads_a()
    moved = copy.deepcopy(_editor()["contact"]["lastAttributionSource"])
    moved["url"] = "https://www.metodoraizana.com/att1/evg/vsl/ads-a"
    moved["mediumId"] = ADS_A_FORM
    moved.pop("fbclid")
    moved["referrer"] = None
    body["attributionSource"] = moved

    attribution = _translate(body).event["data"]["attribution"]

    assert moved["utmSource"] == "meta-ads" and moved["campaign"]
    assert set(attribution.values()) == {""}


def test_fbclid_falls_back_to_the_submission_object() -> None:
    body = _landing_d()
    url = body["attributionSource"]["url"]
    body["attributionSource"]["url"] = re.sub(r"&fbclid=[^&]*", "", url)

    translation = _translate(body)

    assert "fbclid" not in body["attributionSource"]["url"]
    assert translation.event["data"]["attribution"]["fbclid"] == body["attributionSource"]["fbclid"]
    assert translation.has_fbclid is True


def _url_fbclid(body: dict) -> str:
    (value,) = re.findall(r"[?&]fbclid=([^&]*)", body["attributionSource"]["url"])
    return value


def test_the_fbclid_is_read_from_the_url_query() -> None:
    # Sin fbclid en el objeto: el de 165 caracteres de la URL llega igual al evento.
    body = _landing_d()
    body["attributionSource"].pop("fbclid")

    translation = _translate(body)

    assert len(_url_fbclid(body)) == 165
    assert translation.event["data"]["attribution"]["fbclid"] == _url_fbclid(body)
    assert translation.has_fbclid is True


def test_the_fbclid_of_the_url_query_wins_over_the_object() -> None:
    # En la captura los dos son iguales (medido); con el del objeto distinto, gana la
    # URL, que es la que ve la landing.
    body = _landing_d()
    url_fbclid = _url_fbclid(body)
    assert body["attributionSource"]["fbclid"] == url_fbclid
    body["attributionSource"]["fbclid"] = url_fbclid[::-1]

    attribution = _translate(body).event["data"]["attribution"]

    assert attribution["fbclid"] == url_fbclid


# -------------------------------------------------------------- landing por URL


def _ads_a_with_url(url: str) -> dict:
    body = _ads_a()
    body["attributionSource"]["url"] = url
    return body


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.metodoraizana.com/att1/evg/vsl/org-a", ("org-a", "bmaztyhg")),
        ("https://www.metodoraizana.com/att1/evg/vsl/ads-a/?test=yes", ("ads-a", "gopi6lh7")),
        ("https://www.metodoraizana.com/att1/evg/vsl/ads-a#form", ("ads-a", "gopi6lh7")),
        ("https://WWW.MetodoRaizana.com/att1/evg/vsl/ads-a?test=yes", ("ads-a", "gopi6lh7")),
        (
            "https://site.metodoraizana.com.mx/alimenta-tu-tiroides-d/",
            ("alimenta-tu-tiroides-d", "2uafw5bg"),
        ),
    ],
    ids=[
        "org-a",
        "barra-final",
        "fragmento",
        "host-en-mayusculas",
        "barra-final-en-d",
    ],
)
def test_the_landing_resolves_from_host_and_path(url: str, expected: tuple[str, str]) -> None:
    translation = _translate(_ads_a_with_url(url))

    assert (translation.landing_id, translation.offer_code) == expected
    assert "?" not in translation.event["source"]["page_url"]
    assert parse_lead_precheckout(translation.event, config=CONFIG) is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://metodoraizana.com/att1/evg/vsl/ads-a",
        "https://www.metodoraizana.com/att1/evg/vsl/ads-b",
        "https://www.metodoraizana.com/att1/evg/vsl/ADS-A",
        "https://www.metodoraizana.com/att1/evg/vsl/ads-a//",
        "https://www.metodoraizana.com:8443/att1/evg/vsl/ads-a",
        "https://www.metodoraizana.com.evil.test/att1/evg/vsl/ads-a",
        "www.metodoraizana.com/att1/evg/vsl/ads-a",
        "https://[www.metodoraizana.com/att1/evg/vsl/ads-a",
    ],
    ids=[
        "sin-www",
        "ruta-fuera-del-manifiesto",
        "ruta-en-mayusculas",
        "dos-barras",
        "puerto",
        "otro-host",
        "sin-esquema",
        "url-rota",
    ],
)
def test_an_unknown_landing_is_rejected(url: str) -> None:
    rejection = _rejection(_ads_a_with_url(url))

    assert (rejection.reason, rejection.status_code) == ("landing_unknown", 422)


def _att1_with_offer_url(landing_id: str, url: str) -> InstanceManifest:
    payload = tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))
    payload["eventos"] = [*payload["eventos"], "intencion"]
    payload["adaptadores"] = {"ghl": {"formularios": [ADS_A_FORM, LANDING_D_FORM]}}
    (offer,) = [o for o in payload["hotmart"]["ofertas"] if o["landing_id"] == landing_id]
    offer["url"] = url
    return InstanceManifest.from_mapping(payload)


@pytest.mark.parametrize(
    "submitted",
    [
        "https://www.metodoraizana.com/att1/evg/vsl/org-a",
        "https://www.metodoraizana.com/att1/evg/vsl/org-a/?test=yes",
    ],
    ids=["sin-barra", "con-barra"],
)
def test_an_offer_declared_with_a_trailing_slash_still_resolves(submitted: str) -> None:
    # El manifiesto acepta una url con barra final; la comparacion quita una barra de
    # los dos lados, y el evento lleva la ruta tal como la declara el manifiesto.
    declared = "https://www.metodoraizana.com/att1/evg/vsl/org-a/"
    manifest = _att1_with_offer_url("org-a", declared)
    config = manifest.to_commercial_ally_config()

    translation = _translate(_ads_a_with_url(submitted), manifest=manifest)

    assert (translation.landing_id, translation.offer_code) == ("org-a", "bmaztyhg")
    assert translation.event["source"]["page_url"] == declared
    assert parse_lead_precheckout(translation.event, config=config) is not None


def test_two_offers_on_the_same_page_are_ambiguous() -> None:
    config = ATT1_BOTH_FORMS.to_commercial_ally_config()
    twin = OfferLanding(
        offer_code="twin1234",
        site="metodoraizana",
        landing_id="ads-a-twin",
        page_host="www.metodoraizana.com",
        page_path="/att1/evg/vsl/ads-a",
    )
    ambiguous = replace(
        config,
        additional_offer_codes=(*config.additional_offer_codes, twin.offer_code),
        additional_offer_landings=(*config.additional_offer_landings, twin),
    )

    rejection = _rejection(_ads_a(), config=ambiguous)

    assert (rejection.reason, rejection.status_code) == ("landing_ambiguous", 422)


# ------------------------------------------------------------ forma del envio


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["attributionSource"].__setitem__("medium", "survey"),
        lambda body: body["attributionSource"].pop("medium"),
        lambda body: body["attributionSource"].pop("mediumId"),
        lambda body: body["attributionSource"].__setitem__("mediumId", ""),
        lambda body: body["attributionSource"].pop("url"),
        lambda body: body["attributionSource"].__setitem__("url", "  "),
        lambda body: body.pop("attributionSource"),
        lambda body: body.__setitem__("attributionSource", None),
        lambda body: body.__setitem__("attributionSource", [body["attributionSource"]]),
    ],
    ids=[
        "otro-medium",
        "sin-medium",
        "sin-mediumId",
        "mediumId-vacio",
        "sin-url",
        "url-en-blanco",
        "sin-objeto",
        "objeto-null",
        "objeto-en-lista",
    ],
)
def test_without_a_form_submission_object_it_is_rejected(mutate) -> None:
    body = _ads_a()
    mutate(body)

    rejection = _rejection(body)

    assert (rejection.reason, rejection.status_code) == ("not_a_form_submission", 422)


def test_the_last_touch_is_never_used_when_the_top_level_object_is_missing() -> None:
    body = _ads_a()
    body.pop("attributionSource")

    assert body["contact"]["lastAttributionSource"]["medium"] == "form"
    assert _rejection(body).reason == "not_a_form_submission"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.pop("email"),
        lambda body: body.__setitem__("email", " "),
        lambda body: body.__setitem__("email", "lead.anonimo.1-at-example.com"),
        lambda body: body.pop("phone"),
        lambda body: body.__setitem__("phone", ""),
        lambda body: body.pop("contact_id"),
        lambda body: body.__setitem__("contact_id", 12345),
        lambda body: [body.pop(key) for key in ("full_name", "first_name", "last_name")],
        lambda body: [body.__setitem__(key, " ") for key in ("full_name", "first_name", "last_name")],
    ],
    ids=[
        "sin-email",
        "email-en-blanco",
        "email-sin-arroba",
        "sin-phone",
        "phone-vacio",
        "sin-contact_id",
        "contact_id-numero",
        "sin-nombre",
        "nombre-en-blanco",
    ],
)
def test_a_contact_without_its_minimum_fields_is_a_400(mutate) -> None:
    body = _ads_a()
    mutate(body)

    rejection = _rejection(body)

    assert (rejection.reason, rejection.status_code) == ("invalid_payload", 400)


def test_the_body_must_be_an_object() -> None:
    with pytest.raises(GhlAdapterRejection) as caught:
        translate_ghl_form_submission(
            [_ads_a()], config=CONFIG, allowed_forms=frozenset({ADS_A_FORM}), now=NOW
        )

    assert caught.value.reason == "invalid_payload"


def test_the_contact_checks_come_before_the_submission_checks() -> None:
    body = _editor()
    body.pop("email")

    assert _rejection(body).reason == "invalid_payload"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda body: body.pop("full_name"), "Lead Anonimo"),
        (lambda body: [body.pop("full_name"), body.pop("last_name")], "Lead"),
        (lambda body: [body.pop("full_name"), body.pop("first_name")], "Anonimo"),
        (lambda body: body.__setitem__("full_name", "  Lead Anonimo \t"), "Lead Anonimo"),
    ],
    ids=[
        "sin-full_name",
        "solo-first_name",
        "solo-last_name",
        "full_name-con-bordes",
    ],
)
def test_the_name_falls_back_to_first_and_last_name(mutate, expected: str) -> None:
    body = _ads_a()
    mutate(body)

    event = _translate(body).event

    assert event["data"]["buyer"]["name"] == expected
    assert parse_lead_precheckout(event, config=CONFIG) is not None


def test_the_email_is_trimmed_and_lowercased_in_the_event_and_the_dedupe_key() -> None:
    body = _ads_a()
    body["email"] = "  Lead.Anonimo.1@Example.COM "

    event = _translate(body).event

    assert event["data"]["buyer"]["email"] == "lead.anonimo.1@example.com"
    assert event["dedupe_key"] == "metodoraizana:gopi6lh7:lead.anonimo.1@example.com"


# ------------------------------------------ lo que ninguna admision puede guardar
# U+0000 lo rechaza un texto de jsonb (22P05) y un surrogate suelto no se codifica
# en UTF-8: el parser deja pasar los dos, y la RPC fallaria en cada entrega (503 que
# GHL reintenta sin fin). Cualquiera los pone en la URL de la landing.


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _assert_storable(event: dict) -> None:
    assert [text for text in _strings(event) if re.search("[\u0000\ud800-\udfff]", text)] == []
    # Lo que viaja a PostgREST: UTF-8 sin surrogates.
    json.dumps(event, ensure_ascii=False).encode("utf-8")
    assert parse_lead_precheckout(event, config=CONFIG) is not None


@pytest.mark.parametrize(
    ("url_query", "field", "expected"),
    [
        ("?utm_campaign=a%00b&utm_source=fb", "utm_campaign", "ab"),
        ("?utm_campaign=a%00b&utm_source=fb", "sck", "fb~~~~ab"),
        ("?sck=DEL-%00ANUNCIO", "sck", "DEL-ANUNCIO"),
        ("?fbclid=Anon%00Fbclid", "fbclid", "AnonFbclid"),
        ("?utm_campaign=a\ud800b", "utm_campaign", "ab"),
        ("?sck=DEL-\udfffANUNCIO", "sck", "DEL-ANUNCIO"),
    ],
    ids=[
        "nul-en-utm",
        "nul-en-utm-sck",
        "nul-en-sck-entrante",
        "nul-en-fbclid",
        "surrogate-en-utm",
        "surrogate-en-sck-entrante",
    ],
)
def test_attribution_drops_what_no_admission_can_store(
    url_query: str, field: str, expected: str
) -> None:
    body = _ads_a()
    body["attributionSource"]["url"] = body["attributionSource"]["url"].split("?")[0] + url_query

    event = _translate(body).event

    assert event["data"]["attribution"][field] == expected
    _assert_storable(event)


@pytest.mark.parametrize(
    ("mutate", "expected_name"),
    [
        (lambda body: body.__setitem__("full_name", "Lead\u0000 Anonimo"), "Lead Anonimo"),
        (lambda body: body.__setitem__("full_name", "Lead \ud800Anonimo"), "Lead Anonimo"),
        # Un full_name que queda vacio cae a first_name + last_name.
        (lambda body: body.__setitem__("full_name", "\u0000"), "Lead Anonimo"),
        (
            lambda body: body["attributionSource"].__setitem__(
                "referrer", "https://instagram.com\u0000"
            ),
            "Lead Anonimo",
        ),
    ],
    ids=["nul-en-el-nombre", "surrogate-en-el-nombre", "nombre-solo-nul", "nul-en-referrer"],
)
def test_the_name_and_the_referrer_drop_what_no_admission_can_store(
    mutate, expected_name: str
) -> None:
    body = _ads_a()
    mutate(body)

    event = _translate(body).event

    assert event["data"]["buyer"]["name"] == expected_name
    _assert_storable(event)


def test_a_name_made_only_of_what_cannot_be_stored_is_a_400() -> None:
    body = _ads_a()
    for key in ("full_name", "first_name", "last_name"):
        body[key] = "\u0000\ud800"

    rejection = _rejection(body)

    assert (rejection.reason, rejection.status_code) == ("invalid_payload", 400)


@pytest.mark.parametrize(
    "email",
    ["lead.anonimo.1\u0000@example.com", "lead.anonimo.1@exam\ud800ple.com"],
    ids=["nul", "surrogate"],
)
def test_an_email_with_what_cannot_be_stored_is_a_400_not_another_address(email: str) -> None:
    body = _ads_a()
    body["email"] = email

    rejection = _rejection(body)

    assert (rejection.reason, rejection.status_code) == ("invalid_payload", 400)


# ------------------------------------------------------------------- telefono


@pytest.mark.parametrize(
    ("phone", "region"),
    [
        ("+54294", "AR"),
        ("+525512", "MX"),
        ("2944440101", "unknown"),
        ("telefono", "unknown"),
        ("+52147555501021", "MX"),
        ("+3906123456789012", "IT"),
        # Valido para phonenumbers y para el parser, pero de 7 digitos: la RPC exige
        # identity.phone de 8 a 15 (22023 en cada entrega, que seria un 503).
        ("+6834002", "NU"),
        # No geografico: valido para phonenumbers, con region "001", que no es un
        # pais ISO para checkout_country. El log lleva esa region: no es un dato
        # del lead y dice por que se rechazo.
        ("+80012345678", "001"),
    ],
    ids=[
        "ar-truncado",
        "mx-truncado",
        "sin-codigo-de-pais",
        "texto",
        "521-con-11-digitos",
        "it-largo",
        "nu-de-7-digitos",
        "no-geografico",
    ],
)
def test_an_unusable_phone_is_a_422_with_its_region(phone: str, region: str) -> None:
    body = _ads_a()
    body["phone"] = phone

    rejection = _rejection(body)

    assert (rejection.reason, rejection.status_code) == ("phone_unusable", 422)
    assert rejection.phone_region == region
    assert phone not in str(rejection)


def test_mexican_mobile_with_the_legacy_1_is_normalized() -> None:
    # E3: phonenumbers 9.0.37 da invalido +521 + 10 digitos. Se deriva de -d con el
    # telefono medido pasado a la forma +521.
    body = _landing_d()
    measured = body["phone"]
    body["phone"] = "+521" + measured.removeprefix("+52")
    assert not phonenumbers.is_valid_number(phonenumbers.parse(body["phone"], None))

    translation = _translate(body)
    buyer = translation.event["data"]["buyer"]

    assert buyer == {
        "name": "Prueba Editor",
        "email": "lead.anonimo.2@example.com",
        "phone": measured,
        "phone_country_code": "52",
        "phone_national": measured.removeprefix("+52"),
    }
    assert translation.phone_region == "MX"
    submission = parse_lead_precheckout(translation.event, config=CONFIG)
    assert submission is not None and submission.phone_valid is True


def test_the_phone_is_rebuilt_as_e164() -> None:
    # E9: con separadores el numero sale en E.164; AR y MX no cambian.
    body = _ads_a()
    body["phone"] = "+54 294 444-0101"

    buyer = _translate(body).event["data"]["buyer"]

    assert (buyer["phone"], buyer["phone_country_code"], buyer["phone_national"]) == (
        "+542944440101",
        "54",
        "2944440101",
    )


def test_a_leading_zero_national_number_cannot_satisfy_the_parser() -> None:
    # Italia: el E.164 lleva el 0 y national_number no. El parser exige las dos
    # igualdades, asi que el numero saldria phone_valid = false, que la admision
    # 1.1.0 rechaza: el traductor lo corta antes.
    body = _ads_a()
    body["phone"] = "+390612345678"

    rejection = _rejection(body)

    assert (rejection.reason, rejection.phone_region) == ("phone_unusable", "IT")


# ----------------------------------------------------------- lo que no se lee


def test_ghl_noise_never_reaches_the_event() -> None:
    body = _landing_d()
    body["customData"] = {GHL_SETTER_TOKEN_FIELD: "t" * 40, "otro": "valor"}

    event_text = json.dumps(_translate(body).event, ensure_ascii=False)

    leaked = [
        "fallida att",  # tags: la automatizacion de recuperacion de GHL
        body["personalizado1"],
        "personalizado1",
        "Tipo de condición",
        "contact_source",
        body["contact_source"],
        body["contact_id"],
        body["location"]["id"],
        body["workflow"]["id"],
        body["date_created"],
        body["attributionSource"]["ip"],
        body["attributionSource"]["userAgent"],
        body["contact"]["attributionSource"]["fbEventId"],
        body["timezone"],
        "customData",
        "t" * 40,
        '"US"',
    ]
    assert [value for value in leaked if value in event_text] == []


def test_the_custom_fields_do_not_change_the_event() -> None:
    # Sin los ~35 campos personalizados, sin tags, sin location ni los toques de
    # contact: el mismo evento.
    body = _landing_d()
    bare = {
        key: value
        for key, value in body.items()
        if key
        in {
            "contact_id",
            "first_name",
            "last_name",
            "full_name",
            "email",
            "phone",
            "attributionSource",
        }
    }

    assert _without_id(_translate(bare).event) == _without_id(_translate(body).event)


# --------------------------------------------------------------- cuerpo y token


def _raw(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def test_parse_ghl_body_returns_the_captured_objects() -> None:
    for body in (_ads_a(), _editor()):
        assert parse_ghl_body(_raw(body)) == body


@pytest.mark.parametrize(
    "inject",
    [
        # Un campo personalizado que se llame como un campo estandar.
        lambda text: text.replace("{", '{"email": "otra.persona@example.com", ', 1),
        lambda text: text.replace('"contact_id": ', '"phone": "+542944440199", "contact_id": ', 1),
        # Adentro del objeto de atribucion.
        lambda text: text.replace(
            '"mediumId": "EgDqRl2xWc59YjVW1q8W"',
            '"mediumId": "EgDqRl2xWc59YjVW1q8W", "mediumId": "x"',
            1,
        ),
    ],
    ids=[
        "email-repetido",
        "phone-repetido",
        "clave-anidada-repetida",
    ],
)
def test_a_repeated_key_is_a_400(inject) -> None:
    text = inject(json.dumps(_ads_a(), ensure_ascii=False))
    assert text != json.dumps(_ads_a(), ensure_ascii=False)

    with pytest.raises(GhlAdapterRejection) as caught:
        parse_ghl_body(text.encode("utf-8"))

    assert (caught.value.reason, caught.value.status_code) == ("invalid_payload", 400)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"", "invalid_json"),
        (_raw(_ads_a())[:-1], "invalid_json"),
        (_raw(_ads_a()).replace(b'"tags": ""', b'"tags": NaN'), "invalid_json"),
        (_raw(_ads_a()).replace(b"Lead Anonimo", b"Lead \xffAnonimo"), "invalid_json"),
        # Mas hondo de lo que el parser de json aguanta (RecursionError en 3.14).
        (b"[" * 1_000_000 + b"]" * 1_000_000, "invalid_json"),
        (b"[" + _raw(_ads_a()) + b"]", "invalid_payload"),
        (b'"texto"', "invalid_payload"),
    ],
    ids=[
        "vacio",
        "cortado",
        "nan",
        "utf8-roto",
        "anidado-profundo",
        "lista",
        "texto",
    ],
)
def test_a_body_that_is_not_one_json_object_is_a_400(raw: bytes, reason: str) -> None:
    with pytest.raises(GhlAdapterRejection) as caught:
        parse_ghl_body(raw)

    assert (caught.value.reason, caught.value.status_code) == (reason, 400)


@pytest.mark.parametrize(
    ("custom_data", "expected"),
    [
        ({}, None),
        ({GHL_SETTER_TOKEN_FIELD: "k" * 43}, "k" * 43),
        ({GHL_SETTER_TOKEN_FIELD: ""}, ""),
        ({GHL_SETTER_TOKEN_FIELD: 12345}, ""),
        ({GHL_SETTER_TOKEN_FIELD: None}, ""),
        ({"otro": "k" * 43}, None),
        ("k" * 43, None),
        (None, None),
    ],
    ids=[
        "vacio",
        "token",
        "token-vacio",
        "token-numero",
        "token-null",
        "otra-clave",
        "texto",
        "null",
    ],
)
def test_the_body_token_lives_in_custom_data(custom_data: object, expected: str | None) -> None:
    body = _ads_a()
    body["customData"] = custom_data

    assert ghl_body_token(body) == expected


def test_the_captured_bodies_carry_no_token() -> None:
    assert ghl_body_token(_ads_a()) is None
    body = _ads_a()
    body.pop("customData")
    assert ghl_body_token(body) is None


# ------------------------------------------------------------------------ sck


def _core_vectors() -> list[tuple[str, str, str]]:
    vectors = []
    for name, keys in (
        ("lancemos_core_sck_tilde_20260925.json", ("casos", "casos_sin_tilde_que_siguen_vigentes")),
        ("lancemos_core_sck_utm_id_20260927.json", ("casos",)),
    ):
        data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        for key in keys:
            vectors += [(case["nombre"], case["search"], case["sck"]) for case in data[key]]
    return vectors


CORE_VECTORS = _core_vectors()


def test_there_are_ten_core_vectors() -> None:
    assert len(CORE_VECTORS) == 10


@pytest.mark.parametrize(
    ("search", "expected"),
    [(search, sck) for _, search, sck in CORE_VECTORS],
    ids=[name[:60] for name, _, _ in CORE_VECTORS],
)
def test_compose_lancemos_sck_matches_the_core_vectors(search: str, expected: str) -> None:
    # location.search lleva el "?"; urlsplit(url).query no: las dos formas.
    assert compose_lancemos_sck(search) == expected
    assert compose_lancemos_sck(search.removeprefix("?")) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # E02: un utm_id solo es una UTM y compone ~~~~~<id>.
        ("utm_id=120250442190930484&sck=DEL-ANUNCIO", "~~~~~120250442190930484"),
        # Una UTM que queda vacia al sanear no cuenta: gana el sck del anuncio.
        ("utm_source=%C2%BF%3F&sck=DEL-ANUNCIO", "DEL-ANUNCIO"),
        # El sck entrante se sanea apenas: los controles (tab y salto de linea
        # incluidos) se quitan antes de colapsar los espacios.
        ("sck=%20Uno%09%0ADos%20%20Tres%20%1F", "UnoDos Tres"),
        ("sck=a%7Cb~c", "a|b~c"),
        ("fbclid=abc&test=yes", ""),
        ("", ""),
        # Espacios de JavaScript, no de Python: U+00A0 y U+FEFF son espacio, U+0085 no.
        ("utm_source=a%C2%A0b&utm_medium=c%EF%BB%BFd&utm_campaign=e%C2%85f", "a-b~~~c-d~ef"),
        # NFD, no NFKD: la ligadura no se descompone y se elimina.
        ("utm_source=%EF%AC%81n", "n~~~~"),
    ],
    # Los esperados salen de correr el readSck literal del core (4ec04a7) en node.
    ids=[
        "solo-utm_id",
        "utm-que-se-vacia",
        "sck-entrante",
        "sck-entrante-con-barra",
        "sin-atribucion",
        "vacio",
        "espacios-js",
        "nfd",
    ],
)
def test_compose_lancemos_sck_follows_the_core_rules(query: str, expected: str) -> None:
    assert compose_lancemos_sck(query) == expected
