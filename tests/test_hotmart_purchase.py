"""Tests for deterministic Hotmart purchase-approved processing."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from bridge.hotmart import EVENT_PURCHASE_APPROVED, parse_hotmart_purchase_payload
from bridge.supabase import (
    PurchaseCorrelationResult,
    SupabaseClient,
    SupabaseError,
    SupabasePermanentError,
)
from bridge.worker import ResolutionWorker

PURCHASE_APPROVED_PAYLOAD: dict[str, object] = {
    "id": "purchase-event-001",
    "creation_date": 1786212000000,
    "event": "PURCHASE_APPROVED",
    "version": "2.0.0",
    "data": {
        "product": {
            "id": 3526906,
            "ucode": "product-ucode-001",
            "name": "Product Name",
        },
        "buyer": {
            "name": "Buyer name",
            "email": " Buyer@Email.com.br ",
            "checkout_phone": "+55 (31) 99999-9999",
        },
        "purchase": {
            "approved_date": 1786211999000,
            "status": "APPROVED",
            "transaction": "HP17715690036014",
            "offer": {"code": "n82b9jqz"},
        },
    },
}


def test_parses_official_purchase_approved_identifiers() -> None:
    parsed = parse_hotmart_purchase_payload(PURCHASE_APPROVED_PAYLOAD)

    assert parsed is not None
    assert parsed.event_id == "purchase-event-001"
    assert parsed.transaction == "HP17715690036014"
    assert parsed.buyer_email == "buyer@email.com.br"
    assert parsed.buyer_phone == "5531999999999"
    assert parsed.product_id == 3526906
    assert parsed.product_ucode == "product-ucode-001"
    assert parsed.offer_code == "n82b9jqz"
    assert parsed.approved_date_ms == 1786211999000


def test_rejects_purchase_with_malformed_transaction_reference() -> None:
    payload = copy.deepcopy(PURCHASE_APPROVED_PAYLOAD)
    data = payload["data"]
    assert isinstance(data, dict)
    purchase = data["purchase"]
    assert isinstance(purchase, dict)
    purchase["transaction"] = "HP123/../../other"

    assert parse_hotmart_purchase_payload(payload) is None


def test_rejects_purchase_with_out_of_range_approved_timestamp() -> None:
    payload = copy.deepcopy(PURCHASE_APPROVED_PAYLOAD)
    data = payload["data"]
    assert isinstance(data, dict)
    purchase = data["purchase"]
    assert isinstance(purchase, dict)
    purchase["approved_date"] = 10**30

    assert parse_hotmart_purchase_payload(payload) is None


def test_calls_atomic_purchase_correlation_rpc() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[{
                "outcome": "applied",
                "recovery_case_id": "case-001",
                "matched_by": "email_and_phone",
            }],
        )

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client.apply_hotmart_purchase_approved(
        webhook_event_id="event-001",
        buyer_email="buyer@example.test",
        buyer_phone="5531999999999",
        external_product_id="3526906",
        offer_code="offer-001",
        transaction="HP17715690036014",
        approved_at="2026-08-08T12:00:00+00:00",
    ))

    assert result.outcome == "applied"
    assert result.recovery_case_id == "case-001"
    assert result.matched_by == "email_and_phone"
    assert requests[0].url.path == "/rest/v1/rpc/apply_hotmart_purchase_approved"
    assert json.loads(requests[0].content) == {
        "p_webhook_event_id": "event-001",
        "p_buyer_email": "buyer@example.test",
        "p_buyer_phone": "5531999999999",
        "p_external_product_id": "3526906",
        "p_offer_code": "offer-001",
        "p_transaction": "HP17715690036014",
        "p_approved_at": "2026-08-08T12:00:00+00:00",
    }


def _invoke_purchase_rpc_error(
    *, status_code: int, response_body: object
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=response_body)

    client = SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(client.apply_hotmart_purchase_approved(
        webhook_event_id="event-001",
        buyer_email="buyer@example.test",
        buyer_phone="5531999999999",
        external_product_id="3526906",
        offer_code="offer-001",
        transaction="HP17715690036014",
        approved_at="2026-08-08T12:00:00+00:00",
    ))


@pytest.mark.parametrize(
    "status_code",
    [400, 401, 403, 404, 408, 409, 425, 429],
)
def test_purchase_rpc_retries_unclassified_http_errors(status_code: int) -> None:
    with pytest.raises(SupabaseError) as exc_info:
        _invoke_purchase_rpc_error(
            status_code=status_code,
            response_body={"message": "temporary or operational failure"},
        )

    assert not isinstance(exc_info.value, SupabasePermanentError)


@pytest.mark.parametrize(
    ("sqlstate", "message"),
    [
        ("22023", "invalid_purchase_correlation_input"),
        ("22023", "webhook_event_not_purchase_approved"),
        ("22023", "purchase_event_invalid_approved_date"),
        ("22023", "purchase_rpc_payload_mismatch"),
        ("22023", "purchase_approved_at_in_future"),
    ],
)
def test_purchase_rpc_quarantines_explicit_contract_errors(
    sqlstate: str, message: str
) -> None:
    with pytest.raises(SupabasePermanentError):
        _invoke_purchase_rpc_error(
            status_code=400,
            response_body={"code": sqlstate, "message": message},
        )


@pytest.mark.parametrize(
    ("sqlstate", "message"),
    [
        ("22023", "unknown_contract_error"),
        ("23514", "internal_constraint_regression"),
    ],
)
def test_purchase_rpc_retries_unclassified_sql_errors(
    sqlstate: str, message: str
) -> None:
    with pytest.raises(SupabaseError) as exc_info:
        _invoke_purchase_rpc_error(
            status_code=400,
            response_body={"code": sqlstate, "message": message},
        )

    assert not isinstance(exc_info.value, SupabasePermanentError)


def test_resolution_worker_routes_purchase_to_atomic_correlation() -> None:
    calls: list[dict[str, object]] = []

    class SupabaseStub:
        async def apply_hotmart_purchase_approved(
            self, **kwargs: object
        ) -> PurchaseCorrelationResult:
            calls.append(kwargs)
            return PurchaseCorrelationResult(
                outcome="applied",
                recovery_case_id="case-001",
                matched_by="email_and_phone",
            )

    worker = ResolutionWorker(supabase=SupabaseStub())  # type: ignore[arg-type]
    asyncio.run(worker._process_one({
        "id": "event-internal-001",
        "event_type": "PURCHASE_APPROVED",
        "payload": PURCHASE_APPROVED_PAYLOAD,
    }))

    assert calls == [{
        "webhook_event_id": "event-internal-001",
        "buyer_email": "buyer@email.com.br",
        "buyer_phone": "5531999999999",
        "external_product_id": "3526906",
        "offer_code": "n82b9jqz",
        "transaction": "HP17715690036014",
        "approved_at": "2026-08-08T17:59:59+00:00",
    }]


def test_resolution_worker_fails_closed_when_persisted_event_type_is_missing() -> None:
    updates: list[dict[str, object]] = []

    class SupabaseStub:
        async def update_event_status(self, **kwargs: object) -> None:
            updates.append(kwargs)

    worker = ResolutionWorker(supabase=SupabaseStub())  # type: ignore[arg-type]
    asyncio.run(worker._process_one({
        "id": "event-without-type",
        "payload": {},
    }))

    assert updates == [{
        "event_id": "event-without-type",
        "status": "failed",
        "error": "unsupported_persisted_event_type",
    }]


def test_resolution_worker_quarantines_permanent_purchase_rpc_error() -> None:
    updates: list[dict[str, object]] = []

    class SupabaseStub:
        async def apply_hotmart_purchase_approved(self, **_: object) -> None:
            raise SupabasePermanentError(
                "apply_hotmart_purchase_approved_failed: HTTP 400"
            )

        async def update_event_status(self, **kwargs: object) -> None:
            updates.append(kwargs)

    worker = ResolutionWorker(supabase=SupabaseStub())  # type: ignore[arg-type]
    asyncio.run(worker._process_one({
        "id": "purchase-poison-001",
        "event_type": "PURCHASE_APPROVED",
        "payload": PURCHASE_APPROVED_PAYLOAD,
    }))

    assert updates == [{
        "event_id": "purchase-poison-001",
        "status": "failed",
        "error": "purchase_rpc_permanent_failure",
    }]


def test_resolution_worker_does_not_let_one_transient_event_starve_batch() -> None:
    processed: list[str] = []
    events = [
        {
            "id": "purchase-transient-001",
            "event_type": "PURCHASE_APPROVED",
            "payload": PURCHASE_APPROVED_PAYLOAD,
        },
        {
            "id": "purchase-following-002",
            "event_type": "PURCHASE_APPROVED",
            "payload": PURCHASE_APPROVED_PAYLOAD,
        },
    ]

    class SupabaseStub:
        async def fetch_pending_events(self, **_: object) -> list[dict[str, object]]:
            return events

        async def apply_hotmart_purchase_approved(
            self, *, webhook_event_id: str, **_: object
        ) -> PurchaseCorrelationResult:
            if webhook_event_id == "purchase-transient-001":
                raise SupabaseError("supabase_request_failed")
            processed.append(webhook_event_id)
            return PurchaseCorrelationResult(
                outcome="applied",
                recovery_case_id="case-002",
                matched_by="email",
            )

    worker = ResolutionWorker(supabase=SupabaseStub())  # type: ignore[arg-type]
    asyncio.run(worker._process_batch())

    assert processed == ["purchase-following-002"]


def test_disabled_purchase_worker_excludes_purchase_backlog() -> None:
    fetch_calls: list[dict[str, object]] = []

    class SupabaseStub:
        async def fetch_pending_events(
            self, **kwargs: object
        ) -> list[dict[str, object]]:
            fetch_calls.append(kwargs)
            return []

    worker = ResolutionWorker(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        purchase_worker_enabled=False,
    )

    asyncio.run(worker._process_batch())

    assert fetch_calls == [{
        "limit": 10,
        "excluded_event_types": (EVENT_PURCHASE_APPROVED,),
    }]
