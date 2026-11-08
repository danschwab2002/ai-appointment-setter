"""Las rutas del Automation Expert para las campanas (agent-campaigns-v1, E-355).

El agente arma la campana con el duenio, la mira en la vista previa, la guarda y
la valida; nunca la publica ni la pausa (eso lo hace una persona de la lista de
aprobadores en la pagina, ``campaign_approval``).

* ``POST .../campaigns/preview``: valida y renderiza **sin guardar**.
* ``POST .../campaigns``: crea o rehace un borrador.
* ``POST .../campaigns/{id}/validate``: revalida y, sin bloqueos, la base la congela.
* ``GET  .../campaigns`` y ``GET .../campaigns/{id}``: en que estado estan.

Detras de ``AGENT_CAMPAIGNS_ENABLED`` (apagado por defecto: las rutas no existen).
Usa la clave del plugin y el JWT del rol ``automation_expert_change``, como las
de cambio de flujos. El log lleva la ruta, el id y el codigo; nunca el texto.
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

from . import agent_campaigns as rules
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

__all__ = ["CONTRACT_VERSION", "CampaignStore", "check_settings", "register_agent_campaign_routes"]

logger = logging.getLogger(__name__)

CONTRACT_VERSION = "1.0.0"
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 50
LIST_STATES = {"abiertas": True, "todas": False}
_PRODUCT_VERSION = re.compile(r"^v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_RESULT_STATUS = {
    "agent_campaign_open_limit": 409,
    "agent_campaign_revision_limit": 409,
    "agent_campaign_validation_limit": 409,
    "agent_campaign_revision_mismatch": 409,
    "agent_campaign_not_draft": 409,
    "agent_campaign_expired": 409,
    "agent_campaign_key_is_immutable": 409,
}


class CampaignStore(FlowChangeStore):
    """Las cuatro funciones del agente de 20261108005000, con la clave de su rol."""

    async def save_campaign(self, instance: Mapping[str, str], *, campaign_id: str | None, revision: int | None,
                            draft: Mapping[str, Any], purpose: str, requested_by: str) -> dict[str, Any]:
        return await self._rpc("save_agent_campaign_draft_v1", {
            "p_instance": instance, "p_campaign_id": campaign_id, "p_expected_revision": revision,
            "p_draft": draft, "p_purpose": purpose, "p_requested_by_ref": requested_by})

    async def record_validation(self, instance: Mapping[str, str], *, campaign_id: str,  # type: ignore[override]
                                revision: int, validator: Mapping[str, Any], result: Mapping[str, Any],
                                preview: Mapping[str, Any], requested_by: str) -> dict[str, Any]:
        return await self._rpc("record_agent_campaign_validation_v1", {
            "p_instance": instance, "p_campaign_id": campaign_id, "p_revision": revision, "p_validator": validator,
            "p_result": result, "p_preview": preview, "p_requested_by_ref": requested_by})

    async def get_campaign(self, instance: Mapping[str, str], campaign_id: str) -> dict[str, Any]:
        return await self._rpc("get_agent_campaign_v1", {"p_instance": instance, "p_campaign_id": campaign_id})

    async def list_campaigns(self, instance: Mapping[str, str], *, only_open: bool, limit: int) -> dict[str, Any]:
        return await self._rpc("list_agent_campaigns_v1", {"p_instance": instance, "p_only_open": only_open,
                                                            "p_limit": limit})


def check_settings(settings: Any) -> None:
    """Lo que exige el flag al arrancar. Sin la entrada por palabra clave no entra nadie, y sin la pagina no hay si."""
    if not getattr(settings, "agent_campaigns_enabled", False):
        return
    flag = "AGENT_CAMPAIGNS_ENABLED"
    if not getattr(settings, "agent_segments_enabled", False):
        raise ValueError(f"{flag} requires AGENT_SEGMENTS_ENABLED")
    if not getattr(settings, "campaign_inbound_entry_enabled", False):
        raise ValueError(f"{flag} requires CAMPAIGN_INBOUND_ENTRY_ENABLED")
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
    return {"contract": rules.CONTRACT, "rules_version": rules.RULES_VERSION, "product_version": version, "git_sha": sha}


def _campaign_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid_campaign_id") from None


@dataclass(frozen=True)
class _Instance:
    refs: dict[str, str]
    inbox_id: int
    git_sha: str


def _refused(error: rules.CampaignInvalid) -> EntryRefused:
    return EntryRefused(error.code, error.reason)


def register_agent_campaign_routes(app: FastAPI, *, token: str, store: CampaignStore, chatwoot: Any,
                                   context: rules.CampaignContext, approval_origin: str,
                                   git_sha: str | None = None) -> None:
    """Las rutas, detras del flag (lo mira quien llama: con el flag apagado no existen)."""
    manifest = context.manifest
    instance = _Instance(refs={"tenant_ref": manifest.tenant_ref, "ally_ref": manifest.ally_ref},
                         inbox_id=manifest.chatwoot_inbox_id,
                         git_sha=git_sha or os.getenv("GIT_SHA", "") or "unknown")
    slots = asyncio.Semaphore(2)

    def authenticate(authorization: str | None) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="automation_expert_change_authentication_required",
                                headers={"WWW-Authenticate": "Bearer"})

    def envelope(data: Mapping[str, Any]) -> dict[str, Any]:
        return {"contract": rules.CONTRACT, "contract_version": CONTRACT_VERSION, "instance": dict(instance.refs),
                "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "bridge_git_sha": instance.git_sha, "data": dict(data)}

    def failure(error: Exception, route: str, campaign_id: str | None = None) -> HTTPException:
        if isinstance(error, StoreRejected):
            code, http_status = error.code, error.http_status
        elif isinstance(error, StoreUnavailable):
            code, http_status = error.code, 503
        else:  # pragma: no cover - lo que no se espera, cerrado
            code, http_status = "automation_expert_change_unavailable", 503
        logger.info("agent_campaign route=%s campaign=%s status=%s code=%s", route, campaign_id, http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    def result_failure(result: Mapping[str, Any], route: str, campaign_id: str | None) -> HTTPException | None:
        if result.get("ok") is True:
            return None
        code = result.get("reason_code")
        if isinstance(code, str) and code in _RESULT_STATUS:
            logger.info("agent_campaign route=%s campaign=%s status=409 code=%s", route, campaign_id, code)
            return HTTPException(status_code=_RESULT_STATUS[code], detail=code)
        return failure(StoreUnavailable(), route, campaign_id)

    async def wa_me(draft: Mapping[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
        """El link de WhatsApp de la campana, con el numero de la bandeja. Sin numero, un aviso."""
        keywords = draft.get("palabras_clave") or []
        if not keywords:
            return None, []
        try:
            inbox = await chatwoot.get_inbox(inbox_id=instance.inbox_id)
        except Exception:  # noqa: BLE001 - el link es un dato de mas: la campana no depende de el
            return None, [{"code": "campaign_whatsapp_link_unavailable"}]
        link = rules.wa_me_link(inbox.get("phone_number") if isinstance(inbox, Mapping) else None, keywords[0])
        return link, ([] if link else [{"code": "campaign_whatsapp_link_unavailable"}])

    def approval_url(campaign_id: str) -> str:
        """El link de la pagina donde un aprobador la publica. Solo lleva el id, que no da acceso."""
        return f"{approval_origin.rstrip('/')}/flow-approval/c/{campaign_id}"

    def view(draft: Mapping[str, Any], segment: rules.LoadedSegment | None, link: str | None) -> dict[str, Any]:
        return {"render": segment.rendered if segment is not None else None,
                "rendered_bytes": segment.rendered_bytes if segment is not None else None,
                "nombre": draft["nombre"], "desde": draft["desde"], "hasta": draft["hasta"],
                "etiqueta": draft["etiqueta"], "palabras_clave": list(draft.get("palabras_clave", ())),
                "pregunta_entrada": draft.get("pregunta_entrada"), "cupon": draft.get("oferta", {}).get("cupon"),
                "wa_me": link}

    def busy() -> None:
        if slots.locked():
            raise HTTPException(status_code=503, detail="automation_expert_change_busy")

    @app.post(f"{PREFIX}/campaigns/preview")
    async def preview(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """Valida y renderiza sin guardar. Con bloqueos contesta 200 y los cuenta: es para corregir."""
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"campaign"}, allowed={"campaign"})
        try:
            draft = rules.parse_campaign(body["campaign"])
        except rules.CampaignInvalid as error:
            raise _refused(error) from None
        blocks, warnings, segment = rules.instance_blocks(draft, context=context, now=datetime.now(UTC))
        link, link_warnings = await wa_me(draft)
        logger.info("agent_campaign route=preview status=200 blocks=%s", len(blocks))
        return envelope({"ok": not blocks, "blocks": blocks, "warnings": [*warnings, *link_warnings],
                         "preview": view(draft, segment, link)})

    @app.post(f"{PREFIX}/campaigns", status_code=201)
    async def propose(request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"campaign", "purpose", "requested_by"},
                           allowed={"campaign", "purpose", "requested_by", "campaign_id", "revision"})
        requested_by = _requested_by(body)
        purpose = body["purpose"]
        if not isinstance(purpose, str) or not 1 <= len(purpose) <= 500:
            raise EntryRefused("invalid_body", {"field": "purpose", "problem": "length_1_to_500"})
        if looks_like_identifier(purpose):
            raise EntryRefused("identifier_not_allowed", {"field": "purpose", "problem": "phone_or_mail"})
        campaign_id, revision = body.get("campaign_id"), body.get("revision")
        if (campaign_id is None) != (revision is None) or (
                revision is not None and (type(revision) is not int or revision < 1)):
            raise EntryRefused("invalid_body", {"field": "revision", "problem": "campaign_id_and_revision_together"})
        if campaign_id is not None:
            campaign_id = _campaign_id(campaign_id) if isinstance(campaign_id, str) else _campaign_id("")
        try:
            draft = rules.parse_campaign(body["campaign"])
        except rules.CampaignInvalid as error:
            raise _refused(error) from None
        blocks, warnings, segment = rules.instance_blocks(draft, context=context, now=datetime.now(UTC))
        if blocks:
            logger.info("agent_campaign route=propose status=422 code=campaign_draft_blocked blocks=%s", len(blocks))
            return JSONResponse(status_code=422, content={"detail": "campaign_draft_blocked",
                                                          **envelope({"blocks": blocks, "warnings": warnings})})
        busy()
        async with slots:
            try:
                saved = await store.save_campaign(instance.refs, campaign_id=campaign_id, revision=revision,
                                                  draft=draft, purpose=purpose, requested_by=requested_by)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "propose", campaign_id) from None
        refused = result_failure(saved, "propose", campaign_id)
        if refused is not None:
            raise refused
        logger.info("agent_campaign route=propose campaign=%s status=201 warnings=%s", saved.get("campaign_id"),
                    len(warnings))
        link, link_warnings = await wa_me(draft)
        return envelope({"campaign_id": saved.get("campaign_id"), "revision": saved.get("revision"),
                         "status": saved.get("status"), "warnings": [*warnings, *link_warnings],
                         "preview": view(draft, segment, link)})

    @app.post(f"{PREFIX}/campaigns/{{campaign_id}}/validate")
    async def validate_campaign(campaign_id: str, request: Request,
                                authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _campaign_id(campaign_id)
        body = _check_keys(await _body(request), required={"revision", "requested_by"},
                           allowed={"revision", "requested_by"})
        if type(body["revision"]) is not int:
            raise EntryRefused("invalid_body", {"field": "revision", "problem": "not_an_integer"})
        requested_by = _requested_by(body)
        validator = _validator(os.environ)
        if validator is None:
            logger.info("agent_campaign route=validate campaign=%s status=503 code=image_identity_missing", checked)
            raise HTTPException(status_code=503, detail="automation_expert_change_unavailable")
        busy()
        async with slots:
            try:
                stored = await store.get_campaign(instance.refs, checked)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "validate", checked) from None
            if stored.get("revision") != body["revision"]:
                raise HTTPException(status_code=409, detail="agent_campaign_revision_mismatch")
            if stored.get("status") != "borrador":
                raise HTTPException(status_code=409, detail="agent_campaign_not_draft")
            try:
                draft = rules.parse_campaign(stored.get("draft"))
            except rules.CampaignInvalid:
                raise failure(StoreUnavailable(), "validate", checked) from None
            blocks, warnings, segment = rules.instance_blocks(draft, context=context, now=datetime.now(UTC),
                                                              version=int(stored.get("segment_version") or 1))
            # El macro tiene que sumar exactamente esta etiqueta (lo que despues exige ProductLabeler al usarlo).
            try:
                actions = await chatwoot.get_macro_actions(macro_id=draft["macro_etiqueta"])
            except Exception:  # noqa: BLE001 - sin poder leerlo no se congela
                blocks.append({"code": "campaign_macro_unreadable", "field": "macro_etiqueta"})
            else:
                if actions != [{"action_name": "add_label", "action_params": [draft["etiqueta"]]}]:
                    blocks.append({"code": "campaign_macro_does_more_than_add_the_label", "field": "macro_etiqueta"})
            link, link_warnings = await wa_me(draft)
            result = {"blocks": blocks, "warnings": [*warnings, *link_warnings]}
            preview_view = view(draft, segment, link)
            try:
                recorded = await store.record_validation(instance.refs, campaign_id=checked, revision=body["revision"],
                                                         validator=validator, result=result, preview=preview_view,
                                                         requested_by=requested_by)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "validate", checked) from None
        refused = result_failure(recorded, "validate", checked)
        if refused is not None:
            raise refused
        logger.info("agent_campaign route=validate campaign=%s status=200 blocks=%s warnings=%s", checked, len(blocks),
                    len(warnings))
        return envelope({"campaign_id": checked, "status": recorded.get("status"),
                         "fingerprint": recorded.get("fingerprint"), "expires_at": recorded.get("expires_at"),
                         **result, "preview": preview_view,
                         "approval_url": approval_url(checked) if recorded.get("status") == "validada" else None})

    @app.get(f"{PREFIX}/campaigns")
    async def list_campaigns(estado: str = "abiertas", limit: str = str(LIST_DEFAULT_LIMIT),
                             authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        if estado not in LIST_STATES:
            raise EntryRefused("invalid_query", {"field": "estado", "problem": "abiertas_or_todas"})
        if not (limit.isascii() and limit.isdigit()) or not 1 <= int(limit) <= LIST_MAX_LIMIT:
            raise EntryRefused("invalid_query", {"field": "limit", "problem": "between_1_and_50"})
        busy()
        async with slots:
            try:
                listed = await store.list_campaigns(instance.refs, only_open=LIST_STATES[estado], limit=int(limit))
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "list") from None
        rows = listed.get("campaigns")
        if not isinstance(rows, list):
            raise failure(StoreUnavailable(), "list")
        logger.info("agent_campaign route=list status=200 estado=%s campaigns=%s", estado, len(rows))
        return envelope({"estado": estado, "campaigns": rows, "open": listed.get("open"),
                         "open_limit": listed.get("open_limit"), "database_at": listed.get("database_at")})

    @app.get(f"{PREFIX}/campaigns/{{campaign_id}}")
    async def read_campaign(campaign_id: str, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _campaign_id(campaign_id)
        busy()
        async with slots:
            try:
                stored = await store.get_campaign(instance.refs, checked)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "read", checked) from None
        logger.info("agent_campaign route=read campaign=%s status=200", checked)
        keys = ("campaign_id", "status", "effective_status", "revision", "campaign_key", "segment_version", "label",
                "draft", "purpose", "valid_from", "valid_until", "fingerprint", "validated_at", "expires_at",
                "check_report", "checked_at", "approved_at", "paused_at", "closed_at", "created_at", "events")
        data = {key: stored.get(key) for key in keys}
        data["check_clean"] = isinstance(stored.get("check_sha"), str)
        data["approval_url"] = approval_url(checked) if stored.get("status") in (
            "validada", "publicada", "pausada") else None
        validation = stored.get("validation")
        data["preview"] = validation.get("preview") if isinstance(validation, Mapping) else None
        data["result"] = validation.get("result") if isinstance(validation, Mapping) else None
        return envelope(data)
