"""El sondeo del estado en Meta y el aviso en el hilo de Slack (template-request-v1 1.3.0, F-146 parte 4, E-361).

Cuando Meta contesta sobre una plantilla que el Automation Expert pidio, el
bridge avisa **en el mismo hilo de Slack donde se la pidieron** (decision de Dan
del 2026-11-09). Solo con ``META_TEMPLATES_ENABLED``: apagado, este worker no se
crea.

Cada vuelta hace dos cosas, cada una con su propia falla:

* **El sondeo.** Pide a la base las solicitudes que toca mirar, lee en Meta su
  estado, mira si ya esta en el catalogo de la bandeja (y si no, le pide a
  Chatwoot que lo sincronice) y lo anota con
  ``record_meta_template_status_v1``.
* **El aviso.** Reclama en la base los avisos que tocan y los postea. El aviso
  sale del estado de la solicitud, no del paso que la movio: ver la migracion
  20261109000100, que tambien explica por que un aviso sale una sola vez.

El texto es fijo y lo escribe este modulo: el modelo no interviene. El unico dato
variable es el nombre de la plantilla (validado por la base) y el motivo, que sale
de una tabla cerrada.

**Quien postea.** El DM con el Automation Expert es de su app de Slack: solo su
bot puede escribir ahi, asi que el conector de la aliada no sirve. Va con
``META_TEMPLATES_SLACK_BOT_TOKEN``, el mismo ``SLACK_BOT_TOKEN`` del profile. Sin
el token, o con uno mal formado, el sondeo corre igual, los avisos esperan en la
base y ``/ready`` lo dice. Si Slack rechaza el token o el canal, el aviso no gasta
intentos: espera en la base y sale cuando se corrige.

El log lleva el id, la clase y el codigo; nunca el texto de la plantilla ni el
hilo.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from . import template_request as rules
from .flow_change_api import FlowChangeStore, StoreRejected, StoreUnavailable
from .flow_definition import TemplateCatalog
from .meta_templates import MetaTemplate, MetaTemplatesClient, MetaTemplatesError, content_of
from slack_correlation.client import SlackClient, SlackProtocolError, SlackRejectedError

__all__ = ["MetaTemplateStatusWorker", "TemplateStatusStore", "notice_text", "slack_token_problem"]

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60.0
POLL_PAGE = 20
NOTICE_PAGE = 20
SYNC_EVERY = timedelta(minutes=30)
# Graph contesta 100/33 cuando el id no existe, y tambien cuando el token perdio acceso: por eso se confirma en la lista.
META_NOT_FOUND = "meta:100:33"
META_GONE_STATUS = "DELETED"
# Los rechazos de Slack que no dependen del mensaje: reintentarlo igual no cambia nada hasta que alguien lo arregle.
SLACK_BLOCKED = {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired", "no_permission",
                 "missing_scope", "not_allowed_token_type", "channel_not_found", "not_in_channel", "is_archived"}
_META_STATUS = re.compile(r"^[A-Z][A-Z_]{1,31}$")
_REJECTED_REASON_CODE = re.compile(r"^[A-Z][A-Z_]{1,63}$")
_CATEGORIES = {"UTILITY", "MARKETING", "AUTHENTICATION"}
_STORE_ERRORS = (StoreUnavailable, StoreRejected)

_CATEGORY_WORD = {"UTILITY": "de utilidad", "MARKETING": "de marketing", "AUTHENTICATION": "de autenticación"}
# Los motivos, sin jerga: lo lee el duenio. El resto de los codigos cae en UNKNOWN_REASON.
_REJECTED_REASON = {
    "INCORRECT_CATEGORY": "el tipo pedido no corresponde a lo que dice el mensaje.",
    "PROMOTIONAL": "el mensaje vende y estaba pedido como de utilidad.",
    "INVALID_FORMAT": "la forma del mensaje no cumple las reglas de Meta.",
    "TAG_CONTENT_MISMATCH": "el mensaje no corresponde al tipo pedido.",
    "ABUSIVE_CONTENT": "Meta lo considera contenido abusivo.",
    "SCAM": "Meta lo considera engañoso.",
}
UNKNOWN_REASON = "Meta no dio un motivo claro."
REASON_NOT_READ = "no se pudo leer desde acá, se ve en la cuenta de WhatsApp."
_REPLY_HERE = "Contestá en este hilo para seguir."


def notice_text(notice: Mapping[str, Any]) -> str:
    """El aviso, escrito por codigo. ``KeyError`` o ``ValueError`` si la fila no es la que se reclamo."""
    name = notice["template_name"]
    if not isinstance(name, str) or re.fullmatch(r"[a-z0-9][a-z0-9_]{1,99}", name) is None:
        raise ValueError("template_name_invalid")
    kind = notice["kind"]
    if kind == "rechazada":
        code = notice.get("meta_rejected_reason")
        reason = REASON_NOT_READ if code is None else _REJECTED_REASON.get(str(code), UNKNOWN_REASON)
        return (f"*Meta rechazó la plantilla* `{name}`. Motivo: {reason} "
                f"Se corrige con una versión nueva. {_REPLY_HERE}")
    if kind == "recategorizada":
        requested = _CATEGORY_WORD[notice["category_requested"]]
        approved = _CATEGORY_WORD[notice["meta_category"]]
        return (f"*Meta aprobó la plantilla* `{name}`, *pero la cambió de tipo*: se pidió {requested} y quedó "
                f"{approved}. Puede cambiar en qué flujos se usa y lo que cuesta cada mensaje. {_REPLY_HERE}")
    if kind == "aprobada":
        ready = ("Ya se puede usar en un flujo." if notice.get("status") == "en_catalogo"
                 else "Puede tardar hasta 3 horas en quedar disponible para los flujos.")
        return f"*Meta aprobó la plantilla* `{name}`. {ready} {_REPLY_HERE}"
    raise ValueError("notice_kind_invalid")


def slack_token_problem(token: str | None) -> str | None:
    """Lo que ``/ready`` dice si el token no sirve; None si sirve. Un secreto mal cargado no tumba el bridge."""
    if token is None:
        return "sin_token"
    if not token.startswith("xoxb-") or any(character.isspace() for character in token):
        return "token_invalido"
    return None


class TemplateStatusStore(FlowChangeStore):
    """Las funciones de ``service_role`` del sondeo y de los avisos."""

    async def list_to_poll(self, instance: Mapping[str, str], *, limit: int) -> dict[str, Any]:
        return await self._rpc("list_meta_template_requests_to_poll_v1", {"p_instance": instance, "p_limit": limit})

    async def record_status(self, instance: Mapping[str, str], *, request_id: str, meta_template_id: str,
                            meta_status: str, meta_category: str | None, rejected_reason: str | None,
                            in_catalog: bool) -> dict[str, Any]:
        return await self._rpc("record_meta_template_status_v1", {
            "p_instance": instance, "p_request_id": request_id, "p_meta_template_id": meta_template_id,
            "p_meta_status": meta_status, "p_meta_category": meta_category, "p_rejected_reason": rejected_reason,
            "p_in_catalog": in_catalog})

    async def claim_notices(self, instance: Mapping[str, str], *, limit: int) -> dict[str, Any]:
        return await self._rpc("claim_meta_template_notices_v1", {"p_instance": instance, "p_limit": limit})

    async def finish_notice(self, instance: Mapping[str, str], *, request_id: str, kind: str, outcome: str,
                            error_code: str | None) -> dict[str, Any]:
        return await self._rpc("finish_meta_template_notice_v1", {
            "p_instance": instance, "p_request_id": request_id, "p_kind": kind, "p_outcome": outcome,
            "p_error_code": error_code})


class MetaTemplateStatusWorker:
    """El worker recurrente. Nada de lo que falle aca tumba el bridge: se publica en ``/ready``."""

    def __init__(self, *, store: TemplateStatusStore, meta: MetaTemplatesClient, chatwoot: Any, inbox_id: int,
                 instance: Mapping[str, str], slack: SlackClient | None, slack_problem: str = "sin_token",
                 poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds <= 0:
            raise ValueError("invalid poll interval")
        self._store = store
        self._meta = meta
        self._chatwoot = chatwoot
        self._inbox_id = inbox_id
        self._instance = dict(instance)
        self._slack = slack
        self._slack_problem = slack_problem
        self._interval = poll_interval_seconds
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._last_sync_at: datetime | None = None
        # Lo que sigue roto aunque una vuelta no lo toque: casi todas no tienen nada que leer ni que postear.
        self._meta_error: str | None = None
        self._slack_blocked: str | None = None
        self.state = "not_started"
        self.last_error: str | None = None
        self.last_run_at: str | None = None
        self.last_summary = "polled=0 updated=0 errors=0"
        self.notice_counts: dict[str, int] = {}

    @property
    def notices_state(self) -> str:
        if self._slack is None:
            return self._slack_problem
        counts = self.notice_counts
        return (f"sent={counts.get('sent', 0)} no_thread={counts.get('no_thread', 0)} "
                f"failed={counts.get('failed', 0)} unknown={counts.get('unknown', 0)} "
                f"waiting={counts.get('waiting', 0)}")

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        # Corta la vuelta en curso: un aviso cortado en el aire queda sin cerrar y la base lo pasa a 'unknown'.
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        # Primero espera: un reinicio no sale a pedirle a Meta en el arranque.
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.run_once()
            except Exception as error:  # noqa: BLE001 - el worker no se muere: lo publica
                self.state, self.last_error = "degraded", "unexpected"
                logger.warning("meta_template_status_failed error=%s", type(error).__name__)

    async def run_once(self) -> None:
        """Una vuelta: el sondeo y despues los avisos, para que lo recien anotado salga en la misma."""
        errors = await self._poll()
        errors += await self._notify()
        standing = self._meta_error or self._slack_blocked
        self.state = "ok" if not errors and standing is None else "degraded"
        self.last_run_at = self._clock().isoformat().replace("+00:00", "Z")
        if not errors:
            self.last_error = standing

    def _fail(self, code: str) -> int:
        self.last_error = code
        return 1

    async def _poll(self) -> int:
        try:
            listed = await self._store.list_to_poll(self._instance, limit=POLL_PAGE)
        except _STORE_ERRORS as error:
            logger.warning("meta_template_status step=list error=%s", type(error).__name__)
            return self._fail("store_unavailable")
        rows = listed.get("requests")
        rows = [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
        errors = updated = 0
        meta_error: str | None = None
        read_ok = False
        catalog: list[Mapping[str, Any]] | None = None
        catalog_read = synced = False
        for row in rows:
            request_id = str(row.get("request_id"))
            try:
                template = await self._read(row)
            except MetaTemplatesError as error:
                logger.warning("meta_template_status step=read request_id=%s code=%s", request_id, error.code)
                errors += self._fail(error.code)
                meta_error = error.code
                continue
            read_ok = True
            if template is None or _META_STATUS.fullmatch(template.status) is None:
                continue
            category = template.category if template.category in _CATEGORIES else None
            if template.status == "APPROVED" and category is None:
                errors += self._fail("meta_unexpected_answer")
                meta_error = "meta_unexpected_answer"
                continue
            in_catalog = False
            if template.status == "APPROVED":
                if not catalog_read:
                    catalog, catalog_read = await self._read_catalog(), True
                in_catalog = catalog is not None and rules.approved_in_catalog(row, catalog)
                if not in_catalog and not synced:
                    synced = True
                    await self._sync_catalog()
            reason = template.rejected_reason if template.status == "REJECTED" else None
            if reason is not None and _REJECTED_REASON_CODE.fullmatch(reason) is None:
                reason = None
            try:
                recorded = await self._store.record_status(
                    self._instance, request_id=request_id, meta_template_id=template.template_id,
                    meta_status=template.status, meta_category=category, rejected_reason=reason,
                    in_catalog=in_catalog)
            except _STORE_ERRORS as error:
                logger.warning("meta_template_status step=record request_id=%s error=%s", request_id,
                               type(error).__name__)
                errors += self._fail("store_unavailable")
                continue
            if recorded.get("ok") is True:
                updated += 1
        if meta_error is not None:
            self._meta_error = meta_error
        elif read_ok:
            self._meta_error = None
        self.last_summary = f"polled={len(rows)} updated={updated} errors={errors}"
        return errors

    async def _read(self, row: Mapping[str, Any]) -> MetaTemplate | None:
        template_id = row.get("meta_template_id")
        if isinstance(template_id, str):
            try:
                return await self._meta.read(template_id)
            except MetaTemplatesError as error:
                if error.code != META_NOT_FOUND:
                    raise
                # Borrada en WhatsApp Manager, si la lista tampoco la tiene. Se anota y no se sondea mas.
                listed = await self._meta.find(str(row.get("template_name")), str(row.get("language")))
                if listed is not None and listed.template_id == template_id:
                    raise
                return MetaTemplate(template_id=template_id, status=META_GONE_STATUS)
        # Un envio a medias: es ella solo si Meta tiene lo mismo que se pidio (la regla del envio).
        found = await self._meta.find(str(row.get("template_name")), str(row.get("language")))
        if found is None or found.content is None or found.content != content_of(row.get("meta_components")):
            return None
        return found

    async def _read_catalog(self) -> list[Mapping[str, Any]] | None:
        try:
            inbox = await self._chatwoot.get_inbox(inbox_id=self._inbox_id)
            return list(TemplateCatalog.from_inbox_payload({"message_templates": inbox["message_templates"]}).templates)
        except Exception as error:  # noqa: BLE001 - sin catalogo se anota aprobada y se vuelve a mirar
            logger.info("meta_template_status step=catalog error=%s", type(error).__name__)
            return None

    async def _sync_catalog(self) -> None:
        """Pide la sincronizacion, espaciada. Corre aparte en Chatwoot: la plantilla aparece en una vuelta siguiente."""
        now = self._clock()
        if self._last_sync_at is not None and now - self._last_sync_at < SYNC_EVERY:
            return
        self._last_sync_at = now
        try:
            await self._chatwoot.sync_inbox_templates(inbox_id=self._inbox_id)
        except Exception as error:  # noqa: BLE001 - el programador de Chatwoot la trae igual, con hasta 3 h
            logger.info("meta_template_status step=sync error=%s", type(error).__name__)

    async def _notify(self) -> int:
        if self._slack is None:
            return 0
        try:
            claimed = await self._store.claim_notices(self._instance, limit=NOTICE_PAGE)
        except _STORE_ERRORS as error:
            logger.warning("meta_template_status step=claim error=%s", type(error).__name__)
            return self._fail("store_unavailable")
        counts = claimed.get("counts")
        if isinstance(counts, Mapping):
            self.notice_counts = {str(key): value for key, value in counts.items() if type(value) is int}
            self._slack_blocked = (self._slack_blocked or "slack_blocked") if self.notice_counts.get("waiting") else None
        notices = claimed.get("notices")
        errors = 0
        for notice in notices if isinstance(notices, list) else []:
            if isinstance(notice, Mapping):
                errors += await self._post(notice)
        return errors

    async def _with_reason(self, notice: Mapping[str, Any]) -> Mapping[str, Any]:
        """Una rechazada sin motivo guardado (la contesto Meta al crearla): se le pregunta antes de avisar."""
        template_id = notice.get("meta_template_id")
        if notice.get("kind") != "rechazada" or notice.get("meta_rejected_reason") is not None \
                or not isinstance(template_id, str):
            return notice
        try:
            template = await self._meta.read(template_id)
        except MetaTemplatesError as error:
            logger.info("meta_template_status step=reason request_id=%s code=%s", notice.get("request_id"), error.code)
            return notice
        return {**notice, "meta_rejected_reason": template.rejected_reason}

    async def _post(self, notice: Mapping[str, Any]) -> int:
        assert self._slack is not None
        request_id, kind = str(notice.get("request_id")), str(notice.get("kind"))
        message: dict[str, Any] = {}
        outcome, code = "sent", None
        notice = await self._with_reason(notice)
        try:
            message = {"text": notice_text(notice)}
            if isinstance(notice.get("thread_ts"), str):
                message["thread_ts"] = notice["thread_ts"]
            await self._slack.post_message(channel_id=str(notice.get("channel")), message=message)
        except SlackRejectedError as error:
            if error.slack_error in SLACK_BLOCKED:  # el token o el canal: espera sin gastar intentos
                outcome, code = "waiting", f"slack_blocked:{error.slack_error}"
                self._slack_blocked = code
            else:
                outcome, code = "retry", "slack_rejected"  # Slack dijo que no: no llego, se puede repetir
        except SlackProtocolError:
            outcome, code = "unknown", "slack_delivery_unknown"  # puede haber llegado: no se repite
        except (KeyError, ValueError, TypeError):
            outcome, code = "failed", "notice_invalid"
        try:
            await self._store.finish_notice(self._instance, request_id=request_id, kind=kind, outcome=outcome,
                                            error_code=code)
        except _STORE_ERRORS as error:
            # El reclamo queda abierto y a los 5 minutos pasa a 'unknown': no se repite.
            logger.warning("meta_template_status step=finish request_id=%s error=%s", request_id,
                           type(error).__name__)
            return self._fail("store_unavailable")
        logger.info("meta_template_status step=notice request_id=%s kind=%s outcome=%s", request_id, kind, outcome)
        return self._fail(code) if outcome != "sent" and code is not None else 0
