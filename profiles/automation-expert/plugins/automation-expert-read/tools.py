"""Las dos herramientas de lectura del Automation Expert (automation-expert-read-v1).

Contrato: docs/contracts/automation-expert-read-v1.md, «El plugin». Copia el
cliente del molde del copiloto (plugins/operator-correlation-review/tools.py)
con dos cambios: acepta http solo hacia el nombre exacto del bridge que da la
instalacion, con su puerto, y rechaza una respuesta de otro major del contrato.

Solo lee. No sigue redirecciones, corta a los 15 s, valida los parametros antes
de salir a la red y nunca manda un telefono ni un mail: a un lead se lo busca
por numero de conversacion, por el link de pago (del que sale solo el
identificador de la emision) o por nombre.
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
_FLOW_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_ULID_RE = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
# El sck de un link nuestro termina en hermes|v1|<ULID>, con | o ~ y escapado o no.
_LINK_ISSUANCE_RE = re.compile(
    r"hermes(?:~|\||%7[Cc])v1(?:~|\||%7[Cc])([0-7][0-9A-HJKMNP-TV-Z]{25})(?![0-9A-HJKMNP-TV-Z])"
)
_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{8,400}")
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+")
_LONG_NUMBER_RE = re.compile(r"\d[\d\s\-.()+]{5,}\d")
_PHONE_SEPARATORS = frozenset(" \t\n\r -.()+")
_FREE_TEXT_FIELDS = frozenset({"label", "explanation", "what", "why"})
# Los codigos que el bridge contesta; cualquier otro se cambia por uno generico.
_BRIDGE_ERRORS = frozenset({
    "automation_expert_authentication_required",
    "invalid_flow_key",
    "invalid_lead_ref",
    "invalid_lookup",
    "identifier_not_allowed",
    "limit_out_of_range",
    "invalid_cursor",
    "flow_not_found",
    "lead_not_found",
    "automation_expert_read_unavailable",
    "automation_expert_response_too_large",
    "automation_expert_busy",
})


class _NoRedirectHandler(HTTPRedirectHandler):
    """Rechaza toda redireccion: el token nunca viaja a otro pedido."""

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
    # http solo hacia el nombre exacto del bridge que da la instalacion, con su
    # puerto: un nombre de una sola etiqueta se puede resolver en la tailnet
    # (ndots:0), y ahi el token viajaria en claro a otra maquina.
    internal = _profile_setting("AUTOMATION_EXPERT_INTERNAL_HOST").strip()
    if (
        parsed.scheme == "http"
        and internal
        and parsed.port is not None
        and parsed.netloc == internal
    ):
        return base_url
    raise ValueError("automation_expert_api_url_invalid")


def _api_settings() -> tuple[str, str]:
    base_url = _validated_base_url()
    token = _profile_setting("AUTOMATION_EXPERT_API_TOKEN").strip()
    if len(token) < 32:
        raise ValueError("automation_expert_api_token_invalid")
    return base_url, token


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


def _mask(value: str) -> str:
    value = _EMAIL_RE.sub("<tapado>", value)
    return _LONG_NUMBER_RE.sub(
        lambda match: "<tapado>" if sum(c.isdigit() for c in match.group(0)) >= 7 else match.group(0),
        value,
    )


def _mask_free_text(payload: object) -> object:
    """Tapa un mail o un numero largo solo en los campos de texto libre.

    Nunca toca los campos ya validados como uuid, fecha o numero de
    conversacion: tapar siete digitos seguidos en todo romperia uuids.
    """
    if isinstance(payload, dict):
        return {
            key: (_mask(value) if key in _FREE_TEXT_FIELDS and isinstance(value, str)
                  else _mask_free_text(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_mask_free_text(item) for item in payload]
    return payload


def _request(method: str, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
    try:
        base_url, token = _api_settings()
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
            return {"error": detail}
        if exc.code == 401:
            return {"error": "automation_expert_authentication_required"}
        return {"error": "automation_expert_read_unavailable"}
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return {"error": "automation_expert_read_unavailable"}
    if not isinstance(payload, dict):
        return {"error": "automation_expert_response_invalid"}
    version = payload.get("contract_version")
    if not isinstance(version, str) or version.split(".", 1)[0] != CONTRACT_MAJOR:
        return {"error": "automation_expert_contract_unsupported"}
    return _mask_free_text(payload)  # type: ignore[return-value]


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def list_flows(params: dict[str, object], **kwargs: object) -> str:
    """Los flujos de la instancia, o uno solo si se pide su clave."""
    del kwargs
    flow_key = params.get("flow_key")
    if flow_key is None:
        return _dumps(_request("GET", "/internal/automation-expert/v1/flows"))
    if not isinstance(flow_key, str) or not _FLOW_KEY_RE.fullmatch(flow_key):
        return _dumps({"error": "invalid_flow_key"})
    return _dumps(_request("GET", f"/internal/automation-expert/v1/flows/{flow_key}"))


def _lookup_body(params: dict[str, object]) -> dict[str, object]:
    keys = [key for key in ("conversation", "link", "name") if params.get(key) is not None]
    if len(keys) != 1:
        raise ValueError("invalid_lookup")
    key = keys[0]
    value = params[key]
    if key == "conversation":
        if isinstance(value, str):
            text = value.strip().lstrip("#")
            if looks_like_identifier(text):
                raise ValueError("identifier_not_allowed")
            if not re.fullmatch(r"[0-9]{1,6}", text):
                raise ValueError("invalid_lookup")
            value = int(text)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("invalid_lookup")
        if value >= 1_000_000:
            raise ValueError("identifier_not_allowed")
        if value < 1:
            raise ValueError("invalid_lookup")
        return {"conversation": value}
    if key == "link":
        # Del link sale solo el identificador de la emision: el resto, con su
        # fbclid, no sale del plugin.
        if not isinstance(value, str):
            raise ValueError("invalid_lookup")
        match = _LINK_ISSUANCE_RE.search(value)
        if match is None:
            raise ValueError("invalid_lookup")
        return {"issuance": match.group(1)}
    if not isinstance(value, str):
        raise ValueError("invalid_lookup")
    if looks_like_identifier(value):
        raise ValueError("identifier_not_allowed")
    name = " ".join(value.split())
    if not 3 <= len(name) <= 80:
        raise ValueError("invalid_lookup")
    return {"name": name}


def lead_history(params: dict[str, object], **kwargs: object) -> str:
    """Lo que le paso o le va a pasar a un lead, y por que.

    Con lead_ref va directo a la historia. Con una llave, busca primero: si hay
    varios candidatos, los devuelve para que el agente pregunte cual es.
    """
    del kwargs
    limit = params.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        return _dumps({"error": "limit_out_of_range"})
    cursor = params.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not _CURSOR_RE.fullmatch(cursor)):
        return _dumps({"error": "invalid_cursor"})
    lead_ref = params.get("lead_ref")
    if lead_ref is not None:
        if any(params.get(key) is not None for key in ("conversation", "link", "name")):
            return _dumps({"error": "invalid_lookup"})
        try:
            lead_ref = str(UUID(str(lead_ref)))
        except ValueError:
            return _dumps({"error": "invalid_lead_ref"})
    else:
        try:
            body = _lookup_body(params)
        except ValueError as exc:
            return _dumps({"error": str(exc)})
        found = _request("POST", "/internal/automation-expert/v1/leads/lookup", body)
        if "error" in found:
            return _dumps(found)
        data = found.get("data")
        if not isinstance(data, dict) or data.get("result") != "found":
            return _dumps(found)
        lead_ref = data.get("lead_ref")
        try:
            lead_ref = str(UUID(str(lead_ref)))
        except ValueError:
            return _dumps({"error": "automation_expert_response_invalid"})
    query = f"?limit={limit}" + (f"&cursor={cursor}" if cursor else "")
    return _dumps(_request("GET", f"/internal/automation-expert/v1/leads/{lead_ref}/history{query}"))
