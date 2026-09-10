"""Strict HTTP client for tenant-scoped operator correlation endpoints."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

import httpx

_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
_REJECTION_DETAILS_BY_STATUS = {
    404: frozenset({"operator_correlation_case_not_found"}),
    409: frozenset(
        {
            "operator_correlation_already_resolved",
            "operator_correlation_command_expired",
            "operator_correlation_stale_evidence",
            "operator_correlation_idempotency_conflict",
        }
    ),
    422: frozenset({"invalid_operator_correlation_resolution"}),
}
_REDACTED_REJECTION = "operator_bridge_rejected"
_INVALID_JSON = object()


class OperatorBridgeError(RuntimeError):
    """Base class for sanitized operator bridge failures."""


class OperatorBridgeUnavailable(OperatorBridgeError):
    """Raised when the bridge cannot provide a trustworthy response."""


class OperatorBridgeProtocolError(OperatorBridgeUnavailable):
    """Raised when the bridge response violates the expected protocol."""


class OperatorBridgeRejected(OperatorBridgeError):
    """Raised when the bridge explicitly rejects a domain operation."""

    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class OperatorBridgeClient:
    """Call scoped read/write endpoints with separate least-privilege bearers."""

    def __init__(
        self,
        *,
        base_url: str,
        read_bearer: str,
        write_bearer: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = _validate_base_url(base_url)
        self._read_bearer = _required_secret(read_bearer, "read_bearer")
        self._write_bearer = _required_secret(write_bearer, "write_bearer")
        if self._read_bearer == self._write_bearer:
            raise ValueError("operator_bearers_must_be_distinct")
        self._transport = transport

    async def get_case(self, case_id: str) -> dict[str, Any]:
        """Fetch one unresolved case and prove its requested identity."""
        case_id = _required_identifier(case_id, "case_id")
        response = await self._request(
            "GET",
            f"/internal/operator/correlations/unresolved/{quote(case_id, safe='')}",
            bearer=self._read_bearer,
        )
        payload = _json_object(response)
        case = payload.get("case")
        if not isinstance(case, dict) or case.get("case_id") != case_id:
            raise OperatorBridgeProtocolError("operator_bridge_protocol_error")
        return case

    async def prepare(
        self,
        case_id: str,
        idempotency_key: str,
        action: str,
        candidate_id: str | None,
        verification_basis: str,
        actor_ref: str | None = None,
    ) -> dict[str, Any]:
        """Prepare an immutable resolution command without applying it."""
        case_id = _required_identifier(case_id, "case_id")
        idempotency_key = _required_identifier(idempotency_key, "idempotency_key")
        action = _required_identifier(action, "action")
        verification_basis = _required_identifier(
            verification_basis, "verification_basis"
        )
        if candidate_id is not None:
            candidate_id = _required_identifier(candidate_id, "candidate_id")
        if actor_ref is not None:
            actor_ref = _required_identifier(actor_ref, "actor_ref")
            if not actor_ref.startswith("slack."):
                raise ValueError("invalid_actor_ref")
        request_json: dict[str, object] = {
            "case_id": case_id,
            "idempotency_key": idempotency_key,
            "action": action,
            "candidate_id": candidate_id,
            "verification_basis": verification_basis,
        }
        if actor_ref is not None:
            request_json["actor_ref"] = actor_ref
        response = await self._request(
            "POST",
            "/internal/operator/correlations/resolutions/prepare",
            bearer=self._write_bearer,
            json=request_json,
        )
        payload = _json_object(response)
        command = payload.get("command")
        if (
            not isinstance(command, dict)
            or not _is_identifier(command.get("command_id"))
            or command.get("case_id") != case_id
            or command.get("idempotency_key") != idempotency_key
            or command.get("action") != action
            or command.get("candidate_id") != candidate_id
            or command.get("verification_basis") != verification_basis
        ):
            raise OperatorBridgeProtocolError("operator_bridge_protocol_error")
        return command

    async def confirm(
        self,
        command_id: str,
        expected_action: str,
        expected_candidate_id: str | None,
        actor_ref: str | None = None,
    ) -> dict[str, Any]:
        """Confirm a prepared command while reasserting its immutable identity."""
        command_id = _required_identifier(command_id, "command_id")
        expected_action = _required_identifier(expected_action, "expected_action")
        if expected_candidate_id is not None:
            expected_candidate_id = _required_identifier(
                expected_candidate_id, "expected_candidate_id"
            )
        if actor_ref is not None:
            actor_ref = _required_identifier(actor_ref, "actor_ref")
            if not actor_ref.startswith("slack."):
                raise ValueError("invalid_actor_ref")
        request_json: dict[str, object] = {
            "command_id": command_id,
            "expected_action": expected_action,
            "expected_candidate_id": expected_candidate_id,
        }
        if actor_ref is not None:
            request_json["actor_ref"] = actor_ref
        response = await self._request(
            "POST",
            "/internal/operator/correlations/resolutions/confirm",
            bearer=self._write_bearer,
            json=request_json,
        )
        payload = _json_object(response)
        resolution = payload.get("resolution")
        expected_outcome = (
            "linked_candidate"
            if expected_action == "resolve_with_candidate"
            else "closed_without_match"
        )
        if (
            not isinstance(resolution, dict)
            or resolution.get("command_id") != command_id
            or not _is_identifier(resolution.get("resolution_id"))
            or not _is_identifier(resolution.get("case_id"))
            or resolution.get("resolution_outcome") != expected_outcome
            or resolution.get("effective_purchase_intent_id")
            != expected_candidate_id
        ):
            raise OperatorBridgeProtocolError("operator_bridge_protocol_error")
        return resolution

    async def _request(
        self,
        method: str,
        path: str,
        *,
        bearer: str,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        response: httpx.Response | None = None
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {bearer}"},
                transport=self._transport,
                timeout=_TIMEOUT,
                follow_redirects=False,
            ) as client:
                response = await client.request(method, path, json=json)
        except httpx.HTTPError:
            pass
        if response is None:
            raise OperatorBridgeUnavailable("operator_bridge_unavailable")
        allowed_details = _REJECTION_DETAILS_BY_STATUS.get(response.status_code)
        if allowed_details is not None:
            detail = _response_detail(response)
            if not isinstance(detail, str) or detail not in allowed_details:
                detail = _REDACTED_REJECTION
            raise OperatorBridgeRejected(
                status_code=response.status_code,
                detail=detail,
            )
        if response.status_code != 200:
            raise OperatorBridgeUnavailable("operator_bridge_unavailable")
        return response


def _response_detail(response: httpx.Response) -> object:
    payload: object = _INVALID_JSON
    try:
        payload = response.json()
    except ValueError:
        pass
    return payload.get("detail") if isinstance(payload, dict) else None


def _json_object(response: httpx.Response) -> dict[str, Any]:
    payload: object = _INVALID_JSON
    try:
        payload = response.json()
    except ValueError:
        pass
    if not isinstance(payload, dict):
        raise OperatorBridgeProtocolError("operator_bridge_protocol_error")
    return payload


def _validate_base_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("invalid_operator_bridge_base_url")
    parsed = urlsplit(base_url)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("invalid_operator_bridge_base_url") from None
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or not parsed.hostname
    ):
        raise ValueError("invalid_operator_bridge_base_url")
    production = parsed.scheme == "https"
    loopback_test = (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None
    )
    if not production and not loopback_test:
        raise ValueError("invalid_operator_bridge_base_url")
    return base_url.rstrip("/")


def _required_secret(value: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field}_is_required")
    return value


def _is_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not any(character.isspace() for character in value)
    )


def _required_identifier(value: str, field: str) -> str:
    if not _is_identifier(value):
        raise ValueError(f"invalid_{field}")
    return value
