from urllib.parse import parse_qs, urlsplit
import asyncio
import json

import httpx
import pytest

from bridge.checkout_issuance import (
    CheckoutOffer,
    CheckoutIssuanceUnavailable,
    build_checkout_issuance,
)
from bridge.supabase import SupabaseClient, SupabaseCommittedResponseError


ISSUANCE_ULID = "01K5ABCDEFX2VYB4M6X9CDPTZR"


def test_build_checkout_issuance_uses_server_owned_offer_and_opaque_sck() -> None:
    result = build_checkout_issuance(
        offer=CheckoutOffer(
            tenant_ref="lancemos",
            product_ref="F106691755G",
            landing_ref="ads-a",
            offer_code="bxjge6zq",
            checkout_base_url="https://pay.hotmart.com/F106691755G",
            checkout_mode=10,
        ),
        issuance_ulid=ISSUANCE_ULID,
    )

    assert result.sck_value == f"hermes|v1|{ISSUANCE_ULID}"
    assert result.source_value == "hermes"
    assert result.final_url == (
        "https://pay.hotmart.com/F106691755G"
        f"?off=bxjge6zq&checkoutMode=10&src=hermes&sck=hermes%7Cv1%7C{ISSUANCE_ULID}"
    )
    query = parse_qs(urlsplit(result.final_url).query, strict_parsing=True)
    assert query == {
        "off": ["bxjge6zq"],
        "checkoutMode": ["10"],
        "src": ["hermes"],
        "sck": [f"hermes|v1|{ISSUANCE_ULID}"],
    }


def test_build_checkout_issuance_does_not_copy_original_sck_or_pii() -> None:
    result = build_checkout_issuance(
        offer=CheckoutOffer(
            tenant_ref="lancemos",
            product_ref="F106691755G",
            landing_ref="ads-a",
            offer_code="bxjge6zq",
            checkout_base_url="https://pay.hotmart.com/F106691755G",
            checkout_mode=10,
        ),
        issuance_ulid=ISSUANCE_ULID,
    )

    assert "email=" not in result.final_url
    assert "phone=" not in result.final_url
    assert "name=" not in result.final_url
    assert "utm_" not in result.final_url
    assert "fbclid=" not in result.final_url


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("issuance_ulid", "conversation-123", "invalid_issuance_ulid"),
        ("offer_code", "", "invalid_offer"),
        ("checkout_base_url", "https://evil.example/F106691755G", "invalid_checkout_base_url"),
        ("checkout_base_url", "https://pay.hotmart.com/F106691755G?off=other", "invalid_checkout_base_url"),
    ],
)
def test_build_checkout_issuance_rejects_untrusted_components(
    field: str, value: str, reason: str
) -> None:
    kwargs = {
        "tenant_ref": "lancemos",
        "product_ref": "F106691755G",
        "landing_ref": "ads-a",
        "offer_code": "bxjge6zq",
        "checkout_base_url": "https://pay.hotmart.com/F106691755G",
        "checkout_mode": 10,
    }
    if field != "issuance_ulid":
        kwargs[field] = value
    with pytest.raises((CheckoutIssuanceUnavailable, ValueError), match=reason):
        build_checkout_issuance(
            offer=CheckoutOffer(**kwargs),
            issuance_ulid=value if field == "issuance_ulid" else ISSUANCE_ULID,
        )


def test_supabase_reserves_checkout_issuance_with_closed_arguments() -> None:
    requests: list[httpx.Request] = []
    issuance_id = "00000000-0000-0000-0000-000000000301"
    intent_id = "00000000-0000-0000-0000-000000000302"
    final_url = (
        "https://pay.hotmart.com/F106691755G"
        f"?off=bxjge6zq&checkoutMode=10&src=hermes&sck=hermes%7Cv1%7C{ISSUANCE_ULID}"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "outcome": "reserved",
            "issuance_id": issuance_id,
            "issuance_ulid": ISSUANCE_ULID,
            "purchase_intent_id": intent_id,
            "source_kind": "inbound_request",
            "checkout_url_final": final_url,
            "source_value": "hermes",
            "sck_value": f"hermes|v1|{ISSUANCE_ULID}",
        }], request=request)

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client.reserve_chatwoot_checkout_issuance_v2(
        commercial_case_id="00000000-0000-0000-0000-000000000300",
        external_user_id="12025550123",
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        chatwoot_conversation_id=9101,
        trigger_external_message_id="501",
        issuance_ulid=ISSUANCE_ULID,
        now="2026-09-14T12:00:00+00:00",
    ))

    assert result.outcome == "reserved"
    assert result.issuance_id == issuance_id
    assert result.checkout_url_final == final_url
    assert len(requests) == 1
    assert requests[0].url.path == "/rest/v1/rpc/reserve_chatwoot_checkout_issuance_v2"
    assert json.loads(requests[0].content) == {
        "p_commercial_case_id": "00000000-0000-0000-0000-000000000300",
        "p_external_user_id": "12025550123",
        "p_chatwoot_account_id": 1,
        "p_chatwoot_inbox_id": 9,
        "p_chatwoot_conversation_id": 9101,
        "p_trigger_external_message_id": "501",
        "p_issuance_ulid": ISSUANCE_ULID,
        "p_now": "2026-09-14T12:00:00+00:00",
    }


