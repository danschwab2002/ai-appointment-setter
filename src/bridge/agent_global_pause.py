"""Pausar al agente comercial para todas, por etiqueta o por producto (agent-global-pause-v1).

Item 5.4 de la lista definitiva (D4 y D10 del mapa Z1 del 2026-11-26). El
Automation Expert pone y saca la pausa a pedido de una persona de la lista de
aprobadores (``FLOW_APPROVAL_SLACK_APPROVERS``): frenar no manda nada, asi que
no pasa por la pagina. La base guarda quien la pidio y el motivo.

Mientras una pausa esta viva, el turno del bridge no llama al agente en las
conversaciones que alcanza (todas, las que tienen una etiqueta de Chatwoot, o
las de un producto por su etiqueta de producto): el mismo efecto que la
etiqueta ``automation_paused`` en una conversacion, sin ponersela a cada una.
La conversacion queda abierta en Chatwoot con el mensaje sin contestar, para el
equipo; el bridge anota cuales retuvo. Al sacar la pausa no se contesta para
atras: el agente contesta el proximo mensaje. Un mensaje escrito durante la
pausa tampoco se contesta despues (D-54.4): el monitor de conversaciones
trabadas lo vuelve a admitir y, sin la etiqueta, el agente lo contestaria.

Frena solo al agente conversacional. Los flujos (que tienen su pausa, 5.2), el
seguimiento, la reactivacion y el masivo siguen con sus propios frenos.

Piezas:

* ``LivePauses``: las pausas vivas de la base, con cache corta. Si la base no
  contesta se queda con lo ultimo bueno (el patron de ``LiveSegmentCatalog``).
* ``register_agent_pause_routes``: las rutas del AE (poner, sacar, ver).

Detras de ``AGENT_GLOBAL_PAUSE_ENABLED`` (apagado por defecto: no se lee la base
y las rutas no existen).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request, status

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
from .instance_manifest import RESERVED_CHATWOOT_LABELS

__all__ = [
    "CONTRACT",
    "CONTRACT_VERSION",
    "ENV",
    "LivePause",
    "LivePauses",
    "PauseStore",
    "check_settings",
    "message_at",
    "parse_live_pauses",
    "register_agent_pause_routes",
]

logger = logging.getLogger(__name__)

CONTRACT = "agent-global-pause-v1"
CONTRACT_VERSION = "1.0.0"
ENV = "AGENT_GLOBAL_PAUSE_ENABLED"
# D-54.3: diez segundos. Una pausa puesta desde este bridge se aplica en el
# acto (la ruta relee); la cache corta es para otro proceso y para el `hasta`.
REFRESH_SECONDS = 10.0
SCOPES = ("todas", "etiqueta", "producto")
# D-54.5: `hasta` es opcional y, si va, entre 5 minutos y 7 dias.
UNTIL_MIN = timedelta(minutes=5)
UNTIL_MAX = timedelta(days=7)
LIST_LIMIT = 20
_VALUE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RESULT_STATUS = {"agent_pause_open_limit": 409}


def check_settings(settings: Any) -> None:
    """Lo que exige el flag al arrancar. Sin la lista de aprobadores nadie podria pausar."""
    if not getattr(settings, "agent_global_pause_enabled", False):
        return
    if getattr(settings, "instance_manifest", None) is None:
        raise ValueError(f"{ENV} requires an instance manifest")
    if not getattr(settings, "supabase_base_url", None) or not getattr(settings, "supabase_service_role_key", None):
        raise ValueError(f"{ENV} requires SUPABASE_BASE_URL and SUPABASE_SERVICE_ROLE_KEY")
    if not getattr(settings, "automation_expert_change_enabled", False):
        raise ValueError(f"{ENV} requires AUTOMATION_EXPERT_CHANGE_ENABLED")
    if not getattr(settings, "flow_approval_enabled", False):
        raise ValueError(f"{ENV} requires FLOW_APPROVAL_ENABLED (the approvers list)")


# ---------------------------------------------------------------------------
# Lo que lee el turno.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LivePause:
    """Una pausa viva como la sirve ``get_live_agent_global_pauses_v1``."""

    pause_id: str
    scope: str
    scope_value: str | None
    match_label: str | None
    created_at: datetime
    # Cuando termino o termina (levantada, o su `hasta`); None, sin fin.
    ends_at: datetime | None

    def alive(self, now: datetime) -> bool:
        # Sin mirar created_at: una pausa recien puesta no espera a que los
        # relojes de la base y del bridge coincidan.
        return self.ends_at is None or now < self.ends_at

    def covers(self, moment: datetime) -> bool:
        """El mensaje se escribio mientras la pausa estaba puesta."""
        return self.created_at <= moment and self.alive(moment)

    def reaches(self, *, labels: Iterable[str], product_keys: Iterable[str]) -> bool:
        """Si la conversacion entra en el alcance.

        ``producto`` alcanza por la etiqueta del producto que ya tiene la
        conversacion o por el producto que el bridge le asigno en este turno
        (la etiqueta se pone por un macro y el webhook trae las de antes).
        """
        if self.scope == "todas":
            return True
        if self.scope == "etiqueta":
            return self.match_label in set(labels)
        return self.match_label in set(labels) or self.scope_value in set(product_keys)


def _when(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(field)
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(field)
    return moment.astimezone(UTC)


def message_at(payload: Mapping[str, Any]) -> datetime | None:
    """Cuando escribio la persona: el ``created_at`` del webhook ``message_created``.

    Chatwoot lo manda como texto ISO con zona en la raiz (captura
    ``chatwoot_message_created_audio_inbox_9_conv_200_20260928.json``); se
    acepta tambien un epoch. Otra cosa es None.
    """
    value = payload.get("created_at") if isinstance(payload, Mapping) else None
    try:
        if isinstance(value, str):
            return _when(value, "created_at")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return datetime.fromtimestamp(value, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return None


def parse_live_pauses(payload: Mapping[str, Any]) -> tuple[LivePause, ...]:
    """La lectura de la base, entera o nada: una fila sin la forma invalida todo."""
    rows = payload.get("pauses") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("pauses")
    out: list[LivePause] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("pause")
        pause_id = str(UUID(str(row.get("pause_id"))))
        scope = row.get("scope")
        value, label = row.get("scope_value"), row.get("match_label")
        if scope not in SCOPES:
            raise ValueError("scope")
        if scope == "todas":
            if value is not None or label is not None:
                raise ValueError("scope_value")
        elif not (isinstance(value, str) and _VALUE.fullmatch(value) and isinstance(label, str)
                  and _VALUE.fullmatch(label)):
            raise ValueError("scope_value")
        ends_at = row.get("ends_at")
        out.append(LivePause(pause_id=pause_id, scope=scope, scope_value=value, match_label=label,
                             created_at=_when(row.get("created_at"), "created_at"),
                             ends_at=None if ends_at is None else _when(ends_at, "ends_at")))
    return tuple(out)


# El orden en que se elige la pausa que retiene una conversacion: la mas ancha.
_SCOPE_ORDER = {"todas": 0, "producto": 1, "etiqueta": 2}


class LivePauses:
    """Las pausas vivas de la instancia, con cache corta y lo ultimo bueno si la base no contesta.

    Sin ninguna lectura buena (la base caida desde el arranque) no hay pausa
    conocida y el turno sigue como siempre: la admision del turno tambien
    necesita la base, y ``/ready`` lo dice (``never_read``).
    """

    def __init__(self, *, fetch: Callable[[], Awaitable[Mapping[str, Any]]],
                 refresh_seconds: float = REFRESH_SECONDS,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._fetch = fetch
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._pauses: tuple[LivePause, ...] = ()
        self._read = False
        self._failures = 0
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def pauses(self) -> tuple[LivePause, ...]:
        return self._pauses

    async def refresh(self) -> bool:
        """Lee la base. Nunca lanza. False si no se pudo leer (queda lo ultimo bueno)."""
        async with self._lock:
            try:
                pauses = parse_live_pauses(await self._fetch())
            except Exception as error:  # noqa: BLE001 - la base caida no tira el turno de nadie
                self._failures += 1
                logger.warning("agent_global_pause_refresh_failed failures=%s error=%s", self._failures,
                               type(error).__name__)
                return False
            before = {pause.pause_id for pause in self._pauses}
            after = {pause.pause_id for pause in pauses}
            for pause in pauses:
                if pause.pause_id not in before:
                    logger.info("agent_global_pause_live pause=%s scope=%s value=%s", pause.pause_id, pause.scope,
                                pause.scope_value)
            for pause_id in before - after:
                logger.info("agent_global_pause_gone pause=%s", pause_id)
            self._pauses = pauses
            self._read = True
            self._failures = 0
            return True

    def holding(self, *, labels: Iterable[str], product_keys: Iterable[str] = (),
                now: datetime | None = None, message_at: datetime | None = None) -> LivePause | None:
        """La pausa que retiene este mensaje, o None.

        Lo retiene una pausa que alcanza a la conversacion y que esta viva
        ahora (el `hasta` se mira con el reloj del bridge) o que estaba puesta
        cuando la persona lo escribio (``message_at``).
        """
        moment = now or self._clock()
        labels, product_keys = tuple(labels), tuple(product_keys)
        reaching = [pause for pause in self._pauses
                    if (pause.alive(moment) or (message_at is not None and pause.covers(message_at)))
                    and pause.reaches(labels=labels, product_keys=product_keys)]
        if not reaching:
            return None
        return min(reaching, key=lambda pause: (_SCOPE_ORDER[pause.scope], pause.created_at))

    def status_line(self, now: datetime | None = None) -> str:
        """Para ``/ready``: si hay una pausa viva y de que alcance, y como viene la lectura."""
        moment = now or self._clock()
        alive = [pause for pause in self._pauses if pause.alive(moment)]
        if not self._read:
            state = "never_read"
        elif self._failures:
            state = f"failing:{self._failures}"
        else:
            state = "ok"
        if not alive:
            return f"none,refresh={state}"
        scopes = ",".join(sorted({pause.scope for pause in alive}, key=lambda scope: _SCOPE_ORDER[scope]))
        return f"paused:{len(alive)}:{scopes},refresh={state}"

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="agent-global-pause-refresh")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_seconds)
            await self.refresh()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None


# ---------------------------------------------------------------------------
# Las rutas del Automation Expert.
# ---------------------------------------------------------------------------


class PauseStore(FlowChangeStore):
    """Las tres funciones del agente de 20261126100000, con la clave de su rol."""

    async def pause(self, instance: Mapping[str, str], *, scope: str, scope_value: str | None,
                    match_label: str | None, until: str | None, reason: str, requested_by: str) -> dict[str, Any]:
        return await self._rpc("pause_agent_globally_v1", {
            "p_instance": instance, "p_scope": scope, "p_scope_value": scope_value, "p_match_label": match_label,
            "p_until": until, "p_reason": reason, "p_requested_by_ref": requested_by})

    async def resume(self, instance: Mapping[str, str], *, pause_id: str, reason: str | None,
                     requested_by: str) -> dict[str, Any]:
        return await self._rpc("resume_agent_globally_v1", {
            "p_instance": instance, "p_pause_id": pause_id, "p_reason": reason,
            "p_requested_by_ref": requested_by})

    async def list_pauses(self, instance: Mapping[str, str], *, limit: int) -> dict[str, Any]:
        return await self._rpc("list_agent_global_pauses_v1", {"p_instance": instance, "p_limit": limit})


def _pause_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="invalid_pause_id") from None


def _free_text(body: Mapping[str, Any], field: str, *, required: bool) -> str | None:
    value = body.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 500:
        raise EntryRefused("invalid_body", {"field": field, "problem": "length_1_to_500"})
    if looks_like_identifier(value):
        raise EntryRefused("identifier_not_allowed", {"field": field, "problem": "phone_or_mail"})
    return value.strip()


def _until(value: object, now: datetime) -> str | None:
    if value is None:
        return None
    try:
        moment = _when(value, "hasta")
    except ValueError:
        raise EntryRefused("invalid_body", {"field": "hasta", "problem": "not_iso_with_offset"}) from None
    if not now + UNTIL_MIN <= moment <= now + UNTIL_MAX:
        raise EntryRefused("agent_pause_until_invalid", {"field": "hasta", "problem": "5_minutes_to_7_days"})
    return moment.isoformat().replace("+00:00", "Z")


def _view(pause: Mapping[str, Any]) -> dict[str, Any]:
    """La pausa para el AE, en castellano y sin nada que no sea de la pausa."""
    return {
        "pause_id": pause.get("pause_id"),
        "alcance": pause.get("scope"),
        "valor": pause.get("scope_value"),
        "etiqueta": pause.get("match_label"),
        "estado": pause.get("status"),
        "desde": pause.get("created_at"),
        "hasta": pause.get("until"),
        "motivo": pause.get("reason"),
        "pedida_por": pause.get("requested_by"),
        "levantada_at": pause.get("lifted_at"),
        "levantada_por": pause.get("lifted_by"),
        "motivo_al_levantar": pause.get("lift_reason"),
        "conversaciones_retenidas": pause.get("held_conversations"),
        "mensajes_retenidos": pause.get("held_messages"),
    }


def register_agent_pause_routes(
    app: FastAPI, *, token: str, store: PauseStore, approvers: frozenset[str], tenant_ref: str, ally_ref: str,
    product_labels: Mapping[str, str | None], live: LivePauses,
    known_labels: Callable[[], Awaitable[Sequence[str]]] | None = None, git_sha: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Las rutas, detras del flag (lo mira quien llama: con el flag apagado no existen).

    ``product_labels``: la clave de cada producto del manifiesto y su etiqueta
    (None si no declara una: ese producto no se puede pausar solo).
    ``known_labels``: las etiquetas de la cuenta de Chatwoot, si el bridge las
    puede leer; sin eso, una etiqueta se acepta por su forma y la respuesta lo
    avisa (``agent_pause_label_not_verified``).
    """
    instance = {"tenant_ref": tenant_ref, "ally_ref": ally_ref}
    sha = git_sha or os.getenv("GIT_SHA", "") or "unknown"

    def authenticate(authorization: str | None) -> None:
        expected = f"Bearer {token}"
        if authorization is None or not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="automation_expert_change_authentication_required",
                                headers={"WWW-Authenticate": "Bearer"})

    def approver(body: Mapping[str, Any], route: str) -> str:
        # D-54.1: la identidad la pone el plugin desde la sesion de Slack (nunca
        # el modelo); el bridge la compara con la lista de la pagina.
        user = _requested_by(body)
        if user not in approvers:
            logger.info("agent_global_pause route=%s status=403 code=not_an_approver", route)
            raise HTTPException(status_code=403, detail="not_an_approver")
        return user

    def envelope(data: Mapping[str, Any]) -> dict[str, Any]:
        return {"contract": CONTRACT, "contract_version": CONTRACT_VERSION, "instance": dict(instance),
                "generated_at": clock().isoformat().replace("+00:00", "Z"), "bridge_git_sha": sha,
                "data": dict(data)}

    def failure(error: Exception, route: str) -> HTTPException:
        if isinstance(error, StoreRejected):
            code, http_status = error.code, error.http_status
        elif isinstance(error, StoreUnavailable):
            code, http_status = error.code, 503
        else:  # pragma: no cover - lo que no se espera, cerrado
            code, http_status = "automation_expert_change_unavailable", 503
        logger.info("agent_global_pause route=%s status=%s code=%s", route, http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    def outcome(result: Mapping[str, Any], route: str) -> Mapping[str, Any]:
        if result.get("ok") is True and isinstance(result.get("pause"), Mapping):
            return result
        code = result.get("reason_code")
        if isinstance(code, str) and code in _RESULT_STATUS:
            logger.info("agent_global_pause route=%s status=409 code=%s", route, code)
            raise HTTPException(status_code=_RESULT_STATUS[code], detail=code)
        raise failure(StoreUnavailable(), route)

    async def scope_of(body: Mapping[str, Any]) -> tuple[str, str | None, str | None, list[dict[str, str]]]:
        scope = body.get("alcance")
        value = body.get("valor")
        if scope not in SCOPES:
            raise EntryRefused("invalid_body", {"field": "alcance", "problem": "todas_etiqueta_o_producto"})
        if scope == "todas":
            if value is not None:
                raise EntryRefused("invalid_body", {"field": "valor", "problem": "not_with_todas"})
            return scope, None, None, []
        if not isinstance(value, str) or not _VALUE.fullmatch(value):
            raise EntryRefused("invalid_body", {"field": "valor", "problem": "label_or_product_key"})
        if scope == "producto":
            if value not in product_labels:
                raise EntryRefused("agent_pause_product_unknown", {"field": "valor", "problem": "not_in_manifest"})
            label = product_labels[value]
            if label is None:
                # D-54.6: sin etiqueta de producto no hay como saber de que
                # producto es una conversacion; se pausa con «todas».
                raise EntryRefused("agent_pause_product_without_label", {"field": "valor", "problem": "no_label"})
            return scope, value, label, []
        if value in RESERVED_CHATWOOT_LABELS:
            raise EntryRefused("chatwoot_label_reserved", {"field": "valor", "problem": "reserved"})
        if known_labels is None:
            return scope, value, value, [{"code": "agent_pause_label_not_verified"}]
        try:
            labels = set(await known_labels())
        except Exception:  # noqa: BLE001 - sin la lista no se pausa a ciegas una etiqueta escrita a mano
            logger.info("agent_global_pause route=pause status=503 code=chatwoot_tool_unavailable")
            raise HTTPException(status_code=503, detail="chatwoot_tool_unavailable") from None
        if value not in labels:
            # D-54.7: una etiqueta mal escrita dejaria al agente contestando
            # mientras quien pidio la pausa cree que esta frenado.
            raise EntryRefused("chatwoot_label_not_found", {"field": "valor", "problem": "not_in_chatwoot"})
        return scope, value, value, []

    @app.post(f"{PREFIX}/agent-pause", status_code=201)
    async def pause(request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"alcance", "motivo", "requested_by"},
                           allowed={"alcance", "valor", "hasta", "motivo", "requested_by"})
        requested_by = approver(body, "pause")
        reason = _free_text(body, "motivo", required=True)
        assert reason is not None
        until = _until(body.get("hasta"), clock())
        scope, value, label, warnings = await scope_of(body)
        try:
            result = await store.pause(instance, scope=scope, scope_value=value, match_label=label, until=until,
                                       reason=reason, requested_by=requested_by)
        except (StoreRejected, StoreUnavailable) as error:
            raise failure(error, "pause") from None
        result = outcome(result, "pause")
        # El efecto en este proceso, ya: no espera a la proxima lectura.
        applied = await live.refresh()
        logger.info("agent_global_pause route=pause status=201 scope=%s replayed=%s", scope,
                    result.get("replayed") is True)
        return envelope({"pause": _view(result["pause"]), "replayed": result.get("replayed") is True,
                         "aplicada_en_este_bridge": applied, "warnings": warnings})

    @app.post(f"{PREFIX}/agent-pause/{{pause_id}}/resume")
    async def resume(pause_id: str, request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        pause_ref = _pause_id(pause_id)
        body = _check_keys(await _body(request), required={"requested_by"}, allowed={"motivo", "requested_by"})
        requested_by = approver(body, "resume")
        reason = _free_text(body, "motivo", required=False)
        try:
            result = await store.resume(instance, pause_id=pause_ref, reason=reason, requested_by=requested_by)
        except (StoreRejected, StoreUnavailable) as error:
            raise failure(error, "resume") from None
        result = outcome(result, "resume")
        applied = await live.refresh()
        logger.info("agent_global_pause route=resume status=200 replayed=%s", result.get("replayed") is True)
        return envelope({"pause": _view(result["pause"]), "replayed": result.get("replayed") is True,
                         "aplicada_en_este_bridge": applied})

    @app.get(f"{PREFIX}/agent-pause")
    async def state(authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        try:
            result = await store.list_pauses(instance, limit=LIST_LIMIT)
        except (StoreRejected, StoreUnavailable) as error:
            raise failure(error, "state") from None
        rows = result.get("pauses")
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise failure(StoreUnavailable(), "state")
        views = [_view(row) for row in rows]
        live_now = [view for view in views if view["estado"] == "activa"]
        return envelope({"pausado": bool(live_now), "vivas": live_now,
                         "recientes": [view for view in views if view["estado"] != "activa"],
                         "productos": sorted(key for key, label in product_labels.items() if label is not None),
                         "este_bridge": live.status_line()})
