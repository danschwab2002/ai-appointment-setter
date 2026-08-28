"""Scoped review and human-approved correlation resolution handlers."""

from __future__ import annotations

from importlib import import_module
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

_ACTIONS = frozenset({"resolve_with_candidate", "close_without_match"})
_LINK_BASES = frozenset(
    {
        "external_transaction_reference",
        "operator_source_record",
        "customer_confirmation",
    }
)
_CLOSE_BASES = frozenset({"no_valid_candidate_after_review"})
_WRITE_ERRORS = frozenset(
    {
        "invalid_operator_correlation_resolution",
        "operator_correlation_case_not_found",
        "operator_correlation_stale_evidence",
        "operator_correlation_command_expired",
        "operator_correlation_already_resolved",
        "operator_correlation_idempotency_conflict",
    }
)


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject every redirect so a bearer is never copied to another request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def _profile_setting(name: str) -> str:
    try:
        get_secret = import_module("agent.secret_scope").get_secret
    except ImportError:
        return os.getenv(name, "")
    return get_secret(name, "") or ""


def _validated_base_url() -> str:
    base_url = _profile_setting("OPERATOR_CORRELATION_API_URL").strip().rstrip("/")
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("operator_correlation_api_url_invalid")
    return base_url


def _api_settings() -> tuple[str, str]:
    base_url = _validated_base_url()
    token = _profile_setting("OPERATOR_CORRELATION_API_TOKEN").strip()
    if len(token) < 32:
        raise ValueError("operator_correlation_api_token_invalid")
    return base_url, token


def _write_api_settings() -> tuple[str, str]:
    base_url = _validated_base_url()
    token = _profile_setting("OPERATOR_CORRELATION_WRITE_TOKEN").strip()
    if len(token) < 32:
        raise ValueError("operator_correlation_write_token_invalid")
    return base_url, token


def _read_json(path: str) -> dict[str, object]:
    base_url, token = _api_settings()
    request = Request(
        f"{base_url}{path}",
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with build_opener(_NoRedirectHandler()).open(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return {"error": "unresolved_correlation_not_found"}
        if exc.code == 401:
            return {"error": "operator_authentication_failed"}
        return {"error": "operator_correlation_read_unavailable"}
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        return {"error": "operator_correlation_read_unavailable"}
    if not isinstance(payload, dict):
        return {"error": "operator_correlation_response_invalid"}
    return payload


def _post_json(path: str, body: dict[str, object]) -> dict[str, object]:
    base_url, token = _write_api_settings()
    request = Request(
        f"{base_url}{path}",
        method="POST",
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with build_opener(_NoRedirectHandler()).open(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            error_payload = None
        detail = error_payload.get("detail") if isinstance(error_payload, dict) else None
        if isinstance(detail, str) and detail in _WRITE_ERRORS:
            return {"error": detail}
        if exc.code == 401:
            return {"error": "operator_authentication_failed"}
        return {"error": "operator_correlation_write_unavailable"}
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        return {"error": "operator_correlation_write_unavailable"}
    if not isinstance(payload, dict):
        return {"error": "operator_correlation_response_invalid"}
    return payload


def _normalized_uuid(value: object, error: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(error)
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ValueError(error) from exc


def _validated_resolution_payload(
    params: dict[str, object], *, confirmation: bool
) -> dict[str, object]:
    if confirmation:
        command_id = _normalized_uuid(params.get("command_id"), "invalid_command_id")
        action = params.get("expected_action")
        candidate = _normalized_uuid(
            params.get("expected_candidate_id"),
            "invalid_expected_candidate_id",
            nullable=True,
        )
        if action not in _ACTIONS or (
            (action == "resolve_with_candidate") != (candidate is not None)
        ):
            raise ValueError("invalid_confirmation_combination")
        return {
            "command_id": command_id,
            "expected_action": action,
            "expected_candidate_id": candidate,
        }

    case_id = _normalized_uuid(params.get("case_id"), "invalid_case_id")
    idempotency_key = _normalized_uuid(
        params.get("idempotency_key"), "invalid_idempotency_key"
    )
    action = params.get("action")
    candidate = _normalized_uuid(
        params.get("candidate_id"), "invalid_candidate_id", nullable=True
    )
    basis = params.get("verification_basis")
    if action not in _ACTIONS or not isinstance(basis, str):
        raise ValueError("invalid_resolution_combination")
    if action == "resolve_with_candidate":
        valid = candidate is not None and basis in _LINK_BASES
    else:
        valid = candidate is None and basis in _CLOSE_BASES
    if not valid:
        raise ValueError("invalid_resolution_combination")
    return {
        "case_id": case_id,
        "idempotency_key": idempotency_key,
        "action": action,
        "candidate_id": candidate,
        "verification_basis": basis,
    }


def list_unresolved_correlations(params: dict[str, object], **kwargs: object) -> str:
    del kwargs
    limit = params.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 50:
        return json.dumps({"error": "invalid_limit"})
    return json.dumps(
        _read_json(f"/internal/operator/correlations/unresolved?limit={limit}"),
        ensure_ascii=False,
    )


def get_unresolved_correlation(params: dict[str, object], **kwargs: object) -> str:
    del kwargs
    try:
        case_id = _normalized_uuid(params.get("case_id"), "invalid_case_id")
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(
        _read_json(f"/internal/operator/correlations/unresolved/{case_id}"),
        ensure_ascii=False,
    )


def prepare_correlation_resolution(
    params: dict[str, object], **kwargs: object
) -> str:
    del kwargs
    try:
        payload = _validated_resolution_payload(params, confirmation=False)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(
        _post_json(
            "/internal/operator/correlations/resolutions/prepare", payload
        ),
        ensure_ascii=False,
    )


def confirm_correlation_resolution(
    params: dict[str, object], **kwargs: object
) -> str:
    del kwargs
    try:
        payload = _validated_resolution_payload(params, confirmation=True)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    return json.dumps(
        _post_json(
            "/internal/operator/correlations/resolutions/confirm", payload
        ),
        ensure_ascii=False,
    )


def require_resolution_confirmation(
    tool_name: str, args: dict[str, object], **kwargs: object
) -> dict[str, str] | None:
    del kwargs
    if tool_name != "confirm_correlation_resolution":
        return None
    try:
        payload = _validated_resolution_payload(args, confirmation=True)
    except ValueError:
        return {
            "action": "deny",
            "message": "Confirmación de resolución inválida",
            "rule_key": "operator-correlation-resolution:invalid",
        }
    return {
        "action": "approve",
        "message": "Confirmar la resolución manual preparada",
        "rule_key": f"operator-correlation-resolution:{payload['command_id']}",
    }
