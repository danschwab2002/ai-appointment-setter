"""Formularios propios: la definicion en el manifiesto y el evento (own-forms-v1).

Fixture: tests/fixtures/own_forms/att1_lead_precheckout_ghl_alimenta_tu_tiroides_20261116.json,
el lead.precheckout real que armo el adaptador de GHL para el ultimo envio de ATT1
(16/11, base att1-db, solo lectura, comprador sintetico). Un formulario propio de la
misma landing tiene que producir ese mismo evento.
"""

from __future__ import annotations

import copy
import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from bridge.instance_manifest import (
    OWN_FORM_DEFAULT_DISCLAIMER,
    InstanceManifest,
    ManifestError,
)
from bridge.lead_precheckout import parse_lead_precheckout
from bridge.own_forms import (
    OwnFormRejection,
    OwnFormSubmission,
    SlidingWindowLimiter,
    country_options,
    render_embed_js,
    render_page_html,
    translate_own_form_submission,
    webhook_copy,
)

FIXTURES = Path(__file__).parent / "fixtures"
ATT1_TOML = FIXTURES / "instances" / "att1-b7580bc-20261103" / "instancia.toml"
CAPTURE = json.loads(
    (FIXTURES / "own_forms" / "att1_lead_precheckout_ghl_alimenta_tu_tiroides_20261116.json").read_text(
        encoding="utf-8"
    )
)
REAL_EVENT = CAPTURE["event"]
BUYER = REAL_EVENT["data"]["buyer"]
NOW = datetime(2026, 11, 16, 14, 5, 51, 210000, tzinfo=UTC)


def att1_payload() -> dict:
    return tomllib.loads(ATT1_TOML.read_text(encoding="utf-8"))


def att1_with_forms(forms: list[dict]) -> InstanceManifest:
    payload = att1_payload()
    payload["formularios_propios"] = forms
    return InstanceManifest.from_mapping(payload)


ORG_B = {
    "clave": "att1-org-b",
    "landing_id": "alimenta-tu-tiroides",
    "origenes": ["https://site.metodoraizana.com.mx"],
    "links": [
        {"texto": "Política de Privacidad", "url": "https://www.metodoraizana.com/politica-de-privacidad"}
    ],
}
MANIFEST = att1_with_forms([ORG_B])
FORM = MANIFEST.own_forms[0]
CONFIG = next(
    config
    for config in MANIFEST.to_commercial_ally_configs()
    if any(item.landing_id == "alimenta-tu-tiroides" for item in config.offer_landings)
)
LANDING = next(item for item in CONFIG.offer_landings if item.landing_id == "alimenta-tu-tiroides")


def submission(**overrides: str) -> OwnFormSubmission:
    values = {
        "nombre": BUYER["name"],
        "email": BUYER["email"],
        "telefono": BUYER["phone"],
        "pagina": "https://site.metodoraizana.com.mx/alimenta-tu-tiroides",
        "referrer": "https://my.manychat.com",
        "website": "",
    }
    values.update(overrides)
    return OwnFormSubmission.from_json(values)


# ---------------------------------------------------------------- manifest


def test_manifest_without_section_has_no_forms() -> None:
    assert InstanceManifest.from_mapping(att1_payload()).own_forms == ()


