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
* ``GET /internal/automation-expert/v1/flow-changes`` (1.10.0, M16 P7): las
  propuestas de la instancia y en que estado estan, con
  ``list_flow_changes_v1`` (20261030000300). ``?estado=abiertas`` (el default)
  o ``todas``, ``&limit=`` de 1 a 50.
* ``GET /internal/automation-expert/v1/templates`` (1.10.0, M16 P6): el
  catalogo de plantillas de la bandeja, el mismo que mira la validacion
  (``GET /inboxes/<id>`` de Chatwoot), con el texto de cada parte.

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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .flow_change_operator import OperatorInputError, validate_proposal, world_from_store
from .flow_change_preview import template_view
from .flow_change_simulation import (
    native_flow_counts_request,
    new_audience_flow_form,
    new_uncounted_flow,
    simulation_trigger,
)
from .flow_definition import (
    InstanceRuntime,
    InstanceView,
    ProposalItemsInvalid,
    TemplateCatalog,
    as_draft,
    loads_strict,
    parse_items,
    touch_times,
)
from .flow_validator import FlowValidationInputError, validate

__all__ = [
    "BULK_SEND_PAGE_PATH",
    "BulkSendStore",
    "CONTRACT",
    "CONTRACT_VERSION",
    "DB_ROLE",
    "PREFIX",
    "FlowChangeStore",
    "StoreRejected",
    "StoreUnavailable",
    "bulk_send_page_url",
    "check_bulk_send_settings",
    "check_settings",
    "looks_like_identifier",
    "register_bulk_send_routes",
    "register_flow_change_routes",
    "template_has_opt_out_button",
]

logger = logging.getLogger(__name__)

CONTRACT = "flow-change-cycle-v1"
# 1.7.0 (el PR 4 del hito 4): el item nuevo, que crea un flujo nativo y lo publica en cohorte.
# 1.9.0 (A′ PR 3b): el si se da en la pagina de aprobacion del bridge (flow_approval.py).
# 1.10.0 (E-208, M16 P6, P7 y P9): el catalogo, la lista de propuestas y el mensaje en la vista previa.
# 1.11.0 (E-353): la simulacion de una version y un volver de un nativo, y los bloqueos de lo que no tendria efecto.
CONTRACT_VERSION = "1.12.0"
PREFIX = "/internal/automation-expert/v1"
DB_ROLE = "automation_expert_change"
MAX_BODY_BYTES = 256 * 1024
MAX_STORE_BYTES = 4 * 1024 * 1024
# read_flow_engine_v1 pagina de a 50 flujos; mas de 500 en una instancia no es de la v1.
ENGINE_PAGES_MAX = 10
# La lista de propuestas (1.10.0): de a lo sumo 50, como la pagina de aprobacion.
LIST_DEFAULT_LIMIT = 20
LIST_MAX_LIMIT = 50
LIST_STATES = {"abiertas": True, "todas": False}
# El catalogo que se lista: una bandeja con mas plantillas que esto se corta y se dice.
TEMPLATES_MAX = 100
# La historia de los ultimos 8 dias (la vista previa simula 7 y el de mas es la siembra), con un margen
# para que un reloj del bridge atrasado no pida mas de lo que la funcion acepta.
HISTORY_WINDOW = timedelta(days=8) - timedelta(minutes=5)
# La simulacion de un flujo nuevo cuenta los disparos de los ultimos 7 dias (flow-engine-v2 §14).
NEW_FLOW_WINDOW = timedelta(days=7)
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
    "flow_already_registered": 409,
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

    async def _rpc(self, function: str, payload: Mapping[str, Any], *, list_ok: bool = False) -> Any:
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
            # Una funcion que devuelve una lista (read_bulk_sends_v1) la pide con list_ok.
            if not isinstance(body, dict) and not (list_ok and isinstance(body, list)):
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

    async def list_proposals(self, instance: Mapping[str, str], *, only_open: bool, limit: int) -> dict[str, Any]:
        """``list_flow_changes_v1`` (20261030000300): las propuestas, de la mas nueva a la mas vieja."""
        return await self._rpc("list_flow_changes_v1", {"p_instance": instance, "p_only_open": only_open,
                                                        "p_limit": limit})

    async def read_engine(self, instance: Mapping[str, str]) -> dict[str, Any]:
        """``read_flow_engine_v1`` entero: las paginas de 50 flujos, juntas (hasta ENGINE_PAGES_MAX)."""
        document: dict[str, Any] | None = None
        after: str | None = None
        for _ in range(ENGINE_PAGES_MAX):
            page = await self._rpc("read_flow_engine_v1", {"p_instance": instance, "p_after": after})
            if not isinstance(page, dict) or not isinstance(page.get("flows"), list):
                raise StoreUnavailable()
            if document is None:
                document = page
            else:
                document["flows"] = [*document["flows"], *page["flows"]]
            after = page.get("next")
            if after is None:
                document["next"] = None
                return document
        raise StoreUnavailable()

    async def read_new_flow_counts(self, instance: Mapping[str, str], product_ref: str, trigger: str,
                                   since: datetime) -> Any:
        """``read_flow_engine_native_trigger_counts_v1``: los disparos del disparador de un flujo nuevo."""
        return await self._rpc("read_flow_engine_native_trigger_counts_v1", {
            "p_instance": instance, "p_product_ref": product_ref, "p_trigger": trigger,
            "p_since": since.isoformat().replace("+00:00", "Z")})

    async def read_audience_reach(self, instance: Mapping[str, str], flow_id: str, version: int) -> Any:
        """``read_flow_audience_reach_v1`` (20261116000100, E-622): cuantas personas cumplen hoy la audiencia."""
        return await self._rpc("read_flow_audience_reach_v1", {
            "p_instance": instance, "p_flow_id": flow_id, "p_version": version})

    async def read_history(self, instance: Mapping[str, str], product_ref: str, since: datetime) -> Any:
        return await self._rpc("read_flow_engine_history_v1", {
            "p_instance": instance, "p_product_ref": product_ref,
            "p_since": since.isoformat().replace("+00:00", "Z")})


