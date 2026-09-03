import asyncio
from copy import deepcopy
from dataclasses import fields, replace
from decimal import Decimal
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import Settings, create_app
from bridge.commercial_ally import CommercialAllyConfig, JOHANNA_COMMERCIAL_ALLY
from bridge.hotmart import parse_hotmart_payment_failure_payload, parse_hotmart_payload
from bridge.lead_precheckout import parse_lead_precheckout
from bridge.supabase import SupabaseClient


def _config() -> CommercialAllyConfig:
    return CommercialAllyConfig(
        tenant_ref="att1",
        funnel_ref="att1-main",
        binding_version=1,
        ally_ref="ally-one",
        lead_ally_name="Ally One",
        lead_site="ally-one-site",
        lead_landing_id="main",
        lead_page_host="ally-one.example",
        lead_page_path="/offer/main",
        product_hotlink="ATT1HOTLINK",
        product_name="ATT1 Offer",
        product_price=Decimal("49"),
        currency="USD",
        offer_code="att1offer",
        consent_copy_version="att1-whatsapp-v1",
        hotmart_product_id=123456,
        chatwoot_account_id=42,
        chatwoot_inbox_id=24,
        inbound_scope_key="att1-inbound",
        inbound_scope_version=1,
    )


def _write_johanna_value_manifest(path: Path) -> None:
    payload = {
        field.name: getattr(JOHANNA_COMMERCIAL_ALLY, field.name)
        for field in fields(CommercialAllyConfig)
    }
    payload["product_price"] = str(payload["product_price"])
    path.write_text(json.dumps(payload), encoding="utf-8")


def _configure_manifest_environment(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
) -> None:
    environment = {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
        "COMMERCIAL_ALLY_CONFIG_PATH": str(path),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)


def _lead_payload() -> dict[str, object]:
    return {
        "id": "01K3F8QW7N2VYB4M6X9CDPTZRA",
        "event": "lead.precheckout",
        "version": "1.1.0",
        "created_at": "2026-09-01T12:00:00Z",
        "source": {
            "system": "landing",
            "site": "ally-one-site",
            "aliado": "Ally One",
            "landing_id": "main",
            "page_url": "https://ally-one.example/offer/main",
        },
        "data": {
            "buyer": {
                "name": "Test Buyer",
                "email": "buyer@example.test",
                "phone": "+12025550123",
                "phone_country_code": "1",
                "phone_national": "2025550123",
            },
            "product": {
                "hotlink": "ATT1HOTLINK",
                "id": None,
                "name": "ATT1 Offer",
                "price": 49,
                "currency": "USD",
            },
            "offer": {"code": "att1offer"},
            "checkout_url": (
                "https://pay.hotmart.com/ATT1HOTLINK"
                "?off=att1offer&checkoutMode=10"
            ),
            "checkout_country": {"iso": "US", "source": "phone_country_code"},
            "attribution": {
                "utm_source": "test",
                "utm_medium": "test",
                "utm_campaign": "test",
                "utm_content": "test",
                "utm_term": "",
                "sck": "test.test.test",
                "fbclid": "fixture",
                "referrer": "https://example.test/",
            },
            "consent": {
                "marketing_optin": True,
                "whatsapp_contact": True,
                "copy_version": "att1-whatsapp-v1",
            },
        },
        "dedupe_key": "ally-one-site:att1offer:buyer@example.test",
    }


def _payment_failure_payload() -> dict[str, object]:
    return {
        "id": "event-att1-payment-failure",
        "event": "PURCHASE_CANCELED",
        "version": "2.0.0",
        "creation_date": 1788264000000,
        "data": {
            "buyer": {
                "email": "buyer@example.test",
                "checkout_phone": "+12025550123",
            },
            "product": {"id": 123456, "ucode": "ATT1-UCODE"},
            "purchase": {
                "transaction": "HPATT1123456",
                "status": "CANCELED",
                "offer": {"code": "att1offer"},
                "payment": {},
            },
        },
    }


def test_att1_config_accepts_its_lead_and_emits_att1_canonical_scope() -> None:
    parsed = parse_lead_precheckout(_lead_payload(), config=_config())

    assert parsed is not None
    assert parsed.product_hotlink == "ATT1HOTLINK"
    assert parsed.consent_copy_version == "att1-whatsapp-v1"
    canonical = parsed.as_canonical_payload()
    assert canonical["source"]["tenant_ref"] == "att1"  # type: ignore[index]
    assert canonical["source"]["funnel_ref"] == "att1-main"  # type: ignore[index]


