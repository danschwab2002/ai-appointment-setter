"""Las herramientas de cambio del Automation Expert (flow-change-cycle-v1).

Copia el cliente del plugin de lectura (automation-expert-read/tools.py), con
su propia clave (AUTOMATION_EXPERT_CHANGE_API_TOKEN, distinta de la de
lectura): acepta http solo hacia el nombre exacto del bridge, no sigue
redirecciones, corta a los 15 s y rechaza otro major del contrato.

**Quién pide el cambio lo pone el plugin, nunca el modelo.** El ID de Slack de
la persona sale del contexto de la sesión de Hermes
(``gateway.session_context.get_session_env("HERMES_SESSION_USER_ID")``, Hermes
0.21.0, leído el 2026-10-21), y solo en Slack. La base lo guarda como
declarado: el bridge no lo puede verificar.

Ninguna herramienta aprueba ni publica, y no hay pausa: la base no puede
verificar a quien la pide desde el agente. Aprobar, publicar y pausar lo hace
un aprobador en la pagina de aprobacion del bridge (ciclo 1.9.0), con su ID de
Slack verificado, o el comando del operador.

Desde el ciclo 1.10.0 (M16 P6 y P7) suma dos lecturas: el catalogo de
plantillas de la bandeja (``list_templates``) y las propuestas de la instancia
con su estado (``list_flow_changes``).

Desde la 0.6.0 (agent-campaigns-v1, E-355) suma las campanas: ver como queda
(``preview_campaign``, sin guardar), guardar y validar. Publicarla y pausarla lo
hace una persona de la lista en la pagina; el control de calidad lo corre el
operador. Y ``create_chatwoot_label_macro``: un macro que solo suma una etiqueta.
Sin ``AGENT_CAMPAIGNS_ENABLED`` el bridge no tiene esas rutas: ``campaigns_not_enabled``.

Desde la 0.4.0 (automation-expert-chatwoot-v1, F-143) suma la herramienta
acotada de Chatwoot: leer y crear etiquetas, sumarle una a una conversacion y
leer los macros. No borra, no saca etiquetas y no manda mensajes; la lista
cerrada de rutas de Chatwoot la aplica el bridge.

Con el canal del link prendido (ciclo 1.9.0), una validacion que deja la
propuesta ``validada`` postea el boton «Revisar y aprobar» en el mismo DM
(``approval_button.py``). El boton es un link a la pagina del bridge: tampoco
aprueba.
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

from . import approval_button

CONTRACT_MAJOR = "1"
PROFILE_NAME = "automation-expert"
TIMEOUT_SECONDS = 15
PREFIX = "/internal/automation-expert/v1"
_SLACK_USER_RE = re.compile(r"[UW][A-Z0-9]{8,20}")
_SLACK_CHANNEL_RE = re.compile(r"[CDG][A-Z0-9]{8,20}")
_SLACK_THREAD_TS_RE = re.compile(r"[0-9]{1,12}\.[0-9]{1,8}")
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+")
_PHONE_SEPARATORS = frozenset(" \t\n\r -.()+")
# Los codigos que el bridge contesta; cualquier otro se cambia por uno generico.
_DRAFT_BLOCKED = frozenset({"flow_change_draft_blocked", "template_request_draft_blocked", "campaign_draft_blocked"})
_BRIDGE_ERRORS = frozenset({
    "automation_expert_change_authentication_required",
    "invalid_body",
    "body_too_large",
    "invalid_proposal_id",
    "identifier_not_allowed",
    "flow_change_items_invalid",
    # La lista de propuestas (ciclo 1.10.0): un estado o un limite que no van.
    "invalid_query",
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
    # El item nuevo (ciclo 1.7.0): la clave ya existe, o prender no es cohorte.
    "flow_already_registered",
    "new_flow_must_start_in_cohort",
    "template_catalog_unavailable",
    # La solicitud de plantilla de Meta (template-request-v1, F-146 parte 5).
    "template_request_invalid",
    "template_request_draft_blocked",
    "invalid_request_id",
    "meta_template_open_limit",
    "meta_template_revision_limit",
    "meta_template_validation_limit",
    "meta_template_revision_mismatch",
    "meta_template_not_draft",
    "meta_template_request_expired",
    "meta_template_name_taken",
    "meta_template_request_not_found",
    "automation_expert_change_unavailable",
    "automation_expert_change_busy",
    # La herramienta acotada de Chatwoot (automation-expert-chatwoot-v1, F-143).
    "chatwoot_delete_not_allowed",
    "chatwoot_call_not_allowed",
    "chatwoot_label_reserved",
    "chatwoot_label_not_found",
    "chatwoot_conversation_not_found",
    "chatwoot_label_rejected",
    "chatwoot_label_not_confirmed",
    "chatwoot_tool_unavailable",
    "chatwoot_tool_response_invalid",
    "chatwoot_tool_response_too_large",
    "invalid_conversation_id",
    "chatwoot_macro_rejected",
    # Las campanas (agent-campaigns-v1, E-355).
    "invalid_campaign_id",
    "campaign_invalid",
    "campaign_draft_blocked",
    "agent_campaign_not_found",
    "agent_campaign_open_limit",
    "agent_campaign_revision_limit",
    "agent_campaign_validation_limit",
    "agent_campaign_revision_mismatch",
    "agent_campaign_not_draft",
    "agent_campaign_expired",
    "agent_campaign_key_is_immutable",
    "agent_campaign_draft_invalid",
    # E-486 (8d): sumar un telefono al grupo de prueba de un flujo.
    "not_an_approver",
    "flow_cohort_phone_invalid",
    "flow_cohort_contact_not_found",
    "flow_cohort_contact_ambiguous",
    "flow_cohort_full",
    "flow_cohort_needs_native_flow",
    "flow_cohort_flow_not_in_cohort",
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


_REASON_TEXT = re.compile(r"^[<>a-z0-9_]{1,40}$")
# El campo y el problema de un borrador de plantilla son mas largos (template.botones.0.texto).
_REASON_FIELD = re.compile(r"^[<>a-z0-9_.]{1,60}$")


def _reason(value: object) -> dict[str, object] | None:
    """El motivo de un 422 del bridge, con su forma cerrada: textos cortos, el numero de item y listas de claves.

    Lo que no tiene esa forma no pasa: el agente recibe el codigo solo, como antes.
    """
    if not isinstance(value, dict):
        return None
    out: dict[str, object] = {}
    for key in ("field", "code", "problem"):
        text = value.get(key)
        if isinstance(text, str) and _REASON_FIELD.fullmatch(text):
            out[key] = text
    item = value.get("item")
    if type(item) is int and 0 <= item < 100:
        out["item"] = item
    for key in ("missing", "extra"):
        names = value.get(key)
        if isinstance(names, list):
            kept = [name for name in names[:10] if isinstance(name, str) and _REASON_TEXT.fullmatch(name)]
            if kept:
                out[key] = kept
    return out or None


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
            if detail in _DRAFT_BLOCKED and isinstance(data_out, dict):
                out["blocks"] = data_out.get("blocks", [])
                out["warnings"] = data_out.get("warnings", [])
            # Y el motivo de un pedido que no cierra: que campo, que item y que claves.
            reason = _reason(error_payload.get("reason"))
            if reason is not None:
                out["reason"] = reason
            return out
        if exc.code == 401:
            return {"error": "automation_expert_change_authentication_required"}
        # Las rutas de plantillas solo existen con META_TEMPLATES_ENABLED: sin el flag, el 404 generico.
        if exc.code == 404 and detail == "Not Found" and path.startswith(f"{PREFIX}/template-requests"):
            return {"error": "template_requests_not_enabled"}
        # Las rutas de Chatwoot solo existen con AUTOMATION_EXPERT_CHATWOOT_ENABLED: sin el flag, el
        # bridge contesta el 404 generico de FastAPI.
        # Con ese flag pero sin las campanas, POST /macros da 405: GET /macros si existe.
        if exc.code in (404, 405) and detail in ("Not Found", "Method Not Allowed") \
                and path.startswith(f"{PREFIX}/chatwoot/"):
            return {"error": "chatwoot_tool_not_enabled"}
        # E-486 (8d): la cohorte por el AE solo existe con AUTOMATION_EXPERT_COHORT_TOOL_ENABLED.
        if exc.code in (404, 405) and detail in ("Not Found", "Method Not Allowed") \
                and path.startswith(f"{PREFIX}/flows/") and path.endswith("/cohort"):
            return {"error": "cohort_tool_not_enabled"}
        # Lo mismo con las campanas: sin AGENT_CAMPAIGNS_ENABLED sus rutas no existen.
        if exc.code == 404 and detail == "Not Found" and path.startswith(CAMPAIGNS):
            return {"error": "campaigns_not_enabled"}
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
    out = _request("POST", f"{PREFIX}/flow-changes/{proposal_id}/validate",
                   {"revision": revision, "requested_by": requested_by})
    _maybe_post_approval_button(proposal_id, out)
    return _dumps(out)


def _maybe_post_approval_button(proposal_id: str, out: dict[str, object]) -> None:
    """Con la propuesta validada y sin bloqueos, el boton «Revisar y aprobar» (ciclo 1.9.0).

    Lo postea este codigo, nunca el modelo. Con el canal apagado no toca la respuesta.
    """
    data = out.get("data")
    if (
        "error" in out
        or not isinstance(data, dict)
        or data.get("status") != "validada"
        or data.get("proposal_id") != proposal_id
        or data.get("blocks")
    ):
        return
    button = approval_button.post_after_validation(proposal_id, setting=_profile_setting, session=_session)
    if button is not None:
        data["approval_button"] = button


def get_approval_page(params: dict[str, object], **kwargs: object) -> str:
    """El link de la pagina de aprobacion de la instancia (E-465), sin propuesta. No llama al bridge.

    Sale de la misma base que el boton (``FLOW_APPROVAL_BASE_URL``) y solo con la pagina prendida.
    """
    del params, kwargs
    return _dumps(approval_button.approval_page(_profile_setting))


LIST_STATES = ("abiertas", "todas")


def list_flow_changes(params: dict[str, object], **kwargs: object) -> str:
    """Las propuestas de la instancia, de la mas nueva a la mas vieja, con su estado. Solo lee."""
    del kwargs
    estado = params.get("estado", "abiertas")
    limit = params.get("limit", 20)
    if estado not in LIST_STATES or type(limit) is not int or not 1 <= limit <= 50:
        return _dumps({"error": "invalid_query"})
    return _dumps(_request("GET", f"{PREFIX}/flow-changes?estado={estado}&limit={limit}"))


def list_templates(params: dict[str, object], **kwargs: object) -> str:
    """El catalogo de plantillas de la bandeja, con el texto de cada una. Solo lee."""
    del params, kwargs
    return _dumps(_request("GET", f"{PREFIX}/templates"))


def _slack_thread() -> dict[str, object] | None:
    """El canal y el hilo de esta charla, para que el aviso de Meta caiga ahi. Sale de la sesion, nunca del modelo."""
    channel = _session("HERMES_SESSION_CHAT_ID").strip()
    if _session("HERMES_SESSION_PLATFORM").strip().lower() != "slack" or not _SLACK_CHANNEL_RE.fullmatch(channel):
        return None
    thread = _session("HERMES_SESSION_THREAD_ID").strip()
    return {"channel": channel, "thread_ts": thread if _SLACK_THREAD_TS_RE.fullmatch(thread) else None}


def propose_template(params: dict[str, object], **kwargs: object) -> str:
    """Guarda (o rehace) el borrador de una plantilla. Con bloqueos no guarda y los devuelve. No habla con Meta."""
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
    body: dict[str, object] = {"template": params.get("template"), "purpose": purpose.strip(),
                               "requested_by": requested_by}
    slack_thread = _slack_thread()
    if slack_thread is not None:
        body["slack_thread"] = slack_thread
    if params.get("for_flow") is not None:
        body["for_flow"] = params["for_flow"]
    if params.get("request_id") is not None or params.get("revision") is not None:
        request_id = _uuid(params.get("request_id"))
        revision = params.get("revision")
        if request_id is None or type(revision) is not int:
            return _dumps({"error": "invalid_body"})
        body.update(request_id=request_id, revision=revision)
    return _dumps(_request("POST", f"{PREFIX}/template-requests", body))


def validate_template(params: dict[str, object], **kwargs: object) -> str:
    """Valida un borrador de plantilla y deja el boton de la pagina. No lo manda a Meta."""
    del kwargs
    try:
        requested_by = _requested_by()
    except ValueError as exc:
        return _dumps({"error": str(exc)})
    request_id = _uuid(params.get("request_id"))
    revision = params.get("revision")
    if request_id is None or type(revision) is not int:
        return _dumps({"error": "invalid_body"})
    out = _request("POST", f"{PREFIX}/template-requests/{request_id}/validate",
                   {"revision": revision, "requested_by": requested_by})
    _maybe_post_template_button(request_id, out)
    return _dumps(out)


def _maybe_post_template_button(request_id: str, out: dict[str, object]) -> None:
    """Con la solicitud validada y sin bloqueos, el boton de la pagina de la plantilla (template-request-v1)."""
    data = out.get("data")
    if "error" in out or not isinstance(data, dict) or data.get("status") != "validada" or data.get("blocks"):
        return
    button = approval_button.post_template_after_validation(request_id, setting=_profile_setting, session=_session)
    if button is not None:
        data["approval_button"] = button


def list_template_requests(params: dict[str, object], **kwargs: object) -> str:
    """Las solicitudes de plantilla de la instancia, con su estado. Solo lee."""
    del kwargs
    estado = params.get("estado", "abiertas")
    limit = params.get("limit", 20)
    if estado not in LIST_STATES or type(limit) is not int or not 1 <= limit <= 50:
        return _dumps({"error": "invalid_query"})
    return _dumps(_request("GET", f"{PREFIX}/template-requests?estado={estado}&limit={limit}"))


def get_template_request(params: dict[str, object], **kwargs: object) -> str:
    """Relee una solicitud de plantilla. Solo lee."""
    del kwargs
    request_id = _uuid(params.get("request_id"))
    if request_id is None:
        return _dumps({"error": "invalid_request_id"})
    return _dumps(_request("GET", f"{PREFIX}/template-requests/{request_id}"))


def get_flow_change(params: dict[str, object], **kwargs: object) -> str:
    """Relee una propuesta. Solo lee."""
    del kwargs
    proposal_id = _uuid(params.get("proposal_id"))
    if proposal_id is None:
        return _dumps({"error": "invalid_proposal_id"})
    return _dumps(_request("GET", f"{PREFIX}/flow-changes/{proposal_id}"))


# automation-expert-chatwoot-v1 (F-143): la herramienta acotada de Chatwoot. La lista cerrada de
# rutas de Chatwoot vive en el bridge (bridge/automation_expert_chatwoot.py); el plugin solo habla
# con sus cuatro rutas, y valida antes de salir a la red.
CHATWOOT_PREFIX = f"{PREFIX}/chatwoot"
_LABEL_RE = re.compile(r"^[\w-]{1,40}$")


def _label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    label = value.strip().lower()
    if not _LABEL_RE.fullmatch(label) or looks_like_identifier(label):
        return None
    return label


def _chatwoot(method: str, path: str, body: dict[str, object] | None = None) -> str:
    return _dumps(_request(method, f"{CHATWOOT_PREFIX}{path}", body))


def list_chatwoot_labels(params: dict[str, object], **kwargs: object) -> str:
    """Las etiquetas de la cuenta de Chatwoot de la instancia. Solo lee."""
    del params, kwargs
    return _chatwoot("GET", "/labels")


def create_chatwoot_label(params: dict[str, object], **kwargs: object) -> str:
    """Crea una etiqueta en la cuenta de la instancia; si ya esta, no la toca."""
    del kwargs
    try:
        requested_by = _requested_by()
    except ValueError as exc:
        return _dumps({"error": str(exc)})
    title = _label(params.get("title"))
    if title is None:
        return _dumps({"error": "invalid_body"})
    if title.startswith("automation_"):
        return _dumps({"error": "chatwoot_label_reserved"})
    body: dict[str, object] = {"title": title, "requested_by": requested_by}
    for key in ("description", "color", "show_on_sidebar"):
        if params.get(key) is not None:
            body[key] = params[key]
    description = body.get("description")
    if description is not None and (not isinstance(description, str) or looks_like_identifier(description)):
        return _dumps({"error": "identifier_not_allowed" if isinstance(description, str) else "invalid_body"})
    return _chatwoot("POST", "/labels", body)


def add_chatwoot_label_to_conversation(params: dict[str, object], **kwargs: object) -> str:
    """Le suma una etiqueta existente a una conversacion de la bandeja. Nunca saca otra."""
    del kwargs
    try:
        requested_by = _requested_by()
    except ValueError as exc:
        return _dumps({"error": str(exc)})
    conversation_id = params.get("conversation_id")
    label = _label(params.get("label"))
    if type(conversation_id) is not int or not 1 <= conversation_id < 10**12 or label is None:
        return _dumps({"error": "invalid_body"})
    if label.startswith("automation_"):
        return _dumps({"error": "chatwoot_label_reserved"})
    return _chatwoot("POST", f"/conversations/{conversation_id}/labels",
                     {"label": label, "requested_by": requested_by})


def list_chatwoot_macros(params: dict[str, object], **kwargs: object) -> str:
    """Los macros de la cuenta de la instancia, con sus acciones. Solo lee."""
    del params, kwargs
    return _chatwoot("GET", "/macros")


def create_chatwoot_label_macro(params: dict[str, object], **kwargs: object) -> str:
    """Un macro que solo suma esa etiqueta, o el que ya lo hace. La usa una campana para entrar."""
    del kwargs
    try:
        requested_by = _requested_by()
    except ValueError as exc:
        return _dumps({"error": str(exc)})
    label = _label(params.get("label"))
    if label is None:
        return _dumps({"error": "invalid_body"})
    if label.startswith("automation_") or label == "cliente":
        return _dumps({"error": "chatwoot_label_reserved"})
    return _chatwoot("POST", "/macros", {"label": label, "requested_by": requested_by})


# agent-campaigns-v1 (E-355): armar una campana. No hay herramienta para publicarla ni pausarla: eso
# lo hace un aprobador en la pagina, y el control de calidad lo corre el operador por comando.
CAMPAIGNS = f"{PREFIX}/campaigns"


def _campaign_requester() -> dict[str, str] | str:
    try:
        return _requested_by()
    except ValueError as exc:
        return _dumps({"error": str(exc)})


def preview_campaign(params: dict[str, object], **kwargs: object) -> str:
    del kwargs
    return _dumps(_request("POST", f"{CAMPAIGNS}/preview", {"campaign": params.get("campaign")}))


def propose_campaign(params: dict[str, object], **kwargs: object) -> str:
    del kwargs
    requested = _campaign_requester()
    if isinstance(requested, str):
        return requested
    purpose = params.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        return _dumps({"error": "invalid_body"})
    if looks_like_identifier(purpose):
        return _dumps({"error": "identifier_not_allowed"})
    body: dict[str, object] = {"campaign": params.get("campaign"), "purpose": purpose.strip(),
                               "requested_by": requested}
    for key in ("campaign_id", "revision"):
        if params.get(key) is not None:
            body[key] = params[key]
    return _dumps(_request("POST", CAMPAIGNS, body))


def validate_campaign(params: dict[str, object], **kwargs: object) -> str:
    del kwargs
    campaign_id = _uuid(params.get("campaign_id"))
    if campaign_id is None:
        return _dumps({"error": "invalid_campaign_id"})
    requested = _campaign_requester()
    if isinstance(requested, str):
        return requested
    return _dumps(_request("POST", f"{CAMPAIGNS}/{campaign_id}/validate",
                           {"revision": params.get("revision"), "requested_by": requested}))


def get_campaign(params: dict[str, object], **kwargs: object) -> str:
    del kwargs
    campaign_id = _uuid(params.get("campaign_id"))
    if campaign_id is None:
        return _dumps({"error": "invalid_campaign_id"})
    return _dumps(_request("GET", f"{CAMPAIGNS}/{campaign_id}"))


def list_campaigns(params: dict[str, object], **kwargs: object) -> str:
    del kwargs
    estado = params.get("estado", "abiertas")
    if estado not in ("abiertas", "todas"):
        return _dumps({"error": "invalid_query", "reason": {"field": "estado"}})
    return _dumps(_request("GET", f"{CAMPAIGNS}?estado={estado}"))


# E-486 (8d, D14 y D15 aprobadas por Dan el 12/11): sumar un telefono al grupo de prueba.
_FLOW_REF_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_PHONE_DIGITS_RE = re.compile(r"\+?[0-9]{8,15}")


def _phone(value: object) -> str | None:
    """El telefono con solo digitos (y el + inicial), o None. Nunca se devuelve ni se loguea."""
    if not isinstance(value, str) or len(value) > 32:
        return None
    compact = "".join(character for character in value if character not in " \t\n\r-.()")
    return compact if _PHONE_DIGITS_RE.fullmatch(compact) else None


def add_to_test_group(params: dict[str, object], **kwargs: object) -> str:
    """Suma una persona al grupo de prueba (cohorte) de un flujo nativo en cohorte. Solo agrega.

    La persona va por su numero de conversacion de Chatwoot o por su telefono, uno de los dos. Solo un
    aprobador de la pagina puede: el bridge lo chequea con la sesion de Slack (``not_an_approver``). La
    respuesta nunca trae el telefono.
    """
    del kwargs
    try:
        requested_by = _requested_by()
    except ValueError as exc:
        return _dumps({"error": str(exc)})
    product, flow = params.get("product"), params.get("flow")
    if not (isinstance(product, str) and _FLOW_REF_RE.fullmatch(product)
            and isinstance(flow, str) and _FLOW_REF_RE.fullmatch(flow)):
        return _dumps({"error": "invalid_body"})
    conversation_id, raw_phone = params.get("conversation_id"), params.get("phone")
    body: dict[str, object] = {"requested_by": requested_by}
    if conversation_id is not None and raw_phone is None:
        if type(conversation_id) is not int or not 1 <= conversation_id < 10**12:
            return _dumps({"error": "invalid_conversation_id"})
        body["conversation_id"] = conversation_id
    elif raw_phone is not None and conversation_id is None:
        phone = _phone(raw_phone)
        if phone is None:
            return _dumps({"error": "flow_cohort_phone_invalid"})
        body["phone"] = phone
    else:
        return _dumps({"error": "invalid_body"})
    out = _request("POST", f"{PREFIX}/flows/{product}/{flow}/cohort", body)
    if "error" in out:
        return _dumps(out)
    status = out.get("status")
    result: dict[str, object] = {"status": status if status in ("added", "already") else "unknown"}
    for key in ("members", "max_members"):
        value = out.get(key)
        if type(value) is int and 0 <= value <= 100:
            result[key] = value
    return _dumps(result)
