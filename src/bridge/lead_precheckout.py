"""Strict adapter for Lancemos ``lead.precheckout`` contract v1.0.0."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlparse

import phonenumbers

_EVENT_KEYS = {"id", "event", "version", "created_at", "source", "data", "dedupe_key"}
_SOURCE_KEYS = {"system", "site", "aliado", "landing_id", "page_url"}
_DATA_KEYS = {
    "buyer",
    "product",
    "offer",
    "checkout_url",
    "checkout_country",
    "attribution",
    "consent",
}
_BUYER_KEYS = {"name", "email", "phone", "phone_country_code", "phone_national"}
_PRODUCT_KEYS = {"hotlink", "id", "name", "price", "currency"}
_OFFER_KEYS = {"code"}
_COUNTRY_KEYS = {"iso", "source"}
_ATTRIBUTION_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "sck",
    "fbclid",
    "referrer",
}
_CONSENT_KEYS_V1 = {"marketing_optin", "notice"}
_CONSENT_KEYS_V1_1 = {"marketing_optin", "whatsapp_contact", "copy_version"}
_SUPPORTED_VERSIONS = {"1.0.0", "1.1.0"}
_V1_COPY_VERSION = "lead-precheckout-v1-no-explicit-optin"
_V1_1_COPY_VERSION = "johanna-precheckout-whatsapp-disclosure-v1"
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_E164_SHAPE = re.compile(r"\+[1-9][0-9]{1,14}")
_LANDING_OFFERS = {
    "ads-a": "bxjge6zq",
    "ads-b": "mgbgpp19",
    "ads-c": "s1qfxm7m",
    "org-a": "jtt6fcsm",
    "org-b": "ecyu87q0",
    "org-c": "ulhzpw9a",
}


@dataclass(frozen=True)
class LeadPrecheckoutSubmission:
    external_submission_id: str
    contract_version: str
    submitted_at: datetime
    site: str
    aliado: str
    landing_id: str
    page_url: str
    buyer_name: str
    normalized_email: str
    normalized_phone: str | None
    phone_valid: bool
    phone_country_iso: str
    product_hotlink: str
    product_name: str
    product_price: Decimal
    currency: str
    offer_code: str
    checkout_url: str
    dedupe_key: str
    marketing_optin: bool
    consent_copy_version: str

    @property
    def whatsapp_contact_authorized(self) -> bool:
        return (
            self.contract_version == "1.1.0"
            and self.marketing_optin
            and self.phone_valid
        )

    @property
    def activation_authorized(self) -> bool:
        return self.whatsapp_contact_authorized

    def as_canonical_payload(self) -> dict[str, object]:
        return {
            "event_type": "PRECHECKOUT_FORM_SUBMITTED",
            "contract_version": self.contract_version,
            "external_submission_id": self.external_submission_id,
            "submitted_at": self.submitted_at.isoformat().replace("+00:00", "Z"),
            "source": {
                "tenant_ref": "lancemos",
                "funnel_ref": self.site,
                "landing_ref": self.landing_id,
                "page_url": self.page_url,
                "aliado": self.aliado,
            },
            "identity": {
                "email": self.normalized_email,
                "phone": self.normalized_phone,
                "phone_valid": self.phone_valid,
                "phone_country_iso": self.phone_country_iso,
            },
            "lead": {"full_name": self.buyer_name},
            "commerce": {
                "product_ref": self.product_hotlink,
                "product_name": self.product_name,
                "price": str(self.product_price),
                "currency": self.currency,
                "offer_ref": self.offer_code,
                "checkout_url": self.checkout_url,
            },
            "consent": {
                "terms_accepted": False,
                "privacy_accepted": False,
                "whatsapp_contact": self.whatsapp_contact_authorized,
                "marketing_optin": self.marketing_optin,
                "copy_version": self.consent_copy_version,
            },
            "dedupe_key": self.dedupe_key,
            "assurance": {
                "provisional": False,
                "provider_observed": True,
                "activation_authorized": self.activation_authorized,
            },
        }


def _object(value: object, keys: set[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != keys:
        return None
    return value


def _text(value: object, *, allow_empty: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned and not allow_empty:
        return None
    return cleaned


def _timestamp(value: object) -> datetime | None:
    raw = _text(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _valid_phone(
    phone: str, country_code: str, national: str, country_iso: str
) -> str | None:
    if (
        _E164_SHAPE.fullmatch(phone) is None
        or not country_code.isdigit()
        or not national.isdigit()
        or phone != f"+{country_code}{national}"
    ):
        return None
    try:
        parsed = phonenumbers.parse(phone, None)
    except phonenumbers.NumberParseException:
        return None
    region = phonenumbers.region_code_for_number(parsed)
    if (
        not phonenumbers.is_valid_number(parsed)
        or region is None
        or region != country_iso
        or str(parsed.country_code) != country_code
        or str(parsed.national_number) != national
    ):
        return None
    return phone[1:]


def parse_lead_precheckout(payload: object) -> LeadPrecheckoutSubmission | None:
    event = _object(payload, _EVENT_KEYS)
    if event is None:
        return None
    source = _object(event.get("source"), _SOURCE_KEYS)
    data = _object(event.get("data"), _DATA_KEYS)
    if source is None or data is None:
        return None
    buyer = _object(data.get("buyer"), _BUYER_KEYS)
    product = _object(data.get("product"), _PRODUCT_KEYS)
    offer = _object(data.get("offer"), _OFFER_KEYS)
    country = _object(data.get("checkout_country"), _COUNTRY_KEYS)
    attribution = _object(data.get("attribution"), _ATTRIBUTION_KEYS)
    contract_version = _text(event.get("version"))
    consent_keys = (
        _CONSENT_KEYS_V1_1
        if contract_version == "1.1.0"
        else _CONSENT_KEYS_V1
    )
    consent = _object(data.get("consent"), consent_keys)
    if None in (buyer, product, offer, country, attribution, consent):
        return None
    assert buyer is not None and product is not None and offer is not None
    assert country is not None and attribution is not None and consent is not None

    delivery_id = _text(event.get("id"))
    submitted_at = _timestamp(event.get("created_at"))
    site = _text(source.get("site"))
    aliado = _text(source.get("aliado"))
    landing_id = _text(source.get("landing_id"))
    page_url = _text(source.get("page_url"))
    name = _text(buyer.get("name"))
    email_raw = _text(buyer.get("email"))
    phone = _text(buyer.get("phone"))
    phone_country_code = _text(buyer.get("phone_country_code"))
    phone_national = _text(buyer.get("phone_national"))
    country_iso = _text(country.get("iso"))
    offer_code = _text(offer.get("code"))
    checkout_url = _text(data.get("checkout_url"))
    dedupe_key = _text(event.get("dedupe_key"))
    if any(
        value is None
        for value in (
            delivery_id,
            submitted_at,
            site,
            aliado,
            landing_id,
            page_url,
            name,
            email_raw,
            phone,
            phone_country_code,
            phone_national,
            country_iso,
            offer_code,
            checkout_url,
            dedupe_key,
        )
    ):
        return None
    assert isinstance(delivery_id, str) and isinstance(site, str)
    assert isinstance(landing_id, str) and isinstance(offer_code, str)
    assert isinstance(email_raw, str) and isinstance(page_url, str)
    assert isinstance(checkout_url, str) and isinstance(country_iso, str)
    assert isinstance(phone, str) and isinstance(phone_country_code, str)
    assert isinstance(phone_national, str) and isinstance(dedupe_key, str)

    email = email_raw.lower()
    page = urlparse(page_url)
    checkout = urlparse(checkout_url)
    checkout_query = parse_qs(checkout.query, keep_blank_values=True)
    product_price = product.get("price")
    if isinstance(product_price, bool) or not isinstance(product_price, (int, float)):
        return None
    price = Decimal(str(product_price))
    if (
        _ULID.fullmatch(delivery_id) is None
        or event.get("event") != "lead.precheckout"
        or contract_version not in _SUPPORTED_VERSIONS
        or source.get("system") != "landing"
        or site != "psicologajohanna"
        or landing_id not in _LANDING_OFFERS
        or _LANDING_OFFERS[landing_id] != offer_code
        or page.scheme != "https"
        or page.netloc != "psicologajohanna.com"
        or page.path != f"/ldla/evg/vsl/{landing_id}"
        or bool(page.query)
        or bool(page.fragment)
        or _EMAIL.fullmatch(email) is None
        or product.get("hotlink") != "F106691755G"
        or product.get("id") is not None
        or product.get("name") != "Liberate De La Ansiedad"
        or not price.is_finite()
        or price != Decimal("49")
        or product.get("currency") != "USD"
        or checkout.scheme != "https"
        or checkout.netloc != "pay.hotmart.com"
        or checkout.path != "/F106691755G"
        or bool(checkout.fragment)
        or checkout_query.get("off") != [offer_code]
        or country_iso != country_iso.upper()
        or len(country_iso) != 2
        or country.get("source") != "phone_country_code"
        or (
            contract_version == "1.0.0"
            and consent.get("marketing_optin") is not False
        )
        or (
            contract_version == "1.1.0"
            and (
                consent.get("marketing_optin") is not True
                or consent.get("whatsapp_contact") is not True
                or consent.get("copy_version") != _V1_1_COPY_VERSION
            )
        )
        or (
            contract_version == "1.0.0"
            and _text(consent.get("notice")) is None
        )
        or any(not isinstance(value, str) for value in attribution.values())
        or dedupe_key != f"{site}:{offer_code}:{email}"
    ):
        return None

    normalized_phone = _valid_phone(
        phone, phone_country_code, phone_national, country_iso
    )
    if contract_version == "1.1.0" and normalized_phone is None:
        return None
    return LeadPrecheckoutSubmission(
        external_submission_id=delivery_id,
        contract_version=contract_version,
        submitted_at=submitted_at,  # type: ignore[arg-type]
        site=site,
        aliado=aliado,  # type: ignore[arg-type]
        landing_id=landing_id,
        page_url=page_url,
        buyer_name=name,  # type: ignore[arg-type]
        normalized_email=email,
        normalized_phone=normalized_phone,
        phone_valid=normalized_phone is not None,
        phone_country_iso=country_iso,
        product_hotlink="F106691755G",
        product_name="Liberate De La Ansiedad",
        product_price=Decimal("49"),
        currency="USD",
        offer_code=offer_code,
        checkout_url=checkout_url,
        dedupe_key=dedupe_key,
        marketing_optin=contract_version == "1.1.0",
        consent_copy_version=(
            _V1_1_COPY_VERSION
            if contract_version == "1.1.0"
            else _V1_COPY_VERSION
        ),
    )