async def _new_flow_input(store: FlowChangeStore, instance: Mapping[str, str], product: str,
                          item: Mapping[str, Any]) -> dict[str, Any]:
    """Lo que lee la simulacion de un item ``nuevo``: los disparos de su disparador, o por que no."""
    if new_audience_flow_form([item]) is not None:
        # Un disparo por audiencia (manual, fecha, cada) no tiene evento que contar: la simulacion
        # sale de la definicion y de que nace en cohorte, sin leer nada (E-621).
        return {"new_flow_audience": True}
    if new_uncounted_flow([item]) is not None:
        # La palabra clave (Z2 3.9) o un registro (4.3): no hay lectura que los cuente bien; sin leer nada.
        return {"new_flow_uncounted": True}
    # Su evento, o el silencio del agente (P22-3, E-269), que se cuenta con la vara del legado.
    trigger = simulation_trigger(item.get("definicion"))
    if not isinstance(trigger, str):
        return {"simulation_unavailable": "simulation_new_flow_counts_invalid"}
    try:
        counts = await store.read_new_flow_counts(instance, product, trigger, datetime.now(UTC) - NEW_FLOW_WINDOW)
    except (StoreUnavailable, StoreRejected):
        logger.info("automation_expert_change route=validate simulation=flow_engine_unavailable")
        return {"simulation_unavailable": "flow_engine_unavailable"}
    return {"new_flow_counts": counts}


async def _simulation_inputs(store: FlowChangeStore, instance: Mapping[str, str], items: Any,
                             snapshot: Any = None) -> dict[str, Any]:
    """La lectura del motor y la historia del producto de la propuesta, para la simulacion.

    Si no se pueden leer (por ejemplo, la base todavia no tiene la migracion del
    motor), la validacion sigue y la simulacion queda ``failed`` con
    ``flow_engine_unavailable``: la vista previa lo dice y aprobar registra ``failed``.

    Un cambio sobre un flujo nativo (``snapshot`` es el almacen que ya se leyo)
    no lee la historia, que exige la ruta del carrito: cuenta los disparos del
    evento del flujo, como un ``nuevo`` (E-241).
    """
    products = {item.get("producto") for item in items if isinstance(item, Mapping)} if isinstance(items, list) \
        else set()
    if len(products) != 1 or not all(isinstance(product, str) for product in products):
        return {"simulation_unavailable": "simulation_multi_product"}
    new_flows = [item for item in items if isinstance(item, Mapping) and item.get("tipo") == "nuevo"]
    if new_flows:
        # Un flujo nuevo no tiene historia en el motor: se cuentan los disparos de su disparador.
        if len(new_flows) != len(items):
            return {"simulation_unavailable": "simulation_new_flow_mixed"}
        # Varios nuevos (E-657): cada uno se lee por separado, y la simulacion arma uno por flujo.
        reads = [await _new_flow_input(store, instance, next(iter(products)), item) for item in new_flows]
        return reads[0] if len(reads) == 1 else {"new_flows": reads}
    native = native_flow_counts_request(snapshot, items)
    if native is not None:
        if "simulation_unavailable" in native or "native_uncounted" in native:
            return native
        if "audience_reach" in native:
            # Prender o apagar un flujo de audiencia: sin evento, cuenta su audiencia de hoy (E-622).
            request = native["audience_reach"]
            try:
                reach = await store.read_audience_reach(instance, request["flow_id"], request["version"])
            except (StoreUnavailable, StoreRejected):
                logger.info("automation_expert_change route=validate simulation=audience_reach_unavailable")
                return {"simulation_unavailable": "simulation_audience_reach_unavailable"}
            return {"native_audience_reach": reach}
        try:
            counts = await store.read_new_flow_counts(instance, native["product_ref"], native["trigger"],
                                                      datetime.now(UTC) - NEW_FLOW_WINDOW)
        except (StoreUnavailable, StoreRejected):
            logger.info("automation_expert_change route=validate simulation=flow_engine_unavailable native=1")
            return {"simulation_unavailable": "flow_engine_unavailable"}
        return {"native_flow_counts": counts}
    since = datetime.now(UTC) - HISTORY_WINDOW
    try:
        engine = await store.read_engine(instance)
        history = await store.read_history(instance, next(iter(products)), since)
    except (StoreUnavailable, StoreRejected):
        logger.info("automation_expert_change route=validate simulation=flow_engine_unavailable")
        return {"simulation_unavailable": "flow_engine_unavailable"}
    return {"engine": engine, "history": history}


async def _draft_catalog(chatwoot: Any, inbox_id: int) -> TemplateCatalog | None:
    """El catalogo de la bandeja para los defaults de un flujo nuevo, o None si no se pudo leer."""
    try:
        inbox = await chatwoot.get_inbox(inbox_id=inbox_id)
        return TemplateCatalog.from_inbox_payload({"message_templates": inbox["message_templates"]})
    except Exception:  # noqa: BLE001 - sin catalogo el borrador sigue y pide el idioma
        logger.info("automation_expert_change route=propose catalog=unavailable")
        return None


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
    # E-316 (segment-labels-v1): las etiquetas de segmento que acepta etiquetar, con su estado de ahora.
    segment_labels: Callable[[], list[dict[str, str]]] = field(default=lambda: [])
    # E-353: lo que ejecuta el bridge de la instancia, para no publicar sin efecto. None: no se sabe.
    runtime: InstanceRuntime | None = None
    # E-370: las plantillas con imagen de encabezado declarada en el manifiesto.
    template_images: frozenset[str] = frozenset()
    # F4: FLOW_LITERAL_VARIABLES_ENABLED, el texto fijo en las variables de un paso.
    literal_variables: bool = False
    # Z2 3.9: las claves de las reglas de palabra clave servidas ahora. None: no se sabe.
    keyword_rules: Callable[[], frozenset[str]] | None = None