def test_att1_config_rejects_cross_customer_lead_scope() -> None:
    payload = _lead_payload()
    payload["source"]["site"] = "psicologajohanna"  # type: ignore[index]

    assert parse_lead_precheckout(payload, config=_config()) is None
    assert parse_lead_precheckout(_lead_payload()) is None


def test_att1_config_accepts_only_its_hotmart_payment_failure_scope() -> None:
    payload = _payment_failure_payload()

    parsed = parse_hotmart_payment_failure_payload(payload, config=_config())

    assert parsed is not None
    assert parsed.product_id == 123456
    assert parsed.offer_code == "att1offer"
    assert parse_hotmart_payment_failure_payload(payload) is None

    wrong_offer = deepcopy(payload)
    wrong_offer["data"]["purchase"]["offer"]["code"] = "other"  # type: ignore[index]
    assert parse_hotmart_payment_failure_payload(wrong_offer, config=_config()) is None


def test_att1_config_accepts_only_its_hotmart_cart_abandonment_scope() -> None:
    payload = {
        "id": "event-att1-cart-abandonment",
        "event": "PURCHASE_OUT_OF_SHOPPING_CART",
        "version": "2.0.0",
        "creation_date": 1788264000000,
        "data": {
            "buyer": {
                "name": "Test Buyer",
                "email": "buyer@example.test",
                "phone": "+120****0123",
            },
            "product": {"id": 123456, "name": "ATT1 Offer"},
            "offer": {"code": "att1offer"},
            "checkout_country": {"iso": "MX", "name": "México"},
        },
    }

    parsed = parse_hotmart_payload(payload, config=_config())

    assert parsed is not None
    assert parsed.product_id == 123456
    assert parsed.offer_code == "att1offer"

    wrong_offer = deepcopy(payload)
    wrong_offer["data"]["offer"]["code"] = "other"  # type: ignore[index]
    assert parse_hotmart_payload(wrong_offer, config=_config()) is None


def test_app_factory_allows_only_portable_lead_admission_for_att1() -> None:
    config = _config()
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/att1-portability-captures"),
        max_age_seconds=300,
        commercial_ally_config=config,
        commercial_ally_manifest_path=Path("/runtime/commercial-ally.json"),
        lead_precheckout_enabled=True,
        lead_precheckout_secret="test-lead-secret",
        lead_precheckout_site=config.lead_site,
        lead_precheckout_landing_id=config.lead_landing_id,
        lead_precheckout_offer_code=config.offer_code,
    )

    app = create_app(settings, supabase_client=object())  # type: ignore[arg-type]

    assert app is not None


LEGACY_ONLY_CAPABILITIES = {
    "hotmart_abandonment_timer_worker_enabled",
    "precheckout_form_enabled",
    "precheckout_test_mode_enabled",
    "precheckout_first_touch_enabled",
    "precheckout_delayed_first_touch_enabled",
    "precheckout_delayed_outbound_enabled",
    "johanna_abandonment_one_shot_enabled",
    "johanna_abandonment_hotmart_auto_enabled",
    "johanna_payment_failure_hotmart_enabled",
    "johanna_payment_failure_outbound_enabled",
}


@pytest.mark.parametrize("enabled_capability", sorted(LEGACY_ONLY_CAPABILITIES))
def test_att1_runtime_rejects_legacy_only_effect_or_admission_capability(
    enabled_capability: str,
) -> None:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/att1-portability-captures"),
        max_age_seconds=300,
        commercial_ally_config=_config(),
        commercial_ally_manifest_path=Path("/runtime/commercial-ally.json"),
    )
    settings = replace(settings, **{enabled_capability: True})

    with pytest.raises(ValueError, match="ATT1 runtime capabilities are not portable"):
        create_app(settings)


@pytest.mark.parametrize(
    "enabled_capability",
    [
        field.name
        for field in fields(Settings)
        if field.type in (bool, "bool")
        and field.name not in LEGACY_ONLY_CAPABILITIES
    ],
)
def test_att1_runtime_does_not_reject_generic_capability_as_unportable(
    enabled_capability: str,
) -> None:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/att1-portability-captures"),
        max_age_seconds=300,
        commercial_ally_config=_config(),
        commercial_ally_manifest_path=Path("/runtime/commercial-ally.json"),
    )
    settings = replace(settings, **{enabled_capability: True})

    try:
        create_app(settings)
    except ValueError as exc:
        assert "ATT1 runtime capabilities are not portable" not in str(exc)


def test_att1_runtime_rejects_generic_hotmart_admission() -> None:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/att1-portability-captures"),
        max_age_seconds=300,
        commercial_ally_config=_config(),
        hotmart_hottok="configured-test-token",
    )

    with pytest.raises(ValueError, match="ATT1 runtime capabilities are not portable"):
        create_app(settings)


