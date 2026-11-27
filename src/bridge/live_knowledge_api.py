"""Las rutas del Automation Expert para el conocimiento vivo (live-knowledge-v1, pieza 5.5).

El agente lee lo vigente, arma un cambio por secciones con el duenio, lo mira en la
vista previa y lo guarda (validado y congelado 24 horas). Nunca lo publica ni vuelve al
archivo: eso lo hace una persona de la lista de aprobadores en la pagina
(``knowledge_approval``).

* ``GET  .../knowledge``: lo vigente, con las secciones editables.
* ``POST .../knowledge/preview``: valida y devuelve el diff **sin guardar**.
* ``POST .../knowledge``: valida y guarda.
* ``GET  .../knowledge/versions`` y ``GET .../knowledge/versions/{id}``.

Detras de ``AGENT_LIVE_KNOWLEDGE_ENABLED`` (apagado por defecto: las rutas no existen).
Usa la clave del plugin y el JWT del rol ``automation_expert_change``. El log lleva la
ruta, el id y el codigo; nunca el texto del conocimiento.
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
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from . import live_knowledge as rules
from .commercial_knowledge import MAX_RENDERED_BYTES
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

__all__ = ["CONTRACT_VERSION", "KnowledgeStore", "check_settings", "register_live_knowledge_routes"]

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "1.0.0"
KNOWLEDGE = f"{PREFIX}/knowledge"
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 50
LIST_STATES = {"abiertas": True, "todas": False}
_PRODUCT_VERSION = re.compile(r"^v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_RESULT_STATUS = {
    "agent_knowledge_open_limit": 409,
    "agent_knowledge_base_changed": 409,
}


class KnowledgeStore(FlowChangeStore):
    """Las tres funciones del agente de 20261126600000, con la clave de su rol."""

    async def save_version(self, instance: Mapping[str, str], *, base: rules.ServedKnowledge,
                           change: Mapping[str, Any], knowledge: Mapping[str, Any], rendered_sha256: str,
                           rendered_bytes: int, preview: Mapping[str, Any], validator: Mapping[str, Any],
                           purpose: str, requested_by: str) -> dict[str, Any]:
        live = base.origin == rules.ORIGIN_LIVE
        return await self._rpc("save_agent_knowledge_version_v1", {
            "p_instance": instance, "p_file_version": base.file_version, "p_file_sha256": base.file_sha256,
            "p_base_origin": base.origin, "p_base_knowledge_id": base.knowledge_id if live else None,
            "p_base_sha256": base.sha256, "p_change": change, "p_knowledge": knowledge,
            "p_rendered_sha256": rendered_sha256, "p_rendered_bytes": rendered_bytes, "p_preview": preview,
            "p_validator": validator, "p_purpose": purpose, "p_requested_by_ref": requested_by})

    async def get_version(self, instance: Mapping[str, str], knowledge_id: str) -> dict[str, Any]:
        return await self._rpc("get_agent_knowledge_version_v1", {"p_instance": instance,
                                                                   "p_knowledge_id": knowledge_id})

    async def list_versions(self, instance: Mapping[str, str], *, only_open: bool, limit: int) -> dict[str, Any]:
        return await self._rpc("list_agent_knowledge_versions_v1", {"p_instance": instance,
                                                                     "p_only_open": only_open, "p_limit": limit})


def check_settings(settings: Any) -> None:
    """Lo que exige el flag al arrancar: el conocimiento del manifiesto, el AE de cambios y la pagina."""
    if not getattr(settings, "agent_live_knowledge_enabled", False):
        return
    flag = rules.FLAG_ENV
    if getattr(settings, "instance_manifest", None) is None:
        raise ValueError(f"{flag} requires an instance manifest")
    if getattr(settings, "commercial_knowledge", None) is None:
        raise ValueError(f"{flag} requires COMMERCIAL_KNOWLEDGE_ENABLED")
    if getattr(settings, "commercial_knowledge_path", None) is None:
        raise ValueError(f"{flag} requires the knowledge file of the manifest")
    if not getattr(settings, "automation_expert_change_enabled", False):
        raise ValueError(f"{flag} requires AUTOMATION_EXPERT_CHANGE_ENABLED")
    if not getattr(settings, "flow_approval_enabled", False):
        raise ValueError(f"{flag} requires FLOW_APPROVAL_ENABLED")
    if not getattr(settings, "hermes_shadow_enabled", False):
        raise ValueError(f"{flag} requires HERMES_SHADOW_ENABLED")


def validator_identity(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """Con que corrio la validacion: la imagen (SETTER_VERSION y GIT_SHA), nunca lo que dice quien llama."""
    version = environ.get("SETTER_VERSION", "").strip()
    sha = environ.get("GIT_SHA", "").strip().lower()
    if not _PRODUCT_VERSION.fullmatch(version) or not _GIT_SHA.fullmatch(sha):
        return None
    return {"contract": rules.CONTRACT, "rules_version": rules.RULES_VERSION, "product_version": version,
            "git_sha": sha}


def _knowledge_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid_knowledge_id") from None


def preview_payload(evaluation: rules.Evaluation, *, base: rules.ServedKnowledge,
                    change: Mapping[str, Any]) -> dict[str, Any]:
    """Lo que ve el AE y lo que guarda la fila para la pagina: el diff contra lo vigente, sin el texto entero."""
    rendered = evaluation.rendered
    return {"secciones": rules.changed_sections(change),
            "base": base.ref(),
            "rendered_bytes": len(rendered.encode("utf-8")) if rendered is not None else None,
            "max_bytes": MAX_RENDERED_BYTES,
            "diff": rules.render_diff(base.rendered, rendered) if rendered is not None else None,
            "blocks": evaluation.blocks, "warnings": evaluation.warnings}


def register_live_knowledge_routes(app: FastAPI, *, token: str, store: KnowledgeStore,
                                   source: rules.LiveKnowledgeSource, approval_origin: str,
                                   environ: Mapping[str, str] | None = None) -> None:
    """Las rutas, detras del flag (lo mira quien llama: con el flag apagado no existen)."""
    manifest = source.context.manifest
    instance = {"tenant_ref": manifest.tenant_ref, "ally_ref": manifest.ally_ref}
    image_environ: Mapping[str, str] = environ if environ is not None else os.environ
    slots = asyncio.Semaphore(2)

    if EntryRefused not in app.exception_handlers:
        # El de las rutas de cambio lo registra flow_change_api (este flag lo exige); solo para
        # montarlas sueltas en una prueba.
        @app.exception_handler(EntryRefused)
        async def entry_refused(request: Request, error: EntryRefused) -> JSONResponse:
            logger.info("live_knowledge status=422 code=%s", error.code)
            return JSONResponse(status_code=422, content={"detail": error.code, "reason": error.reason})

    def authenticate(authorization: str | None) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="automation_expert_change_authentication_required",
                                headers={"WWW-Authenticate": "Bearer"})

    def envelope(data: Mapping[str, Any]) -> dict[str, Any]:
        return {"contract": rules.CONTRACT, "contract_version": CONTRACT_VERSION, "instance": dict(instance),
                "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"), "data": dict(data)}

    def failure(error: Exception, route: str, knowledge_id: str | None = None) -> HTTPException:
        if isinstance(error, StoreRejected):
            code, http_status = error.code, error.http_status
        elif isinstance(error, StoreUnavailable):
            code, http_status = error.code, 503
        else:  # pragma: no cover - lo que no se espera, cerrado
            code, http_status = "automation_expert_change_unavailable", 503
        logger.info("live_knowledge route=%s knowledge=%s status=%s code=%s", route, knowledge_id, http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    def approval_url(knowledge_id: str) -> str:
        """El link de la pagina donde un aprobador la publica. Solo lleva el id, que no da acceso."""
        return f"{approval_origin.rstrip('/')}/flow-approval/k/{knowledge_id}"

    def busy() -> None:
        if slots.locked():
            raise HTTPException(status_code=503, detail="automation_expert_change_busy")

    def parse(body: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return rules.parse_change({"cambios": body.get("cambios")})
        except rules.KnowledgeChangeInvalid as error:
            raise EntryRefused(error.code, error.reason) from None

    @app.get(KNOWLEDGE)
    async def current(authorization: str | None = Header(default=None)) -> Any:
        """Lo vigente: sobre esto se arma el cambio (la viva publicada, o el archivo)."""
        authenticate(authorization)
        base = source.edit_base()
        served = source.current()
        logger.info("live_knowledge route=current status=200 origin=%s", base.origin)
        return envelope({"vigente": base.ref(), "servido": served.ref(), "estado": source.status_line(),
                         "secciones": dict(base.sections), "editables": list(rules.SECTIONS),
                         "por_producto": base.knowledge.by_product,
                         "rendered_bytes": len(base.rendered.encode("utf-8")), "max_bytes": MAX_RENDERED_BYTES})

    @app.post(f"{KNOWLEDGE}/preview")
    async def preview(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """Valida y muestra el diff sin guardar. Con bloqueos contesta 200 y los cuenta: es para corregir."""
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"cambios"}, allowed={"cambios"})
        change = parse(body)
        base = source.edit_base()
        evaluation = rules.evaluate(change, base=base, context=source.context)
        logger.info("live_knowledge route=preview status=200 blocks=%s", len(evaluation.blocks))
        return envelope({"ok": not evaluation.blocks, **preview_payload(evaluation, base=base, change=change)})

    @app.post(KNOWLEDGE, status_code=201)
    async def propose(request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"cambios", "purpose", "requested_by"},
                           allowed={"cambios", "purpose", "requested_by"})
        requested_by = _requested_by(body)
        purpose = body["purpose"]
        if not isinstance(purpose, str) or not 1 <= len(purpose) <= 500:
            raise EntryRefused("invalid_body", {"field": "purpose", "problem": "length_1_to_500"})
        if looks_like_identifier(purpose):
            raise EntryRefused("identifier_not_allowed", {"field": "purpose", "problem": "phone_or_mail"})
        change = parse(body)
        base = source.edit_base()
        evaluation = rules.evaluate(change, base=base, context=source.context)
        payload = preview_payload(evaluation, base=base, change=change)
        if evaluation.blocks:
            logger.info("live_knowledge route=propose status=422 code=knowledge_change_blocked blocks=%s",
                        len(evaluation.blocks))
            return JSONResponse(status_code=422, content={"detail": "knowledge_change_blocked",
                                                          **envelope({"blocks": evaluation.blocks,
                                                                      "warnings": evaluation.warnings})})
        validator = validator_identity(image_environ)
        if validator is None:
            logger.info("live_knowledge route=propose status=503 code=image_identity_missing")
            raise HTTPException(status_code=503, detail="automation_expert_change_unavailable")
        assert evaluation.knowledge is not None and evaluation.sections is not None
        busy()
        async with slots:
            try:
                saved = await store.save_version(
                    instance, base=base, change=change, knowledge=evaluation.sections,
                    rendered_sha256=evaluation.knowledge.rendered_sha256,
                    rendered_bytes=int(payload["rendered_bytes"]), preview=payload, validator=validator,
                    purpose=purpose.strip(), requested_by=requested_by)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "propose") from None
        if saved.get("ok") is not True:
            code = saved.get("reason_code")
            if isinstance(code, str) and code in _RESULT_STATUS:
                logger.info("live_knowledge route=propose status=409 code=%s", code)
                raise HTTPException(status_code=_RESULT_STATUS[code], detail=code)
            raise failure(StoreUnavailable(), "propose")
        knowledge_id = str(saved.get("knowledge_id"))
        logger.info("live_knowledge route=propose knowledge=%s status=201 live_version=%s", knowledge_id,
                    saved.get("live_version"))
        return envelope({"knowledge_id": knowledge_id, "live_version": saved.get("live_version"),
                         "status": saved.get("status"), "expires_at": saved.get("expires_at"),
                         "approval_url": approval_url(knowledge_id), **payload})

    @app.get(f"{KNOWLEDGE}/versions")
    async def list_versions(estado: str = "abiertas", limit: str = str(LIST_DEFAULT_LIMIT),
                            authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        if estado not in LIST_STATES:
            raise EntryRefused("invalid_query", {"field": "estado", "problem": "abiertas_or_todas"})
        if not (limit.isascii() and limit.isdigit()) or not 1 <= int(limit) <= LIST_MAX_LIMIT:
            raise EntryRefused("invalid_query", {"field": "limit", "problem": "between_1_and_50"})
        busy()
        async with slots:
            try:
                listed = await store.list_versions(instance, only_open=LIST_STATES[estado], limit=int(limit))
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "list") from None
        rows = listed.get("versions")
        if not isinstance(rows, list):
            raise failure(StoreUnavailable(), "list")
        logger.info("live_knowledge route=list status=200 estado=%s versions=%s", estado, len(rows))
        return envelope({"estado": estado, "versions": rows, "servido": source.current().ref(),
                         "database_at": listed.get("database_at")})

    @app.get(f"{KNOWLEDGE}/versions/{{knowledge_id}}")
    async def read_version(knowledge_id: str, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _knowledge_id(knowledge_id)
        busy()
        async with slots:
            try:
                stored = await store.get_version(instance, checked)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "read", checked) from None
        logger.info("live_knowledge route=read knowledge=%s status=200", checked)
        keys = ("knowledge_id", "status", "effective_status", "live_version", "file_version", "base_origin",
                "rendered_bytes", "purpose", "validated_at", "expires_at", "approved_at", "closed_at", "created_at",
                "events", "preview")
        data = {key: stored.get(key) for key in keys}
        data["serving"] = source.serving_state(checked)
        data["approval_url"] = approval_url(checked) if stored.get("status") in ("validada", "publicada") else None
        return envelope(data)
