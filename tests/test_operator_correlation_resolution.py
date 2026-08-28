import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.app import Settings, create_app
from bridge.supabase import OperatorCorrelationResolutionError, SupabaseClient

CASE_ID = "11111111-1111-4111-8111-111111111111"
CANDIDATE_ID = "33333333-3333-4333-8333-333333333333"
COMMAND_ID = "55555555-5555-4555-8555-555555555555"
IDEMPOTENCY_KEY = "77777777-7777-4777-8777-777777777777"


class _FakeResolutionStore:
    def __init__(self) -> None:
        self.prepare_calls: list[dict[str, object]] = []
        self.confirm_calls: list[dict[str, object]] = []

    async def prepare_operator_correlation_resolution(
        self, **kwargs: object
    ) -> dict[str, object]:
        self.prepare_calls.append(kwargs)
        return {
            "command_id": COMMAND_ID,
            "idempotency_key": IDEMPOTENCY_KEY,
            "webhook_event_id": CASE_ID,
            "action": "resolve_with_candidate",
            "selected_purchase_intent_id": CANDIDATE_ID,
            "verification_basis": "operator_source_record",
            "deterministic_outcome": "ambiguous",
            "deterministic_reason_code": "multiple_candidates",
            "candidate_count": 2,
            "expires_at": "2026-08-24T16:10:00+00:00",
            "requires_human_approval": True,
            "automation_blocked": True,
        }

    async def confirm_operator_correlation_resolution(
        self, **kwargs: object
    ) -> dict[str, object]:
        self.confirm_calls.append(kwargs)
        return {
            "resolution_id": "66666666-6666-4666-8666-666666666666",
            "command_id": COMMAND_ID,
            "webhook_event_id": CASE_ID,
            "resolution_outcome": "linked_candidate",
            "effective_purchase_intent_id": CANDIDATE_ID,
            "deterministic_outcome": "ambiguous",
            "applied_at": "2026-08-24T16:05:00+00:00",
            "replayed": False,
            "automation_blocked": True,
        }


class _StaleResolutionStore(_FakeResolutionStore):
    async def confirm_operator_correlation_resolution(
        self, **kwargs: object
    ) -> dict[str, object]:
        raise OperatorCorrelationResolutionError(
            "operator_correlation_stale_evidence"
        )


def _app(store: _FakeResolutionStore) -> TestClient:
    settings = Settings(
        webhook_secret="unused",
        allowed_jid="593999999999@s.whatsapp.net",
        capture_dir=Path("/tmp/operator-correlation-resolution-tests"),
        max_age_seconds=300,
        operator_correlation_read_enabled=True,
        operator_correlation_read_token="r" * 32,
        operator_correlation_tenant_ref="lancemos",
        operator_correlation_funnel_ref="psicologajohanna",
        operator_correlation_write_enabled=True,
        operator_correlation_write_token="w" * 32,
        operator_correlation_actor_ref="juan-operator",
    )
    return TestClient(create_app(settings, supabase_client=store))  # type: ignore[arg-type]


def test_prepare_resolution_uses_server_owned_scope_and_actor() -> None:
    store = _FakeResolutionStore()
    with _app(store) as client:
        response = client.post(
            "/internal/operator/correlations/resolutions/prepare",
            headers={"Authorization": f"Bearer {'w' * 32}"},
            json={
                "case_id": CASE_ID,
                "idempotency_key": IDEMPOTENCY_KEY,
                "action": "resolve_with_candidate",
                "candidate_id": CANDIDATE_ID,
                "verification_basis": "operator_source_record",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "command": {
            "command_id": COMMAND_ID,
            "idempotency_key": IDEMPOTENCY_KEY,
            "case_id": CASE_ID,
            "action": "resolve_with_candidate",
            "candidate_id": CANDIDATE_ID,
            "verification_basis": "operator_source_record",
            "deterministic_outcome": "ambiguous",
            "deterministic_reason_code": "multiple_candidates",
            "candidate_count": 2,
            "expires_at": "2026-08-24T16:10:00+00:00",
            "requires_human_approval": True,
            "automation_blocked": True,
        }
    }
    assert store.prepare_calls == [
        {
            "tenant_ref": "lancemos",
            "funnel_ref": "psicologajohanna",
            "actor_ref": "juan-operator",
            "idempotency_key": IDEMPOTENCY_KEY,
            "webhook_event_id": CASE_ID,
            "action": "resolve_with_candidate",
            "selected_purchase_intent_id": CANDIDATE_ID,
            "verification_basis": "operator_source_record",
        }
    ]


def test_supabase_prepares_resolution_through_narrow_rpc() -> None:
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json=[
                {
                    "command_data": {
                        "command_id": COMMAND_ID,
                        "webhook_event_id": CASE_ID,
                    }
                }
            ],
        )

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        client.prepare_operator_correlation_resolution(
            tenant_ref="lancemos",
            funnel_ref="psicologajohanna",
            actor_ref="juan-operator",
            idempotency_key=IDEMPOTENCY_KEY,
            webhook_event_id=CASE_ID,
            action="resolve_with_candidate",
            selected_purchase_intent_id=CANDIDATE_ID,
            verification_basis="operator_source_record",
        )
    )

    assert result == {"command_id": COMMAND_ID, "webhook_event_id": CASE_ID}
    assert seen == [
        (
            "POST",
            "/rest/v1/rpc/prepare_operator_correlation_resolution",
            {
                "p_tenant_ref": "lancemos",
                "p_funnel_ref": "psicologajohanna",
                "p_actor_ref": "juan-operator",
                "p_idempotency_key": IDEMPOTENCY_KEY,
                "p_webhook_event_id": CASE_ID,
                "p_action": "resolve_with_candidate",
                "p_selected_purchase_intent_id": CANDIDATE_ID,
                "p_verification_basis": "operator_source_record",
            },
        )
    ]


