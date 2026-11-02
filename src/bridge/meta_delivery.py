"""Lo que Meta no entrego: el registro, el unico reintento del 131049 y el 131026.

E-284 (P-12a del plan del 2026-11-02). Chatwoot acepta la plantilla y el bridge
la daba por entregada (``accepted_by_chatwoot``), pero Meta puede rechazarla
despues: Chatwoot marca el mensaje ``failed`` y guarda el codigo en
``content_attributes.external_error`` («131049: ...»). En 30 dias ATT1 tuvo 69
mensajes con 131049 (54 eran el primer mensaje al numero) y Maribel 7 (lector del
OS, 02/11). El bridge no recibe ``message_updated`` (solo se suscribe
``message_created`` y ``conversation_updated``), asi que la falla se lee de
Chatwoot, como ya la lee el Automation Expert (``automation_expert_read``).

Tres piezas, separadas a proposito:

* ``find_rejections`` y ``evaluate_retry`` son el criterio, puro y sin red, sobre
  el detalle de la conversacion (GET show) y su historial (la API de mensajes).
  La API de mensajes trae ``status`` y ``content_attributes`` pero no
  ``additional_attributes``; el show trae el ultimo mensaje con
  ``additional_attributes.template_params`` (capturas del 2026-10-15 y del
  2026-10-29). Por eso la plantilla se lee solo mientras el mensaje rechazado es
  el ultimo, y el reintento sale solo si lo sigue siendo: manda exactamente la
  misma plantilla, con los mismos parametros y el mismo texto.
* ``MetaDeliveryRejectionSweeper`` es el worker recurrente: barre las
  conversaciones con actividad reciente, registra cada rechazo en la base
  (``record_meta_delivery_rejection_v1``) y, con el reintento prendido, toma de
  la base los primeros contactos con 131049 de entre 24 h y el techo, los relee,
  reserva (las barreras de compra, baja, derivacion y caso cerrado estan en la
  base), manda y cierra.
* 131026 (el numero no recibe WhatsApp, decision de Dan del 02/11): con el
  cierre prendido, la base cierra el caso como ``unreachable`` y cancela sus
  toques pendientes, y el equipo recibe un aviso ``MSG-002`` por el conector de
  Slack de la aliada (el mismo productor que ``MOT-002``). Nunca se reintenta.

Solo el 131049 del primer contacto se reintenta, una vez y nunca en rafaga
(Meta, «Per-user marketing template limits»: esperar al menos 24 h; los
reintentos repetidos dentro de 24 h pueden bloquear los envios a esa persona).
El reintento lleva ``recovery_first_touch_retry_of``: su propio rechazo se
registra como ``first_touch_retry`` y no se reintenta. Contrato:
``docs/contracts/meta-delivery-rejections-v1.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from bridge.chatwoot import (
    FIRST_TOUCH_RETRY_MARKER,
    FLOW_ENGINE_PURCHASE_WELCOME_MARKER,
    ChatwootProtocolError,
)
from bridge.messaging import FinalMetaEffect, FinalMetaEffectGate
from bridge.slack_notifications import NotificationProducer
from bridge.supabase import SupabaseError
from slack_correlation.catalog import NotificationCommand
from slack_correlation.producer import ConnectorSemanticConflict

logger = logging.getLogger(__name__)

RETRYABLE_CODE = "131049"
UNREACHABLE_CODE = "131026"
# Meta: no antes de 24 h.
RETRY_MIN_AGE_SECONDS = 86_400
# El techo por defecto: 72 h, como el seguimiento con cupon. Un primer contacto
# de hace una semana ya no tiene sentido, y el dia que se prende el flag no le
# escribe a todo el historico.
DEFAULT_RETRY_MAX_AGE_SECONDS = 259_200
MAX_RETRY_MAX_AGE_SECONDS = 604_800
DEFAULT_SCAN_INTERVAL_SECONDS = 600.0
# Cuanto mira atras el barrido que registra: un rechazo llega a los segundos
# del envio y la conversacion queda con esa actividad. El primer barrido de cada
# proceso mira el techo entero, para que un reinicio no pierda nada.
DEFAULT_LOOKBACK_SECONDS = 21_600
DEFAULT_MAX_RETRIES_PER_SCAN = 3
REJECTION_CONVERSATION_STATUSES = ("open", "resolved", "pending", "snoozed")
UNREACHABLE_EVENT_CODE = "MSG-002"
_NOTICE_NAMESPACE = b"meta-delivery-rejections-v1\0"
# Lo que el bridge ve en Chatwoot y no cambia con el tiempo: se escribe en la
# base (bridge_<motivo>) y la fila no se vuelve a leer. too_early,
# conversation_snoozed y el gate cerrado si pueden cambiar: no se escriben.
PERMANENT_SKIPS = frozenset({
    "message_not_found", "not_retryable", "lead_wrote", "conversation_moved_on",
    "contact_unreadable", "contact_blocked", "phone_unreadable", "message_unreadable",
    "too_late", "template_params_unreadable", "template_not_retryable",
    "content_unreadable",
})
_META_ERROR_CODE_RE = re.compile(r"^(\d{3,6})(?:\D|$)")
_E164_RE = re.compile(r"\+[1-9]\d{7,14}")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+")


def _attributes(message: Mapping[str, object]) -> dict[str, object]:
    """``content_attributes`` como dict: la API lo da asi; la base lo guarda como texto."""
    raw = message.get("content_attributes")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def meta_error_code(message: Mapping[str, object]) -> str | None:
    """Solo el codigo de Meta en digitos, nunca el texto (como el AE)."""
    error = _attributes(message).get("external_error")
    if not isinstance(error, str):
        return None
    match = _META_ERROR_CODE_RE.match(error.strip())
    return match.group(1) if match else None


def touch_kind(message: Mapping[str, object]) -> str | None:
    """Que plantilla nuestra es, por la marca que le pone el bridge, o ``None``."""
    if message.get("message_type") != 1 or message.get("private") is not False:
        return None
    attributes = _attributes(message)
    if attributes.get(FIRST_TOUCH_RETRY_MARKER) is not None:
        return "first_touch_retry"
    if "recovery_first_touch_hash" in attributes:
        if attributes.get(FLOW_ENGINE_PURCHASE_WELCOME_MARKER) is True:
            return "purchase_welcome"
        return "first_touch"
    if "recovery_followup_hash" in attributes:
        return "followup"
    if "reactivation_command_key" in attributes:
        return "reactivation"
    if "conversation_followup_command_key" in attributes:
        return "coupon_followup"
    return None


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _show_template_params(
    details: Mapping[str, object], message_id: int
) -> dict[str, object] | None:
    """Los ``template_params`` del ultimo mensaje del show, si es ``message_id``."""
    embedded = details.get("messages")
    if not isinstance(embedded, list):
        return None
    for message in embedded:
        if not isinstance(message, dict) or message.get("id") != message_id:
            continue
        additional = message.get("additional_attributes")
        params = additional.get("template_params") if isinstance(additional, dict) else None
        if (
            isinstance(params, dict)
            and isinstance(params.get("name"), str)
            and params["name"].strip()
            and isinstance(params.get("language"), str)
            and isinstance(params.get("processed_params"), dict)
        ):
            return dict(params)
    return None


@dataclass(frozen=True)
class Rejection:
    """Un mensaje nuestro que Meta rechazo, tal como lo vio el barrido."""

    conversation_id: int
    message_id: int
    touch_kind: str
    code: str | None
    created_epoch: int
    template_name: str | None
    retry_of_message_id: int | None


def find_rejections(
    entry: Mapping[str, object], *, inbox_id: int
) -> list[Rejection]:
    """Los mensajes nuestros con ``status = failed`` de una conversacion."""
    details = entry.get("conversation")
    messages = entry.get("messages")
    if not isinstance(details, dict) or not isinstance(messages, list):
        raise ChatwootProtocolError("invalid_conversation_payload")
    conversation_id = _positive_int(details.get("id"))
    if conversation_id is None or details.get("inbox_id") != inbox_id:
        raise ChatwootProtocolError("invalid_conversation_scope")
    found: list[Rejection] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("status") != "failed":
            continue
        kind = touch_kind(message)
        message_id = _positive_int(message.get("id"))
        created = _positive_int(message.get("created_at"))
        if kind is None or message_id is None or created is None:
            continue
        params = _show_template_params(details, message_id)
        retry_of = (
            _positive_int(_attributes(message).get(FIRST_TOUCH_RETRY_MARKER))
            if kind == "first_touch_retry"
            else None
        )
        if kind == "first_touch_retry" and retry_of is None:
            continue
        found.append(
            Rejection(
                conversation_id=conversation_id,
                message_id=message_id,
                touch_kind=kind,
                code=meta_error_code(message),
                created_epoch=created,
                template_name=params["name"].strip() if params else None,  # type: ignore[union-attr]
                retry_of_message_id=retry_of,
            )
        )
    return found


@dataclass(frozen=True)
class RetryPlan:
    """Lo que se le manda otra vez: el mismo texto y la misma plantilla."""

    conversation_id: int
    message_id: int
    content: str
    template_params: dict[str, object]
    template_name: str
    template_language: str
    target_phone: str
    contact_email: str | None


def evaluate_retry(
    entry: Mapping[str, object],
    *,
    message_id: int,
    inbox_id: int,
    now_epoch: int,
    allowed_templates: frozenset[str],
    max_age_seconds: int,
) -> RetryPlan | str:
    """El criterio del reintento, sin red: el plan, o por que no."""
    details = entry.get("conversation")
    messages = entry.get("messages")
    if not isinstance(details, dict) or not isinstance(messages, list):
        raise ChatwootProtocolError("invalid_conversation_payload")
    conversation_id = _positive_int(details.get("id"))
    if conversation_id is None or details.get("inbox_id") != inbox_id:
        raise ChatwootProtocolError("invalid_conversation_scope")
    target = next(
        (m for m in messages if isinstance(m, dict) and m.get("id") == message_id), None
    )
    if target is None:
        return "message_not_found"
    if (
        target.get("status") != "failed"
        or touch_kind(target) != "first_touch"
        or meta_error_code(target) != RETRYABLE_CODE
    ):
        return "not_retryable"
    later = [
        m
        for m in messages
        if isinstance(m, dict)
        and (_positive_int(m.get("id")) or 0) > message_id
        and m.get("message_type") != 2
    ]
    if any(m.get("message_type") == 0 for m in later):
        return "lead_wrote"
    if later:
        return "conversation_moved_on"
    if details.get("status") == "snoozed":
        return "conversation_snoozed"
    meta = details.get("meta")
    sender = meta.get("sender") if isinstance(meta, dict) else None
    if not isinstance(sender, dict):
        return "contact_unreadable"
    if sender.get("blocked") is True:
        return "contact_blocked"
    phone = sender.get("phone_number")
    if not isinstance(phone, str) or _E164_RE.fullmatch(phone) is None:
        return "phone_unreadable"
    email = sender.get("email")
    contact_email = (
        email.strip()
        if isinstance(email, str) and _EMAIL_RE.fullmatch(email.strip())
        else None
    )
    created = _positive_int(target.get("created_at"))
    if created is None:
        return "message_unreadable"
    age = now_epoch - created
    if age < RETRY_MIN_AGE_SECONDS:
        return "too_early"
    if age > max_age_seconds:
        return "too_late"
    params = _show_template_params(details, message_id)
    if params is None:
        return "template_params_unreadable"
    name = str(params["name"]).strip()
    if name not in allowed_templates:
        return "template_not_retryable"
    content = target.get("content")
    if not isinstance(content, str) or not content.strip():
        return "content_unreadable"
    return RetryPlan(
        conversation_id=conversation_id,
        message_id=message_id,
        content=content,
        template_params=params,
        template_name=name,
        template_language=str(params["language"]),
        target_phone=phone,
        contact_email=contact_email,
    )


def unreachable_notice(
    rejection: Rejection, *, inbox_id: int
) -> NotificationCommand:
    """El aviso MSG-002 de un 131026: la conversacion, sin telefono ni texto."""
    digest = hashlib.sha256(
        _NOTICE_NAMESPACE + f"{inbox_id}\0{rejection.message_id}".encode("ascii")
    ).hexdigest()
    return NotificationCommand(
        event_id=str(uuid.UUID(hex=digest[:32])),
        event_code=UNREACHABLE_EVENT_CODE,
        dedupe_key=digest,
        occurred_at=datetime.fromtimestamp(rejection.created_epoch, UTC),
        reason_code=f"meta_{UNREACHABLE_CODE}_{rejection.touch_kind}",
        component=f"chatwoot.conversation.{rejection.conversation_id}",
        state="pending",
        count=1,
    )


@dataclass(frozen=True)
class RetryConfig:
    """El reintento del 131049: las plantillas, el techo, el horario y el gate."""

    allowed_templates: frozenset[str]
    send_hours: tuple[int, int]
    time_zone: ZoneInfo
    gate: FinalMetaEffectGate
    max_age_seconds: int = DEFAULT_RETRY_MAX_AGE_SECONDS
    max_per_scan: int = DEFAULT_MAX_RETRIES_PER_SCAN

    def __post_init__(self) -> None:
        start, end = self.send_hours
        if (
            not self.allowed_templates
            or not all(isinstance(n, str) and n.strip() == n and n for n in self.allowed_templates)
            or not 0 <= start < end <= 24
            or not RETRY_MIN_AGE_SECONDS <= self.max_age_seconds <= MAX_RETRY_MAX_AGE_SECONDS
            or not 1 <= self.max_per_scan <= 10
        ):
            raise ValueError("invalid meta first touch retry configuration")


class MetaDeliveryRejectionSweeper:
    """Registra lo que Meta rechazo y, si esta prendido, reintenta una vez."""

    def __init__(
        self,
        *,
        chatwoot: object,
        supabase: object,
        account_id: int,
        inbox_id: int,
        close_unreachable: bool = False,
        producer: NotificationProducer | None = None,
        retry: RetryConfig | None = None,
        scan_interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
        lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
        max_pages: int = 10,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            type(close_unreachable) is not bool
            or not scan_interval_seconds > 0
            or not 600 <= lookback_seconds <= MAX_RETRY_MAX_AGE_SECONDS
            or not 1 <= max_pages <= 20
        ):
            raise ValueError("invalid meta delivery rejection configuration")
        self._chatwoot = chatwoot
        self._supabase = supabase
        self._account_id = account_id
        self._inbox_id = inbox_id
        self._close_unreachable = close_unreachable
        self._producer = producer
        self._retry = retry
        self._scan_interval_seconds = scan_interval_seconds
        self._lookback_seconds = lookback_seconds
        self._max_pages = max_pages
        self._clock = clock
        self._first_scan = True
        self._skips_logged: set[tuple[int, str]] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_scan_state = "never"
        self._last_scan_summary = "never"

    @property
    def last_scan_state(self) -> str:
        return self._last_scan_state

    @property
    def last_scan_summary(self) -> str:
        return self._last_scan_summary

    @property
    def retry_enabled(self) -> bool:
        return self._retry is not None

    @property
    def close_unreachable(self) -> bool:
        return self._close_unreachable

    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self._clock(), UTC).isoformat()

    def _inside_send_hours(self) -> bool:
        assert self._retry is not None
        start, end = self._retry.send_hours
        hour = datetime.fromtimestamp(self._clock(), self._retry.time_zone).hour
        return start <= hour < end

    async def run_once(self) -> dict[str, int]:
        """Un barrido: registrar, avisar los 131026 y reintentar lo que toca."""
        counts: Counter[str] = Counter()
        now_epoch = int(self._clock())
        lookback = self._lookback_seconds
        if self._first_scan and self._retry is not None:
            lookback = max(lookback, self._retry.max_age_seconds)
        conversations = await self._chatwoot.list_recent_conversations_with_messages(  # type: ignore[attr-defined]
            expected_inbox_id=self._inbox_id,
            statuses=REJECTION_CONVERSATION_STATUSES,
            active_since_epoch=now_epoch - lookback,
            max_pages=self._max_pages,
        )
        for entry in conversations:
            if self._stopping.is_set():
                break
            for rejection in find_rejections(entry, inbox_id=self._inbox_id):
                await self._record(rejection, counts)
        self._first_scan = False
        if self._retry is not None:
            if self._inside_send_hours():
                await self._retry_due(counts)
            else:
                counts["retry_outside_send_hours"] += 1
        self._last_scan_state = "healthy"
        self._last_scan_summary = (
            " ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "nothing"
        )
        logger.info("meta_delivery_scan %s", self._last_scan_summary)
        return dict(counts)

    async def _record(self, rejection: Rejection, counts: Counter[str]) -> None:
        recorded = await self._supabase.record_meta_delivery_rejection(  # type: ignore[attr-defined]
            chatwoot_account_id=self._account_id,
            chatwoot_inbox_id=self._inbox_id,
            external_conversation_id=rejection.conversation_id,
            chatwoot_message_id=rejection.message_id,
            touch_kind=rejection.touch_kind,
            meta_error_code=rejection.code,
            template_name=rejection.template_name,
            message_created_at=datetime.fromtimestamp(rejection.created_epoch, UTC).isoformat(),
            retry_of_message_id=rejection.retry_of_message_id,
            close_unreachable=self._close_unreachable,
            now=self._now_iso(),
        )
        if recorded.outcome == "recorded":
            counts["recorded"] += 1
            # Lo que se mide: el codigo, el tipo de toque, la plantilla y la
            # conversacion. Nunca el telefono ni el texto.
            logger.info(
                "meta_delivery_rejected code=%s touch=%s template=%s conversation=%s unreachable=%s",
                rejection.code or "none",
                rejection.touch_kind,
                rejection.template_name or "unknown",
                rejection.conversation_id,
                recorded.unreachable_outcome or "none",
            )
        if recorded.needs_team_notice:
            await self._notify_unreachable(rejection, recorded.rejection_id, counts)

    async def _notify_unreachable(
        self, rejection: Rejection, rejection_id: str, counts: Counter[str]
    ) -> None:
        if self._producer is None:
            counts["notice_without_slack"] += 1
            return
        try:
            await self._producer.admit(unreachable_notice(rejection, inbox_id=self._inbox_id))
        except ConnectorSemanticConflict:
            # El mismo id con otro contenido: ya se admitio y la marca no llego.
            logger.info("meta_delivery_notice_already_admitted conversation=%s", rejection.conversation_id)
        except Exception as exc:  # noqa: BLE001 - el aviso no frena el registro
            counts["notice_failed"] += 1
            logger.warning(
                "meta_delivery_notice_failed conversation=%s error=%s",
                rejection.conversation_id,
                type(exc).__name__,
            )
            return
        await self._supabase.mark_meta_delivery_rejection_notified(  # type: ignore[attr-defined]
            rejection_id=rejection_id, now=self._now_iso()
        )
        counts["notified"] += 1

    def _log_skip(self, message_id: int, reason: str, counts: Counter[str]) -> None:
        counts[f"retry_{reason}"] += 1
        if (message_id, reason) in self._skips_logged:
            return
        self._skips_logged.add((message_id, reason))
        logger.info("meta_first_touch_retry_skipped reason=%s message=%s", reason, message_id)

    async def _retry_due(self, counts: Counter[str]) -> None:
        retry = self._retry
        assert retry is not None
        due = await self._supabase.list_due_meta_first_touch_retries(  # type: ignore[attr-defined]
            chatwoot_inbox_id=self._inbox_id,
            max_age_seconds=retry.max_age_seconds,
            limit=min(50, retry.max_per_scan * 5),
            now=self._now_iso(),
        )
        sent = 0
        for item in due:
            if sent >= retry.max_per_scan or self._stopping.is_set():
                break
            entry = await self._chatwoot.get_conversation_with_messages(  # type: ignore[attr-defined]
                conversation_id=item.external_conversation_id,
                expected_inbox_id=self._inbox_id,
            )
            plan = evaluate_retry(
                entry,
                message_id=item.chatwoot_message_id,
                inbox_id=self._inbox_id,
                now_epoch=int(self._clock()),
                allowed_templates=retry.allowed_templates,
                max_age_seconds=retry.max_age_seconds,
            )
            if isinstance(plan, str):
                self._log_skip(item.chatwoot_message_id, plan, counts)
                if plan in PERMANENT_SKIPS:
                    await self._supabase.block_meta_first_touch_retry(  # type: ignore[attr-defined]
                        rejection_id=item.rejection_id,
                        reason=f"bridge_{plan}",
                        now=self._now_iso(),
                    )
                continue
            delivery_id = f"meta-retry:{self._inbox_id}:{plan.message_id}"
            if not retry.gate.authorize(
                FinalMetaEffect(
                    delivery_id=delivery_id,
                    action_kind="first_touch",
                    mode="approved_template",
                    target_phone=plan.target_phone,
                    content=plan.content,
                    template_name=plan.template_name,
                    template_language=plan.template_language,
                )
            ):
                self._log_skip(plan.message_id, "final_meta_gate_closed", counts)
                continue
            claim = await self._supabase.claim_meta_first_touch_retry(  # type: ignore[attr-defined]
                chatwoot_inbox_id=self._inbox_id,
                chatwoot_message_id=plan.message_id,
                external_user_id=plan.target_phone.removeprefix("+"),
                contact_email=plan.contact_email,
                max_age_seconds=retry.max_age_seconds,
                now=self._now_iso(),
            )
            if claim.outcome != "claimed":
                self._log_skip(plan.message_id, claim.outcome, counts)
                continue
            assert claim.rejection_id is not None
            sent += 1
            await self._send(plan, claim.rejection_id, delivery_id, counts)

    async def _send(
        self, plan: RetryPlan, rejection_id: str, delivery_id: str, counts: Counter[str]
    ) -> None:
        status, message_id, reason = "failed", None, None
        try:
            result = await self._chatwoot.send_first_message(  # type: ignore[attr-defined]
                conversation_id=plan.conversation_id,
                content=plan.content,
                delivery_id=delivery_id,
                template_params=plan.template_params,
                retry_of_message_id=plan.message_id,
            )
            message_id = _positive_int(result.get("message_id")) if isinstance(result, dict) else None
            if message_id is not None:
                status = "sent"
            else:
                reason = "invalid_sent_message"
        except ChatwootProtocolError:
            reason = "chatwoot_protocol_error"
        except httpx.HTTPError:
            reason = "chatwoot_http_error"
        except Exception as exc:  # noqa: BLE001
            # Sin saber si salio, la reserva queda en claimed: no se reenvia nunca.
            counts["retry_unknown"] += 1
            logger.warning(
                "meta_first_touch_retry_unknown message=%s error=%s",
                plan.message_id,
                type(exc).__name__,
            )
            return
        try:
            await self._supabase.settle_meta_first_touch_retry(  # type: ignore[attr-defined]
                rejection_id=rejection_id,
                status=status,
                retry_message_id=message_id,
                failure_reason=reason,
                now=self._now_iso(),
            )
        except SupabaseError as exc:
            logger.warning(
                "meta_first_touch_retry_settle_failed message=%s error=%s",
                plan.message_id,
                type(exc).__name__,
            )
        counts["retried" if status == "sent" else "retry_failed"] += 1
        logger.info(
            "meta_first_touch_retry status=%s template=%s conversation=%s message=%s",
            status,
            plan.template_name,
            plan.conversation_id,
            plan.message_id,
        )

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            self._last_scan_state = "stopped"
            return
        self._stopping.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._last_scan_state = "stopped"

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001
                self._last_scan_state = "error"
                logger.warning(
                    "meta_delivery_scan_failed error_type=%s", type(exc).__name__
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._scan_interval_seconds
                )
            except TimeoutError:
                pass


def parse_retry_templates(raw: str) -> frozenset[str]:
    """``META_FIRST_TOUCH_RETRY_TEMPLATES``: nombres separados por coma."""
    names = [part.strip() for part in raw.split(",")]
    if any(not name or re.fullmatch(r"[a-z0-9_]{1,512}", name) is None for name in names):
        raise ValueError(
            "META_FIRST_TOUCH_RETRY_TEMPLATES must be comma-separated template names"
        )
    return frozenset(names)


__all__: Sequence[str] = (
    "MetaDeliveryRejectionSweeper",
    "Rejection",
    "RetryConfig",
    "RetryPlan",
    "evaluate_retry",
    "find_rejections",
    "meta_error_code",
    "parse_retry_templates",
    "touch_kind",
    "unreachable_notice",
)
