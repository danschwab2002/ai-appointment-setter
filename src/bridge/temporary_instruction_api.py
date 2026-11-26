"""Las rutas del Automation Expert para la instruccion temporal (agent-temporary-instruction-v1).

El agente la redacta con el duenio, la mira en la vista previa y la propone; nunca
la publica ni la retira (eso lo hace una persona de la lista de aprobadores en la
pagina, ``temporary_instruction_page``).

* ``POST .../temporary-instructions/preview``: valida y renderiza **sin guardar**.
* ``POST .../temporary-instructions``: propone; sin bloqueos, la base la congela 24 h.
* ``GET  .../temporary-instructions`` y ``GET .../temporary-instructions/{id}``: estado.

Detras de ``AGENT_TEMPORARY_INSTRUCTION_ENABLED`` (apagado por defecto: las rutas
no existen). Usa la clave del plugin y el JWT del rol ``automation_expert_change``,
como las campanas. El log lleva la ruta, el id y el codigo; nunca el texto.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from . import temporary_instruction as rules
from .agent_campaign_api import _GIT_SHA, _PRODUCT_VERSION
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

__all__ = ["CONTRACT_VERSION", "TemporaryInstructionStore", "check_settings",
           "register_temporary_instruction_routes"]

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "1.0.0"
ROUTES = f"{PREFIX}/temporary-instructions"
LIST_DEFAULT_LIMIT = 10
LIST_MAX_LIMIT = 50
_RESULT_STATUS = {
    "agent_temporary_instruction_window_over": 409,
    "agent_temporary_instruction_window_too_long": 409,
    "agent_temporary_instruction_daily_limit": 409,
}


class TemporaryInstructionStore(FlowChangeStore):
    """Las tres funciones del agente de 20261126200000, con la clave de su rol."""

    async def propose(self, instance: Mapping[str, str], *, text: str, valid_until: str, purpose: str,
                      validator: Mapping[str, Any], requested_by: str) -> dict[str, Any]:
        return await self._rpc("propose_agent_temporary_instruction_v1", {
            "p_instance": instance, "p_text": text, "p_valid_until": valid_until, "p_purpose": purpose,
            "p_validator": validator, "p_requested_by_ref": requested_by})

    async def get_instruction(self, instance: Mapping[str, str], instruction_id: str) -> dict[str, Any]:
        return await self._rpc("get_agent_temporary_instruction_v1", {"p_instance": instance,
                                                                       "p_instruction_id": instruction_id})

    async def list_instructions(self, instance: Mapping[str, str], *, limit: int) -> dict[str, Any]:
        return await self._rpc("list_agent_temporary_instructions_v1", {"p_instance": instance, "p_limit": limit})


def check_settings(settings: Any) -> None:
    """Lo que exige el flag al arrancar: el plugin del AE, la pagina y el manifiesto."""
    if not getattr(settings, "agent_temporary_instruction_enabled", False):
        return
    flag = rules.FLAG_ENV
    if not getattr(settings, "automation_expert_change_enabled", False):
        raise ValueError(f"{flag} requires AUTOMATION_EXPERT_CHANGE_ENABLED")
    if not getattr(settings, "flow_approval_enabled", False):
        raise ValueError(f"{flag} requires FLOW_APPROVAL_ENABLED")
    if getattr(settings, "instance_manifest", None) is None:
        raise ValueError(f"{flag} requires an instance manifest")


def _validator(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """Con que corrio la validacion: la imagen (SETTER_VERSION y GIT_SHA), nunca lo que dice quien llama."""
    version = environ.get("SETTER_VERSION", "").strip()
    sha = environ.get("GIT_SHA", "").strip().lower()
    if not _PRODUCT_VERSION.fullmatch(version) or not _GIT_SHA.fullmatch(sha):
        return None
    return {"contract": rules.CONTRACT, "rules_version": rules.RULES_VERSION, "product_version": version,
            "git_sha": sha}


def _instruction_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid_instruction_id") from None


def register_temporary_instruction_routes(app: FastAPI, *, token: str, store: TemporaryInstructionStore,
                                          instance: Mapping[str, str], time_zone: str | None,
                                          approval_origin: str, git_sha: str | None = None) -> None:
    """Las rutas, detras del flag (lo mira quien llama: con el flag apagado no existen)."""
    refs = {"tenant_ref": instance["tenant_ref"], "ally_ref": instance["ally_ref"]}
    bridge_sha = git_sha or os.getenv("GIT_SHA", "") or "unknown"
    slots = asyncio.Semaphore(2)

    def authenticate(authorization: str | None) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="automation_expert_change_authentication_required",
                                headers={"WWW-Authenticate": "Bearer"})

    def envelope(data: Mapping[str, Any]) -> dict[str, Any]:
        return {"contract": rules.CONTRACT, "contract_version": CONTRACT_VERSION, "instance": dict(refs),
                "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "bridge_git_sha": bridge_sha, "data": dict(data)}

    def failure(error: Exception, route: str, instruction_id: str | None = None) -> HTTPException:
        if isinstance(error, StoreRejected):
            code, http_status = error.code, error.http_status
        elif isinstance(error, StoreUnavailable):
            code, http_status = error.code, 503
        else:  # pragma: no cover - lo que no se espera, cerrado
            code, http_status = "automation_expert_change_unavailable", 503
        logger.info("agent_temporary_instruction route=%s id=%s status=%s code=%s", route, instruction_id,
                    http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    def approval_url(instruction_id: str) -> str:
        """El link de la pagina donde un aprobador la publica o la retira. Solo lleva el id."""
        return f"{approval_origin.rstrip('/')}/flow-approval/i/{instruction_id}"

    def view(draft: rules.InstructionDraft) -> dict[str, Any]:
        return {"render": rules.render(draft.text, draft.valid_until, time_zone=time_zone),
                "hasta": draft.valid_until_iso, "hasta_local": rules.when_text(draft.valid_until, time_zone),
                "caracteres": len(draft.text), "maximo": rules.MAX_CHARS}

    def parsed(raw: object) -> rules.InstructionDraft:
        try:
            return rules.parse_instruction(raw)
        except rules.InstructionInvalid as error:
            raise EntryRefused(error.code, error.reason) from None

    def busy() -> None:
        if slots.locked():
            raise HTTPException(status_code=503, detail="automation_expert_change_busy")

    @app.post(f"{ROUTES}/preview")
    async def preview(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """Valida y renderiza sin guardar. Con bloqueos contesta 200 y los cuenta: es para corregir."""
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"instruction"}, allowed={"instruction"})
        draft = parsed(body["instruction"])
        blocks = rules.instruction_blocks(draft, now=datetime.now(UTC))
        logger.info("agent_temporary_instruction route=preview status=200 blocks=%s", len(blocks))
        return envelope({"ok": not blocks, "blocks": blocks, "preview": view(draft)})

    @app.post(ROUTES, status_code=201)
    async def propose(request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"instruction", "purpose", "requested_by"},
                           allowed={"instruction", "purpose", "requested_by"})
        requested_by = _requested_by(body)
        purpose = body["purpose"]
        if not isinstance(purpose, str) or not 1 <= len(purpose.strip()) <= 500 or "\n" in purpose:
            raise EntryRefused("invalid_body", {"field": "purpose", "problem": "one_line_1_to_500"})
        if looks_like_identifier(purpose):
            raise EntryRefused("identifier_not_allowed", {"field": "purpose", "problem": "phone_or_mail"})
        draft = parsed(body["instruction"])
        blocks = rules.instruction_blocks(draft, now=datetime.now(UTC))
        if blocks:
            logger.info("agent_temporary_instruction route=propose status=422 code=temporary_instruction_blocked "
                        "blocks=%s", len(blocks))
            return JSONResponse(status_code=422, content={"detail": "temporary_instruction_blocked",
                                                          **envelope({"blocks": blocks, "warnings": []})})
        validator = _validator(os.environ)
        if validator is None:
            logger.info("agent_temporary_instruction route=propose status=503 code=image_identity_missing")
            raise HTTPException(status_code=503, detail="automation_expert_change_unavailable")
        busy()
        async with slots:
            try:
                saved = await store.propose(refs, text=draft.text, valid_until=draft.valid_until_iso,
                                            purpose=purpose.strip(), validator=validator, requested_by=requested_by)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "propose") from None
        if saved.get("ok") is not True:
            code = saved.get("reason_code")
            if isinstance(code, str) and code in _RESULT_STATUS:
                logger.info("agent_temporary_instruction route=propose status=409 code=%s", code)
                raise HTTPException(status_code=_RESULT_STATUS[code], detail=code)
            raise failure(StoreUnavailable(), "propose")
        instruction_id = saved.get("instruction_id")
        if not isinstance(instruction_id, str):
            raise failure(StoreUnavailable(), "propose")
        logger.info("agent_temporary_instruction route=propose id=%s version=%s status=201", instruction_id,
                    saved.get("version"))
        return envelope({"instruction_id": instruction_id, "version": saved.get("version"),
                         "status": saved.get("status"), "expires_at": saved.get("expires_at"),
                         "preview": view(draft), "approval_url": approval_url(instruction_id)})

    @app.get(ROUTES)
    async def list_instructions(limit: str = str(LIST_DEFAULT_LIMIT),
                                authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        if not (limit.isascii() and limit.isdigit()) or not 1 <= int(limit) <= LIST_MAX_LIMIT:
            raise EntryRefused("invalid_query", {"field": "limit", "problem": "between_1_and_50"})
        busy()
        async with slots:
            try:
                listed = await store.list_instructions(refs, limit=int(limit))
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "list") from None
        rows = listed.get("instructions")
        if not isinstance(rows, list):
            raise failure(StoreUnavailable(), "list")
        live = listed.get("live_instruction_id")
        out = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            item = dict(row)
            if isinstance(item.get("instruction_id"), str) and item.get("effective_status") in ("validada",
                                                                                                 "publicada"):
                item["approval_url"] = approval_url(item["instruction_id"])
            out.append(item)
        logger.info("agent_temporary_instruction route=list status=200 instructions=%s", len(out))
        return envelope({"instructions": out, "live_instruction_id": live if isinstance(live, str) else None,
                         "database_at": listed.get("database_at")})

    @app.get(f"{ROUTES}/{{instruction_id}}")
    async def read_instruction(instruction_id: str, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _instruction_id(instruction_id)
        busy()
        async with slots:
            try:
                stored = await store.get_instruction(refs, checked)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "read", checked) from None
        logger.info("agent_temporary_instruction route=read id=%s status=200", checked)
        keys = ("instruction_id", "status", "effective_status", "version", "texto", "purpose", "valid_until",
                "expires_at", "approved_at", "closed_at", "closed_by_kind", "close_reason_code", "created_at",
                "events")
        data = {key: stored.get(key) for key in keys}
        data["approval_url"] = approval_url(checked) if stored.get("effective_status") in (
            "validada", "publicada") else None
        return envelope(data)
