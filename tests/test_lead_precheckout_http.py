"""HTTP contract tests for POST /webhooks/lead."""

import asyncio
import hashlib
import hmac
import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from bridge.app import Settings, create_app
from bridge.instance_manifest import InstanceManifest
from bridge.supabase import SupabaseError
from test_instance_wiring import (
    ATT1 as ATT1_INSTANCE,
    _FirstContactAuthority,
    _first_contact_app,
    _first_contact_settings,
)

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
    config = InstanceManifest.from_toml_file(ATT1_MANIFEST).to_commercial_ally_config()
    payload["data"]["product"].update(  # type: ignore[index]
        hotlink=config.product_hotlink, name=config.product_name, price=47
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


# ------------------------------------------- primer contacto tras el formulario
# Con PORTABLE_PRECHECKOUT_FIRST_CONTACT_ENABLED la admision del formulario
# tambien planifica el primer contacto, en una sola RPC. El set completo con el
# que ese flag arranca esta en test_instance_wiring.py. El payload es el inline
# de este archivo con los datos del fixture de ATT1 (no hay lead.precheckout
# capturado de ATT1); el formulario de GHL, con la captura real y la aceptacion
# del riesgo del adaptador, esta en test_ghl_precheckout_adapter_http.py.

FIRST_CONTACT_SECRET = "first-contact-lead-secret"


@pytest.fixture
def att1_instance(tmp_path: Path) -> Path:
    target = tmp_path / "instancia"
    shutil.copytree(ATT1_INSTANCE, target)
    return target


def _first_contact_off(settings: Settings) -> Settings:
    # El mismo runtime con manifiesto sin el flag (y sin lo que solo el flag
    # habilita: el modo directo y la salida durable).
    return replace(
        settings,
        portable_precheckout_first_contact_enabled=False,
        dispatcher_approved_template_direct_enabled=False,
        dispatcher_outbound_enabled=False,
    )


def _post_att1_form(app: object, index: int = 0) -> httpx.Response:
    return _post(app, _att1_payload(*ATT1_OFFERS[index]), secret=FIRST_CONTACT_SECRET)


def test_the_first_contact_flag_admits_and_plans_in_one_rpc(att1_instance: Path) -> None:
    settings = _first_contact_settings(att1_instance)
    authority = _FirstContactAuthority()

    response = _post_att1_form(_first_contact_app(settings, authority))

    assert response.status_code == 200
    assert authority.admission_calls == []
    [call] = authority.plan_calls
    assert call["config"] == settings.commercial_ally_config
    assert call["external_submission_id"] == "01K3F8QW7N2VYB4M6X9CDPTZRA"
    assert (call["scope_key"], call["scope_version"]) == ("att1-primer-contacto", 1)
    assert call["raw_payload"]["event"] == "lead.precheckout"  # type: ignore[index]
    assert call["canonical_payload"]["commerce"]["offer_ref"] == ATT1_OFFERS[0][0]  # type: ignore[index]
    assert call["canonical_payload"]["consent"]["whatsapp_contact"] is True  # type: ignore[index]


def test_without_the_first_contact_flag_the_manifest_runtime_keeps_its_rpc(
    att1_instance: Path,
) -> None:
    settings = _first_contact_off(_first_contact_settings(att1_instance))
    authority = _FirstContactAuthority()

    response = _post_att1_form(_first_contact_app(settings, authority))

    assert response.status_code == 200
    assert authority.plan_calls == []
    [call] = authority.admission_calls
    assert set(call) == {
        "config", "external_submission_id", "raw_payload", "canonical_payload",
    }


@pytest.mark.parametrize(
    "plan",
    [
        {"plan_outcome": "planned", "plan_reason": "first_contact_scheduled"},
        {"plan_outcome": "not_planned", "plan_reason": "pilot_runtime_not_armed"},
        {"plan_outcome": "plan_failed", "plan_reason": "channel_identity_inbox_mismatch"},
    ],
)
def test_the_lead_response_does_not_change_with_the_first_contact_plan(
    att1_instance: Path, plan: dict[str, str]
) -> None:
    # La admision queda igual aunque el plan no proceda: el emisor recibe lo
    # mismo que sin el flag, y nunca un permiso de contacto.
    on = _first_contact_settings(att1_instance)
    authority = _FirstContactAuthority()
    authority.plan = plan

    planned = _post_att1_form(_first_contact_app(on, authority))
    plain = _post_att1_form(
        _first_contact_app(_first_contact_off(on), _FirstContactAuthority())
    )

    assert planned.status_code == plain.status_code == 200
    assert planned.json() == plain.json() == {
        "status": "received",
        "delivery_id": "01K3F8QW7N2VYB4M6X9CDPTZRA",
        "purchase_intent_id": "1f581f3a-c469-45da-8208-9483d1b26f0b",
        "activation_authorized": False,
        "contact_authorized": False,
    }


@pytest.mark.parametrize(
    ("plan_outcome", "level"),
    [("planned", "INFO"), ("not_planned", "INFO"), ("plan_failed", "WARNING")],
)
def test_the_first_contact_plan_is_logged_with_ids_and_codes_only(
    att1_instance: Path,
    caplog: pytest.LogCaptureFixture,
    plan_outcome: str,
    level: str,
) -> None:
    authority = _FirstContactAuthority()
    authority.plan = {"plan_outcome": plan_outcome, "plan_reason": "some_reason_code"}
    app = _first_contact_app(_first_contact_settings(att1_instance), authority)

    with caplog.at_level("INFO", logger="bridge.app"):
        response = _post_att1_form(app)

    assert response.status_code == 200
    [record] = [
        record
        for record in caplog.records
        if record.getMessage().startswith("portable_precheckout_first_contact ")
    ]
    assert record.levelname == level
    assert record.getMessage() == (
        "portable_precheckout_first_contact admission=inserted "
        f"plan={plan_outcome} reason=some_reason_code "
        "submission_id=bfc778e7-5c9f-45e6-a910-651f92312157"
    )
    # Ni el nombre, ni el email, ni el telefono del formulario.
    for personal in ("Test Person", "test.person@example.com", "2025550123"):
        assert personal not in caplog.text


def test_a_failed_admit_and_plan_rpc_is_a_retryable_503(att1_instance: Path) -> None:
    class _Unavailable(_FirstContactAuthority):
        async def admit_and_plan_portable_lead_precheckout(self, **_: object) -> object:
            raise SupabaseError("portable_lead_precheckout_admission_and_plan_failed: HTTP 503")

    app = _first_contact_app(_first_contact_settings(att1_instance), _Unavailable())

    response = _post_att1_form(app)

    assert response.status_code == 503
    assert response.json()["detail"] == "lead_precheckout_persist_unavailable"


def test_a_duplicate_form_without_a_stored_plan_stays_a_terminal_200(
    att1_instance: Path,
) -> None:
    class _Duplicate(_FirstContactAuthority):
        async def admit_and_plan_portable_lead_precheckout(self, **kwargs: object) -> object:
            self.plan_calls.append(kwargs)
            return SimpleNamespace(
                outcome="duplicate",
                submission_id="bfc778e7-5c9f-45e6-a910-651f92312157",
                purchase_intent_id="1f581f3a-c469-45da-8208-9483d1b26f0b",
                plan_outcome=None,
                plan_reason=None,
            )

    authority = _Duplicate()
    app = _first_contact_app(_first_contact_settings(att1_instance), authority)

    response = _post_att1_form(app)

    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert len(authority.plan_calls) == 1