def _instance_view(context: _Context, store: Mapping[str, Any]) -> InstanceView:
    products = store.get("products")
    if not isinstance(products, list) or not products:
        raise StoreUnavailable()
    return InstanceView(tenant_ref=context.instance["tenant_ref"], ally_ref=context.instance["ally_ref"],
                        products=frozenset(products), time_zone=context.time_zone,
                        segment_labels=tuple((entry["etiqueta"], entry["estado"])
                                             for entry in context.segment_labels()),
                        runtime=context.runtime, template_images=context.template_images,
                        literal_variables=context.literal_variables,
                        keyword_rules=context.keyword_rules() if context.keyword_rules is not None else None)


def _finding(finding: Mapping[str, Any]) -> dict[str, Any]:
    return {key: finding.get(key) for key in ("code", "kind", "path", "rule", "reason", "alternative")
            if key in finding}


class EntryRefused(Exception):
    """Un 422 de entrada con su motivo: el codigo cerrado y que campo o que clave no cierra.

    El motivo lleva nombres de campos y de claves, nunca un valor: lo que escribio
    el modelo puede traer un telefono. Lo arma el manejador de la app en
    ``{"detail": code, "reason": {...}}`` y lo anota en el log.
    """

    def __init__(self, code: str, reason: Mapping[str, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.reason = dict(reason)


def _keys(keys: Any) -> list[str]:
    return sorted(key if isinstance(key, str) and _KEY.fullmatch(key) else "<otra>" for key in keys)[:10]


_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")


def _check_keys(body: Any, *, required: set[str], allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(body, Mapping):
        raise EntryRefused("invalid_body", {"field": "body", "problem": "not_object"})
    missing, extra = required - set(body), set(body) - allowed
    if missing or extra:
        reason: dict[str, Any] = {"field": "body"}
        if missing:
            reason["missing"] = sorted(missing)
        if extra:
            reason["extra"] = _keys(extra)
        raise EntryRefused("invalid_body", reason)
    return body


def _touches(item: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    """Cuando sale cada toque antes y despues del cambio, en horas desde el primero."""
    if item.get("tipo") == "nuevo" and isinstance(item.get("definicion"), Mapping):
        return {"flujo": item.get("flujo"), "antes": [], "despues": touch_times(item["definicion"])}
    if item.get("tipo") != "version" or not isinstance(item.get("definicion"), Mapping):
        return None
    flow = next((f for f in snapshot.get("flows", []) if isinstance(f, Mapping)
                 and f.get("flow_id") == item.get("flow_id")), None)
    active = None
    if flow is not None and flow.get("active_version") is not None:
        active = (flow.get("versions") or {}).get(str(flow.get("active_version")))
    return {"flujo": item.get("flujo"),
            "antes": touch_times(active) if isinstance(active, Mapping) else [],
            "despues": touch_times(item["definicion"])}


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
        raise EntryRefused("invalid_body", {"field": "body", "problem": "not_json"}) from None


def _requested_by(body: Mapping[str, Any]) -> str:
    requested = body.get("requested_by")
    user = requested.get("slack_user") if isinstance(requested, Mapping) else None
    if not isinstance(requested, Mapping) or set(requested) != {"slack_user"} or not isinstance(user, str) \
            or not _SLACK_USER.fullmatch(user):
        raise EntryRefused("invalid_body", {"field": "requested_by", "problem": "not_a_slack_user"})
    return user


def _proposal_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid_proposal_id") from None


def register_flow_change_routes(app: FastAPI, *, token: str, store: FlowChangeStore, chatwoot: Any,
                                tenant_ref: str, ally_ref: str, time_zone: str | None, inbox_id: int,
                                git_sha: str | None = None,
                                segment_labels: Callable[[], list[dict[str, str]]] | None = None,
                                runtime: InstanceRuntime | None = None,
                                template_images: frozenset[str] = frozenset(),
                                literal_variables: bool = False,
                                keyword_rules: Callable[[], frozenset[str]] | None = None) -> None:
    """Las rutas, detras del flag (lo mira quien llama: con el flag apagado no existen).

    ``keyword_rules`` (Z2 3.9): las claves de las reglas de palabra clave que el bridge sirve, para que un
    flujo con ``dispara.palabra_clave`` nombre una regla andando (``keyword_rule_unknown``). Sin el, no se mira.

    ``segment_labels`` (E-316): las etiquetas de segmento que acepta etiquetar, con su estado al
    validar. Sin el, solo cliente, como antes.

    ``runtime`` (E-353): lo que ejecuta el bridge de la instancia. Sin el, no se bloquea por eso.

    ``template_images`` (E-370): las plantillas con imagen de encabezado declarada. Sin ellas, la
    regla 13 bloquea toda plantilla con encabezado de imagen.

    ``literal_variables`` (F4): FLOW_LITERAL_VARIABLES_ENABLED. Sin el, ``{"texto": …}`` en las variables
    de un paso es invalid_value, como antes.
    """
    context = _Context(instance={"tenant_ref": tenant_ref, "ally_ref": ally_ref}, time_zone=time_zone,
                       inbox_id=inbox_id, git_sha=git_sha or os.getenv("GIT_SHA", "") or "unknown",
                       runtime=runtime, template_images=template_images, literal_variables=literal_variables,
                       keyword_rules=keyword_rules,
                       **({"segment_labels": segment_labels} if segment_labels is not None else {}))
    slots = asyncio.Semaphore(2)

    @app.exception_handler(EntryRefused)
    async def entry_refused(request: Request, error: EntryRefused) -> JSONResponse:
        route = "validate" if request.url.path.endswith("/validate") else (
            "list" if request.method == "GET" else "propose")
        logger.info("automation_expert_change route=%s status=422 code=%s reason=%s", route, error.code,
                    json.dumps(error.reason, sort_keys=True, separators=(",", ":")))
        return JSONResponse(status_code=422, content={"detail": error.code, "reason": error.reason})

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
            definition = versions.get(str(active)) if active is not None else None
            # El item de una version nueva, ya con la forma que pide POST /flow-changes, y su base:
            # el agente los copia y cambia solo los pasos. Sin version activa no hay de donde partir.
            draft_item = None if not isinstance(definition, Mapping) else {
                "producto": flow.get("product_ref"), "flujo": flow.get("flow_key"), "flow_id": flow.get("flow_id"),
                "tipo": "version", "definicion": as_draft(definition), "en_curso": "seguir"}
            # native: el flujo nacio en el motor (flow-engine-v2); su vuelta atras es pausarlo o apagarlo.
            flows.append({key: flow.get(key) for key in ("flow_id", "product_ref", "flow_key", "state",
                                                          "active_version", "generation", "changed_at")}
                         | {"native": flow.get("native") is True}
                         | {"versions": sorted(int(number) for number in versions),
                            "active_definition": definition,
                            "toques": touch_times(definition) if isinstance(definition, Mapping) else [],
                            "draft_item": draft_item,
                            "draft_base": {flow.get("flow_id"): {"active_version": active}}})
        logger.info("automation_expert_change route=flow_store status=200 flows=%s", len(flows))
        return envelope({"products": snapshot.get("products"), "flows": flows})

    @app.post(f"{PREFIX}/flow-changes", status_code=201)
    async def propose(request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"items", "base", "purpose", "requested_by"},
                           allowed={"items", "base", "purpose", "requested_by", "proposal_id", "revision"})
        requested_by = _requested_by(body)
        purpose, base = body["purpose"], body["base"]
        if not isinstance(purpose, str) or not 1 <= len(purpose) <= 500:
            raise EntryRefused("invalid_body", {"field": "purpose", "problem": "length_1_to_500"})
        if not isinstance(base, Mapping):
            raise EntryRefused("invalid_body", {"field": "base", "problem": "not_object"})
        if looks_like_identifier(purpose):
            raise EntryRefused("identifier_not_allowed", {"field": "purpose", "problem": "phone_or_mail"})
        proposal_id = body.get("proposal_id")
        revision = body.get("revision")
        if (proposal_id is None) != (revision is None) or (
                revision is not None and (type(revision) is not int or revision < 1)):
            raise EntryRefused("invalid_body", {"field": "revision", "problem": "proposal_id_and_revision_together"})
        if proposal_id is not None:
            proposal_id = _proposal_id(proposal_id) if isinstance(proposal_id, str) else _proposal_id("")
        try:
            items = parse_items(body["items"])
        except ProposalItemsInvalid as error:
            raise EntryRefused("flow_change_items_invalid", error.reason()) from None
        busy()
        async with slots:
            # Un flujo nuevo no tiene de donde copiar el idioma de su plantilla: sale del catalogo de la
            # bandeja (el default del esquema). Sin catalogo, el borrador lo pide como hoy.
            catalog = await _draft_catalog(chatwoot, context.inbox_id) if any(
                item.is_new_flow for item in items) else None
            try:
                snapshot = await store.read_store(context.instance)
                world = world_from_store(snapshot)
                result = validate(items, actor="automation_expert", world=world,
                                  instance=_instance_view(context, snapshot), catalog=catalog, phase="draft",
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
        touches = [t for t in (_touches(item, snapshot) for item in result.items_json()) if t is not None]
        return envelope({"proposal_id": saved.get("proposal_id"), "revision": saved.get("revision"),
                         "status": saved.get("status"),
                         "warnings": [_finding(f.to_json()) for f in result.warnings],
                         "toques": touches})

    @app.post(f"{PREFIX}/flow-changes/{{proposal_id}}/validate")
    async def validate_change(proposal_id: str, request: Request,
                              authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        checked = _proposal_id(proposal_id)
        body = _check_keys(await _body(request), required={"revision", "requested_by"},
                           allowed={"revision", "requested_by"})
        if type(body["revision"]) is not int:
            raise EntryRefused("invalid_body", {"field": "revision", "problem": "not_an_integer"})
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
            simulation_inputs = await _simulation_inputs(store, context.instance, proposal.get("items"), snapshot)
            try:
                view = _instance_view(context, snapshot)
                out = validate_proposal({
                    **simulation_inputs,
                    "instance": {**context.instance, "products": sorted(view.products),
                                 "time_zone": context.time_zone,
                                 **({"runtime": view.runtime.to_json()} if view.runtime is not None else {}),
                                 **({"template_images": sorted(view.template_images)}
                                    if view.template_images else {}),
                                 "segment_labels": [{"etiqueta": label, "estado": state}
                                                    for label, state in view.segment_labels]},
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

    @app.get(f"{PREFIX}/flow-changes")
    async def list_changes(estado: str = "abiertas", limit: str = str(LIST_DEFAULT_LIMIT),
                           authorization: str | None = Header(default=None)) -> Any:
        """Las propuestas de la instancia y en que estado estan (M16 P7). Solo lee."""
        authenticate(authorization)
        if estado not in LIST_STATES:
            raise EntryRefused("invalid_query", {"field": "estado", "problem": "abiertas_or_todas"})
        if not (limit.isascii() and limit.isdigit()) or not 1 <= int(limit) <= LIST_MAX_LIMIT:
            raise EntryRefused("invalid_query", {"field": "limit", "problem": "between_1_and_50"})
        busy()
        async with slots:
            try:
                listed = await store.list_proposals(context.instance, only_open=LIST_STATES[estado],
                                                    limit=int(limit))
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "list") from None
        proposals = listed.get("proposals")
        if not isinstance(proposals, list):
            raise failure(StoreUnavailable(), "list")
        logger.info("automation_expert_change route=list status=200 estado=%s proposals=%s", estado,
                    len(proposals))
        return envelope({"estado": estado, "proposals": proposals, "open": listed.get("open"),
                         "open_limit": listed.get("open_limit"), "database_at": listed.get("database_at")})

    @app.get(f"{PREFIX}/templates")
    async def list_templates(authorization: str | None = Header(default=None)) -> Any:
        """El catalogo de plantillas de la bandeja, el mismo que mira la validacion (M16 P6). Solo lee."""
        authenticate(authorization)
        busy()
        async with slots:
            try:
                inbox = await chatwoot.get_inbox(inbox_id=context.inbox_id)
                catalog = TemplateCatalog.from_inbox_payload({"message_templates": inbox["message_templates"]})
            except Exception:  # noqa: BLE001 - un Chatwoot caido no deja listar, y se dice
                logger.info("automation_expert_change route=templates status=503 code=template_catalog_unavailable")
                raise HTTPException(status_code=503, detail="template_catalog_unavailable") from None
        views = sorted((template_view(template) for template in catalog.templates),
                       key=lambda view: (not view["aprobada"], str(view["nombre"]), str(view["idioma"])))
        logger.info("automation_expert_change route=templates status=200 templates=%s", len(views))
        return envelope({"templates": views[:TEMPLATES_MAX], "total": len(views),
                         "truncated": len(views) > TEMPLATES_MAX, "catalog_sha256": catalog.catalog_sha256})

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


# ===========================================================================
# El masivo manejado por el Automation Expert (encargo 6 del mapa del 2026-11-25, piezas 7 y 11).
# ===========================================================================
#
# Tres rutas, detras de ``BULK_SEND_ENABLED`` (apagado por defecto: sin el flag no existen y dan 404):
#
# * ``POST {PREFIX}/bulk-sends``: el borrador de un masivo (plantilla, variables, imagen, hora central,
#   etiqueta de la campana y producto) con ``create_bulk_send_v1`` (20261125000200). Antes de guardar mira
#   el catalogo de la bandeja: la plantilla tiene que estar ``APPROVED`` y tener la respuesta rapida
#   «No más mensajes» (la regla del boton de baja, pieza 11). Devuelve el link de la pagina para subir la
#   lista: la lista nunca pasa por el agente (decision 1).
# * ``GET {PREFIX}/bulk-sends/{id}``: el estado y los conteos (``read_bulk_send_v1``).
# * ``GET {PREFIX}/bulk-sends``: los masivos de la instancia (``read_bulk_sends_v1``).
#
# Con ``BULK_SEND_WARMUP_ENABLED`` (F2, ``bulk_send_warmup``) las tres suman ``warmup`` a cada masivo: el tope
# del dia de su hora pedida, si entra y desde cuando se puede la proxima tanda grande, con el mismo texto
# (``detalle``) que ve el aprobador en la pagina. Apagado, la clave no esta y las respuestas son las de hoy.
#
# Ninguna confirma, arranca ni frena: eso lo hace un aprobador en la pagina. Ninguna respuesta lleva un
# telefono, ni enmascarado: de la vista de la base pasan solo los conteos y los datos del envio (los
# ejemplos enmascarados son de la pagina).
#
# **La clave.** Las funciones del masivo son de ``service_role`` (la migracion no le dio ninguna al rol del
# AE), asi que este cliente usa esa clave y solo llama a esas tres funciones. El agente no la ve: entra con
# ``AUTOMATION_EXPERT_CHANGE_TOKEN`` como las demas rutas.

BULK_SEND_ENV = "BULK_SEND_ENABLED"
BULK_SENDS = f"{PREFIX}/bulk-sends"
# La pagina para subir la lista de un masivo, bajo la pagina de aprobacion (como /flow-approval/envios de
# la audiencia y /flow-approval/p/<id> de una propuesta). La sirve el encargo 5 (bulk_send_page.py): hasta
# entonces el link no abre nada, y con el flag apagado no se arma.
BULK_SEND_PAGE_PATH = "/flow-approval/masivos"
BULK_SEND_UPLOAD_NOTE = (
    "Este link abre la página para subir la lista (CSV, Excel o JSON). Entra un aprobador con «Entrar con "
    "Slack», sube el archivo y ahí ve cuántas personas sirven y cuántas quedaron afuera y por qué. El envío "
    "lo confirma un aprobador en esa misma página, escribiendo el número de personas: nunca sale desde este chat."
)
BULK_LIST_DEFAULT_LIMIT = 20
BULK_LIST_MAX_LIMIT = 50
# La hora central: preparar necesita al menos una hora antes (pieza 8), y mas de 30 dias no es un masivo.
BULK_SCHEDULE_MIN_AHEAD = timedelta(hours=1)
BULK_SCHEDULE_MAX_AHEAD = timedelta(days=30)
BULK_VARIABLE_MAX_CHARS = 200
_BULK_TEMPLATE_NAME = re.compile(r"^[a-z0-9_]{1,512}$")
_BULK_LANGUAGE = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?$")
_BULK_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_BULK_PRODUCT = re.compile(r"^[a-z0-9_-]{1,100}$")
_BULK_MARKER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
_BULK_FIRST_NAME = {"de": "nombre"}
_BULK_WINDOW_MODES = ("centrada", "desde_la_hora")
# Lo que la base devuelve de un envio y llega al agente. Afuera a proposito: los ejemplos (telefono
# enmascarado e inicial), quien subio y quien confirmo (IDs de Slack), la huella del archivo y la nota libre
# del origen (la escribe una persona y puede traer un telefono).
_BULK_SEND_KEYS = ("send_id", "state", "template_name", "template_language", "template_variables",
                   "header_image_url", "campaign_label", "product_refs", "scheduled_at", "window_mode",
                   "rate_millis", "recipient_cap", "created_at", "confirmed_at", "confirmed_count",
                   "confirmed_health", "started_at", "finished_at", "brake_reason", "braked_at")
_BULK_LIST_KEYS = ("state", "uploaded_at", "source_kind", "notice_declared", "rejected_counts", "member_count",
                   "retention_days", "vetted_at", "purged_at", "eligible_count", "excluded_by_reason")
_BULK_COUNT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_BULK_RAISED = {"bulk_send_invalid", "bulk_send_variable_invalid", "bulk_send_not_found", "bulk_send_read_invalid"}


def bulk_send_page_url(origin: str, send_id: str) -> str:
    """El link de la pagina del masivo: el dominio de la pagina de aprobacion y el UUID canonico al final."""
    return f"{origin.strip().rstrip('/')}{BULK_SEND_PAGE_PATH}/{UUID(send_id)}"


def template_has_opt_out_button(template: Mapping[str, Any]) -> bool:
    """La regla del boton de baja (pieza 11): una respuesta rapida que, tocada, ya registra la baja.

    Es la de ``FOLLOWUP_OPT_OUT_BUTTON`` («No más mensajes»). Vale tambien el mismo texto sin tilde («No mas
    mensajes», como la de Maribel que viene de GHL): tocarla manda ese texto y ``detect_explicit_opt_out``
    lo registra como la misma baja (``stop_receiving_messages``). Una respuesta rapida con otro texto, o un
    boton de URL con ese texto, no cuenta.
    """
    from .flow_validator import FOLLOWUP_OPT_OUT_BUTTON
    from .opt_out import detect_explicit_opt_out

    for component in template.get("components") or ():
        if not isinstance(component, Mapping) or component.get("type") != "BUTTONS":
            continue
        for button in component.get("buttons") or ():
            if not isinstance(button, Mapping) or button.get("type") != "QUICK_REPLY":
                continue
            text = button.get("text")
            # Meta corta el texto de una respuesta rapida a 25 caracteres.
            if not isinstance(text, str) or not text.strip() or len(text) > 25:
                continue
            if text == FOLLOWUP_OPT_OUT_BUTTON:
                return True
            match = detect_explicit_opt_out([text])
            if match is not None and match.rule_key == "stop_receiving_messages":
                return True
    return False


def _bulk_template_shape(template: Mapping[str, Any]) -> dict[str, Any]:
    """Lo que el masivo necesita saber de una plantilla: marcadores del cuerpo, encabezado y botones."""
    body_markers: set[str] = set()
    header_format: str | None = None
    header_markers = False
    dynamic_url = False
    for component in template.get("components") or ():
        if not isinstance(component, Mapping):
            continue
        kind = component.get("type")
        if kind == "BODY" and isinstance(component.get("text"), str):
            body_markers = set(_BULK_MARKER.findall(component["text"]))
        elif kind == "HEADER":
            fmt = component.get("format", "TEXT")
            header_format = fmt if isinstance(fmt, str) else "OTRO"
            header_markers = isinstance(component.get("text"), str) and bool(_BULK_MARKER.search(component["text"]))
        elif kind == "BUTTONS":
            for button in component.get("buttons") or ():
                url = button.get("url") if isinstance(button, Mapping) else None
                if isinstance(url, str) and _BULK_MARKER.search(url):
                    dynamic_url = True
    return {"markers": len(body_markers), "header_format": header_format, "header_markers": header_markers,
            "dynamic_url": dynamic_url}


def _bulk_refused(code: str, field_name: str, problem: str | None = None) -> EntryRefused:
    reason: dict[str, Any] = {"field": field_name}
    if problem is not None:
        reason["problem"] = problem
    return EntryRefused(code, reason)


def _bulk_pick_template(catalog: TemplateCatalog, name: str, language: str | None) -> Mapping[str, Any]:
    named = catalog.named(name)
    if language is not None:
        named = tuple(t for t in named if isinstance(t.get("language"), str) and t["language"].strip() == language)
    if not named:
        raise _bulk_refused("bulk_send_template_not_found", "template_name")
    languages = {t["language"].strip() for t in named if isinstance(t.get("language"), str)}
    if len(languages) != 1:
        raise _bulk_refused("bulk_send_template_language_ambiguous", "template_language")
    approved = [t for t in named if t.get("status") == "APPROVED"]
    if not approved:
        raise _bulk_refused("bulk_send_template_not_approved", "template_name")
    return approved[0]


def _bulk_variables(value: Any, markers: int) -> list[Any]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise _bulk_refused("bulk_send_variables_invalid", "template_variables", "not_list")
    variables: list[Any] = []
    for entry in value:
        if isinstance(entry, Mapping) and dict(entry) == _BULK_FIRST_NAME:
            variables.append(dict(_BULK_FIRST_NAME))
        elif isinstance(entry, str) and 1 <= len(entry.strip()) <= BULK_VARIABLE_MAX_CHARS:
            if looks_like_identifier(entry):
                raise _bulk_refused("identifier_not_allowed", "template_variables")
            variables.append(entry.strip())
        else:
            raise _bulk_refused("bulk_send_variables_invalid", "template_variables", "text_or_first_name")
    if len(variables) != markers:
        raise _bulk_refused("bulk_send_variables_mismatch", "template_variables",
                            "fewer_than_markers" if len(variables) < markers else "more_than_markers")
    return variables


def _bulk_schedule(value: Any, now: datetime) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise _bulk_refused("bulk_send_schedule_invalid", "scheduled_at", "not_iso")
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise _bulk_refused("bulk_send_schedule_invalid", "scheduled_at", "not_iso") from None
    if moment.tzinfo is None:
        raise _bulk_refused("bulk_send_schedule_invalid", "scheduled_at", "without_zone")
    if moment < now + BULK_SCHEDULE_MIN_AHEAD:
        raise _bulk_refused("bulk_send_schedule_invalid", "scheduled_at", "less_than_1_hour_ahead")
    if moment > now + BULK_SCHEDULE_MAX_AHEAD:
        raise _bulk_refused("bulk_send_schedule_invalid", "scheduled_at", "more_than_30_days_ahead")
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _bulk_products(value: Any, known: frozenset[str]) -> list[str]:
    if value is None:
        if len(known) == 1:
            return sorted(known)
        raise _bulk_refused("bulk_send_product_required", "product_refs")
    if (not isinstance(value, list) or not 1 <= len(value) <= 20
            or not all(isinstance(p, str) and _BULK_PRODUCT.fullmatch(p) for p in value)
            or len(set(value)) != len(value)):
        raise _bulk_refused("bulk_send_product_invalid", "product_refs", "list_of_products")
    if not set(value) <= known:
        raise _bulk_refused("bulk_send_product_invalid", "product_refs", "not_of_this_instance")
    return list(value)


def _bulk_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {key: count for key, count in value.items()
            if isinstance(key, str) and _BULK_COUNT_KEY.fullmatch(key) and type(count) is int and count >= 0}


def bulk_send_ae_view(view: Mapping[str, Any], *, origin: str) -> dict[str, Any]:
    """Lo que el agente ve de un envio: los datos del envio y los conteos. Nunca un telefono ni un ID de persona."""
    out: dict[str, Any] = {key: view.get(key) for key in _BULK_SEND_KEYS}
    variables = out.get("template_variables")
    out["template_variables"] = [v for v in variables if isinstance(v, str) or v == _BULK_FIRST_NAME] \
        if isinstance(variables, list) else []
    listed = view.get("list")
    if isinstance(listed, Mapping):
        out["list"] = {key: listed.get(key) for key in _BULK_LIST_KEYS}
        out["list"]["rejected_counts"] = _bulk_counts(listed.get("rejected_counts"))
        out["list"]["excluded_by_reason"] = _bulk_counts(listed.get("excluded_by_reason"))
    else:
        out["list"] = None
    out["recipients_by_state"] = _bulk_counts(view.get("recipients_by_state"))
    send_id = _uuid_or_none(view.get("send_id"))
    out["send_id"] = send_id
    # El link de la pagina del masivo: para subir la lista o, ya subida, para revisarla y confirmar.
    out["page_url"] = bulk_send_page_url(origin, send_id) if send_id is not None else None
    return out


class BulkSendStore(FlowChangeStore):
    """Las tres funciones del masivo que usa el agente (20261125000200), con la clave de ``service_role``."""

    async def create_bulk_send(self, instance: Mapping[str, str], send: Mapping[str, Any]) -> dict[str, Any]:
        return await self._rpc("create_bulk_send_v1", {"p_instance": instance, "p_send": send})

    async def read_bulk_send(self, instance: Mapping[str, str], send_id: str) -> dict[str, Any]:
        return await self._rpc("read_bulk_send_v1", {"p_instance": instance, "p_send_id": send_id})

    async def read_bulk_sends(self, instance: Mapping[str, str], *, limit: int) -> list[Any]:
        listed = await self._rpc("read_bulk_sends_v1", {"p_instance": instance, "p_limit": limit}, list_ok=True)
        if not isinstance(listed, list):
            raise StoreUnavailable()
        return listed


def check_bulk_send_settings(settings: Any) -> None:
    """Lo que exige ``BULK_SEND_ENABLED`` al arrancar: las rutas del AE, la pagina (el link) y la base."""
    if not getattr(settings, "bulk_send_enabled", False):
        return
    if not getattr(settings, "automation_expert_change_enabled", False):
        raise ValueError(f"{BULK_SEND_ENV} requires AUTOMATION_EXPERT_CHANGE_ENABLED")
    if not getattr(settings, "flow_approval_enabled", False) or not getattr(settings, "flow_approval_base_url", None):
        raise ValueError(f"{BULK_SEND_ENV} requires FLOW_APPROVAL_ENABLED and FLOW_APPROVAL_BASE_URL")
    if getattr(settings, "instance_manifest", None) is None:
        raise ValueError(f"{BULK_SEND_ENV} requires an instance manifest")
    if not getattr(settings, "supabase_base_url", None) or not getattr(settings, "supabase_service_role_key", None):
        raise ValueError(f"{BULK_SEND_ENV} requires SUPABASE_BASE_URL and SUPABASE_SERVICE_ROLE_KEY")


def register_bulk_send_routes(app: FastAPI, *, token: str, store: BulkSendStore, chatwoot: Any, tenant_ref: str,
                              ally_ref: str, inbox_id: int, product_refs: frozenset[str], approval_origin: str,
                              git_sha: str | None = None, clock: Callable[[], datetime] | None = None,
                              warmup: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None
                              ) -> None:
    """Las tres rutas del masivo, detras del flag (lo mira quien llama: apagado no existen).

    ``product_refs``: los productos de la instancia (los ``funnel_ref`` del manifiesto); el masivo sale
    para uno o varios de ellos y los vetos sacan a quien ya los compro. ``approval_origin``: el dominio de
    la pagina de aprobacion, para el link de la lista. ``warmup``: el calentamiento del numero
    (``BulkSendWarmup.view_for``, F2); sin el no se suma nada.
    """
    instance = {"tenant_ref": tenant_ref, "ally_ref": ally_ref}
    sha = git_sha or os.environ.get("GIT_SHA", "") or "unknown"
    now = clock or (lambda: datetime.now(UTC))
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
        return {"contract": CONTRACT, "contract_version": CONTRACT_VERSION, "instance": dict(instance),
                "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "bridge_git_sha": sha, "data": dict(data)}

    def failure(error: Exception, route: str, send_id: str | None = None) -> HTTPException:
        if isinstance(error, StoreRejected) and error.code in _BULK_RAISED:
            code, http_status = error.code, error.http_status
        elif isinstance(error, StoreUnavailable):
            code, http_status = error.code, 503
        else:
            code, http_status = "automation_expert_change_unavailable", 503
        logger.info("bulk_send_ae route=%s send_id=%s status=%s code=%s", route, send_id, http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    async def with_warmup(view: Mapping[str, Any]) -> dict[str, Any]:
        out = bulk_send_ae_view(view, origin=approval_origin)
        if warmup is None:
            return out
        try:
            out["warmup"] = dict(await warmup(view))
        except Exception as error:  # noqa: BLE001 - el AE dice que no se pudo leer; la pagina no confirma igual
            logger.info("bulk_send_ae warmup=unavailable error=%s", type(error).__name__)
            out["warmup"] = {"error": "warmup_unavailable"}
        return out

    def send_id_of(value: str) -> str:
        try:
            return str(UUID(value))
        except (ValueError, AttributeError):
            raise HTTPException(status_code=422, detail="invalid_bulk_send_id") from None

    @app.post(BULK_SENDS, status_code=201)
    async def propose_bulk_send(request: Request, authorization: str | None = Header(default=None)) -> Any:
        """El borrador de un masivo. No sube la lista, no confirma y no manda."""
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"template_name", "scheduled_at", "requested_by"},
                           allowed={"template_name", "template_language", "template_variables", "header_image_url",
                                    "scheduled_at", "campaign_label", "product_refs", "window_mode", "requested_by"})
        _requested_by(body)
        name, language = body["template_name"], body.get("template_language")
        if not isinstance(name, str) or not _BULK_TEMPLATE_NAME.fullmatch(name):
            raise _bulk_refused("bulk_send_template_not_found", "template_name")
        if language is not None and (not isinstance(language, str) or not _BULK_LANGUAGE.fullmatch(language)):
            raise _bulk_refused("bulk_send_invalid", "template_language")
        scheduled_at = _bulk_schedule(body["scheduled_at"], now())
        products = _bulk_products(body.get("product_refs"), product_refs)
        label = body.get("campaign_label")
        if label is not None and (not isinstance(label, str) or not _BULK_LABEL.fullmatch(label)):
            raise _bulk_refused("bulk_send_invalid", "campaign_label")
        window = body.get("window_mode", "centrada")
        if window not in _BULK_WINDOW_MODES:
            raise _bulk_refused("bulk_send_invalid", "window_mode")
        image = body.get("header_image_url")
        if image is not None and (not isinstance(image, str) or not image.startswith("https://")
                                  or len(image) > 2048 or any(c.isspace() for c in image)):
            raise _bulk_refused("bulk_send_image_invalid", "header_image_url", "https")
        busy()
        async with slots:
            try:
                inbox = await chatwoot.get_inbox(inbox_id=inbox_id)
                catalog = TemplateCatalog.from_inbox_payload({"message_templates": inbox["message_templates"]})
            except Exception:  # noqa: BLE001 - sin catalogo no se sabe si la plantilla tiene la baja
                logger.info("bulk_send_ae route=propose status=503 code=template_catalog_unavailable")
                raise HTTPException(status_code=503, detail="template_catalog_unavailable") from None
            template = _bulk_pick_template(catalog, name, language)
            # La regla del boton de baja (pieza 11): sin «No más mensajes» no hay masivo.
            if not template_has_opt_out_button(template):
                raise _bulk_refused("bulk_send_template_without_opt_out", "template_name", "no_opt_out_quick_reply")
            shape = _bulk_template_shape(template)
            if shape["header_format"] not in (None, "TEXT", "IMAGE"):
                raise _bulk_refused("bulk_send_template_header_unsupported", "template_name")
            if shape["header_markers"] or shape["dynamic_url"]:
                raise _bulk_refused("bulk_send_template_per_person_parts", "template_name",
                                    "header_or_link_changes_per_person")
            if shape["header_format"] == "IMAGE" and image is None:
                raise _bulk_refused("bulk_send_image_required", "header_image_url")
            if shape["header_format"] != "IMAGE" and image is not None:
                raise _bulk_refused("bulk_send_image_not_allowed", "header_image_url")
            variables = _bulk_variables(body.get("template_variables"), shape["markers"])
            send: dict[str, Any] = {
                "template_name": name, "template_language": template["language"].strip(),
                "template_variables": variables, "scheduled_at": scheduled_at, "window_mode": window,
                "product_refs": products, "created_by": "automation_expert"}
            if image is not None:
                send["header_image_url"] = image
            if label is not None:
                send["campaign_label"] = label
            try:
                created = await store.create_bulk_send(instance, send)
                send_id = _uuid_or_none(created.get("send_id")) if created.get("ok") is True else None
                if send_id is None:
                    raise StoreUnavailable()
                view = await store.read_bulk_send(instance, send_id)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "propose") from None
        logger.info("bulk_send_ae route=propose send_id=%s status=201 variables=%s products=%s", send_id,
                    len(variables), len(products))
        return envelope({**await with_warmup(view),
                         "upload_url": bulk_send_page_url(approval_origin, send_id),
                         "upload_note": BULK_SEND_UPLOAD_NOTE})

    @app.get(BULK_SENDS)
    async def list_bulk_sends(limit: str = str(BULK_LIST_DEFAULT_LIMIT),
                              authorization: str | None = Header(default=None)) -> Any:
        """Los masivos de la instancia, del mas nuevo al mas viejo. Solo lee."""
        authenticate(authorization)
        if not (limit.isascii() and limit.isdigit()) or not 1 <= int(limit) <= BULK_LIST_MAX_LIMIT:
            raise EntryRefused("invalid_query", {"field": "limit", "problem": "between_1_and_50"})
        busy()
        async with slots:
            try:
                listed = await store.read_bulk_sends(instance, limit=int(limit))
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "list") from None
        sends = [await with_warmup(view) for view in listed if isinstance(view, Mapping)]
        logger.info("bulk_send_ae route=list status=200 sends=%s", len(sends))
        return envelope({"bulk_sends": sends})

    @app.get(f"{BULK_SENDS}/{{send_id}}")
    async def get_bulk_send(send_id: str, authorization: str | None = Header(default=None)) -> Any:
        """Un masivo: el estado y los conteos. Solo lee."""
        authenticate(authorization)
        checked = send_id_of(send_id)
        busy()
        async with slots:
            try:
                view = await store.read_bulk_send(instance, checked)
            except (StoreUnavailable, StoreRejected) as error:
                raise failure(error, "read", checked) from None
        logger.info("bulk_send_ae route=read send_id=%s status=200", checked)
        return envelope(await with_warmup(view))


def _uuid_or_none(value: Any) -> str | None:
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None
