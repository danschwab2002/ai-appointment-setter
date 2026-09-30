"""HTTP contract tests for POST /webhooks/lead."""

import asyncio
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from bridge.app import Settings, create_app
from bridge.instance_manifest import InstanceManifest

SECRET = "fixture-lead-secret"
ATT1_MANIFEST = Path(__file__).parent / "fixtures" / "instances" / "att1" / "instancia.toml"


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


class _PortableFakeSupabase:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def admit_portable_observed_lead_precheckout(
        self, **kwargs: object
    ) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            outcome="inserted",
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


def test_explicit_manifest_lead_uses_portable_rpc_with_server_binding() -> None:
    supabase = _PortableFakeSupabase()
    app = create_app(
        _settings(commercial_ally_manifest_path="/runtime/commercial-ally.json"),
        supabase_client=supabase,  # type: ignore[arg-type]
    )

    response = _post(app, _payload())

    assert response.status_code == 200
    assert len(supabase.calls) == 1
    assert supabase.calls[0]["config"] == _settings().commercial_ally_config


def _att1_settings(**overrides: object) -> Settings:
    config = InstanceManifest.from_toml_file(ATT1_MANIFEST).to_commercial_ally_config()
    values: dict[str, object] = {
        "commercial_ally_config": config,
        "commercial_ally_manifest_path": "/runtime/instancia.toml",
        "lead_precheckout_site": config.lead_site,
        "lead_precheckout_landing_id": config.lead_landing_id,
        "lead_precheckout_offer_code": config.offer_code,
    }
    values.update(overrides)
    return _settings(**values)


def _att1_payload(offer: str, site: str, landing_id: str, url: str) -> dict[str, object]:
    # No hay lead.precheckout capturado de ATT1: es el payload inline de este
    # archivo con las landings, ofertas y producto del fixture de ATT1.
    payload = _authorized_payload()
    payload["source"].update(  # type: ignore[union-attr]
        site=site, aliado="Dra. Nina Garza", landing_id=landing_id, page_url=url
    )
    payload["data"]["product"].update(  # type: ignore[index]
        hotlink="D98014973Y", name="Alimenta Tu Tiroides", price=47
    )
    payload["data"]["offer"]["code"] = offer  # type: ignore[index]
    payload["data"]["checkout_url"] = (  # type: ignore[index]
        f"https://pay.hotmart.com/D98014973Y?off={offer}&checkoutMode=10"
    )
    payload["data"]["consent"]["copy_version"] = "att1-whatsapp-contact-v1"  # type: ignore[index]
    payload["dedupe_key"] = f"{site}:{offer}:test.person@example.com"
    return payload


ATT1_OFFERS = tuple(
    (offer.code, offer.site, offer.landing_id, offer.url)
    for offer in InstanceManifest.from_toml_file(ATT1_MANIFEST).offers
)


@pytest.mark.parametrize(("offer", "site", "landing_id", "url"), ATT1_OFFERS)
def test_manifest_runtime_admits_the_form_of_every_declared_landing(
    offer: str, site: str, landing_id: str, url: str
) -> None:
    supabase = _PortableFakeSupabase()
    settings = _att1_settings()
    app = create_app(settings, supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, _att1_payload(offer, site, landing_id, url))

    assert response.status_code == 200
    assert response.json()["status"] == "received"
    assert len(supabase.calls) == 1
    assert supabase.calls[0]["config"] == settings.commercial_ally_config
    canonical = supabase.calls[0]["canonical_payload"]
    assert canonical["commerce"]["offer_ref"] == offer  # type: ignore[index]
    assert canonical["source"]["landing_ref"] == landing_id  # type: ignore[index]
    assert canonical["source"]["page_url"] == url  # type: ignore[index]


def test_manifest_runtime_rejects_a_crossed_landing_offer_pair() -> None:
    supabase = _PortableFakeSupabase()
    app = create_app(_att1_settings(), supabase_client=supabase)  # type: ignore[arg-type]
    _, site, landing_id, url = ATT1_OFFERS[1]

    response = _post(app, _att1_payload(ATT1_OFFERS[0][0], site, landing_id, url))

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_lead_precheckout_payload"
    assert supabase.calls == []


def test_manifest_runtime_without_landings_keeps_the_single_form_landing() -> None:
    settings = _att1_settings()
    settings = replace(
        settings,
        commercial_ally_config=replace(
            settings.commercial_ally_config, additional_offer_landings=()
        ),
    )

    for index, expected_status in ((0, 200), (1, 400), (2, 400)):
        supabase = _PortableFakeSupabase()
        app = create_app(settings, supabase_client=supabase)  # type: ignore[arg-type]

        response = _post(app, _att1_payload(*ATT1_OFFERS[index]))

        assert response.status_code == expected_status
        assert len(supabase.calls) == (1 if expected_status == 200 else 0)


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


