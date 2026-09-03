from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx

import bridge.worker as worker_module
from bridge.app import Settings, create_app
from bridge.commercial_ally import CommercialAllyConfig
from bridge.resolution import ResolutionError, resolve_event
from bridge.supabase import PilotBoundaryConfig, SupabaseClient
from bridge.supabase import FollowupExecutionContext
from bridge.worker import DurableDispatcher, ResolutionWorker


def _att1_config() -> CommercialAllyConfig:
    return CommercialAllyConfig(
        tenant_ref="att1",
        funnel_ref="att1-main",
        binding_version=1,
        ally_ref="att1",
        lead_ally_name="Alimenta Tu Tiroides",
        lead_site="att1-site",
        lead_landing_id="main",
        lead_page_host="att1.example.test",
        lead_page_path="/offer/main",
        product_hotlink="ATT1HOTLINK",
        product_name="Alimenta Tu Tiroides",
        product_price=Decimal("47"),
        currency="USD",
        offer_code="att1offer",
        consent_copy_version="att1-consent-v1",
        hotmart_product_id=123456,
        chatwoot_account_id=42,
        chatwoot_inbox_id=24,
        inbound_scope_key="att1-inbound",
        inbound_scope_version=1,
    )


def _portable_worker_settings(config: CommercialAllyConfig) -> Settings:
    return Settings(
        webhook_secret="s" * 32,
        allowed_jid=None,
        capture_dir=Path("/tmp/att1-capture-test"),
        max_age_seconds=300,
        commercial_ally_config=config,
        commercial_ally_manifest_path=Path("/runtime/att1.json"),
        portable_hotmart_recovery_enabled=True,
        worker_enabled=True,
        supabase_base_url="https://supabase.example.test",
        supabase_service_role_key="k" * 32,
        chatwoot_account_id=42,
        chatwoot_inbox_id=24,
        followup_policy_key="att1-cart-recovery",
        followup_policy_version=1,
        pilot_boundary_enabled=True,
        pilot_scope_key="att1-cart-recovery",
        pilot_scope_version=1,
        pilot_tenant_key="att1",
        pilot_channel_provider="waba",
        pilot_channel_account_ref="chatwoot-inbox:24",
    )


def test_resolution_worker_passes_explicit_ally_manifest_to_resolver(monkeypatch) -> None:
    config = _att1_config()
    observed: dict[str, object] = {}

    async def fake_resolve_event(**kwargs: object) -> object:
        observed.update(kwargs)
        return SimpleNamespace(event_id="00000000-0000-4000-8000-000000000001")

    monkeypatch.setattr("bridge.worker.resolve_event", fake_resolve_event)
    worker = ResolutionWorker(
        supabase=object(),  # type: ignore[arg-type]
        policy_key="att1-cart-recovery",
        policy_version=1,
        commercial_ally_config=config,
    )
    asyncio.run(worker._process_one({
        "id": "00000000-0000-4000-8000-000000000001",
        "event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
        "payload": {"opaque": "test"},
    }))

    assert observed["commercial_ally_config"] is config


def test_resolver_passes_explicit_ally_manifest_to_cart_parser(monkeypatch) -> None:
    config = _att1_config()
    observed: dict[str, object] = {}

    def fake_parse(payload: object, *, config: CommercialAllyConfig | None = None):
        observed["payload"] = payload
        observed["config"] = config
        return None

    class SupabaseStub:
        async def update_event_status(self, **kwargs: object) -> None:
            observed["status"] = kwargs

    monkeypatch.setattr("bridge.resolution.parse_hotmart_payload", fake_parse)
    try:
        asyncio.run(resolve_event(
            webhook_event_id="00000000-0000-4000-8000-000000000001",
            payload={"opaque": "test"},
            supabase=SupabaseStub(),  # type: ignore[arg-type]
            commercial_ally_config=config,
        ))
    except ResolutionError as exc:
        assert str(exc) == "invalid_payload_structure"
    else:
        raise AssertionError("invalid parser result was accepted")

    assert observed["config"] is config
    assert observed["status"] == {
        "event_id": "00000000-0000-4000-8000-000000000001",
        "status": "failed",
        "error": "invalid_payload_structure",
    }