def test_confirm_resolution_revalidates_server_owned_scope_and_actor() -> None:
    store = _FakeResolutionStore()
    with _app(store) as client:
        response = client.post(
            "/internal/operator/correlations/resolutions/confirm",
            headers={"Authorization": f"Bearer {'w' * 32}"},
            json={
                "command_id": COMMAND_ID,
                "expected_action": "resolve_with_candidate",
                "expected_candidate_id": CANDIDATE_ID,
            },
        )

    assert response.status_code == 200
    assert response.json()["resolution"] == {
        "resolution_id": "66666666-6666-4666-8666-666666666666",
        "command_id": COMMAND_ID,
        "case_id": CASE_ID,
        "resolution_outcome": "linked_candidate",
        "effective_purchase_intent_id": CANDIDATE_ID,
        "deterministic_outcome": "ambiguous",
        "applied_at": "2026-08-24T16:05:00+00:00",
        "replayed": False,
        "automation_blocked": True,
    }
    assert store.confirm_calls == [
        {
            "tenant_ref": "lancemos",
            "funnel_ref": "psicologajohanna",
            "actor_ref": "juan-operator",
            "command_id": COMMAND_ID,
            "expected_action": "resolve_with_candidate",
            "expected_purchase_intent_id": CANDIDATE_ID,
        }
    ]


def test_supabase_confirms_resolution_through_narrow_rpc() -> None:
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            json=[
                {
                    "resolution_data": {
                        "resolution_id": "66666666-6666-4666-8666-666666666666",
                        "command_id": COMMAND_ID,
                    }
                }
            ],
        )

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        client.confirm_operator_correlation_resolution(
            tenant_ref="lancemos",
            funnel_ref="psicologajohanna",
            actor_ref="juan-operator",
            command_id=COMMAND_ID,
            expected_action="resolve_with_candidate",
            expected_purchase_intent_id=CANDIDATE_ID,
        )
    )

    assert result["command_id"] == COMMAND_ID
    assert seen == [
        (
            "POST",
            "/rest/v1/rpc/confirm_operator_correlation_resolution",
            {
                "p_tenant_ref": "lancemos",
                "p_funnel_ref": "psicologajohanna",
                "p_actor_ref": "juan-operator",
                "p_command_id": COMMAND_ID,
                "p_expected_action": "resolve_with_candidate",
                "p_expected_purchase_intent_id": CANDIDATE_ID,
            },
        )
    ]


def test_supabase_classifies_only_known_resolution_conflicts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "code": "55000",
                "message": "operator_correlation_stale_evidence",
            },
        )

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OperatorCorrelationResolutionError) as raised:
        asyncio.run(
            client.confirm_operator_correlation_resolution(
                tenant_ref="lancemos",
                funnel_ref="psicologajohanna",
                actor_ref="juan-operator",
                command_id=COMMAND_ID,
                expected_action="resolve_with_candidate",
                expected_purchase_intent_id=CANDIDATE_ID,
            )
        )

    assert raised.value.reason == "operator_correlation_stale_evidence"


def test_confirm_maps_stale_evidence_to_conflict() -> None:
    with _app(_StaleResolutionStore()) as client:
        response = client.post(
            "/internal/operator/correlations/resolutions/confirm",
            headers={"Authorization": f"Bearer {'w' * 32}"},
            json={
                "command_id": COMMAND_ID,
                "expected_action": "resolve_with_candidate",
                "expected_candidate_id": CANDIDATE_ID,
            },
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "operator_correlation_stale_evidence"}
