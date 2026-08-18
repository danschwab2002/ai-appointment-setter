import asyncio
import json

import httpx
import pytest

from bridge.supabase import SupabaseClient, SupabaseError


def test_precheckout_admission_uses_exact_rpc_contract() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=[
                {
                    "outcome": "inserted",
                    "submission_id": "bfc778e7-5c9f-45e6-a910-651f92312157",
                    "purchase_intent_id": "1f581f3a-c469-45da-8208-9483d1b26f0b",
                }
            ],
        )

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        client.admit_precheckout_form_submission(
            external_submission_id="form-submit-0001",
            raw_payload={"id": "form-submit-0001"},
            canonical_payload={"external_submission_id": "form-submit-0001"},
        )
    )

    assert result.outcome == "inserted"
    assert result.purchase_intent_id == "1f581f3a-c469-45da-8208-9483d1b26f0b"
    assert observed == {
        "path": "/rest/v1/rpc/admit_precheckout_form_submission",
        "body": {
            "p_external_submission_id": "form-submit-0001",
            "p_raw_payload": {"id": "form-submit-0001"},
            "p_canonical_payload": {
                "external_submission_id": "form-submit-0001"
            },
        },
    }


def test_precheckout_admission_rejects_unknown_outcome() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "outcome": "unexpected",
                    "submission_id": "bfc778e7-5c9f-45e6-a910-651f92312157",
                    "purchase_intent_id": "1f581f3a-c469-45da-8208-9483d1b26f0b",
                }
            ],
        )

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SupabaseError, match="precheckout_admission_invalid_outcome"):
        asyncio.run(
            client.admit_precheckout_form_submission(
                external_submission_id="form-submit-0001",
                raw_payload={"id": "form-submit-0001"},
                canonical_payload={"external_submission_id": "form-submit-0001"},
            )
        )