def test_manifest_parses_form_with_defaults() -> None:
    assert FORM.key == "att1-org-b"
    assert FORM.landing_id == "alimenta-tu-tiroides"
    assert FORM.disclaimer == OWN_FORM_DEFAULT_DISCLAIMER
    assert FORM.page_title == "Dra. Nina Garza"
    assert FORM.button == "Enviar"
    assert FORM.default_country == "MX"
    assert FORM.origins == ("https://site.metodoraizana.com.mx",)
    assert FORM.thanks_url is None and FORM.webhooks == () and FORM.use_core is True


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"clave": "X"}, "clave"),
        ({"landing_id": "no-existe"}, "landing_id"),
        ({"origenes": ["https://site.metodoraizana.com.mx/ruta"]}, "origenes"),
        ({"origenes": ["http://inseguro.com"]}, "origenes"),
        ({"pais": "mx"}, "codigo de pais"),
        ({"paises": ["US"], "pais": "MX"}, "pais debe estar"),
        ({"webhooks": ["http://n8n.local/x"]}, "webhooks"),
        ({"gracias_url": "javascript:alert(1)"}, "gracias_url"),
        ({"usar_core": "si"}, "usar_core"),
        ({"otra": 1}, "claves no soportadas"),
    ],
)
def test_manifest_rejects_bad_forms(change: dict, message: str) -> None:
    with pytest.raises(ManifestError, match=message):
        att1_with_forms([{**ORG_B, **change}])


def test_manifest_rejects_repeated_keys_and_missing_intent_event() -> None:
    with pytest.raises(ManifestError, match="repetidas"):
        att1_with_forms([ORG_B, dict(ORG_B)])
    payload = att1_payload()
    payload["eventos"] = [event for event in payload["eventos"] if event != "intencion"]
    payload.pop("adaptadores", None)
    payload["formularios_propios"] = [ORG_B]
    with pytest.raises(ManifestError, match="intencion"):
        InstanceManifest.from_mapping(payload)


# ---------------------------------------------------------------- the event


def test_event_is_the_one_the_ghl_adapter_wrote_for_the_same_landing() -> None:
    translation = translate_own_form_submission(
        submission(), config=CONFIG, landing=LANDING, now=NOW
    )
    event = translation.event
    expected = copy.deepcopy(REAL_EVENT)
    # El id es un ULID nuevo por envio (como en el adaptador); el resto, igual.
    assert len(event["id"]) == 26
    event = {**event, "id": expected["id"]}
    assert event == expected
    parsed = parse_lead_precheckout(event, config=CONFIG)
    assert parsed is not None and parsed.phone_valid


def test_attribution_comes_from_the_page_url_with_the_core_sck() -> None:
    page = (
        "https://site.metodoraizana.com.mx/alimenta-tu-tiroides?utm_source=meta-ads&utm_medium=ig"
        "&utm_campaign=camp&utm_content=ad1&utm_term=t&utm_id=123&fbclid=FBCLID1"
    )
    translation = translate_own_form_submission(
        submission(pagina=page), config=CONFIG, landing=LANDING, now=NOW
    )
    attribution = translation.event["data"]["attribution"]
    assert attribution["sck"] == "meta-ads~t~ad1~ig~camp~123"
    assert attribution["fbclid"] == "FBCLID1"
    assert attribution["utm_source"] == "meta-ads"
    assert translation.has_utm and translation.has_fbclid
    # La landing y la oferta salen de la definicion, aunque la URL sea otra.
    other = translate_own_form_submission(
        submission(pagina="https://otra.example/x"), config=CONFIG, landing=LANDING, now=NOW
    )
    assert other.event["data"]["offer"] == {"code": "bmaztyhg"}


def test_fbclid_falls_back_to_the_fbc_cookie() -> None:
    sub = OwnFormSubmission.from_json(
        {
            "nombre": BUYER["name"],
            "email": BUYER["email"],
            "telefono": BUYER["phone"],
            "pagina": "https://site.metodoraizana.com.mx/alimenta-tu-tiroides",
            "fbc": "fb.1.1700000000000.CLICKID",
        }
    )
    translation = translate_own_form_submission(sub, config=CONFIG, landing=LANDING, now=NOW)
    assert translation.event["data"]["attribution"]["fbclid"] == "CLICKID"


