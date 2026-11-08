"""La herramienta acotada de Chatwoot del Automation Expert (automation-expert-chatwoot-v1).

Decision del 2026-10-31 (F-143): el agente crea y asigna etiquetas en Chatwoot,
y lee lo que necesita, para que ningun cliente tenga que tocar Chatwoot a mano.
**Lectura y creacion si; borrado no.** Chatwoot no da tokens con alcance fino
(la clave de control es de un usuario administrador, y hoy de las cuentas de
varias aliadas), asi que el alcance vive aca: una lista cerrada de metodos y
rutas de Chatwoot, con la cuenta de la instancia fija en la ruta.

* ``GET  /internal/automation-expert/v1/chatwoot/labels``: las etiquetas de la cuenta.
* ``POST /internal/automation-expert/v1/chatwoot/labels``: crea una etiqueta. Si
  ya existe, contesta 200 con ``created: false`` y no toca nada.
* ``POST /internal/automation-expert/v1/chatwoot/conversations/{id}/labels``: le
  suma una etiqueta que ya existe a una conversacion de la bandeja de la
  instancia. Nunca saca otra: Chatwoot reemplaza la lista entera, asi que se
  lee la lista de la conversacion y se manda esa misma mas la nueva.
* ``GET  /internal/automation-expert/v1/chatwoot/macros``: los macros que ve el
  usuario de la clave, sin quien los creo ni los archivos. Solo lee.
* ``POST /internal/automation-expert/v1/chatwoot/macros``: crea un macro que solo
  suma una etiqueta que ya existe (lo que exige ``ProductLabeler``), o devuelve el
  que ya lo hace (decision de Dan del 2026-11-08, agent-campaigns-v1). Es la unica
  forma de macro que sale de aca: ni otras acciones, ni editar, ni ejecutar.

Las rutas existen solo con ``AUTOMATION_EXPERT_CHATWOOT_ENABLED`` (apagado por
defecto) y piden la clave de cambio del agente (``AUTOMATION_EXPERT_CHANGE_TOKEN``).
La clave de Chatwoot la tiene el bridge: el agente nunca la ve.

Las formas de Chatwoot son las de v4.13.0, leidas en su codigo publico
(``labels_controller.rb``, ``labels/index.json.jbuilder``,
``labels/create.json.jbuilder``, ``label_concern.rb``, ``labelable.rb``,
``macros/index.json.jbuilder``, ``models/_macro.json.jbuilder``) el 2026-11-01.
La creacion de un macro, en v4.13.0 (``macros_controller.rb`` y
``macros/create.json.jbuilder``, leidos el 2026-11-08): ``POST /macros`` con
``name``, ``visibility`` y ``actions``; contesta ``{"payload": {...}}``. Un usuario
``agent`` queda con visibilidad ``personal`` aunque se pida ``global``.

El log lleva la ruta, el estado, el codigo y los conteos; nunca un titulo, una
descripcion ni una clave.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .flow_change_api import PREFIX, looks_like_identifier
from .flow_definition import loads_strict

__all__ = [
    "ALLOWED_CALLS",
    "CONTRACT",
    "CONTRACT_VERSION",
    "ChatwootCallRefused",
    "ChatwootToolUnavailable",
    "ScopedChatwoot",
    "check_settings",
    "register_automation_expert_chatwoot_routes",
]

logger = logging.getLogger(__name__)

CONTRACT = "automation-expert-chatwoot-v1"
CONTRACT_VERSION = "1.1.0"
# Lo que contesta una instancia sin campanas (AGENT_CAMPAIGNS_ENABLED apagado): sin crear macros.
CONTRACT_VERSION_WITHOUT_MACRO_CREATION = "1.0.0"
ROUTE_PREFIX = f"{PREFIX}/chatwoot"
MAX_BODY_BYTES = 8 * 1024
MAX_UPSTREAM_BYTES = 2 * 1024 * 1024
LABELS_MAX = 200
MACROS_MAX = 100
DEFAULT_COLOR = "#1f93ff"  # el default de la columna color de labels en Chatwoot 4.13

# **La lista cerrada.** Metodo y ruta de Chatwoot que la herramienta puede
# pedir; ``{account}`` es siempre la cuenta de la instancia y ``{conversation}``
# un entero positivo. Cualquier otra combinacion, y todo DELETE, se rechaza
# antes de salir a la red. Sumar una ruta aca es una decision, no un arreglo.
ALLOWED_CALLS: tuple[tuple[str, str], ...] = (
    ("GET", "/api/v1/accounts/{account}/labels"),
    ("POST", "/api/v1/accounts/{account}/labels"),
    ("GET", "/api/v1/accounts/{account}/conversations/{conversation}"),
    ("GET", "/api/v1/accounts/{account}/conversations/{conversation}/labels"),
    ("POST", "/api/v1/accounts/{account}/conversations/{conversation}/labels"),
    ("GET", "/api/v1/accounts/{account}/macros"),
    ("POST", "/api/v1/accounts/{account}/macros"),
)
# Crear un macro es de las campanas: sin AGENT_CAMPAIGNS_ENABLED esta ruta queda afuera de la lista.
_MACRO_CREATION = ("POST", "/api/v1/accounts/{account}/macros")

# Las etiquetas del control del bridge: ponerlas pausa o da de baja a una
# persona, y eso no es de esta herramienta.
_RESERVED_PREFIX = "automation_"
# Un macro de esta herramienta no puede ser el de ``cliente`` (sigue por customer_status).
_MACRO_FORBIDDEN_LABELS = frozenset({"cliente"})
_MACRO_NAME_PREFIX = "campana-"
# Chatwoot: UNICODE_CHARACTER_NUMBER_HYPHEN_UNDERSCORE, y el titulo se pasa a minusculas.
_TITLE = re.compile(r"^[\w-]{1,40}$")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_HIDDEN = "<oculto>"


class ChatwootCallRefused(Exception):
    """Un pedido a Chatwoot fuera de la lista cerrada (o un DELETE): no sale."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ChatwootToolUnavailable(Exception):
    """Chatwoot no contesto, o contesto algo que no se reconoce."""

    def __init__(self, code: str = "chatwoot_tool_unavailable", http_status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


def _compile(account_id: int, *, create_macros: bool) -> tuple[tuple[str, re.Pattern[str]], ...]:
    compiled = []
    for method, template in ALLOWED_CALLS:
        if (method, template) == _MACRO_CREATION and not create_macros:
            continue
        pattern = re.escape(template).replace(re.escape("{account}"), str(account_id)).replace(
            re.escape("{conversation}"), r"[1-9][0-9]{0,11}")
        compiled.append((method, re.compile(f"^{pattern}$")))
    return tuple(compiled)


class ScopedChatwoot:
    """El cliente de Chatwoot del agente: solo la lista cerrada, en la cuenta y la bandeja de la instancia."""

    def __init__(self, *, base_url: str, account_id: int, access_token: str, inbox_id: int,
                 transport: httpx.AsyncBaseTransport | None = None, timeout_seconds: float = 15.0,
                 create_macros: bool = False) -> None:
        if type(account_id) is not int or account_id <= 0 or type(inbox_id) is not int or inbox_id <= 0:
            raise ValueError("account and inbox must be positive integers")
        self._base_url = base_url.rstrip("/")
        self._account_id = account_id
        self._access_token = access_token
        self._inbox_id = inbox_id
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._allowed = _compile(account_id, create_macros=create_macros)

    @property
    def account_id(self) -> int:
        return self._account_id

    def check_call(self, method: str, path: str) -> None:
        """Lanza ChatwootCallRefused si el metodo y la ruta no estan en la lista cerrada."""
        verb = method.upper() if isinstance(method, str) else ""
        if verb == "DELETE":
            raise ChatwootCallRefused("chatwoot_delete_not_allowed")
        if not isinstance(path, str) or not any(
                verb == allowed and pattern.fullmatch(path) for allowed, pattern in self._allowed):
            raise ChatwootCallRefused("chatwoot_call_not_allowed")

    async def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        self.check_call(method, path)
        try:
            async with httpx.AsyncClient(base_url=self._base_url, transport=self._transport,
                                         headers={"api_access_token": self._access_token},
                                         timeout=self._timeout_seconds, follow_redirects=False) as client:
                response = await client.request(method.upper(), path,
                                                json=dict(body) if body is not None else None)
        except httpx.HTTPError as error:
            raise ChatwootToolUnavailable() from error
        if len(response.content) > MAX_UPSTREAM_BYTES:
            raise ChatwootToolUnavailable("chatwoot_tool_response_too_large")
        if not 200 <= response.status_code < 300:
            raise ChatwootToolUnavailable(http_status=response.status_code)
        try:
            return response.json()
        except ValueError as error:
            raise ChatwootToolUnavailable() from error

    def _path(self, suffix: str) -> str:
        return f"/api/v1/accounts/{self._account_id}{suffix}"

    async def list_labels(self) -> list[dict[str, Any]]:
        payload = await self._call("GET", self._path("/labels"))
        labels = payload.get("payload") if isinstance(payload, dict) else None
        if not isinstance(labels, list) or not all(
                isinstance(label, dict) and isinstance(label.get("title"), str) for label in labels):
            raise ChatwootToolUnavailable("chatwoot_tool_response_invalid")
        return [_label_view(label) for label in labels]

    async def create_label(self, *, title: str, description: str, color: str,
                           show_on_sidebar: bool) -> tuple[dict[str, Any], bool]:
        """Crea la etiqueta; si ya esta, la devuelve sin tocar nada. (etiqueta, si se creo)."""
        existing = {label["title"]: label for label in await self.list_labels()}
        if title in existing:
            return existing[title], False
        try:
            created = await self._call("POST", self._path("/labels"), {
                "title": title, "description": description, "color": color, "show_on_sidebar": show_on_sidebar})
        except ChatwootToolUnavailable as error:
            # 422: otro la creo entre la lectura y la escritura (titulo unico por cuenta).
            if error.http_status != 422:
                raise
            existing = {label["title"]: label for label in await self.list_labels()}
            if title in existing:
                return existing[title], False
            raise ChatwootToolUnavailable("chatwoot_label_rejected") from None
        if not isinstance(created, dict) or created.get("title") != title:
            raise ChatwootToolUnavailable("chatwoot_tool_response_invalid")
        return _label_view(created), True

    async def _conversation_labels(self, conversation_id: int) -> list[str]:
        payload = await self._call("GET", self._path(f"/conversations/{conversation_id}/labels"))
        labels = payload.get("payload") if isinstance(payload, dict) else None
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise ChatwootToolUnavailable("chatwoot_tool_response_invalid")
        return labels

    async def add_label_to_conversation(self, *, conversation_id: int, label: str) -> dict[str, Any]:
        """Suma una etiqueta existente a una conversacion de la bandeja. Nunca saca otra."""
        if label not in {item["title"] for item in await self.list_labels()}:
            raise ChatwootCallRefused("chatwoot_label_not_found")
        try:
            conversation = await self._call("GET", self._path(f"/conversations/{conversation_id}"))
        except ChatwootToolUnavailable as error:
            if error.http_status == 404:
                raise ChatwootCallRefused("chatwoot_conversation_not_found") from None
            raise
        # Una conversacion de otra bandeja de la cuenta se contesta igual que una que no existe.
        if not isinstance(conversation, dict) or conversation.get("id") != conversation_id \
                or conversation.get("inbox_id") != self._inbox_id:
            raise ChatwootCallRefused("chatwoot_conversation_not_found")
        before = await self._conversation_labels(conversation_id)
        if label in before:
            return {"conversation_id": conversation_id, "label": label, "changed": False, "labels": before}
        # Chatwoot reemplaza la lista (update_labels): se manda la que habia mas la nueva.
        wanted = [*before, label]
        payload = await self._call("POST", self._path(f"/conversations/{conversation_id}/labels"),
                                   {"labels": wanted})
        after = payload.get("payload") if isinstance(payload, dict) else None
        if not isinstance(after, list) or label not in after:
            raise ChatwootToolUnavailable("chatwoot_label_not_confirmed")
        return {"conversation_id": conversation_id, "label": label, "changed": True,
                "labels": [item for item in after if isinstance(item, str)]}

    async def list_macros(self) -> list[dict[str, Any]]:
        payload = await self._call("GET", self._path("/macros"))
        macros = payload.get("payload") if isinstance(payload, dict) else None
        if not isinstance(macros, list) or not all(isinstance(macro, dict) for macro in macros):
            raise ChatwootToolUnavailable("chatwoot_tool_response_invalid")
        return [_macro_view(macro) for macro in macros]

    async def create_label_macro(self, *, label: str) -> tuple[dict[str, Any], bool]:
        """Un macro global que solo suma la etiqueta. Si ya hay uno que hace exactamente eso, ese."""
        if label not in {item["title"] for item in await self.list_labels()}:
            raise ChatwootCallRefused("chatwoot_label_not_found")
        wanted = [{"action_name": "add_label", "action_params": [label]}]
        for macro in await self.list_macros():
            if macro.get("actions") == wanted:
                return macro, False
        try:
            payload = await self._call("POST", self._path("/macros"), {
                "name": f"{_MACRO_NAME_PREFIX}{label}"[:120], "visibility": "global", "actions": wanted})
        except ChatwootToolUnavailable as error:
            if error.http_status in (403, 422):
                raise ChatwootToolUnavailable("chatwoot_macro_rejected") from None
            raise
        created = payload.get("payload") if isinstance(payload, dict) else None
        if not isinstance(created, dict) or type(created.get("id")) is not int or created.get("actions") != wanted:
            raise ChatwootToolUnavailable("chatwoot_tool_response_invalid")
        return _macro_view(created), True


def _clean_text(value: Any, limit: int = 200) -> Any:
    if isinstance(value, (bool, int)):
        return value
    if not isinstance(value, str):
        return _HIDDEN
    if looks_like_identifier(value):
        return _HIDDEN
    return value[:limit]


def _label_view(label: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": label.get("id"), "title": label.get("title"),
            "description": _clean_text(label.get("description")) if label.get("description") is not None else None,
            "color": label.get("color"), "show_on_sidebar": label.get("show_on_sidebar") is True}


def _macro_view(macro: Mapping[str, Any]) -> dict[str, Any]:
    """Un macro sin quien lo creo, sin los archivos y con cada parametro que parezca un dato personal tapado."""
    actions = []
    for action in macro.get("actions") or []:
        if not isinstance(action, Mapping):
            continue
        params = action.get("action_params")
        actions.append({"action_name": _clean_text(action.get("action_name"), 60),
                        "action_params": [_clean_text(param) for param in params][:20]
                        if isinstance(params, list) else []})
    return {"id": macro.get("id"), "name": _clean_text(macro.get("name"), 120),
            "visibility": macro.get("visibility"), "actions": actions}


def check_settings(settings: Any) -> None:
    """Lo que exige el flag al arrancar."""
    if not getattr(settings, "automation_expert_chatwoot_enabled", False):
        return
    if not getattr(settings, "automation_expert_change_enabled", False):
        raise ValueError("AUTOMATION_EXPERT_CHATWOOT_ENABLED requires AUTOMATION_EXPERT_CHANGE_ENABLED")
    manifest = settings.instance_manifest
    if manifest is None:
        raise ValueError("AUTOMATION_EXPERT_CHATWOOT_ENABLED requires an instance manifest")
    if not settings.chatwoot_base_url or not settings.chatwoot_control_api_access_token:
        raise ValueError("AUTOMATION_EXPERT_CHATWOOT_ENABLED requires the Chatwoot control access token")
    # La clave de control puede administrar otras cuentas: la de la herramienta es la del manifiesto.
    if settings.chatwoot_account_id != manifest.chatwoot_account_id:
        raise ValueError("AUTOMATION_EXPERT_CHATWOOT_ENABLED requires CHATWOOT_ACCOUNT_ID to match the manifest")


def _title(value: Any, field: str = "title") -> str:
    if not isinstance(value, str):
        raise _Refused("invalid_body", {"field": field, "problem": "not_a_string"})
    title = value.strip().lower()
    if not _TITLE.fullmatch(title):
        raise _Refused("invalid_body", {"field": field, "problem": "letters_digits_hyphen_1_to_40"})
    if looks_like_identifier(title):
        raise _Refused("identifier_not_allowed", {"field": field, "problem": "phone_or_mail"})
    if title.startswith(_RESERVED_PREFIX):
        raise _Refused("chatwoot_label_reserved", {"field": field, "problem": "automation_prefix"})
    return title


class _Refused(Exception):
    """Un 422 de entrada con su motivo: nombres de campos, nunca un valor."""

    def __init__(self, code: str, reason: Mapping[str, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.reason = dict(reason)


_SLACK_USER = re.compile(r"^[UW][A-Z0-9]{8,20}$")


def _check_body(body: Any, *, required: set[str], allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(body, Mapping):
        raise _Refused("invalid_body", {"field": "body", "problem": "not_object"})
    missing, extra = required - set(body), set(body) - allowed
    if missing or extra:
        reason: dict[str, Any] = {"field": "body"}
        if missing:
            reason["missing"] = sorted(missing)
        if extra:
            reason["extra"] = sorted(key if isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key)
                                     else "<otra>" for key in extra)[:10]
        raise _Refused("invalid_body", reason)
    requested = body["requested_by"]
    user = requested.get("slack_user") if isinstance(requested, Mapping) else None
    if not isinstance(requested, Mapping) or set(requested) != {"slack_user"} or not isinstance(user, str) \
            or not _SLACK_USER.fullmatch(user):
        raise _Refused("invalid_body", {"field": "requested_by", "problem": "not_a_slack_user"})
    return body


async def _body(request: Request) -> Any:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        raise HTTPException(status_code=422, detail="body_too_large")
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(status_code=422, detail="body_too_large")
    try:
        return loads_strict(raw)
    except ValueError:
        raise _Refused("invalid_body", {"field": "body", "problem": "not_json"}) from None


_REFUSED_STATUS = {
    "chatwoot_delete_not_allowed": 403,
    "chatwoot_call_not_allowed": 403,
    "chatwoot_label_not_found": 404,
    "chatwoot_conversation_not_found": 404,
}


def register_automation_expert_chatwoot_routes(app: FastAPI, *, token: str, chatwoot: ScopedChatwoot,
                                               tenant_ref: str, ally_ref: str,
                                               git_sha: str | None = None, create_macros: bool = False) -> None:
    """Las rutas, detras del flag (lo mira quien llama: con el flag apagado no existen).

    ``create_macros`` (``AGENT_CAMPAIGNS_ENABLED``): sin las campanas, ``POST .../macros``
    no existe y el contrato sigue en 1.0.0. Crear un macro solo sirve para una campana.
    """
    instance = {"tenant_ref": tenant_ref, "ally_ref": ally_ref}
    sha = git_sha or os.getenv("GIT_SHA", "") or "unknown"
    slots = asyncio.Semaphore(2)
    contract_version = CONTRACT_VERSION if create_macros else CONTRACT_VERSION_WITHOUT_MACRO_CREATION

    def authenticate(authorization: str | None) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="automation_expert_change_authentication_required",
                                headers={"WWW-Authenticate": "Bearer"})

    def envelope(data: Mapping[str, Any]) -> dict[str, Any]:
        return {"contract": CONTRACT, "contract_version": contract_version, "instance": dict(instance),
                "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "bridge_git_sha": sha, "data": dict(data)}

    def refused(route: str, code: str, reason: Mapping[str, Any] | None = None) -> JSONResponse:
        http_status = _REFUSED_STATUS.get(code, 422)
        logger.info("automation_expert_chatwoot route=%s status=%s code=%s", route, http_status, code)
        content: dict[str, Any] = {"detail": code}
        if reason:
            content["reason"] = dict(reason)
        return JSONResponse(status_code=http_status, content=content)

    def unavailable(route: str, error: ChatwootToolUnavailable) -> HTTPException:
        logger.info("automation_expert_chatwoot route=%s status=503 code=%s upstream=%s", route, error.code,
                    error.http_status)
        return HTTPException(status_code=503, detail=error.code)

    async def run(route: str, call: Any) -> Any:
        if slots.locked():
            raise HTTPException(status_code=503, detail="automation_expert_change_busy")
        async with slots:
            try:
                return await call()
            except ChatwootCallRefused as error:
                return refused(route, error.code)
            except ChatwootToolUnavailable as error:
                raise unavailable(route, error) from None

    @app.get(f"{ROUTE_PREFIX}/labels")
    async def list_labels(authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        out = await run("labels", chatwoot.list_labels)
        if isinstance(out, JSONResponse):
            return out
        logger.info("automation_expert_chatwoot route=labels status=200 labels=%s", len(out))
        return envelope({"labels": out[:LABELS_MAX], "total": len(out), "truncated": len(out) > LABELS_MAX})

    @app.post(f"{ROUTE_PREFIX}/labels")
    async def create_label(request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        try:
            body = _check_body(await _body(request), required={"title", "requested_by"},
                               allowed={"title", "description", "color", "show_on_sidebar", "requested_by"})
            title = _title(body["title"])
            description = body.get("description", "")
            if not isinstance(description, str) or len(description) > 200:
                raise _Refused("invalid_body", {"field": "description", "problem": "string_up_to_200"})
            if looks_like_identifier(description):
                raise _Refused("identifier_not_allowed", {"field": "description", "problem": "phone_or_mail"})
            color = body.get("color", DEFAULT_COLOR)
            if not isinstance(color, str) or not _COLOR.fullmatch(color):
                raise _Refused("invalid_body", {"field": "color", "problem": "hex_like_1f93ff"})
            sidebar = body.get("show_on_sidebar", True)
            if type(sidebar) is not bool:
                raise _Refused("invalid_body", {"field": "show_on_sidebar", "problem": "not_a_boolean"})
        except _Refused as error:
            return refused("create_label", error.code, error.reason)

        async def call() -> Any:
            return await chatwoot.create_label(title=title, description=description.strip(), color=color,
                                               show_on_sidebar=sidebar)

        out = await run("create_label", call)
        if isinstance(out, JSONResponse):
            return out
        label, created = out
        logger.info("automation_expert_chatwoot route=create_label status=%s created=%s",
                    201 if created else 200, created)
        return JSONResponse(status_code=201 if created else 200,
                            content=envelope({"label": label, "created": created}))

    @app.post(f"{ROUTE_PREFIX}/conversations/{{conversation_id}}/labels")
    async def add_label(conversation_id: str, request: Request,
                        authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        try:
            if not (conversation_id.isascii() and conversation_id.isdigit()) or not 1 <= len(conversation_id) <= 12 \
                    or conversation_id.startswith("0"):
                raise _Refused("invalid_conversation_id", {"field": "conversation_id", "problem": "positive_integer"})
            checked = int(conversation_id)
            body = _check_body(await _body(request), required={"label", "requested_by"},
                               allowed={"label", "requested_by"})
            label = _title(body["label"], "label")
        except _Refused as error:
            return refused("add_label", error.code, error.reason)

        async def call() -> Any:
            return await chatwoot.add_label_to_conversation(conversation_id=checked, label=label)

        out = await run("add_label", call)
        if isinstance(out, JSONResponse):
            return out
        logger.info("automation_expert_chatwoot route=add_label conversation_id=%s status=200 changed=%s labels=%s",
                    checked, out["changed"], len(out["labels"]))
        return envelope(out)

    @app.get(f"{ROUTE_PREFIX}/macros")
    async def list_macros(authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        out = await run("macros", chatwoot.list_macros)
        if isinstance(out, JSONResponse):
            return out
        logger.info("automation_expert_chatwoot route=macros status=200 macros=%s", len(out))
        return envelope({"macros": out[:MACROS_MAX], "total": len(out), "truncated": len(out) > MACROS_MAX})

    if not create_macros:
        return

    @app.post(f"{ROUTE_PREFIX}/macros")
    async def create_macro(request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        try:
            body = _check_body(await _body(request), required={"label", "requested_by"},
                               allowed={"label", "requested_by"})
            label = _title(body["label"], "label")
            if label in _MACRO_FORBIDDEN_LABELS:
                raise _Refused("chatwoot_label_reserved", {"field": "label", "problem": "client_label"})
        except _Refused as error:
            return refused("create_macro", error.code, error.reason)

        async def call() -> Any:
            return await chatwoot.create_label_macro(label=label)

        out = await run("create_macro", call)
        if isinstance(out, JSONResponse):
            return out
        macro, created = out
        logger.info("automation_expert_chatwoot route=create_macro status=%s created=%s macro=%s",
                    201 if created else 200, created, macro.get("id"))
        return JSONResponse(status_code=201 if created else 200,
                            content=envelope({"macro": macro, "created": created}))