def test_app_injects_explicit_ally_manifest_into_resolution_worker() -> None:
    config = _att1_config()
    settings = _portable_worker_settings(config)

    app = create_app(settings, supabase_client=object())  # type: ignore[arg-type]

    assert app.state.resolution_worker._commercial_ally_config is config


def test_app_builds_portable_dynamic_sender_behind_closed_meta_gate() -> None:
    settings = replace(
        _portable_worker_settings(_att1_config()),
        dispatcher_enabled=True,
        dispatcher_outbound_enabled=True,
        dispatcher_worker_id="att1-dispatcher",
        hermes_api_base_url="https://hermes.example.test",
        hermes_api_key="h" * 32,
        chatwoot_base_url="https://chatwoot.example.test",
        chatwoot_control_api_access_token="c" * 32,
        chatwoot_pause_macro_id=1,
        waba_first_touch_template_name="att1_initial",
        waba_followup_template_name="att1_followup",
        waba_template_language="es_MX",
        waba_template_category="MARKETING",
    )

    app = create_app(settings, supabase_client=object())  # type: ignore[arg-type]
    dispatcher = app.state.durable_dispatcher

    assert dispatcher is not None
    assert dispatcher._sender is not None
    assert dispatcher._sender._allowed_jid is None
    assert dispatcher._sender._dynamic_recipient_enabled is True
    assert dispatcher._final_meta_effect_gate is not None
    assert dispatcher._final_meta_effect_gate._enabled is False


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"pilot_boundary_enabled": False},
            "RESOLUTION_WORKER_ENABLED requires LANCEMOS_PILOT_BOUNDARY_ENABLED",
        ),
        ({"pilot_tenant_key": "other-tenant"}, "portable resolution worker"),
        ({"chatwoot_account_id": 99}, "portable resolution worker"),
        ({"chatwoot_inbox_id": 99}, "portable resolution worker"),
        ({"pilot_channel_account_ref": "chatwoot-inbox:99"}, "portable resolution worker"),
    ],
)
def test_portable_resolution_worker_rejects_scope_drift(
    changes: dict[str, object],
    message: str,
) -> None:
    settings = replace(_portable_worker_settings(_att1_config()), **changes)

    with pytest.raises(ValueError, match=message):
        create_app(settings, supabase_client=object())  # type: ignore[arg-type]


def test_portable_resolution_worker_reaches_real_parser_and_planner() -> None:
    config = _att1_config()
    calls: dict[str, object] = {}

    class SupabaseStub:
        async def find_contact_by_email(self, _value: str):
            return None

        async def find_contact_by_phone(self, _value: str):
            return None

        async def create_contact(self, **_kwargs: object) -> str:
            return "00000000-0000-4000-8000-000000000010"

        async def create_contact_point(self, **_kwargs: object) -> None:
            return None

        async def plan_cart_recovery(self, **kwargs: object) -> object:
            calls.update(kwargs)
            return SimpleNamespace(
                recovery_case_id="00000000-0000-4000-8000-000000000011"
            )

        async def fetch_conversations(self, **_kwargs: object) -> list[object]:
            return []

        async def fetch_recovery_cases(self, **_kwargs: object) -> list[object]:
            return []

        async def fetch_channel_identities(self, **_kwargs: object) -> list[object]:
            return []

        async def update_event_status(self, **_kwargs: object) -> None:
            return None

    payload: dict[str, object] = {
        "id": "att1-cart-event-1",
        "creation_date": 1788377100000,
        "event": "PURCHASE_OUT_OF_SHOPPING_CART",
        "version": "2.0.0",
        "data": {
            "affiliate": False,
            "product": {"id": 123456, "name": "Alimenta Tu Tiroides"},
            "buyer": {
                "name": "Compradora de prueba",
                "email": "buyer@example.test",
                "phone": "+5215550100999",
            },
            "offer": {"code": "att1offer"},
            "checkout_country": {"name": "México", "iso": "MX"},
        },
    }
    boundary = PilotBoundaryConfig(
        scope_key="att1-cart-recovery",
        scope_version=1,
        tenant_key="att1",
        channel_provider="waba",
        channel_account_ref="chatwoot-inbox:24",
    )

    worker = ResolutionWorker(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        policy_key="att1-cart-recovery",
        policy_version=1,
        allowed_jid=None,
        chatwoot_account_id=42,
        chatwoot_inbox_id=24,
        pilot_boundary=boundary,
        commercial_ally_config=config,
    )
    asyncio.run(worker._process_one({
        "id": "00000000-0000-4000-8000-000000000001",
        "event_type": "PURCHASE_OUT_OF_SHOPPING_CART",
        "payload": payload,
    }))

    assert calls["chatwoot_account_id"] == 42
    assert calls["chatwoot_inbox_id"] == 24
    assert calls["external_user_id"] == "5215550100999"
    assert calls["pilot_boundary"] is boundary


