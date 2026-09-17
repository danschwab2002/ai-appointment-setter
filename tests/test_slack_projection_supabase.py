import asyncio
from datetime import UTC, datetime
import json

import httpx
import pytest

from bridge.correlation_preresolution import (
    CorrelationCandidate,
    CorrelationEvidence,
    CorrelationEvidenceFact,
    CorrelationPreresolutionClaim,
    PreresolutionRecommendation,
)
from bridge.slack_projection import SlackCorrelationNotificationClaim
from bridge.supabase import SupabaseClient, SupabaseError
from slack_correlation.catalog import (
    CorrelationRecommendation,
    CorrelationRecommendationEvidence,
)


SOURCE_ID = "11111111-1111-4111-8111-111111111111"
CLAIM_TOKEN = "22222222-2222-4222-8222-222222222222"
NOTIFICATION_ID = "33333333-3333-4333-8333-333333333333"


def test_claims_scoped_slack_projection_rows_with_a_fenced_lease() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith(
            "/rpc/claim_slack_correlation_notifications_v2"
        ):
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[
                {
                    "source_event_id": SOURCE_ID,
                    "source_event_type": "PURCHASE_APPROVED",
                    "notification_contract_version": 3,
                    "outcome": "ambiguous",
                    "reason_code": "multiple_candidates",
                    "candidate_count": 2,
                    "occurred_at": "2026-09-08T12:00:00+00:00",
                    "claim_token": CLAIM_TOKEN,
                    "lease_generation": 4,
                    "recommendation_data": {
                        "recommendation_ref": "44444444-4444-4444-8444-444444444444",
                        "candidate_id": "55555555-5555-4555-8555-555555555555",
                        "candidate_label": "Persona 2",
                        "evidence": [
                            {
                                "kind": "precheckout_time_proximity_minutes",
                                "value": 4,
                            }
                        ],
                        "evidence_fingerprint": "f" * 64,
                        "model_name": "resolver-model",
                        "prompt_version": "correlation-preresolution-v1",
                    },
                }
            ],
        )

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )

    claims = asyncio.run(
        client.claim_slack_correlation_notifications(
            tenant_ref="att1",
            funnel_ref="att1-main",
            worker_id="att1-slack-1",
            limit=1,
            lease_seconds=60,
            binding_version=3,
        )
    )

    assert claims == [
        SlackCorrelationNotificationClaim(
            source_event_id=SOURCE_ID,
            source_event_type="PURCHASE_APPROVED",
            notification_contract_version=3,
            outcome="ambiguous",
            reason_code="multiple_candidates",
            candidate_count=2,
            occurred_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
            claim_token=CLAIM_TOKEN,
            lease_generation=4,
            recommendation=CorrelationRecommendation(
                recommendation_ref="44444444-4444-4444-8444-444444444444",
                candidate_id="55555555-5555-4555-8555-555555555555",
                candidate_label="Persona 2",
                evidence=(
                    CorrelationRecommendationEvidence(
                        kind="precheckout_time_proximity_minutes",
                        value=4,
                    ),
                ),
                evidence_fingerprint="f" * 64,
                model_name="resolver-model",
                prompt_version="correlation-preresolution-v1",
            ),
        )
    ]
    assert requests[0].url.path.endswith(
        "/rpc/claim_slack_correlation_notifications_v2"
    )
    assert requests[1].url.path.endswith(
        "/rpc/claim_slack_correlation_notifications_v3"
    )
    assert json.loads(requests[1].content) == {
        "p_tenant_ref": "att1",
        "p_funnel_ref": "att1-main",
        "p_worker_id": "att1-slack-1",
        "p_limit": 1,
        "p_lease_seconds": 60,
        "p_binding_version": 3,
    }