def test_supplied_manifest_cannot_impersonate_legacy_values_to_enable_hotmart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "commercial-ally.json"
    _write_johanna_value_manifest(manifest)
    _configure_manifest_environment(monkeypatch, manifest)
    monkeypatch.setenv("HOTMART_HOTTOK", "configured-test-token")

    settings = Settings.from_env()

    with pytest.raises(ValueError, match="ATT1 runtime capabilities are not portable"):
        create_app(settings)


def test_supplied_manifest_with_legacy_values_still_requires_durable_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "commercial-ally.json"
    _write_johanna_value_manifest(manifest)
    _configure_manifest_environment(monkeypatch, manifest)

    settings = Settings.from_env()

    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "commercial_ally_binding_unavailable"}


@pytest.mark.parametrize(
    "boolean_field",
    [field.name for field in fields(Settings) if field.type in (bool, "bool")],
)
def test_settings_reject_every_non_boolean_value_for_boolean_fields(
    boolean_field: str,
) -> None:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/att1-portability-captures"),
        max_age_seconds=300,
        commercial_ally_config=_config(),
    )
    settings = replace(settings, **{boolean_field: 1})

    with pytest.raises(ValueError, match="Settings boolean fields must be bool"):
        create_app(settings)


@pytest.mark.parametrize("invalid_value", ["true", None, [], object()])
def test_settings_reject_representative_non_boolean_truthy_and_falsey_values(
    invalid_value: object,
) -> None:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/att1-portability-captures"),
        max_age_seconds=300,
        commercial_ally_config=_config(),
    )
    settings = replace(settings, precheckout_form_enabled=invalid_value)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Settings boolean fields must be bool"):
        create_app(settings)


def test_scoped_inbound_gate_reaches_its_stop_and_handoff_dependency_check() -> None:
    config = _config()
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/att1-portability-captures"),
        max_age_seconds=300,
        commercial_ally_config=config,
        commercial_ally_manifest_path=Path("/runtime/commercial-ally.json"),
        chatwoot_account_id=config.chatwoot_account_id,
        chatwoot_inbox_id=config.chatwoot_inbox_id,
        chatwoot_cut_b_scope_key=config.inbound_scope_key,
        chatwoot_cut_b_scope_version=config.inbound_scope_version,
        chatwoot_scoped_inbound_senders_enabled=True,
    )

    with pytest.raises(
        ValueError,
        match="requires all stop and handoff gates",
    ):
        create_app(settings)


def test_supabase_resolves_the_exact_active_att1_binding() -> None:
    config = _config()
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        row = {
            field: getattr(config, field)
            for field in config.__dataclass_fields__
            if field != "product_price"
        }
        row["product_price"] = "49.00"
        row["status"] = "active"
        row["created_at"] = "2026-09-01T00:00:00+00:00"
        row["updated_at"] = "2026-09-01T00:00:00+00:00"
        return httpx.Response(200, json=[row])

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="test-secret",
        transport=httpx.MockTransport(handler),
    )

    resolved = asyncio.run(client.resolve_commercial_ally_runtime_binding(config))

    assert resolved == config
    assert seen == [{
        "p_tenant_ref": "att1",
        "p_funnel_ref": "att1-main",
        "p_binding_version": 1,
    }]


def test_supabase_portable_lead_admission_sends_server_owned_binding_identity() -> None:
    config = _config()
    seen: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=[{
            "outcome": "inserted",
            "submission_id": "bfc778e7-5c9f-45e6-a910-651f92312157",
            "purchase_intent_id": "1f581f3a-c469-45da-8208-9483d1b26f0b",
        }])

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="test-secret",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(client.admit_portable_observed_lead_precheckout(
        config=config,
        external_submission_id="submission-att1",
        raw_payload={"id": "submission-att1"},
        canonical_payload={"source": {"tenant_ref": "att1"}},
    ))

    assert seen == [(
        "/rest/v1/rpc/admit_portable_observed_lead_precheckout",
        {
            "p_tenant_ref": "att1",
            "p_funnel_ref": "att1-main",
            "p_binding_version": 1,
            "p_external_submission_id": "submission-att1",
            "p_raw_payload": {"id": "submission-att1"},
            "p_canonical_payload": {"source": {"tenant_ref": "att1"}},
        },
    )]


def test_att1_readiness_fails_closed_without_durable_binding_authority() -> None:
    settings = Settings(
        webhook_secret="test-secret",
        allowed_jid=None,
        capture_dir=Path("/tmp/att1-portability-captures"),
        max_age_seconds=300,
        commercial_ally_config=_config(),
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "commercial_ally_binding_unavailable"}