def test_supabase_rejects_inconsistent_checkout_issuance_row() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "outcome": "reserved",
            "issuance_id": "00000000-0000-0000-0000-000000000301",
            "issuance_ulid": ISSUANCE_ULID,
            "purchase_intent_id": "00000000-0000-0000-0000-000000000302",
            "source_kind": "inbound_request",
            "checkout_url_final": "https://pay.hotmart.com/F106691755G?off=other",
            "source_value": "hermes",
            "sck_value": f"hermes|v1|{ISSUANCE_ULID}",
        }], request=request)

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SupabaseCommittedResponseError):
        asyncio.run(client.reserve_chatwoot_checkout_issuance_v2(
            commercial_case_id="00000000-0000-0000-0000-000000000300",
            external_user_id="12025550123",
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            chatwoot_conversation_id=9101,
            trigger_external_message_id="501",
            issuance_ulid=ISSUANCE_ULID,
            now="2026-09-14T12:00:00+00:00",
        ))


def test_supabase_admits_hermes_sck_purchase_without_legacy_identity_fallback() -> None:
    event_id = "00000000-0000-0000-0000-000000000303"
    issuance_id = "00000000-0000-0000-0000-000000000301"
    intent_id = "00000000-0000-0000-0000-000000000302"
    case_id = "00000000-0000-0000-0000-000000000300"
    payload = {"data": {"purchase": {"origin": {
        "sck": f"hermes|v1|{ISSUANCE_ULID}"
    }}}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("admit_and_correlate_hotmart_checkout_issuance_v2")
        body = json.loads(request.content)
        assert body["p_payload"] == payload
        assert body["p_sck_value"] == f"hermes|v1|{ISSUANCE_ULID}"
        return httpx.Response(200, json=[{
            "admission_outcome": "inserted",
            "webhook_event_id": event_id,
            "correlation_outcome": "matched",
            "issuance_id": issuance_id,
            "purchase_intent_id": intent_id,
            "commercial_case_id": case_id,
        }], request=request)

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client.admit_and_correlate_hotmart_checkout_issuance_v2(
        external_event_id="hotmart-event-1",
        payload=payload,
        sck_value=f"hermes|v1|{ISSUANCE_ULID}",
        now="2026-09-14T12:02:00+00:00",
    ))
    assert result.admission_outcome == "inserted"
    assert result.correlation_outcome == "matched"
    assert result.issuance_id == issuance_id


def test_supabase_accepts_purchase_already_approved_with_durable_ids() -> None:
    event_id = "00000000-0000-0000-0000-000000000303"
    issuance_id = "00000000-0000-0000-0000-000000000301"
    intent_id = "00000000-0000-0000-0000-000000000302"
    case_id = "00000000-0000-0000-0000-000000000300"
    payload = {"data": {"purchase": {"origin": {
        "sck": f"hermes|v1|{ISSUANCE_ULID}"
    }}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "admission_outcome": "duplicate",
            "webhook_event_id": event_id,
            "correlation_outcome": "purchase_already_approved",
            "issuance_id": issuance_id,
            "purchase_intent_id": intent_id,
            "commercial_case_id": case_id,
        }], request=request)

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client.admit_and_correlate_hotmart_checkout_issuance_v2(
        external_event_id="hotmart-event-1",
        payload=payload,
        sck_value=f"hermes|v1|{ISSUANCE_ULID}",
        now="2026-09-14T12:02:00+00:00",
    ))
    assert result.correlation_outcome == "purchase_already_approved"
    assert result.issuance_id == issuance_id
    assert result.purchase_intent_id == intent_id
    assert result.commercial_case_id == case_id


def test_supabase_authorizes_and_finalizes_checkout_issuance() -> None:
    issuance_id = "00000000-0000-0000-0000-000000000301"
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("authorize_chatwoot_checkout_issuance_v2"):
            return httpx.Response(200, json=[{
                "outcome": "request_started",
                "issuance_id": issuance_id,
                "status": "request_started",
            }], request=request)
        if request.url.path.endswith("finalize_chatwoot_checkout_issuance_v2"):
            return httpx.Response(200, json=[{
                "outcome": "finalized",
                "issuance_id": issuance_id,
                "status": "accepted_by_chatwoot",
            }], request=request)
        raise AssertionError(f"unexpected RPC: {request.url.path}")

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    authorized = asyncio.run(client.authorize_chatwoot_checkout_issuance_v2(
        issuance_id=issuance_id,
        external_user_id="12025550123",
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        chatwoot_conversation_id=9101,
        trigger_external_message_id="501",
        now="2026-09-14T12:00:30+00:00",
    ))
    finalized = asyncio.run(client.finalize_chatwoot_checkout_issuance_v2(
        issuance_id=issuance_id,
        status="accepted_by_chatwoot",
        chatwoot_message_id=7001,
        failure_code=None,
        now="2026-09-14T12:01:00+00:00",
    ))

    assert authorized.outcome == "request_started"
    assert finalized.status == "accepted_by_chatwoot"
    assert paths == [
        "/rest/v1/rpc/authorize_chatwoot_checkout_issuance_v2",
        "/rest/v1/rpc/finalize_chatwoot_checkout_issuance_v2",
    ]
