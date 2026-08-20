import asyncio
import json

import httpx
import pytest

from bridge.supabase import (
    PurchaseIntentCorrelationResult,
    SupabaseClient,
    SupabaseError,
)


def _client(body: object, *, status: int = 200) -> tuple[SupabaseClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=body, request=request)

    return (
        SupabaseClient(
            base_url="https://supabase.example.test",
            service_role_key="service-role",
            transport=httpx.MockTransport(handler),
        ),
        requests,
    )


@pytest.mark.parametrize(
    ("outcome", "intent_id", "matched_by", "candidate_count", "handoff"),
    [
        ("resolved", "11111111-1111-4111-8111-111111111111", "email_and_phone", 1, False),
        ("unmatched", None, None, 0, True),
        ("ambiguous", None, None, 2, True),
        ("conflict", None, None, 2, True),
    ],
)
def test_correlates_exact_hotmart_event(
    outcome: str,
    intent_id: str | None,
    matched_by: str | None,
    candidate_count: int,
    handoff: bool,
) -> None:
    event_id = "22222222-2222-4222-8222-222222222222"
    client, requests = _client([
        {
            "outcome": outcome,
            "purchase_intent_id": intent_id,
            "matched_by": matched_by,
            "candidate_count": candidate_count,
            "manual_handoff_required": handoff,
        }
    ])

    result = asyncio.run(
        client.correlate_hotmart_purchase_intent(webhook_event_id=event_id)
    )

    assert result == PurchaseIntentCorrelationResult(
        outcome=outcome,
        purchase_intent_id=intent_id,
        matched_by=matched_by,
        candidate_count=candidate_count,
        manual_handoff_required=handoff,
    )
    assert requests[0].url.path == "/rest/v1/rpc/correlate_hotmart_purchase_intent"
    assert json.loads(requests[0].content) == {"p_webhook_event_id": event_id}


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        [{"outcome": "resolved"}],
        [{"outcome": "invented", "purchase_intent_id": None, "matched_by": None, "candidate_count": 0, "manual_handoff_required": True}],
        [{"outcome": "resolved", "purchase_intent_id": None, "matched_by": "email", "candidate_count": 1, "manual_handoff_required": False}],
        [{"outcome": "unmatched", "purchase_intent_id": None, "matched_by": None, "candidate_count": True, "manual_handoff_required": True}],
    ],
)
def test_correlation_response_fails_closed(body: object) -> None:
    client, _ = _client(body)

    with pytest.raises(SupabaseError):
        asyncio.run(
            client.correlate_hotmart_purchase_intent(
                webhook_event_id="22222222-2222-4222-8222-222222222222"
            )
        )