@pytest.mark.parametrize("contract_version", [1, 2])
def test_claims_historical_projection_before_v3(
    contract_version: int,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if not request.url.path.endswith(
            "/rpc/claim_slack_correlation_notifications_v2"
        ):
            raise AssertionError("V3 must not be called while historical work exists")
        return httpx.Response(
            200,
            json=[
                {
                    "source_event_id": SOURCE_ID,
                    "source_event_type": "PURCHASE_APPROVED",
                    "notification_contract_version": contract_version,
                    "outcome": "conflict",
                    "reason_code": "email_phone_conflict",
                    "candidate_count": 1,
                    "occurred_at": "2026-09-08T12:00:00+00:00",
                    "claim_token": CLAIM_TOKEN,
                    "lease_generation": 4,
                }
            ],
        )

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )

    claims = asyncio.run(
        client.claim_slack_correlation_notifications(
            tenant_ref="att1",
            funnel_ref="att1-main",
            worker_id="att1-slack-1",
            limit=1,
            lease_seconds=60,
            binding_version=3,
        )
    )

    assert len(requests) == 1
    assert claims == [
        SlackCorrelationNotificationClaim(
            source_event_id=SOURCE_ID,
            source_event_type="PURCHASE_APPROVED",
            notification_contract_version=contract_version,
            outcome="conflict",
            reason_code="email_phone_conflict",
            candidate_count=1,
            occurred_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
            claim_token=CLAIM_TOKEN,
            lease_generation=4,
            recommendation=None,
        )
    ]


def test_completes_and_releases_only_the_exact_projection_lease() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"applied": True}])

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(
        client.complete_slack_correlation_notification(
            source_event_id=SOURCE_ID,
            claim_token=CLAIM_TOKEN,
            lease_generation=4,
            notification_id=NOTIFICATION_ID,
        )
    )
    asyncio.run(
        client.release_slack_correlation_notification(
            source_event_id=SOURCE_ID,
            claim_token=CLAIM_TOKEN,
            lease_generation=4,
            failure_code="connector_admission_unknown",
        )
    )

    assert requests[0].url.path.endswith(
        "/rpc/complete_slack_correlation_notification"
    )
    assert json.loads(requests[0].content) == {
        "p_source_event_id": SOURCE_ID,
        "p_claim_token": CLAIM_TOKEN,
        "p_lease_generation": 4,
        "p_notification_id": NOTIFICATION_ID,
    }
    assert requests[1].url.path.endswith(
        "/rpc/release_slack_correlation_notification"
    )
    assert json.loads(requests[1].content) == {
        "p_source_event_id": SOURCE_ID,
        "p_claim_token": CLAIM_TOKEN,
        "p_lease_generation": 4,
        "p_failure_code": "connector_admission_unknown",
    }


