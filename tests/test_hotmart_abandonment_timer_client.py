import asyncio
import json

import httpx
import pytest

from bridge.supabase import (
    HotmartAbandonmentReevaluationResult,
    SupabaseClient,
    SupabaseError,
)

TIMER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_TIMER_ID = "22222222-2222-4222-8222-222222222222"


def _client(
    responses: list[object], *, status: int = 200
) -> tuple[SupabaseClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    pending = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=pending.pop(0), request=request)

    return (
        SupabaseClient(
            base_url="https://supabase.example.test",
            service_role_key="service-role",
            transport=httpx.MockTransport(handler),
        ),
        requests,
    )


def test_lists_ids_and_reevaluates_without_exposing_intent_data() -> None:
    timer_id = TIMER_ID
    now = "2026-08-21T16:00:00+00:00"
    client, requests = _client(
        [
            [{"reevaluation_id": timer_id}],
            [
                {
                    "reevaluation_id": timer_id,
                    "reevaluation_status": "completed",
                    "reevaluation_outcome": "blocked_not_authorized",
                    "completed_at": now,
                    "replayed": False,
                }
            ],
        ]
    )

    assert asyncio.run(
        client.list_due_hotmart_abandonment_reevaluations(
            now=now, batch_size=10
        )
    ) == [timer_id]
    assert asyncio.run(
        client.reevaluate_hotmart_abandonment_timer(
            reevaluation_id=timer_id, now=now
        )
    ) == HotmartAbandonmentReevaluationResult(
        reevaluation_id=timer_id,
        status="completed",
        outcome="blocked_not_authorized",
        completed_at=now,
        replayed=False,
    )

    assert requests[0].url.path == (
        "/rest/v1/rpc/list_due_hotmart_abandonment_reevaluations"
    )
    assert json.loads(requests[0].content) == {
        "p_now": now,
        "p_batch_size": 10,
    }
    assert requests[1].url.path == (
        "/rest/v1/rpc/reevaluate_hotmart_abandonment_timer"
    )
    assert json.loads(requests[1].content) == {
        "p_reevaluation_id": timer_id,
        "p_now": now,
    }


@pytest.mark.parametrize(
    "body",
    [
        [{"reevaluation_id": TIMER_ID}, {"reevaluation_id": TIMER_ID}],
        [{"reevaluation_id": "timer-1"}],
        [{"purchase_intent_id": "intent-1"}],
    ],
)
def test_due_list_fails_closed_on_invalid_shape(body: object) -> None:
    client, _ = _client([body])

    with pytest.raises(SupabaseError):
        asyncio.run(
            client.list_due_hotmart_abandonment_reevaluations(
                now="2026-08-21T16:00:00+00:00",
                batch_size=10,
            )
        )


@pytest.mark.parametrize(
    "body",
    [
        [],
        [{"reevaluation_id": "timer-1"}],
        [
            {
                "reevaluation_id": OTHER_TIMER_ID,
                "reevaluation_status": "completed",
                "reevaluation_outcome": "blocked_not_authorized",
                "completed_at": "2026-08-21T16:00:00+00:00",
                "replayed": False,
            }
        ],
        [
            {
                "reevaluation_id": TIMER_ID,
                "reevaluation_status": "completed",
                "reevaluation_outcome": "send_message",
                "completed_at": "2026-08-21T16:00:00+00:00",
                "replayed": False,
            }
        ],
    ],
)
def test_reevaluation_fails_closed_on_invalid_shape(body: object) -> None:
    client, _ = _client([body])

    with pytest.raises(SupabaseError):
        asyncio.run(
            client.reevaluate_hotmart_abandonment_timer(
                reevaluation_id=TIMER_ID,
                now="2026-08-21T16:00:00+00:00",
            )
        )


def test_reevaluation_rejects_invalid_input_uuid_before_http() -> None:
    client, requests = _client([])

    with pytest.raises(SupabaseError):
        asyncio.run(
            client.reevaluate_hotmart_abandonment_timer(
                reevaluation_id="timer-1",
                now="2026-08-21T16:00:00+00:00",
            )
        )

    assert requests == []
