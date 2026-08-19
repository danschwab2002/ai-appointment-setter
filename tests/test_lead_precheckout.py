"""Contract tests for the observed Lancemos lead.precheckout event."""

import asyncio
import json
from copy import deepcopy

import httpx

from bridge.lead_precheckout import parse_lead_precheckout
from bridge.supabase import SupabaseClient


def _payload() -> dict[str, object]:
    return {
        "id": "01K3F8QW7N2VYB4M6X9CDPTZRA",
        "event": "lead.precheckout",
        "version": "1.0.0",
        "created_at": "2026-08-18T16:42:07.318-03:00",
        "source": {
            "system": "landing",
            "site": "psicologajohanna",
            "aliado": "Psicologa Johanna",
            "landing_id": "ads-a",
            "page_url": "https://psicologajohanna.com/ldla/evg/vsl/ads-a",
        },
        "data": {
            "buyer": {
                "name": "  Maria Example  ",
                "email": " Maria.Example@Example.COM ",
                "phone": "+12025550123",
                "phone_country_code": "1",
                "phone_national": "2025550123",
            },
            "product": {
                "hotlink": "F106691755G",
                "id": None,
                "name": "Liberate De La Ansiedad",
                "price": 49,
                "currency": "USD",
            },
            "offer": {"code": "bxjge6zq"},
            "checkout_url": "https://pay.hotmart.com/F106691755G?off=bxjge6zq&checkoutMode=10&email=Maria.Example%40Example.COM",
            "checkout_country": {"iso": "US", "source": "phone_country_code"},
            "attribution": {
                "utm_source": "facebook",
                "utm_medium": "cpc",
                "utm_campaign": "campaign",
                "utm_content": "creative",
                "utm_term": "",
                "sck": "facebook.cpc.campaign",
                "fbclid": "fixture-click-id",
                "referrer": "https://example.test/",
            },
            "consent": {
                "marketing_optin": False,
                "notice": "sin consentimiento explicito - dato entregado para completar una compra",
            },
        },
        "dedupe_key": "psicologajohanna:bxjge6zq:maria.example@example.com",
    }


def test_parses_observed_contract_into_non_contactable_intent() -> None:
    parsed = parse_lead_precheckout(_payload())

    assert parsed is not None
    assert parsed.external_submission_id == "01K3F8QW7N2VYB4M6X9CDPTZRA"
    assert parsed.normalized_email == "maria.example@example.com"
    assert parsed.normalized_phone == "12025550123"
    assert parsed.phone_valid is True
    assert parsed.marketing_optin is False
    assert parsed.whatsapp_contact_authorized is False
    canonical = parsed.as_canonical_payload()
    assert canonical["assurance"] == {
        "provisional": False,
        "provider_observed": True,
        "activation_authorized": False,
    }


def test_invalid_country_phone_is_admitted_as_non_contactable() -> None:
    payload = _payload()
    buyer = payload["data"]["buyer"]  # type: ignore[index]
    buyer.update(  # type: ignore[union-attr]
        phone="+57123", phone_country_code="57", phone_national="123"
    )

    parsed = parse_lead_precheckout(payload)

    assert parsed is not None
    assert parsed.normalized_phone is None
    assert parsed.phone_valid is False
    assert parsed.whatsapp_contact_authorized is False


def test_rejects_wrong_dedupe_key() -> None:
    payload = _payload()
    payload["dedupe_key"] = "psicologajohanna:bxjge6zq:other@example.com"

    assert parse_lead_precheckout(payload) is None


def test_rejects_landing_offer_mismatch() -> None:
    payload = _payload()
    payload["data"]["offer"]["code"] = "ecyu87q0"  # type: ignore[index]

    assert parse_lead_precheckout(payload) is None


def test_rejects_checkout_host_or_offer_mismatch() -> None:
    for checkout_url in (
        "https://evil.example/F106691755G?off=bxjge6zq",
        "https://pay.hotmart.com/F106691755G?off=other",
    ):
        payload = deepcopy(_payload())
        payload["data"]["checkout_url"] = checkout_url  # type: ignore[index]
        assert parse_lead_precheckout(payload) is None


def test_rejects_noncanonical_landing_and_checkout_urls() -> None:
    landing_urls = (
        "https://user:pass@psicologajohanna.com/ldla/evg/vsl/ads-a",
        "https://psicologajohanna.com:443/ldla/evg/vsl/ads-a",
        "https://psicologajohanna.com/other/ads-a",
        "https://psicologajohanna.com/ldla/evg/vsl/ads-a#fragment",
    )
    checkout_urls = (
        "https://user:pass@pay.hotmart.com/F106691755G?off=bxjge6zq",
        "https://pay.hotmart.com:443/F106691755G?off=bxjge6zq",
        "https://pay.hotmart.com/F106691755G?off=bxjge6zq#fragment",
    )
    for field, values in (("page_url", landing_urls), ("checkout_url", checkout_urls)):
        for value in values:
            payload = deepcopy(_payload())
            if field == "page_url":
                payload["source"][field] = value  # type: ignore[index]
            else:
                payload["data"][field] = value  # type: ignore[index]
            assert parse_lead_precheckout(payload) is None


def test_rejects_optin_true_until_form_and_policy_are_approved() -> None:
    payload = _payload()
    payload["data"]["consent"]["marketing_optin"] = True  # type: ignore[index]

    assert parse_lead_precheckout(payload) is None


def test_rejects_unknown_or_extra_contract_fields() -> None:
    payload = _payload()
    payload["unexpected"] = "field"

    assert parse_lead_precheckout(payload) is None


def test_rejects_non_ulid_delivery_id() -> None:
    payload = _payload()
    payload["id"] = "form-submit-1"

    assert parse_lead_precheckout(payload) is None


def test_rejects_non_finite_or_unconfirmed_price() -> None:
    for price in (float("nan"), float("inf"), 47, 67):
        payload = deepcopy(_payload())
        payload["data"]["product"]["price"] = price  # type: ignore[index]

        assert parse_lead_precheckout(payload) is None


def test_equivalent_float_price_is_canonicalized_for_the_rpc() -> None:
    payload = deepcopy(_payload())
    payload["data"]["product"]["price"] = 49.0  # type: ignore[index]

    parsed = parse_lead_precheckout(payload)

    assert parsed is not None
    canonical = parsed.as_canonical_payload()
    assert canonical["commerce"]["price"] == "49"  # type: ignore[index]


def test_observed_admission_uses_its_separate_atomic_rpc() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=[{
                "outcome": "inserted",
                "submission_id": "bfc778e7-5c9f-45e6-a910-651f92312157",
                "purchase_intent_id": "1f581f3a-c469-45da-8208-9483d1b26f0b",
            }],
        )

    payload = _payload()
    parsed = parse_lead_precheckout(payload)
    assert parsed is not None
    client = SupabaseClient(
        base_url="https://example.supabase.test",
        service_role_key="test-service-role",
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(client.admit_observed_lead_precheckout(
        external_submission_id=parsed.external_submission_id,
        raw_payload=payload,
        canonical_payload=parsed.as_canonical_payload(),
    ))

    assert result.outcome == "inserted"
    assert observed["path"] == "/rest/v1/rpc/admit_observed_lead_precheckout"
    body = observed["body"]
    assert isinstance(body, dict)
    assert body["p_external_submission_id"] == parsed.external_submission_id