@pytest.mark.parametrize(
    ("landing_ref", "offer_ref"),
    (
        ("ads-a", "bxjge6zq"),
        ("ads-b", "mgbgpp19"),
        ("ads-c", "s1qfxm7m"),
        ("org-a", "jtt6fcsm"),
        ("org-b", "ecyu87q0"),
        ("org-c", "ulhzpw9a"),
    ),
)
def test_each_published_pair_reaches_durable_admission(
    landing_ref: str, offer_ref: str
) -> None:
    payload = _payload()
    payload["source"]["landing_id"] = landing_ref  # type: ignore[index]
    payload["source"]["page_url"] = (  # type: ignore[index]
        f"https://psicologajohanna.com/ldla/evg/vsl/{landing_ref}"
    )
    payload["data"]["offer"]["code"] = offer_ref  # type: ignore[index]
    payload["data"]["checkout_url"] = (  # type: ignore[index]
        f"https://pay.hotmart.com/F106691755G?off={offer_ref}"
    )
    payload["dedupe_key"] = (
        f"psicologajohanna:{offer_ref}:test.person@example.com"
    )
    supabase = _FakeSupabase()
    app = create_app(_settings(), supabase_client=supabase)  # type: ignore[arg-type]

    response = _post(app, payload)

    assert response.status_code == 200
    assert len(supabase.calls) == 1


def test_unpublished_landing_offer_pair_is_rejected_before_persistence() -> None:
    payload = _payload()
    payload["source"]["landing_id"] = "org-b"  # type: ignore[index]
    payload["source"]["page_url"] = (  # type: ignore[index]
        "https://psicologajohanna.com/ldla/evg/vsl/org-b"
    )
    payload["data"]["offer"]["code"] = "mgbgpp19"  # type: ignore[index]
    payload["data"]["checkout_url"] = (  # type: ignore[index]
        "https://pay.hotmart.com/F106691755G?off=mgbgpp19"
    )
    payload["dedupe_key"] = (
        "psicologajohanna:mgbgpp19:test.person@example.com"
    )
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


class _InferenceFakeSupabase(_FakeSupabase):
    def __init__(self, outcome: str = "inserted") -> None:
        super().__init__(outcome)
        self.stored: dict[str, object] = {}

    async def get_lead_first_name_inference(self, name_key: str) -> object | None:
        return self.stored.get(name_key)

    async def record_lead_first_name_inference(self, **kwargs: object) -> str:
        self.stored[str(kwargs["name_key"])] = kwargs["inference"]
        return "inserted"


def _inference_client(requests: list[httpx.Request]) -> object:
    from bridge.lead_first_name import FirstNameInferenceClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"result":"confident","first_name":"Test"}'}}
                ]
            },
        )

    return FirstNameInferenceClient(
        base_url="http://hermes:8642/v1",
        api_key="test-key",
        model_name="agente-comercial",
        transport=httpx.MockTransport(handler),
    )


def test_admitted_form_infers_the_first_name_after_answering() -> None:
    from bridge.lead_first_name import FirstNameInference, lead_name_key

    supabase = _InferenceFakeSupabase()
    requests: list[httpx.Request] = []
    app = create_app(
        _settings(
            lead_first_name_greeting_enabled=True,
            lead_first_name_inference_enabled=True,
        ),
        supabase_client=supabase,  # type: ignore[arg-type]
        lead_first_name_client=_inference_client(requests),  # type: ignore[arg-type]
    )

    response = _post(app, _payload())

    assert response.status_code == 200
    assert response.json()["status"] == "received"
    assert len(requests) == 1
    assert json.loads(requests[0].content)["messages"][1]["content"] == (
        '{"full_name": "Test Person"}'
    )
    assert supabase.stored == {
        lead_name_key("Test Person"): FirstNameInference("confident", "Test")
    }


@pytest.mark.parametrize(
    ("inference_enabled", "outcome"),
    [(False, "inserted"), (True, "duplicate"), (True, "semantic_conflict")],
)
def test_the_model_is_not_called_when_off_or_when_the_form_was_not_new(
    inference_enabled: bool, outcome: str
) -> None:
    supabase = _InferenceFakeSupabase(outcome)
    requests: list[httpx.Request] = []
    app = create_app(
        _settings(
            lead_first_name_greeting_enabled=True,
            lead_first_name_inference_enabled=inference_enabled,
        ),
        supabase_client=supabase,  # type: ignore[arg-type]
        lead_first_name_client=_inference_client(requests),  # type: ignore[arg-type]
    )

    response = _post(app, _payload())

    assert response.status_code == 200
    assert requests == []
    assert supabase.stored == {}