def test_portable_resolution_worker_plans_payment_failure() -> None:
    config = _att1_config()
    calls: dict[str, object] = {}

    class SupabaseStub:
        async def find_contact_by_email(self, _value: str):
            return None

        async def find_contact_by_phone(self, _value: str):
            return None

        async def create_contact(self, **_kwargs: object) -> str:
            return "00000000-0000-4000-8000-000000000010"

        async def create_contact_point(self, **_kwargs: object) -> None:
            return None

        async def plan_payment_failure_recovery(self, **kwargs: object) -> object:
            calls.update(kwargs)
            return SimpleNamespace(
                recovery_case_id="00000000-0000-4000-8000-000000000011"
            )

        async def fetch_conversations(self, **_kwargs: object) -> list[object]:
            return []

        async def fetch_recovery_cases(self, **_kwargs: object) -> list[object]:
            return []

        async def fetch_channel_identities(self, **_kwargs: object) -> list[object]:
            return []

        async def update_event_status(self, **_kwargs: object) -> None:
            return None

    payload: dict[str, object] = {
        "id": "att1-payment-failure-1",
        "creation_date": 1788377100000,
        "event": "PURCHASE_CANCELED",
        "version": "2.0.0",
        "data": {
            "product": {"id": 123456, "name": "Alimenta Tu Tiroides"},
            "buyer": {
                "name": "Compradora de prueba",
                "email": "buyer@example.test",
                "phone": "+5215550100999",
            },
            "checkout_country": {"name": "México", "iso": "MX"},
            "purchase": {
                "transaction": "HPFAIL001",
                "status": "CANCELED",
                "offer": {"code": "att1offer"},
            },
        },
    }
    boundary = PilotBoundaryConfig(
        scope_key="att1-payment-failure",
        scope_version=1,
        tenant_key="att1",
        channel_provider="waba",
        channel_account_ref="chatwoot-inbox:24",
    )
    worker = ResolutionWorker(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        policy_key="att1-payment-failure",
        policy_version=1,
        allowed_jid=None,
        chatwoot_account_id=42,
        chatwoot_inbox_id=24,
        pilot_boundary=boundary,
        commercial_ally_config=config,
        payment_failure_enabled=True,
    )

    asyncio.run(worker._process_one({
        "id": "00000000-0000-4000-8000-000000000001",
        "event_type": "PURCHASE_CANCELED",
        "payload": payload,
    }))

    assert calls["external_product_id"] == "123456"
    assert calls["product_name"] == "Alimenta Tu Tiroides"
    assert calls["offer_code"] == "att1offer"
    assert calls["external_user_id"] == "5215550100999"
    assert calls["pilot_boundary"] is boundary


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"portable_recipient_enabled": False}, "portable recipient capability"),
        ({"pilot_boundary": None}, "portable recipient boundary"),
        ({"chatwoot_account_id": None}, "portable recipient Chatwoot account"),
        ({"chatwoot_inbox_id": None}, "portable recipient Chatwoot inbox"),
        (
            {"pilot_boundary": PilotBoundaryConfig(
                scope_key="att1-cart-recovery",
                scope_version=1,
                tenant_key="other-tenant",
                channel_provider="waba",
                channel_account_ref="chatwoot-inbox:24",
            )},
            "portable recipient tenant",
        ),
        (
            {"pilot_boundary": PilotBoundaryConfig(
                scope_key="att1-cart-recovery",
                scope_version=1,
                tenant_key="att1",
                channel_provider="evolution",
                channel_account_ref="chatwoot-inbox:24",
            )},
            "portable recipient provider",
        ),
        ({"chatwoot_account_id": 99}, "portable recipient Chatwoot account"),
        ({"chatwoot_inbox_id": 99}, "portable recipient Chatwoot inbox"),
        ({"sender": object()}, "portable WABA outbound requires final Meta effect gate"),
        (
            {"pilot_boundary": PilotBoundaryConfig(
                scope_key="att1-cart-recovery",
                scope_version=1,
                tenant_key="att1",
                channel_provider="waba",
                channel_account_ref="chatwoot-inbox:99",
            )},
            "portable recipient channel account",
        ),
    ],
)
def test_direct_dispatcher_rejects_incomplete_or_drifting_portable_scope(
    changes: dict[str, object],
    message: str,
) -> None:
    kwargs: dict[str, object] = {
        "supabase": object(),
        "worker_id": "dispatcher-test",
        "commercial_ally_config": _att1_config(),
        "portable_recipient_enabled": True,
        "chatwoot_account_id": 42,
        "chatwoot_inbox_id": 24,
        "pilot_boundary": PilotBoundaryConfig(
            scope_key="att1-cart-recovery",
            scope_version=1,
            tenant_key="att1",
            channel_provider="waba",
            channel_account_ref="chatwoot-inbox:24",
        ),
    }
    kwargs.update(changes)

    with pytest.raises(ValueError, match=message):
        DurableDispatcher(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),
        ({"buyer_phone": None}, False),
        ({"product_name": "Otro producto"}, False),
        ({"offer_code": "otra-oferta"}, False),
    ],
)
def test_portable_recipient_requires_exact_manifest_context(
    changes: dict[str, object],
    expected: bool,
) -> None:
    context = replace(
        FollowupExecutionContext(
            action_id="00000000-0000-4000-8000-000000000020",
            action_type="first_contact_review",
            step_key="initial",
            recovery_case_id="00000000-0000-4000-8000-000000000021",
            contact_id="00000000-0000-4000-8000-000000000022",
            source_event_id="00000000-0000-4000-8000-000000000023",
            buyer_name=None,
            buyer_email=None,
            buyer_phone="5215550100999",
            product_name="Alimenta Tu Tiroides",
            offer_code="att1offer",
            current_goal=None,
            lead_stage="new",
        ),
        **changes,
    )

    assert worker_module._is_authorized_followup_recipient(
        execution_context=context,
        allowed_jid=None,
        commercial_ally_config=_att1_config(),
        portable_recipient_enabled=True,
    ) is expected


