"""Las rutas de cambio del Automation Expert (flow-change-cycle-v1, «Las rutas y las herramientas»).

El agente propone un cambio de flujo y lo valida; nunca lo aprueba ni lo
publica (eso es del comando del operador, scripts/flow_change_operator.py).
Diseno: el almacen, §10.1.

* ``POST /internal/automation-expert/v1/flow-changes``: crea o rehace un
  borrador. Corre el validador en fase ``draft``; con bloqueos contesta 422
  ``flow_change_draft_blocked`` y no guarda.
* ``POST /internal/automation-expert/v1/flow-changes/{id}/validate``: valida en
  fase ``publish`` con el catalogo de la bandeja, arma la vista previa y la
  registra.
* ``GET /internal/automation-expert/v1/flow-changes/{id}``: relee la propuesta.
* ``GET /internal/automation-expert/v1/flow-store``: los flujos del almacen con
  su ``flow_id`` y su definicion activa, que es de donde el agente copia lo que
  cambia (la lectura de la 1a no los trae mientras siga leyendo la proyeccion).

La pausa del agente no existe todavia: la base no puede verificar quien la
pide (V2#5). Pausar es del comando del operador.

**Las claves, separadas.** El plugin entra con ``AUTOMATION_EXPERT_CHANGE_TOKEN``,
que alcanza solo estas rutas. El bridge entra a PostgREST con
``AUTOMATION_EXPERT_CHANGE_DB_KEY``, el JWT del rol ``automation_expert_change``,
que alcanza solo los cuatro envoltorios de 20261021000100: este modulo tiene su
propio cliente y nunca usa el de ``service_role``.

El log lleva la ruta, el id de la propuesta, los conteos y el codigo; nunca el
cuerpo, el «para que» ni una clave.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .flow_change_operator import OperatorInputError, validate_proposal, world_from_store
from .flow_definition import InstanceView, ProposalItemsInvalid, loads_strict, parse_items
from .flow_validator import FlowValidationInputError, validate

__all__ = [
    "CONTRACT",
    "CONTRACT_VERSION",
    "DB_ROLE",
    "PREFIX",
    "FlowChangeStore",
    "StoreRejected",
    "StoreUnavailable",
    "check_settings",
    "looks_like_identifier",
    "register_flow_change_routes",
]

logger = logging.getLogger(__name__)

CONTRACT = "flow-change-cycle-v1"
CONTRACT_VERSION = "1.3.0"
PREFIX = "/internal/automation-expert/v1"
DB_ROLE = "automation_expert_change"
MAX_BODY_BYTES = 256 * 1024
MAX_STORE_BYTES = 4 * 1024 * 1024
_CODE = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
_SLACK_USER = re.compile(r"^[UW][A-Z0-9]{8,20}$")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")
_PHONE_SEPARATORS = frozenset(" \t\n\r -.()+")
# Lo que la base contesta con un resultado ({"ok": false, "reason_code"}) y su estado HTTP.
_RESULT_STATUS = {
    "flow_change_stale_base": 409,
    "flow_change_revision_mismatch": 409,
    "flow_change_not_draft": 409,
    "flow_change_proposal_expired": 409,
    "flow_change_open_limit": 409,
    "flow_change_revision_limit": 409,
    "flow_change_validation_limit": 409,
}
# Lo que la base levanta como excepcion, por SQLSTATE (PostgREST 14 manda P0002
# como 500: se mira el codigo, no el estado HTTP; capturado el 2026-10-21 en
# tests/fixtures/automation_expert_change/).
_RAISED_STATUS = {"22023": 422, "P0002": 404, "55000": 409}


class StoreUnavailable(Exception):
    """El almacen no contesto o contesto algo que no se reconoce."""

    def __init__(self, code: str = "automation_expert_change_unavailable") -> None:
        super().__init__(code)
        self.code = code


class StoreRejected(Exception):
    """La base rechazo el pedido con un codigo cerrado."""

    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


class FlowChangeStore:
    """El cliente de los cuatro envoltorios, con la clave del rol del Automation Expert."""

    def __init__(self, *, base_url: str, db_key: str, transport: httpx.AsyncBaseTransport | None = None,
                 timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._db_key = db_key
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    async def _rpc(self, function: str, payload: Mapping[str, Any]) -> Any:
        headers = {"apikey": self._db_key, "Authorization": f"Bearer {self._db_key}",
                   "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(base_url=self._base_url, headers=headers, transport=self._transport,
                                         timeout=self._timeout_seconds) as client:
                response = await client.post(f"/rest/v1/rpc/{function}", content=json.dumps(payload))
        except httpx.HTTPError as error:
            raise StoreUnavailable() from error
        if len(response.content) > MAX_STORE_BYTES:
            raise StoreUnavailable("automation_expert_change_response_too_large")
        try:
            body = response.json()
        except ValueError as error:
            raise StoreUnavailable() from error
        if response.status_code == 200:
            if not isinstance(body, dict):
                raise StoreUnavailable()
            return body
        sqlstate = body.get("code") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else None
        if sqlstate == "54000":
            raise StoreUnavailable("automation_expert_change_response_too_large")
        # Del mensaje solo se cree un codigo con la forma de los codigos: el
        # resto (un error de Postgres con texto) es «no disponible».
        if sqlstate in _RAISED_STATUS and isinstance(message, str) and _CODE.fullmatch(message):
            raise StoreRejected(message, _RAISED_STATUS[sqlstate])
        raise StoreUnavailable()

    async def read_store(self, instance: Mapping[str, str]) -> dict[str, Any]:
        return await self._rpc("read_flow_store_v1", {"p_instance": instance})

    async def save_draft(self, instance: Mapping[str, str], *, proposal_id: str | None, revision: int | None,
                         items: list[dict[str, Any]], base: Mapping[str, Any], purpose: str,
                         requested_by: str) -> dict[str, Any]:
        return await self._rpc("save_flow_change_draft_v1", {
            "p_instance": instance, "p_proposal_id": proposal_id, "p_expected_revision": revision,
            "p_items": items, "p_base": base, "p_purpose": purpose, "p_requested_by_ref": requested_by})

    async def record_validation(self, instance: Mapping[str, str], *, proposal_id: str, revision: int,
                                validated_base: Mapping[str, Any], validator: Mapping[str, Any],
                                result: Mapping[str, Any], preview: Mapping[str, Any],
                                requested_by: str) -> dict[str, Any]:
        return await self._rpc("record_flow_change_validation_v1", {
            "p_instance": instance, "p_proposal_id": proposal_id, "p_revision": revision,
            "p_validated_base": validated_base, "p_validator": validator, "p_result": result,
            "p_preview": preview, "p_requested_by_ref": requested_by})

    async def get_proposal(self, instance: Mapping[str, str], proposal_id: str) -> dict[str, Any]:
        return await self._rpc("get_flow_change_proposal_v1", {"p_instance": instance, "p_proposal_id": proposal_id})


def looks_like_identifier(value: str) -> bool:
    """La regla de la lectura: un mail, o siete caracteres numericos seguidos (sin contar separadores)."""
    if _EMAIL.search(value):
        return True
    run = 0
    for character in value:
        if character in _PHONE_SEPARATORS:
            continue
        run = run + 1 if character.isnumeric() else 0
        if run >= 7:
            return True
    return False


def _jwt_role(token: str) -> str | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError):
        return None
    role = claims.get("role") if isinstance(claims, dict) else None
    return role if isinstance(role, str) else None


def check_settings(settings: Any) -> None:
    """Lo que exige el flag al arrancar (diseno del almacen, §10.1, «El bridge»)."""
    if not getattr(settings, "automation_expert_change_enabled", False):
        return
    if settings.instance_manifest is None:
        raise ValueError("AUTOMATION_EXPERT_CHANGE_ENABLED requires an instance manifest")
    if not settings.supabase_base_url:
        raise ValueError("AUTOMATION_EXPERT_CHANGE_ENABLED requires SUPABASE_BASE_URL")
    if not settings.automation_expert_read_enabled:
        raise ValueError("AUTOMATION_EXPERT_CHANGE_ENABLED requires AUTOMATION_EXPERT_READ_ENABLED")
    token = settings.automation_expert_change_token
    if token is None or len(token) < 32:
        raise ValueError("AUTOMATION_EXPERT_CHANGE_TOKEN must contain at least 32 characters")
    from dataclasses import fields as dataclass_fields

    clashing = sorted(
        field.name for field in dataclass_fields(settings)
        if field.name != "automation_expert_change_token"
        and isinstance(value := getattr(settings, field.name), str)
        and hmac.compare_digest(token.encode("utf-8", "surrogatepass"), value.encode("utf-8", "surrogatepass"))
    )
    if clashing:
        raise ValueError("AUTOMATION_EXPERT_CHANGE_TOKEN must differ from every other secret and value of the "
                         "bridge configuration; it equals " + ", ".join(clashing))
    db_key = settings.automation_expert_change_db_key
    if db_key is None or _jwt_role(db_key) != DB_ROLE:
        raise ValueError("AUTOMATION_EXPERT_CHANGE_DB_KEY must be a JWT whose role is automation_expert_change")
    service = settings.supabase_service_role_key or ""
    if hmac.compare_digest(db_key.encode("utf-8"), service.encode("utf-8")):
        raise ValueError("AUTOMATION_EXPERT_CHANGE_DB_KEY must not be the service_role key")


@dataclass(frozen=True)
class _Context:
    instance: dict[str, str]
    time_zone: str | None
    inbox_id: int
    git_sha: str


def _instance_view(context: _Context, store: Mapping[str, Any]) -> InstanceView:
    products = store.get("products")
    if not isinstance(products, list) or not products:
        raise StoreUnavailable()
    return InstanceView(tenant_ref=context.instance["tenant_ref"], ally_ref=context.instance["ally_ref"],
                        products=frozenset(products), time_zone=context.time_zone)


def _finding(finding: Mapping[str, Any]) -> dict[str, Any]:
    return {key: finding.get(key) for key in ("code", "kind", "path", "rule", "reason", "alternative")
            if key in finding}


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
        raise HTTPException(status_code=422, detail="invalid_body") from None


def _requested_by(body: Mapping[str, Any]) -> str:
    requested = body.get("requested_by")
    user = requested.get("slack_user") if isinstance(requested, Mapping) else None
    if not isinstance(requested, Mapping) or set(requested) != {"slack_user"} or not isinstance(user, str) \
            or not _SLACK_USER.fullmatch(user):
        raise HTTPException(status_code=422, detail="invalid_body")
    return user


def _proposal_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid_proposal_id") from None


def register_flow_change_routes(app: FastAPI, *, token: str, store: FlowChangeStore, chatwoot: Any,
                                tenant_ref: str, ally_ref: str, time_zone: str | None, inbox_id: int,
                                git_sha: str | None = None) -> None:
    """Las tres rutas, detras del flag (lo mira quien llama: con el flag apagado no existen)."""
    context = _Context(instance={"tenant_ref": tenant_ref, "ally_ref": ally_ref}, time_zone=time_zone,
                       inbox_id=inbox_id, git_sha=git_sha or os.getenv("GIT_SHA", "") or "unknown")
    slots = asyncio.Semaphore(2)

    def authenticate(authorization: str | None) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization.encode("utf-8"),
                                                            expected.encode("utf-8")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="automation_expert_change_authentication_required",
                                headers={"WWW-Authenticate": "Bearer"})

    def busy() -> None:
        if slots.locked():
            raise HTTPException(status_code=503, detail="automation_expert_change_busy")

    def envelope(data: Mapping[str, Any]) -> dict[str, Any]:
        return {"contract": CONTRACT, "contract_version": CONTRACT_VERSION, "instance": dict(context.instance),
                "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "bridge_git_sha": context.git_sha, "data": dict(data)}

    def failure(error: Exception, route: str, proposal_id: str | None = None) -> HTTPException:
        if isinstance(error, StoreRejected):
            code, http_status = error.code, error.http_status
        elif isinstance(error, StoreUnavailable):
            code, http_status = error.code, 503
        else:  # pragma: no cover - lo que no se espera, cerrado
            code, http_status = "automation_expert_change_unavailable", 503
        logger.info("automation_expert_change route=%s proposal_id=%s status=%s code=%s",
                    route, proposal_id, http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    def result_failure(result: Mapping[str, Any], route: str, proposal_id: str | None) -> HTTPException | None:
        if result.get("ok") is True:
            return None
        code = result.get("reason_code")
        if isinstance(code, str) and code in _RESULT_STATUS:
            logger.info("automation_expert_change route=%s proposal_id=%s status=409 code=%s", route, proposal_id, code)
            return HTTPException(status_code=_RESULT_STATUS[code], detail=code)
        return failure(StoreUnavailable(), route, proposal_id)

    @app.get(f"{PREFIX}/flow-store")
    async def read_flow_store(authorization: str | None = Header(default=None)) -> Any:
        """Los flujos del almacen con su definicion activa: de aca copia el agente lo que cambia."""
        authenticate(authorization)
        busy()
        async with slots:
            try:
                snapshot = await store.read_store(context.instance)
                world_from_store(snapshot)  # la misma forma que valida el borrador
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "flow_store") from None
            except (OperatorInputError, ValueError):
                raise failure(StoreUnavailable(), "flow_store") from None
        flows = []
        for flow in snapshot.get("flows", []):
            versions = flow.get("versions") or {}
            active = flow.get("active_version")
            flows.append({key: flow.get(key) for key in ("flow_id", "product_ref", "flow_key", "state",
                                                          "active_version", "generation", "changed_at")}
                         | {"versions": sorted(int(number) for number in versions),
                            "active_definition": versions.get(str(active)) if active is not None else None})
        logger.info("automation_expert_change route=flow_store status=200 flows=%s", len(flows))
        return envelope({"products": snapshot.get("products"), "flows": flows})

    @app.post(f"{PREFIX}/flow-changes", status_code=201)
    async def propose(request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        body = await _body(request)
        if not isinstance(body, Mapping) or not {"items", "base", "purpose", "requested_by"} <= set(body) \
                or not set(body) <= {"items", "base", "purpose", "requested_by", "proposal_id", "revision"}:
            raise HTTPException(status_code=422, detail="invalid_body")
        requested_by = _requested_by(body)
        purpose, base = body["purpose"], body["base"]
        if not isinstance(purpose, str) or not 1 <= len(purpose) <= 500 or not isinstance(base, Mapping):
            raise HTTPException(status_code=422, detail="invalid_body")
        if looks_like_identifier(purpose):
            raise HTTPException(status_code=422, detail="identifier_not_allowed")
        proposal_id = body.get("proposal_id")
        revision = body.get("revision")
        if (proposal_id is None) != (revision is None) or (
                revision is not None and (type(revision) is not int or revision < 1)):
            raise HTTPException(status_code=422, detail="invalid_body")
        if proposal_id is not None:
            proposal_id = _proposal_id(proposal_id) if isinstance(proposal_id, str) else _proposal_id("")
        try:
            items = parse_items(body["items"])
        except ProposalItemsInvalid:
            raise HTTPException(status_code=422, detail="flow_change_items_invalid") from None
        busy()
        async with slots:
            try:
                snapshot = await store.read_store(context.instance)
                world = world_from_store(snapshot)
                result = validate(items, actor="automation_expert", world=world,
                                  instance=_instance_view(context, snapshot), catalog=None, phase="draft",
                                  draft_base=base)
            except FlowValidationInputError as error:
                raise HTTPException(status_code=404, detail=error.code) from None
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "propose") from None
            except (OperatorInputError, ValueError):
                raise failure(StoreUnavailable(), "propose") from None
            if not result.ok:
                logger.info("automation_expert_change route=propose status=422 code=flow_change_draft_blocked "
                            "blocks=%s", len(result.blocks))
                return JSONResponse(status_code=422, content={
                    "detail": "flow_change_draft_blocked",
                    **envelope({"blocks": [_finding(f.to_json()) for f in result.blocks],
                                "warnings": [_finding(f.to_json()) for f in result.warnings]})})
            try:
                saved = await store.save_draft(context.instance, proposal_id=proposal_id, revision=revision,
                                               items=result.items_json(), base=base, purpose=purpose,
                                               requested_by=requested_by)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "propose", proposal_id) from None
        refused = result_failure(saved, "propose", proposal_id)
        if refused is not None:
            raise refused
        logger.info("automation_expert_change route=propose proposal_id=%s status=201 items=%s warnings=%s",
                    saved.get("proposal_id"), len(items), len(result.warnings))
        return envelope({"proposal_id": saved.get("proposal_id"), "revision": saved.get("revision"),
                         "status": saved.get("status"),
                         "warnings": [_finding(f.to_json()) for f in result.warnings]})

    @app.post(f"{PREFIX}/flow-changes/{{proposal_id}}/validate")
    async def validate_change(proposal_id: str, request: Request,
                              authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _proposal_id(proposal_id)
        body = await _body(request)
        if not isinstance(body, Mapping) or set(body) != {"revision", "requested_by"} \
                or type(body["revision"]) is not int:
            raise HTTPException(status_code=422, detail="invalid_body")
        requested_by = _requested_by(body)
        busy()
        async with slots:
            try:
                proposal = await store.get_proposal(context.instance, checked)
                snapshot = await store.read_store(context.instance)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "validate", checked) from None
            if proposal.get("revision") != body["revision"]:
                raise HTTPException(status_code=409, detail="flow_change_revision_mismatch")
            try:
                inbox = await chatwoot.get_inbox(inbox_id=context.inbox_id)
            except Exception:  # noqa: BLE001 - un Chatwoot caido no deja validar, y se dice
                raise HTTPException(status_code=503, detail="template_catalog_unavailable") from None
            if not isinstance(inbox, Mapping) or not isinstance(inbox.get("message_templates"), list):
                raise HTTPException(status_code=503, detail="template_catalog_unavailable")
            try:
                view = _instance_view(context, snapshot)
                out = validate_proposal({
                    "instance": {**context.instance, "products": sorted(view.products),
                                 "time_zone": context.time_zone},
                    "store": snapshot,
                    "proposal": {"id": checked, "revision": proposal.get("revision"),
                                 "items": proposal.get("items"), "draft_base": proposal.get("draft_base"),
                                 "purpose": proposal.get("purpose"),
                                 "requested_by_kind": proposal.get("requested_by_kind")},
                    "catalog": {"message_templates": inbox["message_templates"]},
                }, environ=os.environ)
            except OperatorInputError as error:
                code = error.code if error.code in ("flow_not_found", "flow_change_items_invalid") \
                    else "automation_expert_change_unavailable"
                raise HTTPException(status_code=404 if code == "flow_not_found" else
                                    (422 if code == "flow_change_items_invalid" else 503), detail=code) from None
            except StoreUnavailable as error:
                raise failure(error, "validate", checked) from None
            try:
                recorded = await store.record_validation(
                    context.instance, proposal_id=checked, revision=body["revision"],
                    validated_base=out["validated_base"], validator=out["validator"], result=out["result"],
                    preview=out["preview"], requested_by=requested_by)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "validate", checked) from None
        refused = result_failure(recorded, "validate", checked)
        if refused is not None:
            raise refused
        logger.info("automation_expert_change route=validate proposal_id=%s status=200 blocks=%s warnings=%s",
                    checked, len(out["result"]["blocks"]), len(out["result"]["warnings"]))
        return envelope({
            "proposal_id": checked, "status": recorded.get("status"),
            "fingerprint": recorded.get("fingerprint"), "expires_at": recorded.get("expires_at"),
            "blocks": [_finding(b) for b in out["result"]["blocks"]],
            "warnings": [_finding(w) for w in out["result"]["warnings"]],
            "preview": out["preview"],
        })

    @app.get(f"{PREFIX}/flow-changes/{{proposal_id}}")
    async def read_change(proposal_id: str, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _proposal_id(proposal_id)
        busy()
        async with slots:
            try:
                proposal = await store.get_proposal(context.instance, checked)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "read", checked) from None
        logger.info("automation_expert_change route=read proposal_id=%s status=200", checked)
        keys = ("proposal_id", "status", "effective_status", "revision", "items", "draft_base", "purpose",
                "requested_by_kind", "fingerprint", "validated_at", "expires_at", "approved_at",
                "approved_by_kind", "published_at", "closed_at", "created_at", "events")
        data = {key: proposal.get(key) for key in keys}
        validation = proposal.get("validation")
        data["preview"] = validation.get("preview") if isinstance(validation, Mapping) else None
        return envelope(data)
