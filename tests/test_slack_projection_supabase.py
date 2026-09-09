import asyncio
from datetime import UTC, datetime
import json

import httpx
import pytest

from bridge.slack_projection import SlackCorrelationNotificationClaim
from bridge.supabase import SupabaseClient, SupabaseError


SOURCE_ID = "11111111-1111-4111-8111-111111111111"
CLAIM_TOKEN = "22222222-2222-4222-8222-222222222222"
NOTIFICATION_ID = "33333333-3333-4333-8333-333333333333"


def test_claims_scoped_slack_projection_rows_with_a_fenced_lease() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "source_event_id": SOURCE_ID,
                    "outcome": "unmatched",
                    "reason_code": "identity_not_found",
                    "candidate_count": 0,
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

    assert claims == [
        SlackCorrelationNotificationClaim(
            source_event_id=SOURCE_ID,
            outcome="unmatched",
            reason_code="identity_not_found",
            candidate_count=0,
            occurred_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
            claim_token=CLAIM_TOKEN,
            lease_generation=4,
        )
    ]
    assert requests[0].url.path.endswith(
        "/rpc/claim_slack_correlation_notifications"
    )
    assert json.loads(requests[0].content) == {
        "p_tenant_ref": "att1",
        "p_funnel_ref": "att1-main",
        "p_worker_id": "att1-slack-1",
        "p_limit": 1,
        "p_lease_seconds": 60,
        "p_binding_version": 3,
    }


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