def test_supabase_payment_failure_planner_uses_atomic_portable_rpc() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "recovery_case_id": "00000000-0000-4000-8000-000000000011",
            "followup_sequence_id": "00000000-0000-4000-8000-000000000012",
            "scheduled_action_id": "00000000-0000-4000-8000-000000000013",
            "created": True,
        }], request=request)

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    boundary = PilotBoundaryConfig(
        scope_key="att1-payment-failure",
        scope_version=1,
        tenant_key="att1",
        channel_provider="waba",
        channel_account_ref="chatwoot-inbox:24",
    )

    result = asyncio.run(client.plan_payment_failure_recovery(
        webhook_event_id="00000000-0000-4000-8000-000000000001",
        contact_id="00000000-0000-4000-8000-000000000002",
        external_product_id="123456",
        product_name="Alimenta Tu Tiroides",
        offer_code="att1offer",
        policy_key="att1-payment-failure",
        policy_version=1,
        abandoned_at="2026-09-03T12:00:00+00:00",
        chatwoot_account_id=42,
        chatwoot_inbox_id=24,
        external_user_id="5215550100999",
        pilot_boundary=boundary,
    ))

    assert result.created is True
    assert requests[0].url.path == (
        "/rest/v1/rpc/plan_portable_payment_failure_recovery"
    )
    assert json.loads(requests[0].content)["p_scope_key"] == (
        "att1-payment-failure"
    )
    assert "p_tenant_key" not in json.loads(requests[0].content)
