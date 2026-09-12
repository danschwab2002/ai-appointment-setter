import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from bridge.operator_correlations import (
    InvalidCorrelationEvidence,
    build_unresolved_correlation,
    mask_email,
    mask_phone,
)
from bridge.app import Settings, create_app
from bridge.supabase import SupabaseClient


def test_operator_identity_is_masked_before_leaving_the_bridge() -> None:
    assert mask_email("buyer@example.com") == "b***r@example.com"
    assert mask_email("a@example.com") == "***@example.com"
    assert mask_email("ab@example.com") == "***@example.com"
    assert mask_phone("593991234567") == "********4567"
    assert mask_email(None) is None
    assert mask_phone(None) is None


def test_unresolved_correlation_explains_conflict_without_exposing_identity() -> None:
    raw = {
        "webhook_event_id": "11111111-1111-4111-8111-111111111111",
        "scope_id": "22222222-2222-4222-8222-222222222222",
        "event_type": "PURCHASE_APPROVED",
        "outcome": "conflict",
        "candidate_count": 2,
        "reason_code": "email_phone_conflict",
        "manual_handoff_required": True,
        "observed_at": "2026-08-24T10:00:00+00:00",
        "scope": {
            "tenant_ref": "lancemos",
            "funnel_ref": "psicologajohanna",
            "product_ref": "f106691755g",
            "offer_ref": "bxjge6zq",
        },
        "identity": {
            "email_present": True,
            "phone_present": True,
            "masked_email": "b***r@example.com",
            "masked_phone": "********4567",
        },
        "candidates": [
            {
                "purchase_intent_id": "33333333-3333-4333-8333-333333333333",
                "email_match": True,
                "phone_match": False,
                "submitted_at": "2026-08-24T09:00:00+00:00",
                "lifecycle_state": "waiting_for_purchase",
                "masked_email": "b***r@example.com",
                "masked_phone": "********9999",
            },
            {
                "purchase_intent_id": "44444444-4444-4444-8444-444444444444",
                "email_match": False,
                "phone_match": True,
                "submitted_at": "2026-08-24T09:05:00+00:00",
                "lifecycle_state": "waiting_for_purchase",
                "masked_email": "o***r@example.com",
                "masked_phone": "********4567",
            },
        ],
    }

    result = build_unresolved_correlation(raw, include_candidates=True)

    assert result["case_id"] == raw["webhook_event_id"]
    assert result["outcome"] == "conflict"
    assert result["reason"] == (
        "El email y el teléfono apuntan a intenciones diferentes o una señal "
        "contradice a la otra."
    )
    assert result["automation_blocked"] is True
    assert result["identity"] == {
        "email_present": True,
        "phone_present": True,
        "masked_email": "b***r@example.com",
        "masked_phone": "********4567",
    }
    assert result["candidates"][0]["matched_by"] == ["email"]
    assert result["candidates"][1]["matched_by"] == ["phone"]
    rendered = repr(result)
    assert "buyer@example.com" not in rendered
    assert "593991234567" not in rendered


def test_scoped_projection_may_omit_foreign_candidates_fail_closed() -> None:
    raw = _raw_conflict()
    raw["candidate_count"] = 1
    raw["candidates"] = []

    case = build_unresolved_correlation(raw, include_candidates=True)

    assert case["candidate_count"] == 1
    assert case["candidates"] == []
    assert case["automation_blocked"] is True


def test_non_handoff_correlation_cannot_be_presented_as_unresolved() -> None:
    with pytest.raises(InvalidCorrelationEvidence, match="not_unresolved"):
        build_unresolved_correlation(
            {
                "webhook_event_id": "11111111-1111-4111-8111-111111111111",
                "event_type": "PURCHASE_APPROVED",
                "outcome": "resolved",
                "candidate_count": 1,
                "reason_code": "exact_email",
                "manual_handoff_required": False,
                "observed_at": "2026-08-24T10:00:00+00:00",
                "scope": None,
                "identity": None,
                "candidates": [],
            },
            include_candidates=False,
        )


def test_raw_identity_is_rejected_outside_exact_private_review() -> None:
    raw = _raw_conflict()
    raw["identity"] = {
        "normalized_email": "buyer@example.com",
        "normalized_phone": "593991234567",
    }

    with pytest.raises(InvalidCorrelationEvidence, match="contains_raw_identity"):
        build_unresolved_correlation(raw, include_candidates=False)
    with pytest.raises(InvalidCorrelationEvidence, match="contains_raw_identity"):
        build_unresolved_correlation(raw, include_candidates=True)


def test_exact_private_review_projects_complete_identity() -> None:
    raw = _raw_conflict()
    raw["identity"] = {
        "normalized_email": "buyer@example.com",
        "normalized_phone": "593991234567",
    }
    raw["candidates"][0].update({
        "normalized_email": "buyer@example.com",
        "normalized_phone": "593999999999",
    })

    result = build_unresolved_correlation(
        raw, include_candidates=True, include_private_identity=True
    )

    assert result["identity"] == {
        "email": "buyer@example.com",
        "phone": "593991234567",
    }
    assert result["candidates"][0]["email"] == "buyer@example.com"
    assert result["candidates"][0]["phone"] == "593999999999"
    assert "masked_email" not in result["identity"]
    assert "masked_phone" not in result["candidates"][0]


