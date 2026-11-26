"""Las rutas del Automation Expert para las reglas de palabra clave (``keyword-rules-v1``, Z1 3.9).

El agente arma la regla con quien se la pide, la mira en la vista previa y la
guarda; nunca la publica ni la pausa (eso lo hace una persona de la lista de
aprobadores en la pagina, ``keyword_rule_approval``).

* ``POST .../keyword-rules/preview``: valida **sin guardar** y devuelve el link ``wa.me``.
* ``POST .../keyword-rules``: crea o rehace un borrador. Con bloqueos, 422.
* ``GET  .../keyword-rules`` y ``GET .../keyword-rules/{id}``: en que estado estan.

Detras de ``KEYWORD_RULES_ENABLED`` (apagado por defecto: las rutas no existen).
Usa la clave del plugin y el JWT del rol ``automation_expert_change``, como las
campanas. El log lleva la ruta, el id y el codigo; nunca el texto.
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

from . import keyword_rules as rules
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

__all__ = ["CONTRACT_VERSION", "KeywordRuleStore", "register_keyword_rule_routes"]

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "1.0.0"
ROUTES = f"{PREFIX}/keyword-rules"
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 50
LIST_STATES = {"abiertas": True, "todas": False}
_RESULT_STATUS = {
    "keyword_rule_open_limit": 409,
    "keyword_rule_key_taken": 409,
    "keyword_rule_revision_limit": 409,
    "keyword_rule_revision_mismatch": 409,
    "keyword_rule_not_draft": 409,
    "keyword_rule_key_is_immutable": 409,
}


class KeywordRuleStore(FlowChangeStore):
    """Las tres funciones del agente de 20261126400000, con la clave de su rol."""

    async def save_rule(self, instance: Mapping[str, str], *, rule_id: str | None, revision: int | None,
                        rule: Mapping[str, Any], purpose: str, requested_by: str) -> dict[str, Any]:
        return await self._rpc("save_keyword_rule_draft_v1", {
            "p_instance": instance, "p_rule_id": rule_id, "p_expected_revision": revision, "p_rule": rule,
            "p_purpose": purpose, "p_requested_by_ref": requested_by})

    async def get_rule(self, instance: Mapping[str, str], rule_id: str) -> dict[str, Any]:
        return await self._rpc("get_keyword_rule_v1", {"p_instance": instance, "p_rule_id": rule_id})

    async def list_rules(self, instance: Mapping[str, str], *, only_open: bool, limit: int) -> dict[str, Any]:
        return await self._rpc("list_keyword_rules_v1", {"p_instance": instance, "p_only_open": only_open,
                                                          "p_limit": limit})


def _rule_id(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid_keyword_rule_id") from None


def approval_url(origin: str, rule_id: str) -> str:
    """El link de la pagina donde un aprobador la publica. Solo lleva el id, que no da acceso."""
    return f"{origin.rstrip('/')}/flow-approval/w/{rule_id}"


async def instance_blocks(rule: Mapping[str, Any], *, live: rules.KeywordRules, chatwoot: Any,
                          rule_id: str | None = None) -> list[dict[str, Any]]:
    """Todo lo que frena: los choques, lo reservado y que el macro solo sume la etiqueta."""
    blocks = rules.rule_blocks(rule, context=live.context, others=live.live_keywords(), rule_id=rule_id)
    try:
        actions = await chatwoot.get_macro_actions(macro_id=rule["macro_etiqueta"])
    except Exception:  # noqa: BLE001 - sin poder leerlo no se guarda ni se publica
        blocks.append({"code": "keyword_rule_macro_unreadable", "field": "macro_etiqueta"})
    else:
        problem = rules.macro_block(actions, rule["etiqueta"])
        if problem is not None:
            blocks.append(problem)
    return blocks


def register_keyword_rule_routes(app: FastAPI, *, token: str, store: KeywordRuleStore, chatwoot: Any,
                                 live: rules.KeywordRules, tenant_ref: str, ally_ref: str, inbox_id: int,
                                 approval_origin: str, git_sha: str | None = None) -> None:
    """Las rutas, detras del flag (lo mira quien llama: con el flag apagado no existen)."""
    refs = {"tenant_ref": tenant_ref, "ally_ref": ally_ref}
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

    def failure(error: Exception, route: str, rule_id: str | None = None) -> HTTPException:
        if isinstance(error, StoreRejected):
            code, http_status = error.code, error.http_status
        elif isinstance(error, StoreUnavailable):
            code, http_status = error.code, 503
        else:  # pragma: no cover - lo que no se espera, cerrado
            code, http_status = "automation_expert_change_unavailable", 503
        logger.info("keyword_rule route=%s rule=%s status=%s code=%s", route, rule_id, http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    def busy() -> None:
        if slots.locked():
            raise HTTPException(status_code=503, detail="automation_expert_change_busy")

    async def wa_me(rule: Mapping[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
        try:
            inbox = await chatwoot.get_inbox(inbox_id=inbox_id)
        except Exception:  # noqa: BLE001 - el link es un dato de mas: la regla no depende de el
            return None, [{"code": "keyword_rule_whatsapp_link_unavailable"}]
        link = rules.wa_me_link(inbox.get("phone_number") if isinstance(inbox, Mapping) else None,
                                rule["palabra_clave"])
        return link, ([] if link else [{"code": "keyword_rule_whatsapp_link_unavailable"}])

    def parsed(raw: object) -> dict[str, Any]:
        try:
            return rules.parse_rule(raw)
        except rules.KeywordRuleInvalid as error:
            raise EntryRefused(error.code, error.reason) from None

    def view(rule: Mapping[str, Any], link: str | None) -> dict[str, Any]:
        return {"nombre": rule["nombre"], "palabra_clave": rule["palabra_clave"],
                "palabra_normalizada": rule["palabra_normalizada"], "etiqueta": rule["etiqueta"], "wa_me": link}

    @app.post(f"{ROUTES}/preview")
    async def preview(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """Valida sin guardar. Con bloqueos contesta 200 y los cuenta: es para corregir."""
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"rule"}, allowed={"rule"})
        rule = parsed(body["rule"])
        blocks = await instance_blocks(rule, live=live, chatwoot=chatwoot)
        link, warnings = await wa_me(rule)
        logger.info("keyword_rule route=preview status=200 blocks=%s", len(blocks))
        return envelope({"ok": not blocks, "blocks": blocks, "warnings": warnings, "preview": view(rule, link)})

    @app.post(ROUTES, status_code=201)
    async def propose(request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"rule", "purpose", "requested_by"},
                           allowed={"rule", "purpose", "requested_by", "rule_id", "revision"})
        requested_by = _requested_by(body)
        purpose = body["purpose"]
        if not isinstance(purpose, str) or not 1 <= len(purpose) <= 500:
            raise EntryRefused("invalid_body", {"field": "purpose", "problem": "length_1_to_500"})
        if looks_like_identifier(purpose):
            raise EntryRefused("identifier_not_allowed", {"field": "purpose", "problem": "phone_or_mail"})
        rule_id, revision = body.get("rule_id"), body.get("revision")
        if (rule_id is None) != (revision is None) or (
                revision is not None and (type(revision) is not int or revision < 1)):
            raise EntryRefused("invalid_body", {"field": "revision", "problem": "rule_id_and_revision_together"})
        if rule_id is not None:
            rule_id = _rule_id(rule_id)
        rule = parsed(body["rule"])
        blocks = await instance_blocks(rule, live=live, chatwoot=chatwoot, rule_id=rule_id)
        if blocks:
            logger.info("keyword_rule route=propose status=422 code=keyword_rule_draft_blocked blocks=%s",
                        len(blocks))
            return JSONResponse(status_code=422, content={"detail": "keyword_rule_draft_blocked",
                                                          **envelope({"blocks": blocks, "warnings": []})})
        busy()
        async with slots:
            try:
                saved = await store.save_rule(refs, rule_id=rule_id, revision=revision, rule=rule,
                                              purpose=purpose, requested_by=requested_by)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "propose", rule_id) from None
        if saved.get("ok") is not True:
            code = saved.get("reason_code")
            if isinstance(code, str) and code in _RESULT_STATUS:
                logger.info("keyword_rule route=propose rule=%s status=409 code=%s", rule_id, code)
                raise HTTPException(status_code=_RESULT_STATUS[code], detail=code)
            raise failure(StoreUnavailable(), "propose", rule_id)
        saved_id = str(saved.get("rule_id"))
        link, warnings = await wa_me(rule)
        logger.info("keyword_rule route=propose rule=%s status=201", saved_id)
        return envelope({"rule_id": saved_id, "revision": saved.get("revision"), "status": saved.get("status"),
                         "warnings": warnings, "preview": view(rule, link),
                         "approval_url": approval_url(approval_origin, saved_id)})

    @app.get(ROUTES)
    async def list_rules(estado: str = "abiertas", limit: str = str(LIST_DEFAULT_LIMIT),
                         authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        if estado not in LIST_STATES:
            raise EntryRefused("invalid_query", {"field": "estado", "problem": "abiertas_or_todas"})
        if not (limit.isascii() and limit.isdigit()) or not 1 <= int(limit) <= LIST_MAX_LIMIT:
            raise EntryRefused("invalid_query", {"field": "limit", "problem": "between_1_and_50"})
        busy()
        async with slots:
            try:
                listed = await store.list_rules(refs, only_open=LIST_STATES[estado], limit=int(limit))
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "list") from None
        rows = listed.get("rules")
        if not isinstance(rows, list):
            raise failure(StoreUnavailable(), "list")
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("rule_id"), str):
                row["serving"] = live.serving_state(row["rule_id"]) if row.get("status") == "publicada" else None
        logger.info("keyword_rule route=list status=200 estado=%s rules=%s", estado, len(rows))
        return envelope({"estado": estado, "rules": rows, "database_at": listed.get("database_at")})

    @app.get(f"{ROUTES}/{{rule_id}}")
    async def read_rule(rule_id: str, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _rule_id(rule_id)
        busy()
        async with slots:
            try:
                stored = await store.get_rule(refs, checked)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "read", checked) from None
        logger.info("keyword_rule route=read rule=%s status=200", checked)
        keys = ("rule_id", "status", "revision", "rule_key", "keyword", "label", "rule", "purpose", "approved_at",
                "paused_at", "closed_at", "created_at", "events")
        data = {key: stored.get(key) for key in keys}
        data["serving"] = live.serving_state(checked) if stored.get("status") == "publicada" else None
        data["approval_url"] = approval_url(approval_origin, checked) if stored.get("status") != "descartada" else None
        return envelope(data)