def test_checkout_url_matches_the_core_hotmart_builder() -> None:
    page = "https://site.metodoraizana.com.mx/alimenta-tu-tiroides?utm_source=meta-ads&fbclid=F1"
    translation = translate_own_form_submission(
        submission(pagina=page), config=CONFIG, landing=LANDING, now=NOW
    )
    parts = urlsplit(translation.checkout_url)
    assert (parts.netloc, parts.path) == ("pay.hotmart.com", "/D98014973Y")
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    assert query == {
        "off": "bmaztyhg",
        "checkoutMode": "10",
        "name": BUYER["name"],
        "email": BUYER["email"],
        "phone": BUYER["phone"],
        "phoneac": "58",
        "phonenumber": "4121234567",
        "utm_source": "meta-ads",
        "sck": "meta-ads~~~~",
        "fbclid": "F1",
    }


def test_mexican_legacy_mobile_is_normalized() -> None:
    translation = translate_own_form_submission(
        submission(telefono="+5215512345678"), config=CONFIG, landing=LANDING, now=NOW
    )
    buyer = translation.event["data"]["buyer"]
    assert buyer["phone"] == "+525512345678"
    assert buyer["phone_country_code"] == "52"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("nombre", "", "nombre"),
        ("nombre", "a", "nombre"),
        ("email", "sin-arroba", "email"),
        ("email", "a@b.c" + "x" * 260, "email"),
        ("telefono", "5512345678", "telefono"),
        ("telefono", "+52123", "telefono"),
        ("telefono", "", "telefono"),
    ],
)
def test_bad_fields_are_rejected_with_a_user_message(field: str, value: str, code: str) -> None:
    with pytest.raises(OwnFormRejection) as exc:
        translate_own_form_submission(
            submission(**{field: value}), config=CONFIG, landing=LANDING, now=NOW
        )
    assert exc.value.code == code
    assert exc.value.user_message


def test_unstorable_characters_are_dropped_from_the_name() -> None:
    translation = translate_own_form_submission(
        submission(nombre="Prueba\u0000 Sintetica"), config=CONFIG, landing=LANDING, now=NOW
    )
    assert translation.event["data"]["buyer"]["name"] == "Prueba Sintetica"


def test_body_that_is_not_an_object_is_rejected() -> None:
    with pytest.raises(OwnFormRejection):
        OwnFormSubmission.from_json(["x"])


def test_webhook_copy_carries_contact_landing_and_attribution_only() -> None:
    translation = translate_own_form_submission(
        submission(), config=CONFIG, landing=LANDING, now=NOW
    )
    copy_ = webhook_copy(translation.event, form_key=FORM.key)
    assert copy_["form"] == "att1-org-b"
    assert copy_["contact"]["email"] == BUYER["email"]
    assert copy_["landing"]["landing_id"] == "alimenta-tu-tiroides"
    assert "dedupe_key" not in copy_ and "id" not in copy_


# ---------------------------------------------------------------- limits and assets


def test_sliding_window_limiter() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_s=10)
    assert limiter.allow("k", now=0) and limiter.allow("k", now=1)
    assert not limiter.allow("k", now=2)
    assert limiter.allow("k", now=11)
    assert limiter.allow("otra", now=2)


def test_country_options_have_dial_codes() -> None:
    assert country_options(("MX", "US"))[0] == {"iso": "MX", "nombre": "México", "prefijo": "52"}
    assert country_options(("US",))[0]["prefijo"] == "1"


def test_embed_js_carries_the_config_and_is_safe_inside_a_script() -> None:
    script = render_embed_js(FORM, submit_url="https://bridge.example/f/att1-org-b/enviar")
    assert "/*__SF_CONFIG__*/null" not in script
    assert '"clave": "att1-org-b"' in script
    assert "https://bridge.example/f/att1-org-b/enviar" in script
    assert "</style" not in script and "</script" not in script


def test_page_html_escapes_and_points_to_the_embed() -> None:
    html = render_page_html(FORM, embed_url="https://bridge.example/f/att1-org-b/embed.js")
    assert '<script src="https://bridge.example/f/att1-org-b/embed.js"></script>' in html
    assert 'data-setter-form="att1-org-b"' in html
    assert "<title>Dra. Nina Garza</title>" in html
    assert "{{" not in html
