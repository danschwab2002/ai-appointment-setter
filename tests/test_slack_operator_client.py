import asyncio

import httpx
import pytest

from slack_correlation.operator_client import (
    OperatorBridgeClient,
    OperatorBridgeProtocolError,
    OperatorBridgeRejected,
    OperatorBridgeUnavailable,
)

CASE_ID = "11111111-1111-4111-8111-111111111111"
COMMAND_ID = "55555555-5555-4555-8555-555555555555"
CANDIDATE_ID = "33333333-3333-4333-8333-333333333333"
IDEMPOTENCY_KEY = "77777777-7777-4777-8777-777777777777"
READ_TOKEN = "read-" + "r" * 32
WRITE_TOKEN = "write-" + "w" * 32


def test_get_case_uses_read_bearer_and_validates_case_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"case": {"case_id": CASE_ID, "automation_blocked": True}},
            request=request,
        )

    client = OperatorBridgeClient(
        base_url="http://127.0.0.1:8080",
        read_bearer=READ_TOKEN,
        write_bearer=WRITE_TOKEN,
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(client.get_case(CASE_ID))

    assert result == {"case_id": CASE_ID, "automation_blocked": True}
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == (
        f"/internal/operator/correlations/unresolved/{CASE_ID}"
    )
    assert requests[0].headers["Authorization"] == f"Bearer {READ_TOKEN}"
    assert requests[0].extensions["timeout"] == {
        "connect": 5.0,
        "read": 10.0,
        "write": 10.0,
        "pool": 5.0,
    }


def test_prepare_uses_write_bearer_and_returns_matching_command() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "command": {
                    "command_id": COMMAND_ID,
                    "case_id": CASE_ID,
                    "idempotency_key": IDEMPOTENCY_KEY,
                    "action": "resolve_with_candidate",
                    "candidate_id": CANDIDATE_ID,
                    "verification_basis": "operator_source_record",
                }
            },
            request=request,
        )

    client = OperatorBridgeClient(
        base_url="https://bridge.example.test",
        read_bearer=READ_TOKEN,
        write_bearer=WRITE_TOKEN,
        transport=httpx.MockTransport(handler),
    )

    command = asyncio.run(
        client.prepare(
            CASE_ID,
            IDEMPOTENCY_KEY,
            "resolve_with_candidate",
            CANDIDATE_ID,
            "operator_source_record",
        )
    )

    assert command["command_id"] == COMMAND_ID
    assert requests[0].method == "POST"
    assert requests[0].url.path == (
        "/internal/operator/correlations/resolutions/prepare"
    )
    assert requests[0].headers["Authorization"] == f"Bearer {WRITE_TOKEN}"
    assert requests[0].read().decode() == (
        '{"case_id":"' + CASE_ID + '","idempotency_key":"' + IDEMPOTENCY_KEY
        + '","action":"resolve_with_candidate","candidate_id":"' + CANDIDATE_ID
        + '","verification_basis":"operator_source_record"}'
    )


def test_confirm_uses_write_bearer_and_validates_command_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "resolution": {
                    "resolution_id": "66666666-6666-4666-8666-666666666666",
                    "command_id": COMMAND_ID,
                    "case_id": CASE_ID,
                    "resolution_outcome": "linked_candidate",
                    "effective_purchase_intent_id": CANDIDATE_ID,
                }
            },
            request=request,
        )

    client = OperatorBridgeClient(
        base_url="http://127.0.0.1:9090/",
        read_bearer=READ_TOKEN,
        write_bearer=WRITE_TOKEN,
        transport=httpx.MockTransport(handler),
    )

    resolution = asyncio.run(
        client.confirm(COMMAND_ID, "resolve_with_candidate", CANDIDATE_ID)
    )

    assert resolution["command_id"] == COMMAND_ID
    assert requests[0].url.path == (
        "/internal/operator/correlations/resolutions/confirm"
    )
    assert requests[0].headers["Authorization"] == f"Bearer {WRITE_TOKEN}"
    assert requests[0].read().decode() == (
        '{"command_id":"' + COMMAND_ID
        + '","expected_action":"resolve_with_candidate",'
        '"expected_candidate_id":"' + CANDIDATE_ID + '"}'
    )


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (404, "operator_correlation_case_not_found"),
        (409, "operator_correlation_stale_evidence"),
        (422, "invalid_operator_correlation_resolution"),
    ],
)
def test_explicit_domain_statuses_raise_typed_allowlisted_rejection(
    status: int, detail: str
) -> None:
    client = OperatorBridgeClient(
        base_url="https://bridge.example.test",
        read_bearer=READ_TOKEN,
        write_bearer=WRITE_TOKEN,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                json={"detail": detail, "secret": "raw-sensitive-body"},
                request=request,
            )
        ),
    )

    with pytest.raises(OperatorBridgeRejected) as raised:
        asyncio.run(client.get_case(CASE_ID))

    assert raised.value.status_code == status
    assert raised.value.detail == detail
    assert str(raised.value) == detail
    assert "raw-sensitive-body" not in repr(raised.value)


