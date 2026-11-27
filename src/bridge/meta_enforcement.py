"""Los frenos ante Meta (meta-enforcement-brakes-v1, F1 del mapa del 2026-11-27).

Contrato: ``docs/contracts/meta-enforcement-brakes-v1.md``. Diseno: decisiones D2 a
D6 del mapa «Frenos ante Meta y calentamiento» del vault.

Tres cosas que Meta le hace a la cuenta de WhatsApp y que el bridge no veia:

* **Recategoriza una plantilla** aprobada de utilidad a marketing (o a
  autenticacion). La plantilla sigue APPROVED y el motor nativo con permiso
  ``formulario`` la seguia mandando a precio de marketing (H6 del mapa). Desde
  ahora queda **retirada**: no sale por ningun camino y el paso se comporta como
  con una plantilla no aprobada, con el motivo ``template_recategorized``. El aviso
  previo de Meta (``correct_category``) solo avisa. La libera una persona.
* **Cuenta las recategorizaciones seguidas** (D4): desde la segunda, el pedido
  nuevo de una plantilla de UTILIDAD del Automation Expert se frena
  (``meta_recategorization_streak``); marketing sigue.
* **Restringe la cuenta** (``account_update``): la instancia queda **congelada**
  y ningun envio iniciado por el bridge sale (``meta_account_restricted``). Las
  respuestas del agente en una conversacion abierta siguen. No se descongela
  solo, ni con el UNBAN: lo levanta una persona.

Dos entradas, un registro (D2): el sondeo (``MetaEnforcementWorker``, cada 15
minutos, la lista de plantillas de la cuenta con ``category`` y
``correct_category``) y la ruta ``/webhooks/meta`` (firma obligatoria). Lo que
decide lo decide la base (migracion 20261127100000).

**Donde frena.** Todos los envios que inicia el bridge leen el catalogo de la
bandeja con ``ChatwootClient.get_inbox`` antes de mandar (el legado, el motor
nativo, los seguimientos, la reactivacion, el primer contacto). Esos componentes
reciben un cliente con ``with_catalog_filter(live.braked_catalog)``: la plantilla
retirada (o todas, con la instancia congelada) llega marcada con
``approved_templates.BRAKE_KEY`` y estado ``BRAKED_STATUS``.
``parse_approved_template`` la rechaza con el motivo del freno y los demas
lectores del catalogo, como a una no aprobada. El masivo no lee el catalogo de
Chatwoot: consulta ``live.initiated_brake`` antes de arrancar y antes de cada
lote. Las respuestas del agente usan el cliente sin filtro.

Decisiones (default, sin aprobacion pedida; van tambien en el reporte):

* **D-F1.1** La primera lectura del sondeo es linea de base (migracion).
* **D-F1.2** Solo retira UTILITY a MARKETING/AUTHENTICATION ya hecho (migracion).
* **D-F1.3** Que cuenta el contador y que lo reinicia (migracion).
* **D-F1.4** Lo repetido en 7 dias no se registra dos veces (migracion).
* **D-F1.5** Que ``account_update`` congela (migracion): todo ACCOUNT_RESTRICTION
  salvo UNBAN y RATE_LIMIT_RECOVERY, que solo avisan.
* **D-F1.6** Sin una lectura buena del estado (la base caida desde el arranque)
  no hay freno conocido y los envios siguen como hoy; ``/ready`` lo dice
  (``never_read``). Despues, se queda con lo ultimo bueno.
* **D-F1.7** La ruta del webhook procesa en linea con un tope de 8 segundos: si
  la base no contesta devuelve 503 y Meta reintenta, en vez de contestar 200 y
  perder el evento.
* **D-F1.8** El idioma del webhook viene como ``en-US`` (doc de Meta) y el del
  catalogo como ``en_US``: se normaliza con ``_``.
* **D-F1.9** Las acciones a mano van por ``/internal/automation-expert/v1/meta-
  enforcement`` con el token del Automation Expert y una persona de la lista de
  aprobadores de la pagina (``FLOW_APPROVAL_SLACK_APPROVERS``), el mecanismo de la
  pausa global (5.4). Existen solo con el flag, ``AUTOMATION_EXPERT_CHANGE_ENABLED``
  y ``FLOW_APPROVAL_ENABLED``; sin ellos ``/ready`` dice ``manual=sin_aprobadores``.
  La base las escribe con ``service_role`` (el bridge ya autentico a la persona).
* **D-F1.10** Con ``META_WEBHOOK_VERIFY_TOKEN`` y ``META_APP_SECRET`` la ruta
  existe; sin ninguno, no; con uno solo, el bridge no arranca (configuracion a
  medias).
* **D-F1.11** El aviso sale por el bot del Automation Expert
  (``META_TEMPLATES_SLACK_BOT_TOKEN``) en el canal de la aliada (``[slack].canal``),
  el mecanismo de ``template_status.py``. Sin token o sin canal, los avisos
  esperan en la base.

El log lleva codigos y conteos, nunca el cuerpo de Meta ni el texto de Slack.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse

from .approved_templates import BRAKE_KEY, BRAKED_STATUS
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
from .meta_templates import MetaTemplatesClient, MetaTemplatesError
from slack_correlation.client import SlackClient, SlackProtocolError, SlackRejectedError

__all__ = [
    "CONTRACT",
    "CONTRACT_VERSION",
    "ENV",
    "REASON_ACCOUNT_RESTRICTED",
    "REASON_RECATEGORIZED",
    "REASON_STREAK",
    "EnforcementStore",
    "LiveEnforcement",
    "MetaEnforcementWorker",
    "WebhookEvents",
    "check_settings",
    "notice_text",
    "parse_webhook",
    "register_meta_enforcement_routes",
    "signature_ok",
]

logger = logging.getLogger(__name__)

CONTRACT = "meta-enforcement-brakes-v1"
CONTRACT_VERSION = "1.0.0"
ENV = "META_ENFORCEMENT_BRAKES_ENABLED"
VERIFY_TOKEN_ENV = "META_WEBHOOK_VERIFY_TOKEN"
APP_SECRET_ENV = "META_APP_SECRET"

REASON_RECATEGORIZED = "template_recategorized"
REASON_ACCOUNT_RESTRICTED = "meta_account_restricted"
REASON_STREAK = "meta_recategorization_streak"
STREAK_BRAKE_AT = 2  # D4: el «dos» es de Brayan; Meta no publica el umbral.

POLL_EVERY = timedelta(minutes=15)
TICK_SECONDS = 60.0
REFRESH_SECONDS = 60.0
NOTICE_PAGE = 20
WEBHOOK_MAX_BYTES = 1024 * 1024
WEBHOOK_PROCESS_SECONDS = 8.0
WEBHOOK_PATH = "/webhooks/meta"
ROUTE = f"{PREFIX}/meta-enforcement"

ACTIONS = ("congelar", "descongelar", "liberar_plantilla", "liberar_contador")
_CATEGORIES = frozenset({"UTILITY", "MARKETING", "AUTHENTICATION"})
_NAME = re.compile(r"^[a-z0-9][a-z0-9_]{0,249}$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?$")
_CODE = re.compile(r"^[A-Z][A-Z_]{1,63}$")
_WABA = re.compile(r"^[0-9]{6,32}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_SIGNATURE = re.compile(r"^sha256=([0-9a-f]{64})$")
_STORE_ERRORS = (StoreUnavailable, StoreRejected)
SLACK_BLOCKED = {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired", "no_permission",
                 "missing_scope", "not_allowed_token_type", "channel_not_found", "not_in_channel", "is_archived"}
# D-F1.5: solo avisan. El resto de ACCOUNT_RESTRICTION congela (lo decide la base).
NOTICE_ONLY_VIOLATIONS = frozenset({"UTILITY_TEMPLATE_ABUSE_UNBAN", "UTILITY_TEMPLATE_ABUSE_RATE_LIMIT_RECOVERY"})


def check_settings(settings: Any) -> None:
    """Lo que exige el flag al arrancar. Apagado no mira nada."""
    if not getattr(settings, "meta_enforcement_brakes_enabled", False):
        return
    if getattr(settings, "instance_manifest", None) is None:
        raise ValueError(f"{ENV} requires an instance manifest")
    if not getattr(settings, "supabase_base_url", None) or not getattr(settings, "supabase_service_role_key", None):
        raise ValueError(f"{ENV} requires SUPABASE_BASE_URL and SUPABASE_SERVICE_ROLE_KEY")
    if not getattr(settings, "meta_templates_access_token", None) or not _WABA.fullmatch(
            str(getattr(settings, "meta_waba_id", None) or "")):
        raise ValueError(f"{ENV} requires META_TEMPLATES_ACCESS_TOKEN and META_WABA_ID (the poll)")
    token = getattr(settings, "meta_webhook_verify_token", None)
    secret = getattr(settings, "meta_app_secret", None)
    if (token is None) != (secret is None):
        raise ValueError(f"{ENV}: {VERIFY_TOKEN_ENV} and {APP_SECRET_ENV} go together (D-F1.10)")


# ---------------------------------------------------------------------------
# El texto de los avisos: fijo, escrito por codigo.
# ---------------------------------------------------------------------------

_CATEGORY_WORD = {"UTILITY": "de utilidad", "MARKETING": "de marketing", "AUTHENTICATION": "de autenticación"}
_VIOLATION_WORD = {
    "UTILITY_TEMPLATE_ABUSE": "mal uso de plantillas de utilidad",
    "UTILITY_TEMPLATE_ABUSE_RATE_LIMIT": "límite de mensajes de utilidad por mal uso",
    "UTILITY_TEMPLATE_ABUSE_UNBAN": "mal uso de plantillas de utilidad",
    "UTILITY_TEMPLATE_ABUSE_RATE_LIMIT_RECOVERY": "límite de mensajes de utilidad",
}
_ALL_SENDS = "flujos, seguimientos, reactivación, primer contacto y masivo"
_LIFT_BY_HAND = "No se levanta solo: lo levanta una persona de la lista de aprobadores."


def _name_of(data: Mapping[str, Any]) -> str:
    name = data.get("template_name")
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise ValueError("template_name_invalid")
    return name


def notice_text(kind: str, data: Mapping[str, Any]) -> str:
    """El aviso de Slack. ``ValueError`` o ``KeyError`` si la fila no es la que se espera."""
    if kind == "recategorizacion_previa":
        return (f"*Meta avisó que va a cambiar de tipo la plantilla* `{_name_of(data)}`: hoy es "
                f"{_CATEGORY_WORD[data['from']]} y pasa a {_CATEGORY_WORD[data['to']]}. Cuando cambie, el bridge "
                "deja de mandarla. Si no corresponde, se puede pedir revisión en WhatsApp Manager.")
    if kind == "recategorizacion_hecha":
        return (f"*Meta cambió de tipo la plantilla* `{_name_of(data)}`: era {_CATEGORY_WORD[data['from']]} y ahora "
                f"es {_CATEGORY_WORD[data['to']]}. El bridge dejó de mandarla en todos los envíos ({_ALL_SENDS}). "
                "Se puede pedir revisión en WhatsApp Manager dentro de 60 días; si Meta la aprueba, la libera una "
                "persona de la lista de aprobadores.")
    if kind == "contador_dos":
        streak = data.get("streak")
        if type(streak) is not int or streak < STREAK_BRAKE_AT:
            raise ValueError("streak_invalid")
        return (f"*Meta ya cambió de tipo {streak} plantillas de utilidad seguidas* en esta cuenta. Para no "
                "acercarnos a la restricción de utilidad, el bridge frena los pedidos nuevos de plantillas de "
                "utilidad; las de marketing siguen. Se libera a mano, o sola cuando una plantilla pedida como "
                "utilidad sale aprobada como utilidad.")
    if kind == "congelamiento":
        if data.get("source") == "manual":
            return (f"*Se congelaron a mano los envíos que inicia el bridge* ({_ALL_SENDS}). El agente sigue "
                    f"contestando en las conversaciones abiertas. {_LIFT_BY_HAND}")
        violation = _VIOLATION_WORD.get(str(data.get("violation_type")), "una restricción de la cuenta")
        return (f"*Meta restringió la cuenta de WhatsApp* ({violation}). El bridge congeló todos los envíos que "
                f"inicia ({_ALL_SENDS}); el agente sigue contestando en las conversaciones abiertas. {_LIFT_BY_HAND}")
    if kind == "restriccion_levantada_por_meta":
        violation = _VIOLATION_WORD.get(str(data.get("violation_type")), "una restricción de la cuenta")
        return (f"*Meta avisó que levantó una restricción de la cuenta* ({violation}). El bridge no se "
                "descongela solo: si ya está todo en orden, lo levanta una persona de la lista de aprobadores.")
    raise ValueError("notice_kind_invalid")


# ---------------------------------------------------------------------------
# La base.
# ---------------------------------------------------------------------------


class EnforcementStore(FlowChangeStore):
    """Las funciones de ``service_role`` de la migracion 20261127100000."""

    async def record_categories(self, instance: Mapping[str, str], *, waba_id: str,
                                templates: list[Mapping[str, Any]], observed_at: str) -> dict[str, Any]:
        return await self._rpc("record_meta_template_categories_v1", {
            "p_instance": instance, "p_waba_id": waba_id, "p_templates": templates, "p_observed_at": observed_at})

    async def record_category_webhook(self, instance: Mapping[str, str], *, waba_id: str,
                                      event: Mapping[str, Any]) -> dict[str, Any]:
        return await self._rpc("record_meta_template_category_webhook_v1", {
            "p_instance": instance, "p_waba_id": waba_id, "p_event": event})

    async def record_account_event(self, instance: Mapping[str, str], *, waba_id: str,
                                   event: Mapping[str, Any]) -> dict[str, Any]:
        return await self._rpc("record_meta_account_event_v1", {
            "p_instance": instance, "p_waba_id": waba_id, "p_event": event})

    async def get_state(self, instance: Mapping[str, str], *, waba_id: str) -> dict[str, Any]:
        return await self._rpc("get_meta_enforcement_state_v1", {"p_instance": instance, "p_waba_id": waba_id})

    async def act(self, instance: Mapping[str, str], *, waba_id: str, action: str, target: Mapping[str, str] | None,
                  reason: str, actor: str) -> dict[str, Any]:
        return await self._rpc("act_on_meta_enforcement_v1", {
            "p_instance": instance, "p_waba_id": waba_id, "p_action": action, "p_target": target,
            "p_reason": reason, "p_actor_ref": actor})

    async def claim_notices(self, instance: Mapping[str, str], *, limit: int) -> dict[str, Any]:
        return await self._rpc("claim_meta_enforcement_notices_v1", {"p_instance": instance, "p_limit": limit})

    async def finish_notice(self, instance: Mapping[str, str], *, notice_id: str, outcome: str,
                            error_code: str | None) -> dict[str, Any]:
        return await self._rpc("finish_meta_enforcement_notice_v1", {
            "p_instance": instance, "p_notice_id": notice_id, "p_outcome": outcome, "p_error_code": error_code})


# ---------------------------------------------------------------------------
# Lo que leen los envios.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnforcementState:
    """El estado como lo sirve ``_meta_enforcement_state``, ya validado."""

    retired: frozenset[tuple[str, str]] = frozenset()
    frozen: bool = False
    frozen_source: str | None = None
    streak: int = 0
    last_seen_at: str | None = None


def parse_state(raw: object) -> EnforcementState:
    """Entera o nada: una forma que no cierra es ``ValueError`` y se queda lo ultimo bueno."""
    if not isinstance(raw, Mapping):
        raise ValueError("state")
    retired: set[tuple[str, str]] = set()
    rows = raw.get("retired")
    if not isinstance(rows, list):
        raise ValueError("retired")
    for row in rows:
        name = row.get("template_name") if isinstance(row, Mapping) else None
        language = row.get("language") if isinstance(row, Mapping) else None
        if not isinstance(name, str) or not isinstance(language, str):
            raise ValueError("retired_row")
        retired.add((name, language))
    frozen = raw.get("frozen")
    if frozen is not None and not isinstance(frozen, Mapping):
        raise ValueError("frozen")
    streak = raw.get("streak")
    if type(streak) is not int or streak < 0:
        raise ValueError("streak")
    seen = raw.get("last_seen_at")
    return EnforcementState(
        retired=frozenset(retired), frozen=frozen is not None,
        frozen_source=str(frozen.get("source")) if isinstance(frozen, Mapping) else None,
        streak=streak, last_seen_at=seen if isinstance(seen, str) else None)


class LiveEnforcement:
    """El estado de los frenos en este proceso, con lo ultimo bueno si la base no contesta (D-F1.6)."""

    def __init__(self, *, fetch: Callable[[], Awaitable[Mapping[str, Any]]],
                 refresh_seconds: float = REFRESH_SECONDS,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._fetch = fetch
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._state = EnforcementState()
        self._read = False
        self._failures = 0
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.manual_routes = "sin_aprobadores"
        self.webhook = "sin_secreto"

    @property
    def state(self) -> EnforcementState:
        return self._state

    def apply(self, raw: object) -> bool:
        """El estado que devolvio una escritura. False si no cierra (queda el anterior)."""
        try:
            state = parse_state(raw)
        except ValueError:
            return False
        if state.frozen != self._state.frozen:
            logger.warning("meta_enforcement_frozen=%s source=%s", state.frozen, state.frozen_source)
        for name, language in sorted(state.retired - self._state.retired):
            logger.warning("meta_enforcement_retired template=%s language=%s", name, language)
        self._state = state
        self._read = True
        self._failures = 0
        return True

    async def refresh(self) -> bool:
        """Lee la base. Nunca lanza."""
        async with self._lock:
            try:
                raw = await self._fetch()
            except Exception as error:  # noqa: BLE001 - la base caida no frena ni suelta nada
                self._failures += 1
                logger.warning("meta_enforcement_refresh_failed failures=%s error=%s", self._failures,
                               type(error).__name__)
                return False
            if not self.apply(raw):
                self._failures += 1
                return False
            return True

    def initiated_brake(self, name: str, language: str) -> str | None:
        """El motivo por el que un envio iniciado por el bridge no sale, o None."""
        if self._state.frozen:
            return REASON_ACCOUNT_RESTRICTED
        return REASON_RECATEGORIZED if (name, language) in self._state.retired else None

    def utility_request_brake(self) -> str | None:
        """D4: desde la segunda recategorizacion seguida, el pedido nuevo de utilidad se frena."""
        return REASON_STREAK if self._state.streak >= STREAK_BRAKE_AT else None

    def _mark(self, payload: dict[str, object], *, include_freeze: bool) -> dict[str, object]:
        templates = payload.get("message_templates")
        if not isinstance(templates, list):
            return payload
        frozen = include_freeze and self._state.frozen
        if not frozen and not self._state.retired:
            return payload
        marked: list[object] = []
        for template in templates:
            if isinstance(template, Mapping):
                key = (template.get("name"), template.get("language"))
                reason = (REASON_ACCOUNT_RESTRICTED if frozen
                          else REASON_RECATEGORIZED if key in self._state.retired else None)
                if reason is not None:
                    template = {**template, "status": BRAKED_STATUS, BRAKE_KEY: reason}
            marked.append(template)
        return {**payload, "message_templates": marked}

    def braked_catalog(self, payload: dict[str, object]) -> dict[str, object]:
        """El catalogo para un envio que inicia el bridge: retiradas y, congelada, todas."""
        return self._mark(payload, include_freeze=True)

    def retired_catalog(self, payload: dict[str, object]) -> dict[str, object]:
        """El catalogo para una plantilla dentro de una conversacion abierta (el link del agente): solo retiradas."""
        return self._mark(payload, include_freeze=False)

    def readiness(self) -> str:
        """Para ``/ready`` (clave ``meta_enforcement``): sin nombres de plantillas ni datos de nadie."""
        if not self._read:
            refresh = "never_read"
        elif self._failures:
            refresh = f"failing:{self._failures}"
        else:
            refresh = "ok"
        state = self._state
        return (f"frozen={'yes' if state.frozen else 'no'},retired={len(state.retired)},streak={state.streak},"
                f"last_poll={state.last_seen_at or 'never'},refresh={refresh},webhook={self.webhook},"
                f"manual={self.manual_routes}")

    async def start(self) -> None:
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="meta-enforcement-refresh")

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
# El sondeo y los avisos.
# ---------------------------------------------------------------------------


class MetaEnforcementWorker:
    """Cada minuto mira los avisos; cada 15, el sondeo. Nada de lo que falle aca tumba el bridge."""

    def __init__(self, *, store: EnforcementStore, meta: MetaTemplatesClient, live: LiveEnforcement,
                 instance: Mapping[str, str], waba_id: str, slack: SlackClient | None, channel: str | None,
                 slack_problem: str = "sin_token", tick_seconds: float = TICK_SECONDS,
                 poll_every: timedelta = POLL_EVERY,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if not math.isfinite(tick_seconds) or tick_seconds <= 0:
            raise ValueError("invalid tick")
        self._store = store
        self._meta = meta
        self._live = live
        self._instance = dict(instance)
        self._waba_id = waba_id
        self._slack = slack if channel else None
        self._channel = channel
        self._slack_problem = slack_problem if slack is None else "sin_canal"
        self._tick = tick_seconds
        self._poll_every = poll_every
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._last_poll: datetime | None = None
        self.state = "not_started"
        self.last_error: str | None = None
        self.last_summary = "polled=0"
        self.notice_counts: dict[str, int] = {}

    @property
    def notices_state(self) -> str:
        if self._slack is None:
            return self._slack_problem
        counts = self.notice_counts
        return " ".join(f"{key}={counts.get(key, 0)}" for key in
                        ("enviado", "pendiente", "esperando", "fallido", "incierto"))

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="meta-enforcement-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._tick)
            try:
                await self.run_once()
            except Exception as error:  # noqa: BLE001 - el worker no se muere: lo publica
                self.state, self.last_error = "degraded", "unexpected"
                logger.warning("meta_enforcement_worker_failed error=%s", type(error).__name__)

    async def run_once(self, *, force_poll: bool = False) -> None:
        now = self._clock()
        errors = 0
        if force_poll or self._last_poll is None or now - self._last_poll >= self._poll_every:
            errors += await self.poll()
        errors += await self.notify()
        self.state = "ok" if not errors else "degraded"
        if not errors:
            self.last_error = None

    async def poll(self) -> int:
        self._last_poll = self._clock()
        try:
            templates = await self._meta.list_categories()
        except MetaTemplatesError as error:
            logger.warning("meta_enforcement step=poll code=%s", error.code)
            self.last_error = error.code
            return 1
        rows = [{**row, "language": str(row["language"]).replace("-", "_")} for row in templates]
        try:
            result = await self._store.record_categories(
                self._instance, waba_id=self._waba_id, templates=rows,
                observed_at=self._clock().isoformat().replace("+00:00", "Z"))
        except _STORE_ERRORS as error:
            logger.warning("meta_enforcement step=record error=%s", type(error).__name__)
            self.last_error = "store_unavailable"
            return 1
        self._live.apply(result.get("state"))
        counts = result.get("counts") if isinstance(result.get("counts"), Mapping) else {}
        self.last_summary = (f"polled={len(rows)} retired={counts.get('retirada', 0)} "
                             f"approval={counts.get('aprobacion', 0)} baseline={result.get('baseline') is True}")
        logger.info("meta_enforcement step=poll %s", self.last_summary)
        return 0

    async def notify(self) -> int:
        if self._slack is None or self._channel is None:
            return 0
        try:
            claimed = await self._store.claim_notices(self._instance, limit=NOTICE_PAGE)
        except _STORE_ERRORS as error:
            logger.warning("meta_enforcement step=claim error=%s", type(error).__name__)
            self.last_error = "store_unavailable"
            return 1
        counts = claimed.get("counts")
        if isinstance(counts, Mapping):
            self.notice_counts = {str(k): v for k, v in counts.items() if type(v) is int}
        errors = 0
        notices = claimed.get("notices")
        for notice in notices if isinstance(notices, list) else []:
            if isinstance(notice, Mapping):
                errors += await self._post(notice)
        return errors

    async def _post(self, notice: Mapping[str, Any]) -> int:
        assert self._slack is not None and self._channel is not None
        notice_id, kind = str(notice.get("notice_id")), str(notice.get("kind"))
        data = notice.get("data") if isinstance(notice.get("data"), Mapping) else {}
        outcome, code = "sent", None
        try:
            await self._slack.post_message(channel_id=self._channel, message={"text": notice_text(kind, data)})
        except SlackRejectedError as error:
            if error.slack_error in SLACK_BLOCKED:
                outcome, code = "waiting", f"slack_blocked:{error.slack_error}"
            else:
                outcome, code = "retry", "slack_rejected"
        except SlackProtocolError:
            outcome, code = "unknown", "slack_delivery_unknown"
        except (KeyError, ValueError, TypeError):
            outcome, code = "failed", "notice_invalid"
        try:
            await self._store.finish_notice(self._instance, notice_id=notice_id, outcome=outcome, error_code=code)
        except _STORE_ERRORS as error:
            logger.warning("meta_enforcement step=finish notice=%s error=%s", notice_id, type(error).__name__)
            self.last_error = "store_unavailable"
            return 1
        logger.info("meta_enforcement step=notice notice=%s kind=%s outcome=%s", notice_id, kind, outcome)
        if outcome != "sent" and code is not None:
            self.last_error = code
            return 1
        return 0


# ---------------------------------------------------------------------------
# El webhook de Meta.
# ---------------------------------------------------------------------------


def signature_ok(secret: str, body: bytes, header: str | None) -> bool:
    """``X-Hub-Signature-256``: ``sha256=`` + HMAC-SHA256 del cuerpo crudo con el secreto de la app."""
    if not secret or header is None:
        return False
    match = _SIGNATURE.fullmatch(header.strip())
    if match is None:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, match.group(1))


def _iso(seconds: object) -> str | None:
    if type(seconds) is not int or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


@dataclass
class WebhookEvents:
    """Lo que trae un POST, ya leido. Lo que no es de la cuenta o no se entiende solo se cuenta."""

    categories: list[dict[str, Any]] = field(default_factory=list)
    accounts: list[dict[str, Any]] = field(default_factory=list)
    other_account: int = 0
    ignored: int = 0
    invalid: int = 0


def _category_event(value: Mapping[str, Any], occurred_at: str) -> dict[str, Any] | None:
    name = value.get("message_template_name")
    language = value.get("message_template_language")
    template_id = value.get("message_template_id")
    new = value.get("new_category")
    previous, correct = value.get("previous_category"), value.get("correct_category")
    if not isinstance(name, str) or not isinstance(language, str) or new not in _CATEGORIES:
        return None
    language = language.replace("-", "_")  # D-F1.8
    if _NAME.fullmatch(name) is None or _LANGUAGE.fullmatch(language) is None:
        return None
    if type(template_id) is int and template_id > 0:
        template_ref: str | None = str(template_id)
    elif isinstance(template_id, str) and template_id.isdigit():
        template_ref = template_id
    else:
        template_ref = None
    event: dict[str, Any] = {"meta_template_id": template_ref, "name": name, "language": language,
                             "new_category": new, "occurred_at": occurred_at}
    if previous in _CATEGORIES and correct is None:
        event.update(kind="hecho", previous_category=previous)
    elif correct in _CATEGORIES and previous is None:
        event.update(kind="aviso_previo", correct_category=correct,
                     effective_at=_iso(value.get("category_update_timestamp")))
    else:
        return None
    return event


def _account_event(value: Mapping[str, Any], occurred_at: str) -> dict[str, Any] | None:
    if value.get("event") != "ACCOUNT_RESTRICTION":
        return None
    violation_info = value.get("violation_info")
    violation = violation_info.get("violation_type") if isinstance(violation_info, Mapping) else None
    if violation is not None and (not isinstance(violation, str) or _CODE.fullmatch(violation) is None):
        violation = None
    types: list[str] = []
    info = value.get("restriction_info")
    for item in info if isinstance(info, list) else []:
        kind = item.get("restriction_type") if isinstance(item, Mapping) else None
        if isinstance(kind, str) and _CODE.fullmatch(kind) and kind not in types:
            types.append(kind)
    return {"event": "ACCOUNT_RESTRICTION", "violation_type": violation, "restriction_types": types[:10],
            "occurred_at": occurred_at}


def parse_webhook(payload: object, *, waba_id: str, now: datetime) -> WebhookEvents:
    """Lee ``template_category_update`` y ``account_update``. El resto de los campos se ignora.

    Un ``entry`` de otra cuenta de WhatsApp se descarta: una app de Meta puede estar
    suscrita a varias.
    """
    out = WebhookEvents()
    if not isinstance(payload, Mapping) or payload.get("object") != "whatsapp_business_account":
        out.invalid += 1
        return out
    entries = payload.get("entry")
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            out.invalid += 1
            continue
        if str(entry.get("id")) != waba_id:
            out.other_account += 1
            continue
        occurred_at = _iso(entry.get("time")) or now.isoformat().replace("+00:00", "Z")
        changes = entry.get("changes")
        for change in changes if isinstance(changes, list) else []:
            value = change.get("value") if isinstance(change, Mapping) else None
            field_name = change.get("field") if isinstance(change, Mapping) else None
            if not isinstance(value, Mapping):
                out.invalid += 1
                continue
            if field_name == "template_category_update":
                event = _category_event(value, occurred_at)
                if event is None:
                    out.invalid += 1
                else:
                    out.categories.append(event)
            elif field_name == "account_update":
                account = _account_event(value, occurred_at)
                if account is None:
                    out.ignored += 1
                else:
                    out.accounts.append(account)
            else:
                out.ignored += 1
    return out


# ---------------------------------------------------------------------------
# Las rutas.
# ---------------------------------------------------------------------------


def register_meta_enforcement_routes(
    app: FastAPI, *, store: EnforcementStore, live: LiveEnforcement, tenant_ref: str, ally_ref: str,
    waba_id: str, verify_token: str | None, app_secret: str | None,
    ae_token: str | None = None, approvers: frozenset[str] | None = None, git_sha: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """La ruta del webhook (con su secreto) y las acciones a mano (con aprobadores). Las llama quien
    tiene el flag prendido: apagado, no existe ninguna."""
    instance = {"tenant_ref": tenant_ref, "ally_ref": ally_ref}
    sha = git_sha or os.getenv("GIT_SHA", "") or "unknown"

    if verify_token is not None and app_secret is not None:
        live.webhook = "on"

        @app.get(WEBHOOK_PATH)
        async def meta_webhook_verify(
            mode: str | None = Query(default=None, alias="hub.mode"),
            token: str | None = Query(default=None, alias="hub.verify_token"),
            challenge: str | None = Query(default=None, alias="hub.challenge"),
        ) -> Any:
            if (mode != "subscribe" or token is None or challenge is None or _CHALLENGE.fullmatch(challenge) is None
                    or not hmac.compare_digest(token.encode("utf-8"), verify_token.encode("utf-8"))):
                logger.info("meta_webhook verify=refused")
                raise HTTPException(status_code=403, detail="meta_webhook_verify_refused")
            logger.info("meta_webhook verify=ok")
            return PlainTextResponse(challenge)

        @app.post(WEBHOOK_PATH)
        async def meta_webhook(request: Request,
                               signature: str | None = Header(default=None, alias="X-Hub-Signature-256")) -> Any:
            body = await request.body()
            if len(body) > WEBHOOK_MAX_BYTES:
                raise HTTPException(status_code=413, detail="meta_webhook_too_large")
            if not signature_ok(app_secret, body, signature):
                # Fail-closed: sin firma valida no se lee nada.
                logger.warning("meta_webhook signature=refused")
                raise HTTPException(status_code=401, detail="meta_webhook_signature_invalid")
            try:
                payload = json.loads(body)
            except ValueError:
                logger.warning("meta_webhook body=invalid_json")
                return {"ok": True, "ignored": "invalid_json"}
            events = parse_webhook(payload, waba_id=waba_id, now=clock())

            async def process() -> dict[str, int]:
                done = {"categories": 0, "accounts": 0}
                for event in events.categories:
                    result = await store.record_category_webhook(instance, waba_id=waba_id, event=event)
                    live.apply(result.get("state"))
                    done["categories"] += 1
                for event in events.accounts:
                    result = await store.record_account_event(instance, waba_id=waba_id, event=event)
                    live.apply(result.get("state"))
                    done["accounts"] += 1
                return done

            try:
                done = await asyncio.wait_for(process(), WEBHOOK_PROCESS_SECONDS)
            except StoreRejected as error:
                # Un evento que la base no acepta no se arregla con el reintento de Meta.
                logger.warning("meta_webhook step=record code=%s", error.code)
                return {"ok": False, "rejected": error.code}
            except (StoreUnavailable, TimeoutError):
                logger.warning("meta_webhook step=record status=503")
                raise HTTPException(status_code=503, detail="meta_webhook_store_unavailable") from None
            logger.info("meta_webhook categories=%s accounts=%s other_account=%s ignored=%s invalid=%s",
                        done["categories"], done["accounts"], events.other_account, events.ignored, events.invalid)
            return {"ok": True, **done, "other_account": events.other_account, "ignored": events.ignored,
                    "invalid": events.invalid}

    if ae_token is None or approvers is None:
        return
    live.manual_routes = "on"

    def authenticate(authorization: str | None) -> None:
        expected = f"Bearer {ae_token}"
        if authorization is None or not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="automation_expert_change_authentication_required",
                                headers={"WWW-Authenticate": "Bearer"})

    def envelope(data: Mapping[str, Any]) -> dict[str, Any]:
        return {"contract": CONTRACT, "contract_version": CONTRACT_VERSION, "instance": dict(instance),
                "generated_at": clock().isoformat().replace("+00:00", "Z"), "bridge_git_sha": sha,
                "data": dict(data)}

    def failure(error: Exception, route: str) -> HTTPException:
        if isinstance(error, StoreRejected):
            code, http_status = error.code, error.http_status
        else:
            code, http_status = getattr(error, "code", "automation_expert_change_unavailable"), 503
        logger.info("meta_enforcement route=%s status=%s code=%s", route, http_status, code)
        return HTTPException(status_code=http_status, detail=code)

    def view(raw: object) -> dict[str, Any]:
        state = raw if isinstance(raw, Mapping) else {}
        return {"congelada": state.get("frozen") is not None, "congelamiento": state.get("frozen"),
                "retiradas": state.get("retired") if isinstance(state.get("retired"), list) else [],
                "recategorizaciones_seguidas": state.get("streak"),
                "pedidos_de_utilidad_frenados": type(state.get("streak")) is int
                and state["streak"] >= STREAK_BRAKE_AT,
                "ultimo_sondeo": state.get("last_seen_at"), "este_bridge": live.readiness()}

    @app.get(ROUTE)
    async def meta_enforcement_state(authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        try:
            raw = await store.get_state(instance, waba_id=waba_id)
        except _STORE_ERRORS as error:
            raise failure(error, "state") from None
        live.apply(raw)
        return envelope(view(raw))

    @app.post(ROUTE)
    async def meta_enforcement_act(request: Request, authorization: str | None = Header(default=None)) -> Any:
        authenticate(authorization)
        body = _check_keys(await _body(request), required={"accion", "motivo", "requested_by"},
                           allowed={"accion", "motivo", "requested_by", "plantilla"})
        user = _requested_by(body)
        if user not in approvers:
            logger.info("meta_enforcement route=act status=403 code=not_an_approver")
            raise HTTPException(status_code=403, detail="not_an_approver")
        action = body.get("accion")
        if action not in ACTIONS:
            raise EntryRefused("invalid_body", {"field": "accion", "problem": "|".join(ACTIONS)})
        reason = body.get("motivo")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise EntryRefused("invalid_body", {"field": "motivo", "problem": "length_1_to_500"})
        if looks_like_identifier(reason):
            raise EntryRefused("identifier_not_allowed", {"field": "motivo", "problem": "phone_or_mail"})
        target: dict[str, str] | None = None
        if action == "liberar_plantilla":
            plantilla = body.get("plantilla")
            name = plantilla.get("nombre") if isinstance(plantilla, Mapping) else None
            language = plantilla.get("idioma") if isinstance(plantilla, Mapping) else None
            if (not isinstance(plantilla, Mapping) or set(plantilla) != {"nombre", "idioma"}
                    or not isinstance(name, str) or _NAME.fullmatch(name) is None
                    or not isinstance(language, str) or _LANGUAGE.fullmatch(language) is None):
                raise EntryRefused("invalid_body", {"field": "plantilla", "problem": "nombre_e_idioma"})
            target = {"template_name": name, "language": language}
        elif "plantilla" in body:
            raise EntryRefused("invalid_body", {"field": "plantilla", "problem": "only_with_liberar_plantilla"})
        try:
            result = await store.act(instance, waba_id=waba_id, action=action, target=target,
                                     reason=reason.strip(), actor=user)
        except _STORE_ERRORS as error:
            raise failure(error, "act") from None
        applied = live.apply(result.get("state"))
        logger.info("meta_enforcement route=act action=%s replayed=%s", action, result.get("replayed") is True)
        return envelope({"accion": action, "replayed": result.get("replayed") is True,
                         "aplicada_en_este_bridge": applied, **view(result.get("state"))})
