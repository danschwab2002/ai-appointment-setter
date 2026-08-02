"""Fail-closed validation for authoritative Supabase lookup responses."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from bridge.supabase import SupabaseClient, SupabaseError


_LOOKUP_METHODS = (
    "find_contact_by_email",
    "find_contact_by_phone",
    "fetch_conversations",
    "fetch_recovery_cases",
    "fetch_channel_identities",
)


def _client(response_body: object) -> SupabaseClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body, request=request)

    return SupabaseClient(
        base_url="https://fake.supabase.co",
        service_role_key="fake-service-role-key",
        transport=httpx.MockTransport(handler),
    )


def _invoke(client: SupabaseClient, method_name: str) -> Any:
    if method_name == "find_contact_by_email":
        coroutine = client.find_contact_by_email("test@example.com")
    elif method_name == "find_contact_by_phone":
        coroutine = client.find_contact_by_phone("15550000000")
    else:
        coroutine = getattr(client, method_name)(contact_id="contact-test")
    return asyncio.run(coroutine)


@pytest.mark.parametrize("method_name", _LOOKUP_METHODS)
def test_authoritative_lookup_rejects_non_list_200_body(
    method_name: str,
) -> None:
    with pytest.raises(SupabaseError):
        _invoke(_client({"unexpected": "object"}), method_name)


@pytest.mark.parametrize(
    ("method_name", "incomplete_row"),
    [
        ("find_contact_by_email", {"contacts": None}),
        ("find_contact_by_phone", {"contacts": {"id": None}}),
        (
            "fetch_conversations",
            {
                "id": "conversation-test",
                "status": "active",
                "automation_status": "enabled",
            },
        ),
        (
            "fetch_recovery_cases",
            {"id": "case-test", "status": "active"},
        ),
        (
            "fetch_channel_identities",
            {"id": "identity-test", "channel": "whatsapp"},
        ),
    ],
)
def test_authoritative_lookup_rejects_incomplete_row(
    method_name: str,
    incomplete_row: dict[str, object],
) -> None:
    with pytest.raises(SupabaseError):
        _invoke(_client([incomplete_row]), method_name)


def test_conversation_lookup_rejects_non_boolean_human_takeover() -> None:
    row = {
        "id": "conversation-test",
        "status": "active",
        "automation_status": "enabled",
        "human_takeover": "false",
    }

    with pytest.raises(SupabaseError):
        _invoke(_client([row]), "fetch_conversations")


def test_conversation_lookup_rejects_invalid_message_direction() -> None:
    row = {
        "id": "conversation-test",
        "status": "active",
        "automation_status": "enabled",
        "human_takeover": False,
        "last_message_direction": "sideways",
    }

    with pytest.raises(SupabaseError):
        _invoke(_client([row]), "fetch_conversations")


def test_recovery_case_lookup_requires_product_name() -> None:
    row = {
        "id": "case-test",
        "status": "active",
        "lead_stage": "new",
        "product_name": None,
    }

    with pytest.raises(SupabaseError):
        _invoke(_client([row]), "fetch_recovery_cases")
