"""HTTP contract tests for POST /webhooks/lead."""

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from bridge.app import Settings, create_app

SECRET = "fixture-lead-secret"


class _FakeSupabase:
    def __init__(self, outcome: str = "inserted") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def admit_observed_lead_precheckout(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            outcome=self.outcome,
            submission_id="bfc778e7-5c9f-45e6-a910-651f92312157",
            purchase_intent_id="1f581f3a-c469-45da-8208-9483d1b26f0b",
        )


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "webhook_secret": "unused",
        "allowed_jid": "12025550123@s.whatsapp.net",
        "capture_dir": "/tmp/unused",
        "max_age_seconds": 300,
        "lead_precheckout_enabled": True,
        "lead_precheckout_secret": SECRET,
        "lead_precheckout_max_age_seconds": 300,
        "lead_precheckout_site": "psicologajohanna",
        "lead_precheckout_landing_id": "ads-a",
        "lead_precheckout_offer_code": "bxjge6zq",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _payload() -> dict[str, object]:
    return {
        "id": "01K3F8QW7N2VYB4M6X9CDPTZRA",
        "event": "lead.precheckout",
        "version": "1.0.0",
        "created_at": datetime.now(UTC).isoformat(),
        "source": {
            "system": "landing",
            "site": "psicologajohanna",
            "aliado": "Psicologa Johanna",
            "landing_id": "ads-a",
            "page_url": "https://psicologajohanna.com/ldla/evg/vsl/ads-a",
        },
        "data": {
            "buyer": {
                "name": "Test Person",
                "email": "test.person@example.com",
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
            "checkout_url": "https://pay.hotmart.com/F106691755G?off=bxjge6zq&checkoutMode=10",
            "checkout_country": {"iso": "US", "source": "phone_country_code"},
            "attribution": {
                "utm_source": "",
                "utm_medium": "",
                "utm_campaign": "",
                "utm_content": "",
                "utm_term": "",
                "sck": "",
                "fbclid": "",
                "referrer": "",
            },
            "consent": {
                "marketing_optin": False,
                "notice": "sin consentimiento explicito - dato entregado para completar una compra",
            },
        },
        "dedupe_key": "psicologajohanna:bxjge6zq:test.person@example.com",
    }


def _authorized_payload() -> dict[str, object]:
    payload = _payload()
    payload["version"] = "1.1.0"
    payload["data"]["buyer"]["phone"] = "+12025550123"  # type: ignore[index]
    payload["data"]["consent"] = {  # type: ignore[index]
        "marketing_optin": True,
        "whatsapp_contact": True,
        "copy_version": "johanna-precheckout-whatsapp-disclosure-v1",
    }
    return payload


def _post(
    app: object,
    payload: object,
    *,
    secret: str = SECRET,
    delivery: str = "01K3F8QW7N2VYB4M6X9CDPTZRA",
    event: str = "lead.precheckout",
    signature_override: str | None = None,
    content_type: str | None = "application/json; charset=utf-8",
    user_agent: str | None = "lancemos-lead-relay/1.0",
) -> httpx.Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        headers = {
            "X-Lancemos-Event": event,
            "X-Lancemos-Delivery": delivery,
            "X-Lancemos-Signature": signature_override or signature,
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        if user_agent is not None:
            headers["User-Agent"] = user_agent
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/webhooks/lead",
                content=body,
                headers=headers,
            )

    return asyncio.run(send())


def test_signed_scoped_event_is_durably_admitted_without_outbound_authority() -> None:
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, _payload())

    assert response.status_code == 200
    assert response.json() == {
        "status": "received",
        "delivery_id": "01K3F8QW7N2VYB4M6X9CDPTZRA",
        "purchase_intent_id": "1f581f3a-c469-45da-8208-9483d1b26f0b",
        "activation_authorized": False,
        "contact_authorized": False,
    }
    assert len(supabase.calls) == 1


def test_v1_1_signed_consent_reaches_canonical_admission_but_response_stays_closed() -> None:
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, _authorized_payload())

    assert response.status_code == 200
    assert response.json()["activation_authorized"] is False
    assert response.json()["contact_authorized"] is False
    canonical = supabase.calls[0]["canonical_payload"]
    assert isinstance(canonical, dict)
    assert canonical["contract_version"] == "1.1.0"
    assert canonical["consent"]["copy_version"] == (  # type: ignore[index]
        "johanna-precheckout-whatsapp-disclosure-v1"
    )
    assert canonical["assurance"]["activation_authorized"] is True  # type: ignore[index]


