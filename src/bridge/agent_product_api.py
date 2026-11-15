"""Las rutas del Automation Expert para los productos (product-card-v1, E-569, MP-07).

El agente arma el producto con el usuario (el minimo, el texto libre con el
esqueleto como guia), lo mira en la vista previa, lo guarda y lo valida; nunca
lo publica (eso es de la pagina de aprobacion, MP-08 y MP-20).

* ``POST .../products/preview``: valida y renderiza **sin guardar**.
* ``POST .../products``: crea (la version siguiente de la clave) o rehace un borrador.
* ``POST .../products/{id}/validate``: revalida y, sin bloqueos, la base lo congela 24 h.
* ``GET  .../products`` y ``GET .../products/{id}``: en que estado estan.

Detras de ``AGENT_PRODUCTS_ENABLED`` (apagado por defecto: las rutas no
existen y el bridge no llama a ninguna RPC de productos). Usa la clave del
plugin y el JWT del rol ``automation_expert_change``, como las campanas. El log
lleva la ruta, el id y el codigo; nunca el texto.
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

from . import agent_products as rules
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
from .instance_manifest import InstanceManifest

__all__ = ["AGENT_PRODUCTS_ENV", "CONTRACT_VERSION", "ProductStore", "check_settings", "register_agent_product_routes"]

logger = logging.getLogger(__name__)

AGENT_PRODUCTS_ENV = "AGENT_PRODUCTS_ENABLED"
# 0.2.0: la de E-570, con los alias en el minimo (§2.1).
CONTRACT_VERSION = "0.2.0"
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 50
LIST_STATES = {"abiertas": True, "todas": False}
_PRODUCT_VERSION = re.compile(r"^v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_RESULT_STATUS = {
    "agent_product_open_limit": 409,
    "agent_product_revision_limit": 409,
    "agent_product_validation_limit": 409,
    "agent_product_revision_mismatch": 409,
    "agent_product_not_draft": 409,
    "agent_product_expired": 409,
    "agent_product_key_is_immutable": 409,
}


class ProductStore(FlowChangeStore):
    """Las cuatro funciones del agente de 20261115000100, con la clave de su rol."""

    async def save_product(self, instance: Mapping[str, str], *, product_id: str | None, revision: int | None,
                           minimum: Mapping[str, Any], free_text: str, warnings: list[dict[str, str]],
                           purpose: str, requested_by: str) -> dict[str, Any]:
        return await self._rpc("save_agent_product_draft_v1", {
            "p_instance": instance, "p_product_id": product_id, "p_expected_revision": revision,
            "p_minimum": minimum, "p_free_text": free_text, "p_warnings": warnings, "p_purpose": purpose,
            "p_requested_by_ref": requested_by})

    async def record_product_validation(self, instance: Mapping[str, str], *, product_id: str, revision: int,
                                        validator: Mapping[str, Any], result: Mapping[str, Any],
                                        preview: Mapping[str, Any], requested_by: str) -> dict[str, Any]:
        return await self._rpc("record_agent_product_validation_v1", {
            "p_instance": instance, "p_product_id": product_id, "p_revision": revision, "p_validator": validator,
            "p_result": result, "p_preview": preview, "p_requested_by_ref": requested_by})

    async def get_product(self, instance: Mapping[str, str], product_id: str) -> dict[str, Any]:
        return await self._rpc("get_agent_product_v1", {"p_instance": instance, "p_product_id": product_id})

    async def list_products(self, instance: Mapping[str, str], *, only_open: bool, limit: int) -> dict[str, Any]:
        return await self._rpc("list_agent_products_v1", {"p_instance": instance, "p_only_open": only_open,
                                                           "p_limit": limit})


def check_settings(settings: Any) -> None:
    """Lo que exige el flag al arrancar: las rutas del agente (su clave y su rol) y el manifiesto."""
    if not getattr(settings, "agent_products_enabled", False):
        return
    if not getattr(settings, "automation_expert_change_enabled", False):
        raise ValueError(f"{AGENT_PRODUCTS_ENV} requires AUTOMATION_EXPERT_CHANGE_ENABLED")
    if getattr(settings, "instance_manifest", None) is None:
        raise ValueError(f"{AGENT_PRODUCTS_ENV} requires an instance manifest")


def _validator(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """Con que corrio la validacion: la imagen (SETTER_VERSION y GIT_SHA), nunca lo que dice quien llama."""
    version = environ.get("SETTER_VERSION", "").strip()
    sha = environ.get("GIT_SHA", "").strip().lower()
    if not _PRODUCT_VERSION.fullmatch(version) or not _GIT_SHA.fullmatch(sha):
        return None
    return {"contract": rules.CONTRACT, "rules_version": rules.RULES_VERSION, "product_version": version,
            "git_sha": sha}


def _product_id(value: Any) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="invalid_product_id") from None


def _refused(error: rules.ProductCardInvalid) -> EntryRefused:
    return EntryRefused(error.code, error.reason)


def _preview(parsed: rules.ParsedCard, card: Mapping[str, Any]) -> dict[str, Any]:
    """Lo que el AE le muestra al usuario. Sin hotlink ni codigos: el render es lo que va a leer el agente."""
    name = card.get("nombre") if isinstance(card.get("nombre"), str) else None
    key = card.get("clave") if isinstance(card.get("clave"), str) else None
    return {"clave": key, "nombre": name, "render": parsed.render, "rendered_bytes": parsed.render_bytes,
            "render_sha256": parsed.render_sha256, "max_render_bytes": rules.MAX_RENDER_BYTES}


def register_agent_product_routes(app: FastAPI, *, token: str, store: ProductStore, manifest: InstanceManifest,
                                  git_sha: str | None = None) -> None:
    """Las rutas, detras del flag (lo mira quien llama: con el flag apagado no existen)."""
    instance = {"tenant_ref": manifest.tenant_ref, "ally_ref": manifest.ally_ref}
    bridge_sha = git_sha or os.getenv("GIT_SHA", "") or "unknown"
    slots = asyncio.Semaphore(2)

    def authenticate(authorization: str | None) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="automation_expert_change_authentication_required",
                                headers={"WWW-Authenticate": "Bearer"})

    def envelope(data: Mapping[str, Any]) -> dict[str, Any]:
        return {"contract": rules.CONTRACT, "contract_version": CONTRACT_VERSION, "instance": dict(instance),
                "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "bridge_git_sha": bridge_sha, "data": dict(data)}

    def failure(error: Exception, route: str, product_id: str | None = None) -> HTTPException:
        if isinstance(error, StoreRejected):
            code, http_status = error.code, error.http_status
        elif isinstance(error, StoreUnavailable):
            code, http_status = error.code, 503
        else:  # pragma: no cover - lo que no se espera, cerrado
            code, http_status = "automation_expert_change_unavailable", 503
        logger.info("agent_product route=%s product=%s status=%s code=%s", route, product_id, http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    def result_failure(result: Mapping[str, Any], route: str, product_id: str | None) -> HTTPException | None:
        if result.get("ok") is True:
            return None
        code = result.get("reason_code")
        if isinstance(code, str) and code in _RESULT_STATUS:
            logger.info("agent_product route=%s product=%s status=409 code=%s", route, product_id, code)
            return HTTPException(status_code=_RESULT_STATUS[code], detail=code)
        return failure(StoreUnavailable(), route, product_id)

    def busy() -> None:
        if slots.locked():
            raise HTTPException(status_code=503, detail="automation_expert_change_busy")

    async def context(route: str, product_id: str | None = None) -> rules.ProductContext:
        """El manifiesto mas la identidad de los productos vigentes de la base (``taken``)."""
        try:
            listed = await store.list_products(instance, only_open=True, limit=1)
        except (StoreUnavailable, StoreRejected) as error:
            raise failure(error, route, product_id) from None
        taken = listed.get("taken")
        if not isinstance(taken, list) or not all(isinstance(row, Mapping) for row in taken):
            raise failure(StoreUnavailable(), route, product_id)
        return rules.ProductContext.from_manifest(manifest, taken)

    def parse(card: Any, product_context: rules.ProductContext) -> rules.ParsedCard:
        try:
            return rules.parse_product_card(card, context=product_context)
        except rules.ProductCardInvalid as error:
            raise _refused(error) from None

    @app.post(f"{PREFIX}/products/preview")
    async def preview(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """Valida y renderiza sin guardar. Con bloqueos contesta 200 y los cuenta: es para corregir."""
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"product"}, allowed={"product"})
        busy()
        async with slots:
            product_context = await context("preview")
        parsed = parse(body["product"], product_context)
        logger.info("agent_product route=preview status=200 blocks=%s warnings=%s", len(parsed.blocks),
                    len(parsed.warnings))
        return envelope({"ok": parsed.ok, "blocks": list(parsed.blocks), "warnings": list(parsed.warnings),
                         "preview": _preview(parsed, body["product"])})

    @app.post(f"{PREFIX}/products", status_code=201)
    async def propose(request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"product", "purpose", "requested_by"},
                           allowed={"product", "purpose", "requested_by", "product_id", "revision"})
        requested_by = _requested_by(body)
        purpose = body["purpose"]
        if not isinstance(purpose, str) or not 1 <= len(purpose) <= 500:
            raise EntryRefused("invalid_body", {"field": "purpose", "problem": "length_1_to_500"})
        if looks_like_identifier(purpose):
            raise EntryRefused("identifier_not_allowed", {"field": "purpose", "problem": "phone_or_mail"})
        product_id, revision = body.get("product_id"), body.get("revision")
        if (product_id is None) != (revision is None) or (
                revision is not None and (type(revision) is not int or revision < 1)):
            raise EntryRefused("invalid_body", {"field": "revision", "problem": "product_id_and_revision_together"})
        if product_id is not None:
            product_id = _product_id(product_id)
        busy()
        async with slots:
            product_context = await context("propose", product_id)
            parsed = parse(body["product"], product_context)
            if not parsed.ok:
                logger.info("agent_product route=propose status=422 code=product_draft_blocked blocks=%s",
                            len(parsed.blocks))
                return JSONResponse(status_code=422, content={"detail": "product_draft_blocked", **envelope({
                    "blocks": list(parsed.blocks), "warnings": list(parsed.warnings),
                    "preview": _preview(parsed, body["product"])})})
            assert parsed.minimum is not None and parsed.free_text is not None
            try:
                saved = await store.save_product(instance, product_id=product_id, revision=revision,
                                                 minimum=parsed.minimum, free_text=parsed.free_text,
                                                 warnings=list(parsed.warnings), purpose=purpose,
                                                 requested_by=requested_by)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "propose", product_id) from None
        refused = result_failure(saved, "propose", product_id)
        if refused is not None:
            raise refused
        logger.info("agent_product route=propose product=%s status=201 warnings=%s", saved.get("product_id"),
                    len(parsed.warnings))
        return envelope({"product_id": saved.get("product_id"), "revision": saved.get("revision"),
                         "product_version": saved.get("product_version"), "status": saved.get("status"),
                         "warnings": list(parsed.warnings), "preview": _preview(parsed, body["product"])})

    @app.post(f"{PREFIX}/products/{{product_id}}/validate")
    async def validate_product(product_id: str, request: Request,
                               authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _product_id(product_id)
        body = _check_keys(await _body(request), required={"revision", "requested_by"},
                           allowed={"revision", "requested_by"})
        if type(body["revision"]) is not int:
            raise EntryRefused("invalid_body", {"field": "revision", "problem": "not_an_integer"})
        requested_by = _requested_by(body)
        validator = _validator(os.environ)
        if validator is None:
            logger.info("agent_product route=validate product=%s status=503 code=image_identity_missing", checked)
            raise HTTPException(status_code=503, detail="automation_expert_change_unavailable")
        busy()
        async with slots:
            try:
                stored = await store.get_product(instance, checked)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "validate", checked) from None
            if stored.get("revision") != body["revision"]:
                raise HTTPException(status_code=409, detail="agent_product_revision_mismatch")
            if stored.get("status") != "borrador":
                raise HTTPException(status_code=409, detail="agent_product_not_draft")
            minimum, free_text = stored.get("minimo"), stored.get("texto_libre")
            if not isinstance(minimum, Mapping) or not isinstance(free_text, str):
                raise failure(StoreUnavailable(), "validate", checked)
            card = {**minimum, "texto_libre": free_text}
            product_context = await context("validate", checked)
            try:
                parsed = rules.parse_product_card(card, context=product_context)
            except rules.ProductCardInvalid:
                raise failure(StoreUnavailable(), "validate", checked) from None
            result = {"blocks": list(parsed.blocks), "warnings": list(parsed.warnings)}
            preview_view = _preview(parsed, card)
            try:
                recorded = await store.record_product_validation(instance, product_id=checked,
                                                                 revision=body["revision"], validator=validator,
                                                                 result=result, preview=preview_view,
                                                                 requested_by=requested_by)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "validate", checked) from None
        refused = result_failure(recorded, "validate", checked)
        if refused is not None:
            raise refused
        logger.info("agent_product route=validate product=%s status=200 blocks=%s warnings=%s", checked,
                    len(parsed.blocks), len(parsed.warnings))
        return envelope({"product_id": checked, "status": recorded.get("status"),
                         "fingerprint": recorded.get("fingerprint"), "expires_at": recorded.get("expires_at"),
                         **result, "preview": preview_view})

    @app.get(f"{PREFIX}/products")
    async def list_products(estado: str = "abiertas", limit: str = str(LIST_DEFAULT_LIMIT),
                            authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        if estado not in LIST_STATES:
            raise EntryRefused("invalid_query", {"field": "estado", "problem": "abiertas_or_todas"})
        if not (limit.isascii() and limit.isdigit()) or not 1 <= int(limit) <= LIST_MAX_LIMIT:
            raise EntryRefused("invalid_query", {"field": "limit", "problem": "between_1_and_50"})
        busy()
        async with slots:
            try:
                listed = await store.list_products(instance, only_open=LIST_STATES[estado], limit=int(limit))
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "list") from None
        rows = listed.get("products")
        if not isinstance(rows, list):
            raise failure(StoreUnavailable(), "list")
        logger.info("agent_product route=list status=200 estado=%s products=%s", estado, len(rows))
        return envelope({"estado": estado, "products": rows, "open": listed.get("open"),
                         "open_limit": listed.get("open_limit"), "database_at": listed.get("database_at")})

    @app.get(f"{PREFIX}/products/{{product_id}}")
    async def read_product(product_id: str, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _product_id(product_id)
        busy()
        async with slots:
            try:
                stored = await store.get_product(instance, checked)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "read", checked) from None
        logger.info("agent_product route=read product=%s status=200", checked)
        keys = ("product_id", "status", "effective_status", "revision", "product_key", "product_version", "minimo",
                "texto_libre", "advertencias", "purpose", "fingerprint", "validated_at", "expires_at",
                "approved_at", "paused_at", "closed_at", "created_at", "events")
        data = {key: stored.get(key) for key in keys}
        validation = stored.get("validation")
        data["preview"] = validation.get("preview") if isinstance(validation, Mapping) else None
        data["result"] = validation.get("result") if isinstance(validation, Mapping) else None
        return envelope(data)
