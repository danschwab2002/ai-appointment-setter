"""Tests for the provisional pre-checkout form adapter."""

import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from bridge.precheckout import (
    EMULATED_CONTRACT_VERSION,
    PRECHECKOUT_EVENT_TYPE,
    PrecheckoutScope,
    parse_emulated_precheckout_submission,
)
from bridge.supabase import SupabaseClient

FIXTURE = Path(__file__).parent / "fixtures/precheckout_form_submission_v1.json"


EMULATED_PAYLOAD: dict[str, object] = {
    "id": "form-submit-0001",
    "event": "PRECHECKOUT_FORM_SUBMITTED",
    "version": "1.0.0-emulated",
    "created_at": "2026-08-14T22:15:00Z",
    "lead": {
        "full_name": "Lead de Prueba",
        "phone_e164": "+1" + "202" + "555" + "0123",
    },
}

SCOPE = PrecheckoutScope(
    tenant_ref="joana",
    funnel_ref="libre-de-ansiedad",
    landing_ref="bcl-main",
    product_ref="libre-de-ansiedad",
    offer_ref="bxjge6zq",
    consent_copy_version="form-screenshot-2026-08-14",
)


def test_emulated_submission_normalizes_to_non_authoritative_internal_event() -> None:
    parsed = parse_emulated_precheckout_submission(EMULATED_PAYLOAD, scope=SCOPE)

    assert parsed is not None
    assert parsed.external_submission_id == "form-submit-0001"
    assert parsed.event_type == PRECHECKOUT_EVENT_TYPE
    assert parsed.contract_version == EMULATED_CONTRACT_VERSION
    assert parsed.submitted_at == datetime(2026, 8, 14, 22, 15, tzinfo=UTC)
    assert parsed.normalized_email is None
    assert parsed.normalized_phone == "1" + "202" + "555" + "0123"
    assert parsed.product_ref == "libre-de-ansiedad"
    assert parsed.offer_ref == "bxjge6zq"
    assert parsed.terms_accepted is False
    assert parsed.privacy_accepted is False
    assert parsed.whatsapp_contact_authorized is False
    assert parsed.provisional is True
    assert parsed.provider_observed is False
    assert parsed.activation_authorized is False


def test_emulated_submission_rejects_missing_phone_before_persistence() -> None:
    payload = copy.deepcopy(EMULATED_PAYLOAD)
    lead = payload["lead"]
    assert isinstance(lead, dict)
    del lead["phone_e164"]

    assert parse_emulated_precheckout_submission(payload, scope=SCOPE) is None


def test_documented_fixture_matches_the_emulated_contract() -> None:
    payload = json.loads(FIXTURE.read_text())

    parsed = parse_emulated_precheckout_submission(payload, scope=SCOPE)

    assert parsed is not None
    assert parsed.external_submission_id == "form-submit-fixture-0001"
    assert parsed.activation_authorized is False


class _AdmissionTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.request: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return httpx.Response(
            200,
            json=[{
                "outcome": "inserted",
                "submission_id": "bfc778e7-5c9f-45e6-a910-651f92312157",
                "purchase_intent_id": "1f581f3a-c469-45da-8208-9483d1b26f0b",
            }],
        )


def test_durable_admission_sends_raw_and_canonical_payload_to_atomic_rpc() -> None:
    parsed = parse_emulated_precheckout_submission(EMULATED_PAYLOAD, scope=SCOPE)
    assert parsed is not None
    transport = _AdmissionTransport()
    client = SupabaseClient(
        base_url="https://example.supabase.test",
        service_role_key="test-service-role",
        transport=transport,
    )

    result = asyncio.run(client.admit_precheckout_form_submission(
        external_submission_id=parsed.external_submission_id,
        raw_payload=EMULATED_PAYLOAD,
        canonical_payload=parsed.as_canonical_payload(),
    ))

    assert result.outcome == "inserted"
    assert result.purchase_intent_id == "1f581f3a-c469-45da-8208-9483d1b26f0b"
    assert transport.request is not None
    assert transport.request.url.path == (
        "/rest/v1/rpc/admit_precheckout_form_submission"
    )
    body = json.loads(transport.request.content)
    assert body["p_external_submission_id"] == "form-submit-0001"
    assert body["p_raw_payload"] == EMULATED_PAYLOAD
    assert body["p_canonical_payload"]["identity"] == {
        "phone": "1" + "202" + "555" + "0123",
    }
    assert body["p_canonical_payload"]["consent"] == {
        "terms_accepted": False,
        "privacy_accepted": False,
        "whatsapp_contact": False,
        "copy_version": "form-screenshot-2026-08-14",
    }
    assert body["p_canonical_payload"]["assurance"] == {
        "provisional": True,
        "provider_observed": False,
        "activation_authorized": False,
    }