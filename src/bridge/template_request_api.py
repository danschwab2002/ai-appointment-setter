"""Las rutas del Automation Expert para las solicitudes de plantilla (template-request-v1, F-146 parte 2).

El agente redacta una plantilla con el duenio, la guarda como borrador y la
valida; nunca la aprueba ni la manda (eso es de la pagina de aprobacion y del
bridge, partes 3 y 4). Nada de este modulo llama a Meta.

* ``POST /internal/automation-expert/v1/template-requests``: crea o rehace un
  borrador. Corre las reglas (template_request.check) con el catalogo de la
  bandeja si se puede leer; con bloqueos contesta 422
  ``template_request_draft_blocked`` y no guarda.
* ``POST /internal/automation-expert/v1/template-requests/{id}/validate``:
  vuelve a correr las reglas sobre lo guardado, con el catalogo (obligatorio) y,
  si viene de una propuesta de flujo, con el paso ``{a_crear: <nombre>}`` que la
  espera; sin bloqueos arma el pedido exacto de Meta y la burbuja, y la base la
  congela 24 horas con su huella.
* ``GET /internal/automation-expert/v1/template-requests``: las solicitudes y en
  que estado estan (``?estado=abiertas`` o ``todas``, ``&limit=`` de 1 a 50).
* ``GET /internal/automation-expert/v1/template-requests/{id}``: relee una.

Detras de ``META_TEMPLATES_ENABLED`` (apagado por defecto): sin el flag las
rutas no existen. Usa la misma clave del plugin (``AUTOMATION_EXPERT_CHANGE_TOKEN``)
y el mismo JWT del rol ``automation_expert_change`` que las rutas de cambio de
flujos (flow_change_api.py), nunca el de ``service_role``.

El log lleva la ruta, el id, los conteos y el codigo; nunca el texto de la
plantilla, el «para que» ni una clave.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from . import template_request as rules
from .flow_change_api import (
    PREFIX,
    EntryRefused,
    FlowChangeStore,
    StoreRejected,
    StoreUnavailable,
    _body,
    _check_keys,
    _requested_by,
    looks_like_identifier,
)
from .flow_definition import TemplateCatalog

__all__ = [
    "CONTRACT",
    "CONTRACT_VERSION",
    "DAILY_CAP_DEFAULT",
    "TemplateRequestStore",
    "check_settings",
    "register_template_request_routes",
]

logger = logging.getLogger(__name__)

CONTRACT = rules.CONTRACT
CONTRACT_VERSION = "1.0.0"
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 50
LIST_STATES = {"abiertas": True, "todas": False}
# D8: las solicitudes que se mandan a Meta por dia y por instancia. Lo usa el envio (parte 3).
DAILY_CAP_DEFAULT = 5
_PRODUCT_VERSION = re.compile(r"^v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_POINTER = re.compile(r"^(/[a-z0-9_]{1,40}){1,16}$")
_RESULT_STATUS = {
    "meta_template_open_limit": 409,
    "meta_template_revision_limit": 409,
    "meta_template_validation_limit": 409,
    "meta_template_revision_mismatch": 409,
    "meta_template_not_draft": 409,
    "meta_template_request_expired": 409,
    "meta_template_name_taken": 409,
}


class TemplateRequestStore(FlowChangeStore):
    """Las cuatro funciones del agente de 20261102000200, con la clave de su rol (y la relectura de la propuesta)."""

    async def save_request(self, instance: Mapping[str, str], *, request_id: str | None, revision: int | None,
                           draft: Mapping[str, Any], purpose: str, proposal_id: str | None,
                           step_pointer: str | None, requested_by: str) -> dict[str, Any]:
        return await self._rpc("save_meta_template_request_draft_v1", {
            "p_instance": instance, "p_request_id": request_id, "p_expected_revision": revision,
            "p_draft": draft, "p_purpose": purpose, "p_proposal_id": proposal_id,
            "p_step_pointer": step_pointer, "p_requested_by_ref": requested_by})

    async def record_request_validation(self, instance: Mapping[str, str], *, request_id: str, revision: int,
                                        validator: Mapping[str, Any], result: Mapping[str, Any],
                                        meta_request: Mapping[str, Any] | None, preview: Mapping[str, Any],
                                        requested_by: str) -> dict[str, Any]:
        return await self._rpc("record_meta_template_request_validation_v1", {
            "p_instance": instance, "p_request_id": request_id, "p_revision": revision,
            "p_validator": validator, "p_result": result, "p_meta_request": meta_request,
            "p_preview": preview, "p_requested_by_ref": requested_by})

    async def get_request(self, instance: Mapping[str, str], request_id: str) -> dict[str, Any]:
        return await self._rpc("get_meta_template_request_v1", {"p_instance": instance,
                                                               "p_request_id": request_id})

    async def list_requests(self, instance: Mapping[str, str], *, only_open: bool, limit: int) -> dict[str, Any]:
        return await self._rpc("list_meta_template_requests_v1", {"p_instance": instance,
                                                                 "p_only_open": only_open, "p_limit": limit})


def check_settings(settings: Any) -> None:
    """Lo que exige el flag al arrancar (diseno de F-146, §8).

    Sin la pagina de aprobacion no hay quien apruebe, y sin las rutas de cambio
    del agente no hay quien redacte. El token de Meta y el WABA los exige la
    parte 3, que es la que los usa: hoy nada llama a Meta.
    """
    if not getattr(settings, "meta_templates_enabled", False):
        return
    if not getattr(settings, "automation_expert_change_enabled", False):
        raise ValueError("META_TEMPLATES_ENABLED requires AUTOMATION_EXPERT_CHANGE_ENABLED")
    if not getattr(settings, "flow_approval_enabled", False):
        raise ValueError("META_TEMPLATES_ENABLED requires FLOW_APPROVAL_ENABLED")
    if settings.instance_manifest is None:
        raise ValueError("META_TEMPLATES_ENABLED requires an instance manifest")


def _request_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid_request_id") from None


def _catalog_names(catalog: TemplateCatalog) -> list[tuple[str, str]]:
    return [(str(t.get("name")), str(t.get("language", "")).strip()) for t in catalog.templates
            if isinstance(t.get("name"), str)]


@dataclass(frozen=True)
class _Context:
    instance: dict[str, str]
    inbox_id: int
    git_sha: str


def _validator(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """Con que corrio la validacion: la imagen (SETTER_VERSION y GIT_SHA), nunca lo que dice quien llama."""
    version = environ.get("SETTER_VERSION", "").strip()
    sha = environ.get("GIT_SHA", "").strip().lower()
    if not _PRODUCT_VERSION.fullmatch(version) or not _GIT_SHA.fullmatch(sha):
        return None
    return {"contract": CONTRACT, "rules_version": rules.RULES_VERSION, "product_version": version, "git_sha": sha}


def _for_flow(body: Mapping[str, Any]) -> tuple[str | None, str | None]:
    link = body.get("for_flow")
    if link is None:
        return None, None
    if not isinstance(link, Mapping) or set(link) != {"proposal_id", "step"} \
            or not isinstance(link.get("step"), str) or not _POINTER.fullmatch(link["step"]):
        raise EntryRefused("invalid_body", {"field": "for_flow", "problem": "proposal_id_and_step_pointer"})
    try:
        proposal_id = str(UUID(str(link["proposal_id"])))
    except ValueError:
        raise EntryRefused("invalid_body", {"field": "for_flow.proposal_id", "problem": "not_a_uuid"}) from None
    return proposal_id, link["step"]


def register_template_request_routes(app: FastAPI, *, token: str, store: TemplateRequestStore, chatwoot: Any,
                                     tenant_ref: str, ally_ref: str, inbox_id: int,
                                     template_context: rules.TemplateContext, git_sha: str | None = None) -> None:
    """Las rutas, detras del flag (lo mira quien llama: con el flag apagado no existen).

    El manejador de ``EntryRefused`` es el de las rutas de cambio de flujos, que
    el flag exige prendidas.
    """
    context = _Context(instance={"tenant_ref": tenant_ref, "ally_ref": ally_ref}, inbox_id=inbox_id,
                       git_sha=git_sha or os.getenv("GIT_SHA", "") or "unknown")
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

    def failure(error: Exception, route: str, request_id: str | None = None) -> HTTPException:
        if isinstance(error, StoreRejected):
            code, http_status = error.code, error.http_status
        elif isinstance(error, StoreUnavailable):
            code, http_status = error.code, 503
        else:  # pragma: no cover - lo que no se espera, cerrado
            code, http_status = "automation_expert_change_unavailable", 503
        logger.info("template_request route=%s request_id=%s status=%s code=%s", route, request_id,
                    http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    def result_failure(result: Mapping[str, Any], route: str, request_id: str | None) -> HTTPException | None:
        if result.get("ok") is True:
            return None
        code = result.get("reason_code")
        if isinstance(code, str) and code in _RESULT_STATUS:
            logger.info("template_request route=%s request_id=%s status=409 code=%s", route, request_id, code)
            return HTTPException(status_code=_RESULT_STATUS[code], detail=code)
        return failure(StoreUnavailable(), route, request_id)

    async def catalog(required: bool) -> TemplateCatalog | None:
        try:
            inbox = await chatwoot.get_inbox(inbox_id=context.inbox_id)
            return TemplateCatalog.from_inbox_payload({"message_templates": inbox["message_templates"]})
        except Exception:  # noqa: BLE001 - un Chatwoot caido: al validar no se sigue, al proponer si
            if required:
                raise HTTPException(status_code=503, detail="template_catalog_unavailable") from None
            logger.info("template_request route=propose catalog=unavailable")
            return None

    def findings(result: rules.TemplateResult) -> dict[str, Any]:
        return result.to_json()

    @app.post(f"{PREFIX}/template-requests", status_code=201)
    async def propose(request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"template", "purpose", "requested_by"},
                           allowed={"template", "purpose", "requested_by", "request_id", "revision", "for_flow"})
        requested_by = _requested_by(body)
        purpose = body["purpose"]
        if not isinstance(purpose, str) or not 1 <= len(purpose) <= 500:
            raise EntryRefused("invalid_body", {"field": "purpose", "problem": "length_1_to_500"})
        if looks_like_identifier(purpose):
            raise EntryRefused("identifier_not_allowed", {"field": "purpose", "problem": "phone_or_mail"})
        request_id, revision = body.get("request_id"), body.get("revision")
        if (request_id is None) != (revision is None) or (
                revision is not None and (type(revision) is not int or revision < 1)):
            raise EntryRefused("invalid_body", {"field": "revision", "problem": "request_id_and_revision_together"})
        if request_id is not None:
            request_id = _request_id(request_id) if isinstance(request_id, str) else _request_id("")
        proposal_id, step = _for_flow(body)
        try:
            draft = rules.parse_template(body["template"])
        except rules.TemplateRequestInvalid as error:
            raise EntryRefused(error.code, error.reason) from None
        busy()
        async with slots:
            found = await catalog(required=False)
            result = rules.check(draft, template_context,
                                 catalog_names=None if found is None else _catalog_names(found))
            if not result.ok:
                logger.info("template_request route=propose status=422 code=template_request_draft_blocked "
                            "blocks=%s", len(result.blocks))
                return JSONResponse(status_code=422, content={
                    "detail": "template_request_draft_blocked", **envelope(findings(result))})
            try:
                saved = await store.save_request(context.instance, request_id=request_id, revision=revision,
                                                 draft=draft.to_json(), purpose=purpose, proposal_id=proposal_id,
                                                 step_pointer=step, requested_by=requested_by)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "propose", request_id) from None
        refused = result_failure(saved, "propose", request_id)
        if refused is not None:
            raise refused
        logger.info("template_request route=propose request_id=%s status=201 warnings=%s",
                    saved.get("request_id"), len(result.warnings))
        return envelope({"request_id": saved.get("request_id"), "revision": saved.get("revision"),
                         "status": saved.get("status"), "variables": list(draft.variables),
                         "warnings": findings(result)["warnings"],
                         "burbuja": rules.bubble(draft, template_context)})

    @app.post(f"{PREFIX}/template-requests/{{request_id}}/validate")
    async def validate_request(request_id: str, request: Request,
                               authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _request_id(request_id)
        body = _check_keys(await _body(request), required={"revision", "requested_by"},
                           allowed={"revision", "requested_by"})
        if type(body["revision"]) is not int:
            raise EntryRefused("invalid_body", {"field": "revision", "problem": "not_an_integer"})
        requested_by = _requested_by(body)
        validator = _validator(os.environ)
        if validator is None:
            logger.info("template_request route=validate request_id=%s status=503 code=image_identity_missing",
                        checked)
            raise HTTPException(status_code=503, detail="automation_expert_change_unavailable")
        busy()
        async with slots:
            try:
                stored = await store.get_request(context.instance, checked)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "validate", checked) from None
            if stored.get("revision") != body["revision"]:
                raise HTTPException(status_code=409, detail="meta_template_revision_mismatch")
            if stored.get("status") != "borrador":
                raise HTTPException(status_code=409, detail="meta_template_not_draft")
            try:
                draft = rules.draft_from_stored(stored.get("draft"))
            except rules.TemplateRequestInvalid:
                raise failure(StoreUnavailable(), "validate", checked) from None
            found = await catalog(required=True)
            assert found is not None
            result = rules.check(draft, template_context, catalog_names=_catalog_names(found))
            proposal_id, step = stored.get("proposal_id"), stored.get("step_pointer")
            if isinstance(proposal_id, str) and isinstance(step, str):
                try:
                    proposal = await store.get_proposal(context.instance, proposal_id)
                except (StoreUnavailable, StoreRejected) as error:
                    raise failure(error, "validate", checked) from None
                target = rules.step_at(proposal.get("items"), step)
                item = re.match(r"^/items/[0-9]+/definicion", step)
                definition = rules.step_at(proposal.get("items"), item.group(0)) if item else None
                rules.check_for_step(draft, definition, target, result)
            meta = rules.meta_request(draft, template_context) if result.ok else None
            preview = {"burbuja": rules.bubble(draft, template_context), "catalog_sha256": found.catalog_sha256}
            try:
                recorded = await store.record_request_validation(
                    context.instance, request_id=checked, revision=body["revision"], validator=validator,
                    result=findings(result), meta_request=meta, preview=preview, requested_by=requested_by)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "validate", checked) from None
        refused = result_failure(recorded, "validate", checked)
        if refused is not None:
            raise refused
        logger.info("template_request route=validate request_id=%s status=200 blocks=%s warnings=%s",
                    checked, len(result.blocks), len(result.warnings))
        return envelope({"request_id": checked, "status": recorded.get("status"),
                         "fingerprint": recorded.get("fingerprint"), "expires_at": recorded.get("expires_at"),
                         **findings(result), "preview": preview})

    @app.get(f"{PREFIX}/template-requests")
    async def list_template_requests(estado: str = "abiertas", limit: str = str(LIST_DEFAULT_LIMIT),
                                     authorization: str | None = Header(default=None)) -> Any:
        """Las solicitudes de la instancia y en que estado estan. Solo lee."""
        authenticate(authorization)
        if estado not in LIST_STATES:
            raise EntryRefused("invalid_query", {"field": "estado", "problem": "abiertas_or_todas"})
        if not (limit.isascii() and limit.isdigit()) or not 1 <= int(limit) <= LIST_MAX_LIMIT:
            raise EntryRefused("invalid_query", {"field": "limit", "problem": "between_1_and_50"})
        busy()
        async with slots:
            try:
                listed = await store.list_requests(context.instance, only_open=LIST_STATES[estado],
                                                   limit=int(limit))
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "list") from None
        rows = listed.get("requests")
        if not isinstance(rows, list):
            raise failure(StoreUnavailable(), "list")
        logger.info("template_request route=list status=200 estado=%s requests=%s", estado, len(rows))
        return envelope({"estado": estado, "requests": rows, "open": listed.get("open"),
                         "open_limit": listed.get("open_limit"), "database_at": listed.get("database_at")})

    @app.get(f"{PREFIX}/template-requests/{{request_id}}")
    async def read_template_request(request_id: str, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _request_id(request_id)
        busy()
        async with slots:
            try:
                stored = await store.get_request(context.instance, checked)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "read", checked) from None
        logger.info("template_request route=read request_id=%s status=200", checked)
        keys = ("request_id", "status", "effective_status", "revision", "template_name", "language",
                "category_requested", "draft", "purpose", "proposal_id", "step_pointer", "fingerprint",
                "validated_at", "expires_at", "approved_at", "approved_by_kind", "sent_at", "meta_status",
                "meta_category", "recategorized", "meta_rejected_reason", "in_catalog_at", "closed_at",
                "created_at", "events")
        data = {key: stored.get(key) for key in keys}
        validation = stored.get("validation")
        data["preview"] = validation.get("preview") if isinstance(validation, Mapping) else None
        data["result"] = validation.get("result") if isinstance(validation, Mapping) else None
        return envelope(data)