def test_supabase_reader_lists_only_unresolved_rows_with_read_only_requests() -> None:
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.method, request.url.path, body))
        return httpx.Response(200, json=[{"case_data": _raw_conflict()}])

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )

    rows = asyncio.run(
        client.list_unresolved_purchase_intent_correlations(
            tenant_ref="lancemos",
            funnel_ref="psicologajohanna",
            limit=20,
        )
    )

    assert len(rows) == 1
    assert rows[0]["outcome"] == "conflict"
    assert seen == [
        (
            "POST",
            "/rest/v1/rpc/list_operator_unresolved_correlations",
            {
                "p_tenant_ref": "lancemos",
                "p_funnel_ref": "psicologajohanna",
                "p_limit": 20,
                "p_webhook_event_id": None,
            },
        )
    ]


def test_supabase_reader_filters_masked_list_by_exact_case_id() -> None:
    case_id = "11111111-1111-4111-8111-111111111111"

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "p_tenant_ref": "lancemos",
            "p_funnel_ref": "psicologajohanna",
            "p_limit": 1,
            "p_webhook_event_id": case_id,
        }
        return httpx.Response(200, json=[{"case_data": _raw_conflict()}])

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )

    rows = asyncio.run(
        client.list_unresolved_purchase_intent_correlations(
            tenant_ref="lancemos",
            funnel_ref="psicologajohanna",
            limit=1,
            webhook_event_id=case_id,
        )
    )

    assert rows == [_raw_conflict()]


def test_supabase_reader_gets_one_unresolved_case_by_exact_id() -> None:
    case_id = "11111111-1111-4111-8111-111111111111"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith(
            "/rpc/get_operator_unresolved_correlation"
        )
        assert json.loads(request.content) == {
            "p_tenant_ref": "lancemos",
            "p_funnel_ref": "psicologajohanna",
            "p_webhook_event_id": case_id,
        }
        return httpx.Response(200, json=[])

    client = SupabaseClient(
        base_url="https://example.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(
        client.get_unresolved_purchase_intent_correlation(
            tenant_ref="lancemos",
            funnel_ref="psicologajohanna",
            webhook_event_id=case_id,
        )
    )

    assert result is None


def _raw_conflict() -> dict[str, object]:
    return {
        "webhook_event_id": "11111111-1111-4111-8111-111111111111",
        "scope_id": "22222222-2222-4222-8222-222222222222",
        "event_type": "PURCHASE_APPROVED",
        "outcome": "conflict",
        "candidate_count": 1,
        "reason_code": "email_phone_conflict",
        "manual_handoff_required": True,
        "observed_at": "2026-08-24T10:00:00+00:00",
        "scope": {
            "tenant_ref": "lancemos",
            "funnel_ref": "psicologajohanna",
            "product_ref": "f106691755g",
            "offer_ref": "bxjge6zq",
        },
        "identity": {
            "email_present": True,
            "phone_present": True,
            "masked_email": "b***r@example.com",
            "masked_phone": "********4567",
        },
        "candidates": [
            {
                "purchase_intent_id": "33333333-3333-4333-8333-333333333333",
                "email_match": True,
                "phone_match": False,
                "submitted_at": "2026-08-24T09:00:00+00:00",
                "lifecycle_state": "waiting_for_purchase",
                "masked_email": "b***r@example.com",
                "masked_phone": "********9999",
            }
        ],
    }


class _FakeCorrelationReader:
    def __init__(self, *, private_identity: bool = True) -> None:
        self.private_identity = private_identity

    async def list_unresolved_purchase_intent_correlations(
        self,
        *,
        tenant_ref: str,
        funnel_ref: str,
        limit: int = 20,
        webhook_event_id: str | None = None,
    ) -> list[dict[str, object]]:
        assert tenant_ref == "lancemos"
        assert funnel_ref == "psicologajohanna"
        assert (limit, webhook_event_id) in {
            (20, None),
            (1, _raw_conflict()["webhook_event_id"]),
        }
        return [_raw_conflict()]

    async def get_unresolved_purchase_intent_correlation(
        self, *, tenant_ref: str, funnel_ref: str, webhook_event_id: str
    ) -> dict[str, object] | None:
        assert tenant_ref == "lancemos"
        assert funnel_ref == "psicologajohanna"
        if webhook_event_id == _raw_conflict()["webhook_event_id"]:
            raw = _raw_conflict()
            if not self.private_identity:
                return raw
            raw["identity"] = {
                "normalized_email": "buyer@example.com",
                "normalized_phone": "593991234567",
            }
            raw["candidates"][0].update({
                "normalized_email": "buyer@example.com",
                "normalized_phone": "593999999999",
            })
            return raw
        return None


