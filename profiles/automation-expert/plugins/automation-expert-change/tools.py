"""Las cuatro herramientas de cambio del Automation Expert (flow-change-cycle-v1).

Copia el cliente del plugin de lectura (automation-expert-read/tools.py), con
su propia clave (AUTOMATION_EXPERT_CHANGE_API_TOKEN, distinta de la de
lectura): acepta http solo hacia el nombre exacto del bridge, no sigue
redirecciones, corta a los 15 s y rechaza otro major del contrato.

**Quién pide el cambio lo pone el plugin, nunca el modelo.** El ID de Slack de
la persona sale del contexto de la sesión de Hermes
(``gateway.session_context.get_session_env("HERMES_SESSION_USER_ID")``, Hermes
0.21.0, leído el 2026-10-21), y solo en Slack. La base lo guarda como
declarado: el bridge no lo puede verificar.

Ninguna herramienta aprueba ni publica, y no hay pausa: pausar es del comando
del operador mientras la base no pueda verificar a quien lo pide.
"""

from __future__ import annotations

from importlib import import_module
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

CONTRACT_MAJOR = "1"
PROFILE_NAME = "automation-expert"
TIMEOUT_SECONDS = 15
PREFIX = "/internal/automation-expert/v1"
_SLACK_USER_RE = re.compile(r"[UW][A-Z0-9]{8,20}")
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+")
_PHONE_SEPARATORS = frozenset(" \t\n\r -.()+")
# Los codigos que el bridge contesta; cualquier otro se cambia por uno generico.
_BRIDGE_ERRORS = frozenset({
    "automation_expert_change_authentication_required",
    "invalid_body",
    "body_too_large",
    "invalid_proposal_id",
    "identifier_not_allowed",
    "flow_change_items_invalid",
    "flow_change_draft_blocked",
    "flow_not_found",
    "flow_change_not_found",
    "flow_change_stale_base",
    "flow_change_revision_mismatch",
    "flow_change_not_draft",
    "flow_change_proposal_expired",
    "flow_change_open_limit",
    "flow_change_revision_limit",
    "flow_change_validation_limit",
    "flow_creation_not_enabled",
    "template_catalog_unavailable",
    "automation_expert_change_unavailable",
    "automation_expert_change_busy",
})