def test_claims_loads_and_completes_ai_preresolution_with_bounded_evidence() -> None:
    requests: list[httpx.Request] = []
    candidate_id = "55555555-5555-4555-8555-555555555555"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/claim_operator_correlation_preresolutions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "webhook_event_id": SOURCE_ID,
                        "source_event_type": "PURCHASE_APPROVED",
                        "outcome": "ambiguous",
                        "claim_token": CLAIM_TOKEN,
                        "lease_generation": 4,
                    }
                ],
            )
        if request.url.path.endswith(
            "/get_operator_correlation_preresolution_evidence"
        ):
            return httpx.Response(
                200,
                json=[
                    {
                        "evidence_data": {
                            "case_id": SOURCE_ID,
                            "event_type": "PURCHASE_APPROVED",
                            "outcome": "ambiguous",
                            "candidates": [
                                {"candidate_id": candidate_id, "label": "Persona 1"},
                                {
                                    "candidate_id": "66666666-6666-4666-8666-666666666666",
                                    "label": "Persona 2",
                                },
                            ],
                            "facts": [
                                {
                                    "evidence_id": f"fact-time-{candidate_id}",
                                    "candidate_id": candidate_id,
                                    "kind": "precheckout_time_proximity_minutes",
                                    "value": 4,
                                    "independent": True,
                                    "discriminating": True,
                                }
                            ],
                        }
                    }
                ],
            )
        if request.url.path.endswith("/complete_operator_correlation_preresolution"):
            return httpx.Response(
                200,
                json=[
                    {
                        "recommendation_ref": "44444444-4444-4444-8444-444444444444",
                        "status": "recommended",
                    }
                ],
            )
        raise AssertionError(request.url.path)

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )
    claim = asyncio.run(
        client.claim_correlation_preresolution(
            tenant_ref="lancemos",
            funnel_ref="psicologajohanna",
            worker_id="correlation-ai-1",
        )
    )
    assert claim == CorrelationPreresolutionClaim(
        case_id=SOURCE_ID,
        event_type="PURCHASE_APPROVED",
        outcome="ambiguous",
        claim_token=CLAIM_TOKEN,
        lease_generation=4,
    )

    evidence = asyncio.run(
        client.load_correlation_evidence(
            tenant_ref="lancemos",
            funnel_ref="psicologajohanna",
            case_id=SOURCE_ID,
            claim_token=CLAIM_TOKEN,
            lease_generation=4,
        )
    )
    assert evidence == CorrelationEvidence(
        case_id=SOURCE_ID,
        event_type="PURCHASE_APPROVED",
        outcome="ambiguous",
        candidates=(
            CorrelationCandidate(candidate_id=candidate_id, label="Persona 1"),
            CorrelationCandidate(
                candidate_id="66666666-6666-4666-8666-666666666666",
                label="Persona 2",
            ),
        ),
        facts=(
            CorrelationEvidenceFact(
                evidence_id=f"fact-time-{candidate_id}",
                candidate_id=candidate_id,
                kind="precheckout_time_proximity_minutes",
                value=4,
                independent=True,
                discriminating=True,
            ),
        ),
    )

    recommendation = PreresolutionRecommendation(
        status="recommended",
        candidate_id=candidate_id,
        candidate_label="Persona 1",
        supporting_facts=evidence.facts,
        model_name="resolver-model",
        prompt_version="correlation-preresolution-v1",
    )
    asyncio.run(
        client.complete_correlation_preresolution(
            case_id=SOURCE_ID,
            claim_token=CLAIM_TOKEN,
            lease_generation=4,
            disposition="recommended",
            recommendation=recommendation,
        )
    )
    assert json.loads(requests[2].content) == {
        "p_webhook_event_id": SOURCE_ID,
        "p_claim_token": CLAIM_TOKEN,
        "p_lease_generation": 4,
        "p_disposition": "recommended",
        "p_recommended_purchase_intent_id": candidate_id,
        "p_supporting_evidence": [
            {"kind": "precheckout_time_proximity_minutes", "value": 4}
        ],
        "p_model_name": "resolver-model",
        "p_prompt_version": "correlation-preresolution-v1",
    }


def test_releases_ai_preresolution_with_only_a_bounded_failure_code() -> None:
    requests: list[httpx.Request] = []
    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request) or httpx.Response(200, json=None)
            )
        ),
    )

    asyncio.run(
        client.release_correlation_preresolution(
            case_id=SOURCE_ID,
            claim_token=CLAIM_TOKEN,
            lease_generation=4,
            failure_code="correlation_preresolution_failed",
        )
    )

    assert requests[0].url.path.endswith(
        "/rpc/release_operator_correlation_preresolution"
    )
    assert json.loads(requests[0].content) == {
        "p_webhook_event_id": SOURCE_ID,
        "p_claim_token": CLAIM_TOKEN,
        "p_lease_generation": 4,
        "p_failure_code": "correlation_preresolution_failed",
    }


def test_claim_rejects_malformed_projection_evidence() -> None:
    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[
                    {
                        "source_event_id": SOURCE_ID,
                        "source_event_type": "PURCHASE_APPROVED",
                        "notification_contract_version": 2,
                        "outcome": "resolved",
                        "reason_code": "exact_email",
                        "candidate_count": 1,
                        "occurred_at": "2026-09-08T12:00:00+00:00",
                        "claim_token": CLAIM_TOKEN,
                        "lease_generation": 4,
                    }
                ],
            )
        ),
    )

    with pytest.raises(SupabaseError, match="slack_correlation_projection_claim_invalid"):
        asyncio.run(
            client.claim_slack_correlation_notifications(
                tenant_ref="att1",
                funnel_ref="att1-main",
                worker_id="att1-slack-1",
                limit=1,
                lease_seconds=60,
                binding_version=None,
            )
        )
