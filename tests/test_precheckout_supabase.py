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


def test_precheckout_first_touch_uses_exact_rpc_contracts() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("begin_precheckout_test_first_touch"):
            return httpx.Response(
                200,
                json=[{
                    "outcome": "started",
                    "command_id": "bfc778e7-5c9f-45e6-a910-651f92312157",
                    "command_status": "request_started",
                    "target_phone": "".join(("120", "2555", "0123")),
                    "buyer_name": "Lead de Prueba",
                    "chatwoot_conversation_id": 321,
                    "template_name": "libre_ansiedad_test_first_touch_v1",
                    "template_language": "es_AR",
                    "template_category": "MARKETING",
                    "copy_version": "libre-ansiedad-precheckout-first-touch-v1",
                }],
            )
        return httpx.Response(
            200,
            json=[{
                "command_id": "bfc778e7-5c9f-45e6-a910-651f92312157",
                "command_status": "accepted_by_chatwoot",
            }],
        )

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    phone = "".join(("120", "2555", "0123"))
    started = asyncio.run(client.begin_precheckout_test_first_touch(
        command_key="controlled-first-touch-001",
        purchase_intent_id="1f581f3a-c469-45da-8208-9483d1b26f0b",
        allowed_external_user_id=phone,
        chatwoot_account_id=1,
        chatwoot_inbox_id=2,
    ))
    finished = asyncio.run(client.finish_precheckout_test_first_touch(
        command_id=started.command_id,
        outcome="accepted_by_chatwoot",
        chatwoot_conversation_id=321,
        chatwoot_message_id=654,
        failure_code=None,
    ))

    assert started.command_status == "request_started"
    assert finished.command_status == "accepted_by_chatwoot"
    assert requests == [
        (
            "/rest/v1/rpc/begin_precheckout_test_first_touch",
            {
                "p_command_key": "controlled-first-touch-001",
                "p_purchase_intent_id": "1f581f3a-c469-45da-8208-9483d1b26f0b",
                "p_allowed_external_user_id": phone,
                "p_chatwoot_account_id": 1,
                "p_chatwoot_inbox_id": 2,
            },
        ),
        (
            "/rest/v1/rpc/finish_precheckout_test_first_touch",
            {
                "p_command_id": "bfc778e7-5c9f-45e6-a910-651f92312157",
                "p_outcome": "accepted_by_chatwoot",
                "p_chatwoot_conversation_id": 321,
                "p_chatwoot_message_id": 654,
                "p_failure_code": None,
            },
        ),
    ]