class _NoRedirectHandler(HTTPRedirectHandler):
    """Rechaza toda redireccion: la clave nunca viaja a otro pedido."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def _profile_setting(name: str) -> str:
    """Lee del .env del profile (agent.secret_scope), y si no hay Hermes, del entorno."""
    try:
        get_secret = import_module("agent.secret_scope").get_secret
    except ImportError:
        return os.getenv(name, "")
    return get_secret(name, "") or ""


def _session(name: str) -> str:
    """Una variable de la sesion de Hermes (el gateway la pone por pedido)."""
    try:
        get_session_env = import_module("gateway.session_context").get_session_env
    except ImportError:
        return os.getenv(name, "")
    return get_session_env(name, "") or ""


def _validated_base_url() -> str:
    base_url = _profile_setting("AUTOMATION_EXPERT_API_URL").strip().rstrip("/")
    parsed = urlparse(base_url)
    if (
        parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path
    ):
        raise ValueError("automation_expert_api_url_invalid")
    if parsed.scheme == "https":
        return base_url
    internal = _profile_setting("AUTOMATION_EXPERT_INTERNAL_HOST").strip()
    if parsed.scheme == "http" and internal and parsed.port is not None and parsed.netloc == internal:
        return base_url
    raise ValueError("automation_expert_api_url_invalid")


def _requested_by() -> dict[str, str]:
    platform = _session("HERMES_SESSION_PLATFORM").strip().lower()
    user = _session("HERMES_SESSION_USER_ID").strip()
    if platform != "slack" or not _SLACK_USER_RE.fullmatch(user):
        raise ValueError("requested_by_unavailable")
    return {"slack_user": user}


def looks_like_identifier(value: str) -> bool:
    """La misma regla que el bridge: un mail, o siete caracteres numericos seguidos."""
    if _EMAIL_RE.search(value):
        return True
    run = 0
    for character in value:
        if character in _PHONE_SEPARATORS:
            continue
        run = run + 1 if character.isnumeric() else 0
        if run >= 7:
            return True
    return False


def _request(method: str, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
    try:
        base_url = _validated_base_url()
        token = _profile_setting("AUTOMATION_EXPERT_CHANGE_API_TOKEN").strip()
        if len(token) < 32:
            raise ValueError("automation_expert_change_api_token_invalid")
    except ValueError as exc:
        return {"error": str(exc)}
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(f"{base_url}{path}", method=method, data=data, headers=headers)
    try:
        with build_opener(_NoRedirectHandler()).open(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError):
            error_payload = None
        detail = error_payload.get("detail") if isinstance(error_payload, dict) else None
        if isinstance(detail, str) and detail in _BRIDGE_ERRORS:
            out: dict[str, object] = {"error": detail}
            # Los bloqueos de un borrador vuelven para que el agente los corrija.
            data_out = error_payload.get("data") if isinstance(error_payload, dict) else None
            if detail == "flow_change_draft_blocked" and isinstance(data_out, dict):
                out["blocks"] = data_out.get("blocks", [])
                out["warnings"] = data_out.get("warnings", [])
            return out
        if exc.code == 401:
            return {"error": "automation_expert_change_authentication_required"}
        return {"error": "automation_expert_change_unavailable"}
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return {"error": "automation_expert_change_unavailable"}
    if not isinstance(payload, dict):
        return {"error": "automation_expert_change_response_invalid"}
    version = payload.get("contract_version")
    if not isinstance(version, str) or version.split(".", 1)[0] != CONTRACT_MAJOR:
        return {"error": "automation_expert_change_contract_unsupported"}
    return payload


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _uuid(value: object) -> str | None:
    try:
        return str(UUID(str(value))) if isinstance(value, str) else None
    except ValueError:
        return None


def get_flow_definitions(params: dict[str, object], **kwargs: object) -> str:
    """Los flujos del almacen con su definicion activa. Solo lee."""
    del params, kwargs
    return _dumps(_request("GET", f"{PREFIX}/flow-store"))


def propose_flow_change(params: dict[str, object], **kwargs: object) -> str:
    """Guarda (o rehace) un borrador. Con bloqueos no guarda y los devuelve."""
    del kwargs
    try:
        requested_by = _requested_by()
    except ValueError as exc:
        return _dumps({"error": str(exc)})
    purpose = params.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        return _dumps({"error": "invalid_body"})
    if looks_like_identifier(purpose):
        return _dumps({"error": "identifier_not_allowed"})
    body: dict[str, object] = {"items": params.get("items"), "base": params.get("base"),
                               "purpose": purpose.strip(), "requested_by": requested_by}
    if params.get("proposal_id") is not None or params.get("revision") is not None:
        proposal_id = _uuid(params.get("proposal_id"))
        revision = params.get("revision")
        if proposal_id is None or type(revision) is not int:
            return _dumps({"error": "invalid_body"})
        body.update(proposal_id=proposal_id, revision=revision)
    return _dumps(_request("POST", f"{PREFIX}/flow-changes", body))


def validate_flow_change(params: dict[str, object], **kwargs: object) -> str:
    """Valida un borrador y devuelve la vista previa. No publica."""
    del kwargs
    try:
        requested_by = _requested_by()
    except ValueError as exc:
        return _dumps({"error": str(exc)})
    proposal_id = _uuid(params.get("proposal_id"))
    revision = params.get("revision")
    if proposal_id is None or type(revision) is not int:
        return _dumps({"error": "invalid_body"})
    return _dumps(_request("POST", f"{PREFIX}/flow-changes/{proposal_id}/validate",
                           {"revision": revision, "requested_by": requested_by}))


def get_flow_change(params: dict[str, object], **kwargs: object) -> str:
    """Relee una propuesta. Solo lee."""
    del kwargs
    proposal_id = _uuid(params.get("proposal_id"))
    if proposal_id is None:
        return _dumps({"error": "invalid_proposal_id"})
    return _dumps(_request("GET", f"{PREFIX}/flow-changes/{proposal_id}"))
