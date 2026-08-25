"""Read-only HTTP handlers for unresolved correlation review."""

from __future__ import annotations

from importlib import import_module
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject every redirect so the bearer is never copied to another request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def _profile_setting(name: str) -> str:
    try:
        get_secret = import_module("agent.secret_scope").get_secret
    except ImportError:
        return os.getenv(name, "")
    return get_secret(name, "") or ""


def _api_settings() -> tuple[str, str]:
    base_url = _profile_setting("OPERATOR_CORRELATION_API_URL").strip().rstrip("/")
    token = _profile_setting("OPERATOR_CORRELATION_API_TOKEN").strip()
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
    if len(token) < 32:
        raise ValueError("operator_correlation_api_token_invalid")
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
    case_id = params.get("case_id")
    if not isinstance(case_id, str):
        return json.dumps({"error": "invalid_case_id"})
    try:
        normalized_case_id = str(UUID(case_id))
    except ValueError:
        return json.dumps({"error": "invalid_case_id"})
    return json.dumps(
        _read_json(
            f"/internal/operator/correlations/unresolved/{normalized_case_id}"
        ),
        ensure_ascii=False,
    )