def test_prepare_rejects_mismatched_verification_identity() -> None:
    client = OperatorBridgeClient(
        base_url="https://bridge.example.test",
        read_bearer=READ_TOKEN,
        write_bearer=WRITE_TOKEN,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "command": {
                        "command_id": COMMAND_ID,
                        "case_id": CASE_ID,
                        "idempotency_key": IDEMPOTENCY_KEY,
                        "action": "resolve_with_candidate",
                        "candidate_id": CANDIDATE_ID,
                        "verification_basis": "customer_confirmation",
                    }
                },
                request=request,
            )
        ),
    )

    with pytest.raises(OperatorBridgeProtocolError) as raised:
        asyncio.run(
            client.prepare(
                CASE_ID,
                IDEMPOTENCY_KEY,
                "resolve_with_candidate",
                CANDIDATE_ID,
                "operator_source_record",
            )
        )

    assert str(raised.value) == "operator_bridge_protocol_error"


def test_unknown_rejection_detail_is_redacted() -> None:
    raw_detail = f"database exploded with {READ_TOKEN} and customer payload"
    client = OperatorBridgeClient(
        base_url="https://bridge.example.test",
        read_bearer=READ_TOKEN,
        write_bearer=WRITE_TOKEN,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                409, json={"detail": raw_detail}, request=request
            )
        ),
    )

    with pytest.raises(OperatorBridgeRejected) as raised:
        asyncio.run(client.get_case(CASE_ID))

    assert raised.value.detail == "operator_bridge_rejected"
    assert raw_detail not in repr(raised.value)
    assert READ_TOKEN not in repr(raised.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"private raw body"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"case": []}),
        httpx.Response(200, json={"case": {"case_id": "foreign-case"}}),
    ],
)
def test_invalid_json_shape_or_identity_is_a_sanitized_protocol_error(
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response.request = request
        return response

    client = OperatorBridgeClient(
        base_url="https://bridge.example.test",
        read_bearer=READ_TOKEN,
        write_bearer=WRITE_TOKEN,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(OperatorBridgeProtocolError) as raised:
        asyncio.run(client.get_case(CASE_ID))

    assert str(raised.value) == "operator_bridge_protocol_error"
    assert raised.value.__context__ is None
    assert "private raw body" not in repr(raised.value)
    assert READ_TOKEN not in repr(raised.value)


@pytest.mark.parametrize("status", [401, 500, 503])
def test_unexpected_http_status_is_sanitized_unavailable(status: int) -> None:
    client = OperatorBridgeClient(
        base_url="https://bridge.example.test",
        read_bearer=READ_TOKEN,
        write_bearer=WRITE_TOKEN,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                content=f"private {READ_TOKEN}".encode(),
                request=request,
            )
        ),
    )

    with pytest.raises(OperatorBridgeUnavailable) as raised:
        asyncio.run(client.get_case(CASE_ID))

    assert str(raised.value) == "operator_bridge_unavailable"
    assert READ_TOKEN not in repr(raised.value)


def test_transport_failure_is_sanitized_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"private {READ_TOKEN}", request=request)

    client = OperatorBridgeClient(
        base_url="https://bridge.example.test",
        read_bearer=READ_TOKEN,
        write_bearer=WRITE_TOKEN,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(OperatorBridgeUnavailable) as raised:
        asyncio.run(client.get_case(CASE_ID))

    assert str(raised.value) == "operator_bridge_unavailable"
    assert raised.value.__context__ is None
    assert READ_TOKEN not in repr(raised.value)


def test_redirect_is_not_followed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"Location": "https://attacker.example/steal"},
            request=request,
        )

    client = OperatorBridgeClient(
        base_url="https://bridge.example.test",
        read_bearer=READ_TOKEN,
        write_bearer=WRITE_TOKEN,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(OperatorBridgeUnavailable):
        asyncio.run(client.get_case(CASE_ID))

    assert len(requests) == 1
    assert requests[0].url.host == "bridge.example.test"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://bridge.example.test",
        "http://localhost:8080",
        "http://127.0.0.1",
        "https://user:password@bridge.example.test",
        "https://bridge.example.test/internal",
        "https://bridge.example.test:bad",
        "https://bridge.example.test:99999",
        "https://bridge.example.test?tenant=other",
        "https://bridge.example.test#fragment",
    ],
)
def test_base_url_must_be_https_origin_or_ported_ipv4_loopback(
    base_url: str,
) -> None:
    with pytest.raises(ValueError, match="invalid_operator_bridge_base_url"):
        OperatorBridgeClient(
            base_url=base_url,
            read_bearer=READ_TOKEN,
            write_bearer=WRITE_TOKEN,
        )


def test_read_and_write_bearers_must_be_distinct() -> None:
    with pytest.raises(ValueError, match="operator_bearers_must_be_distinct"):
        OperatorBridgeClient(
            base_url="https://bridge.example.test",
            read_bearer=READ_TOKEN,
            write_bearer=READ_TOKEN,
        )
