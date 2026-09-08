"""Supabase client contract for sanitary Johanna funnel observations."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from bridge.supabase import SupabaseClient, SupabaseError


PARAMS = {
    "version": "1.0.0",
    "event_id": "01K4N9YQ2T7W3H5J8M6P0R1SVC",
    "event_type": "preform_opened",
    "occurred_at": "2026-09-08T08:00:00Z",
    "anonymous_session_id": "01K4N9YQ2T7W3H5J8M6P0R1SVD",
    "landing_ref": "ads-a",
    "offer_ref": "bxjge6zq",
    "utm_source": "meta",
    "utm_medium": "paid_social",
    "utm_campaign": None,
    "utm_content": None,
    "utm_term": None,
}


def test_client_uses_only_the_atomic_rpc_and_closed_scalar_arguments() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{
            "outcome": "inserted",
            "event_id": PARAMS["event_id"],
        }])

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client.admit_johanna_funnel_event(**PARAMS))  # type: ignore[arg-type]

    assert result.outcome == "inserted"
    assert result.event_id == PARAMS["event_id"]
    assert len(requests) == 1
    assert requests[0].url.path == "/rest/v1/rpc/admit_johanna_funnel_event_v1"
    assert json.loads(requests[0].content) == {
        "p_version": PARAMS["version"],
        "p_event_id": PARAMS["event_id"],
        "p_event_type": PARAMS["event_type"],
        "p_occurred_at": PARAMS["occurred_at"],
        "p_anonymous_session_id": PARAMS["anonymous_session_id"],
        "p_landing_ref": PARAMS["landing_ref"],
        "p_offer_ref": PARAMS["offer_ref"],
        "p_utm_source": PARAMS["utm_source"],
        "p_utm_medium": PARAMS["utm_medium"],
        "p_utm_campaign": None,
        "p_utm_content": None,
        "p_utm_term": None,
    }
    assert b"payload" not in requests[0].content
    assert b"email" not in requests[0].content
    assert b"phone" not in requests[0].content
    assert b"fbclid" not in requests[0].content


@pytest.mark.parametrize("row", [
    {"outcome": "unknown", "event_id": PARAMS["event_id"]},
    {"outcome": "inserted", "event_id": "not-the-request-event"},
    {"outcome": "inserted", "event_id": PARAMS["event_id"], "extra": True},
])
def test_client_rejects_malformed_committed_rpc_rows(row: dict[str, object]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[row], request=request)

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SupabaseError, match="johanna_funnel_event_admission_invalid_row"):
        asyncio.run(client.admit_johanna_funnel_event(**PARAMS))  # type: ignore[arg-type]