def _operator_app(
    *, enabled: bool = True, reader: _FakeCorrelationReader | None = None
) -> TestClient:
    settings = Settings(
        webhook_secret="unused",
        allowed_jid="593999999999@s.whatsapp.net",
        capture_dir=Path("/tmp/operator-correlation-tests"),
        max_age_seconds=300,
        operator_correlation_read_enabled=enabled,
        operator_correlation_read_token=("t" * 32 if enabled else None),
        operator_correlation_tenant_ref=("lancemos" if enabled else None),
        operator_correlation_funnel_ref=(
            "psicologajohanna" if enabled else None
        ),
    )
    return TestClient(
        create_app(
            settings,
            supabase_client=reader or _FakeCorrelationReader(),  # type: ignore[arg-type]
        )
    )


def test_operator_endpoint_lists_masked_unresolved_cases() -> None:
    with _operator_app() as client:
        response = client.get(
            "/internal/operator/correlations/unresolved",
            headers={"Authorization": f"Bearer {'t' * 32}"},
        )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    rendered = response.text
    assert "b***r@example.com" in rendered
    assert "********4567" in rendered
    assert "buyer@example.com" not in rendered
    assert "593991234567" not in rendered


def test_operator_endpoint_gets_exact_case_from_masked_list_projection() -> None:
    case_id = "11111111-1111-4111-8111-111111111111"
    with _operator_app() as client:
        response = client.get(
            "/internal/operator/correlations/unresolved",
            params={"limit": 1, "case_id": case_id},
            headers={"Authorization": f"Bearer {'t' * 32}"},
        )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    case = response.json()["cases"][0]
    assert case["case_id"] == case_id
    assert case["identity"] == {
        "email_present": True,
        "phone_present": True,
        "masked_email": "b***r@example.com",
        "masked_phone": "********4567",
    }
    assert "buyer@example.com" not in response.text
    assert "593991234567" not in response.text


def test_operator_exact_public_endpoint_stays_masked() -> None:
    case_id = "11111111-1111-4111-8111-111111111111"
    with _operator_app() as client:
        response = client.get(
            f"/internal/operator/correlations/unresolved/{case_id}",
            headers={"Authorization": f"Bearer {'t' * 32}"},
        )

    assert response.status_code == 200
    case = response.json()["case"]
    assert case["identity"] == {
        "email_present": True,
        "phone_present": True,
        "masked_email": "b***r@example.com",
        "masked_phone": "********4567",
    }
    assert "candidates" not in case
    assert "buyer@example.com" not in response.text
    assert "593991234567" not in response.text


def test_operator_endpoint_is_default_off_and_requires_its_own_bearer() -> None:
    with _operator_app(enabled=False) as disabled_client:
        disabled = disabled_client.get(
            "/internal/operator/correlations/unresolved"
        )
    assert disabled.status_code == 404

    with _operator_app() as client:
        missing = client.get("/internal/operator/correlations/unresolved")
        wrong = client.get(
            "/internal/operator/correlations/unresolved",
            headers={"Authorization": "Bearer wrong"},
        )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"


def test_operator_endpoint_gets_exact_case_with_complete_private_identity() -> None:
    case_id = "11111111-1111-4111-8111-111111111111"
    with _operator_app() as client:
        response = client.get(
            f"/internal/operator/correlations/unresolved/{case_id}/private-review",
            headers={"Authorization": f"Bearer {'t' * 32}"},
        )
        missing = client.get(
            "/internal/operator/correlations/unresolved/"
            "99999999-9999-4999-8999-999999999999/private-review",
            headers={"Authorization": f"Bearer {'t' * 32}"},
        )

    assert response.status_code == 200
    case = response.json()["case"]
    assert case["case_id"] == case_id
    assert case["candidates"] == [
        {
            "purchase_intent_id": "33333333-3333-4333-8333-333333333333",
            "matched_by": ["email"],
            "submitted_at": "2026-08-24T09:00:00+00:00",
            "lifecycle_state": "waiting_for_purchase",
            "email": "buyer@example.com",
            "phone": "593999999999",
        }
    ]
    assert case["identity"] == {
        "email": "buyer@example.com",
        "phone": "593991234567",
    }
    assert missing.status_code == 404


def test_private_exact_endpoint_remains_compatible_before_expand_migration() -> None:
    case_id = "11111111-1111-4111-8111-111111111111"
    with _operator_app(reader=_FakeCorrelationReader(private_identity=False)) as client:
        response = client.get(
            f"/internal/operator/correlations/unresolved/{case_id}/private-review",
            headers={"Authorization": f"Bearer {'t' * 32}"},
        )

    assert response.status_code == 200
    case = response.json()["case"]
    assert case["identity"] == {
        "email_present": True,
        "phone_present": True,
        "masked_email": "b***r@example.com",
        "masked_phone": "********4567",
    }
    assert "buyer@example.com" not in response.text
    assert "593991234567" not in response.text