def test_contract_version_with_whitespace_is_rejected_before_rpc() -> None:
    for version in ("1.0.0 ", "1.1.0 "):
        supabase = _FakeSupabase()
        app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]
        payload = _authorized_payload() if version.startswith("1.1.0") else _payload()
        payload["version"] = version

        response = _post(app, payload)

        assert response.status_code == 400
        assert response.json()["detail"] == "invalid_lead_precheckout_payload"
        assert supabase.calls == []


def test_equivalent_float_price_reaches_rpc_in_canonical_form() -> None:
    payload = _payload()
    payload["data"]["product"]["price"] = 49.0  # type: ignore[index]
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, payload)

    assert response.status_code == 200
    canonical = supabase.calls[0]["canonical_payload"]
    assert isinstance(canonical, dict)
    assert canonical["commerce"]["price"] == "49"  # type: ignore[index]


def test_invalid_signature_is_rejected_before_json_or_persistence() -> None:
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, {"not": "the contract"}, signature_override="sha256=" + "0" * 64)

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_lead_signature"
    assert supabase.calls == []


def test_invalid_or_missing_transport_headers_are_rejected_before_persistence() -> None:
    cases = (
        {"content_type": None},
        {"content_type": "text/plain; charset=utf-8"},
        {"content_type": "application/json; charset=latin-1"},
        {"user_agent": None},
        {"user_agent": "unexpected/9"},
    )
    for overrides in cases:
        supabase = _FakeSupabase()
        app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

        response = _post(app, _payload(), **overrides)  # type: ignore[arg-type]

        assert response.status_code == 400
        assert response.json()["detail"] == "invalid_lead_transport_headers"
        assert supabase.calls == []


def test_delivery_and_event_headers_must_match_signed_body() -> None:
    for header, value in (("delivery", "01K3F8QW7N2VYB4M6X9CDPTZRB"), ("event", "purchase.approved")):
        supabase = _FakeSupabase()
        app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]
        kwargs = {header: value}

        response = _post(app, _payload(), **kwargs)  # type: ignore[arg-type]

        assert response.status_code == 400
        assert response.json()["detail"] == "lead_header_payload_mismatch"
        assert supabase.calls == []


def test_unscoped_offer_is_rejected_as_invalid_before_persistence() -> None:
    payload = _payload()
    payload["source"]["landing_id"] = "org-b"  # type: ignore[index]
    payload["source"]["page_url"] = "https://psicologajohanna.com/ldla/evg/vsl/org-b"  # type: ignore[index]
    payload["data"]["offer"]["code"] = "ecyu87q0"  # type: ignore[index]
    payload["data"]["checkout_url"] = "https://pay.hotmart.com/F106691755G?off=ecyu87q0"  # type: ignore[index]
    payload["dedupe_key"] = "psicologajohanna:ecyu87q0:test.person@example.com"
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, payload)

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_lead_precheckout_payload"
    assert supabase.calls == []


def test_stale_signed_event_is_rejected() -> None:
    payload = _payload()
    payload["created_at"] = "2026-08-18T00:00:00Z"
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, payload)

    assert response.status_code == 401
    assert response.json()["detail"] == "stale_lead_precheckout"
    assert supabase.calls == []


def test_receiver_is_default_off() -> None:
    supabase = _FakeSupabase()
    app = create_app(_settings(lead_precheckout_enabled=False), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, _payload())

    assert response.status_code == 503
    assert response.json()["detail"] == "lead_precheckout_not_enabled"
    assert supabase.calls == []


def test_enabled_receiver_without_secret_fails_at_startup() -> None:
    with pytest.raises(ValueError, match="LEAD_PRECHECKOUT_SECRET is required"):
        create_app(_settings(lead_precheckout_secret=None))


def test_enabled_receiver_rejects_scope_expansion_at_startup() -> None:
    with pytest.raises(ValueError, match="scope must match commercial ally config"):
        create_app(
            _settings(
                lead_precheckout_landing_id="org-b",
                lead_precheckout_offer_code="ecyu87q0",
            )
        )


def test_duplicate_and_conflict_are_terminal_200_responses() -> None:
    for outcome, expected_status in (("duplicate", "duplicate"), ("semantic_conflict", "conflict")):
        supabase = _FakeSupabase(outcome)
        app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

        response = _post(app, _payload())

        assert response.status_code == 200
        assert response.json()["status"] == expected_status
        assert response.json()["activation_authorized"] is False
        assert response.json()["contact_authorized"] is False
