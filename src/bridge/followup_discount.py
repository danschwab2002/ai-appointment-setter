"""El seguimiento con cupon para quien nos contesto y no compro.

Decision de Dan del 2026-09-28: a quien ya hablo con nosotros, recibio nuestra
respuesta y se quedo callado 24 h sin comprar, se le manda UNA plantilla con un
10 % de descuento y un boton que abre el checkout de Hotmart con el cupon ya
cargado. A quien nunca contesto no se le manda nada: plantillas de marketing a
quien no interactua degradan la cuenta de Meta.

Cuatro piezas, separadas a proposito, con la misma forma que la reactivacion:

* ``parse_followup_template`` valida la plantilla del catalogo que publica
  Chatwoot: aprobada, tres marcadores y exactamente un boton de URL dinamica
  sobre ``https://pay.hotmart.com/``.
* ``followup_button_suffix`` arma la parte variable del boton desde el link que
  emitio la base, sumandole ``offDiscount``. El link ya trae la oferta del lead,
  ``src=hermes``, el ``sck`` con la marca del recuperador y el ``fbclid``.
* ``evaluate_followup_candidate`` es el criterio, puro y sin red: decide sobre
  el detalle de la conversacion y su historial si corresponde el seguimiento o
  por que no. Se testea contra payloads capturados de produccion.
* ``ConversationFollowupSweeper`` es el worker recurrente: reserva en Supabase
  (donde viven las barreras de compra, derivacion y opt-out), autoriza el link,
  manda la plantilla y cierra las dos filas con lo que Chatwoot responda.

No se pisa con la reactivacion: la reactivacion exige que el ultimo mensaje sea
del lead y el seguimiento que sea nuestro. Ver
``docs/contracts/conversation-followup-discount-v1.md``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from bridge.chatwoot import ChatwootProtocolError
from bridge.checkout_issuance import generate_issuance_ulid
from bridge.lead_first_name import resolve_greeting_name
from bridge.reactivation import _last_conversational_message, _scan_summary


logger = logging.getLogger(__name__)

# 24 h desde el ultimo mensaje del lead: la ventana de servicio de Meta ya se
# cerro, asi que el seguimiento sale por plantilla. 72 h es el techo: el dia que
# se prende el flag no le escribe a todo el historico (decision de Dan).
DEFAULT_MIN_INBOUND_AGE_SECONDS = 86_400
DEFAULT_MAX_INBOUND_AGE_SECONDS = 259_200
DEFAULT_SCAN_INTERVAL_SECONDS = 300.0

HOTMART_CHECKOUT_PREFIX = "https://pay.hotmart.com/"
FOLLOWUP_BUTTON_URL = HOTMART_CHECKOUT_PREFIX + "{{1}}"
COUPON_CODE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Lo que puede ir despues de pay.hotmart.com/ sin escapar nada: el link que
# emite la base ya viene con el separador del sck codificado (%7C).
_BUTTON_SUFFIX_RE = re.compile(r"[A-Za-z0-9_-]+\?[A-Za-z0-9._~%=&-]+")
MAX_BUTTON_SUFFIX_CHARS = 1_800

_E164_RE = re.compile(r"\+[1-9]\d{6,14}")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+")
_PLACEHOLDER_RE = re.compile(r"\{\{(\d+)\}\}")
_HOTMART_LINK_RE = re.compile(r"https://pay\.hotmart\.com/")
_PAYMENT_FAILED_MARKER = "no pudo completarse"

REGIME_LINK_SENT = "link_sent_no_purchase"
REGIME_WENT_QUIET = "went_quiet"
REGIME_PAYMENT_FAILED = "payment_failed"


@dataclass(frozen=True)
class FollowupTemplate:
    """La plantilla aprobada: nombre, producto y cupon, mas un boton de URL."""

    name: str
    language: str
    category: str
    body: str

    def render(self, *, first_name: str, product_name: str, coupon_code: str) -> str:
        """El texto que se ve en Chatwoot, con los marcadores reemplazados."""
        _require_parameters(first_name, product_name, coupon_code)
        return (
            self.body.replace("{{1}}", first_name)
            .replace("{{2}}", product_name)
            .replace("{{3}}", coupon_code)
        )

    def params(
        self,
        *,
        first_name: str,
        product_name: str,
        coupon_code: str,
        button_suffix: str,
    ) -> dict[str, object]:
        """``template_params`` tal como los espera la API de Chatwoot.

        Chatwoot 4.13 arma el componente del boton desde
        ``processed_params.buttons`` (``Whatsapp::TemplateProcessorService``,
        ``process_button_components``): cada entrada con ``type: 'url'`` se
        manda como ``sub_type: url`` con el ``parameter`` como texto, y el
        indice es la posicion en la lista. La plantilla tiene un solo boton,
        asi que va en el indice 0.
        """
        _require_parameters(first_name, product_name, coupon_code)
        if not isinstance(button_suffix, str) or not button_suffix:
            raise ValueError("followup_button_suffix_empty")
        return {
            "name": self.name,
            "category": self.category,
            "language": self.language,
            "processed_params": {
                "body": {"1": first_name, "2": product_name, "3": coupon_code},
                "buttons": [{"type": "url", "parameter": button_suffix}],
            },
        }


def _require_parameters(*values: str) -> None:
    # Una variable vacia hace fallar el envio en Meta: el lead no recibe nada y
    # el error vuelve tarde.
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("followup_template_parameter_empty")


def parse_followup_template(
    inbox_payload: object,
    *,
    template_name: str,
    expected_language: str | None = None,
) -> FollowupTemplate:
    """Extraer la plantilla del seguimiento del catalogo que publica Chatwoot.

    Falla cerrado ante cualquier duda, igual que la reactivacion: se lee del
    inbox en cada barrido para que una baja en Meta corte el envio.
    """
    if not isinstance(template_name, str) or not template_name.strip():
        raise ValueError("followup_template_name_missing")
    if not isinstance(inbox_payload, dict):
        raise ChatwootProtocolError("invalid_inbox_payload")
    templates = inbox_payload.get("message_templates")
    if not isinstance(templates, list):
        raise ChatwootProtocolError("invalid_inbox_payload")

    wanted = template_name.strip()
    matches = [
        template
        for template in templates
        if isinstance(template, dict) and template.get("name") == wanted
    ]
    if len(matches) != 1:
        raise ChatwootProtocolError("followup_template_not_found")
    template = matches[0]
    if template.get("status") != "APPROVED":
        raise ChatwootProtocolError("followup_template_not_approved")

    language = template.get("language")
    category = template.get("category")
    if (
        not isinstance(language, str)
        or not language.strip()
        or not isinstance(category, str)
        or not category.strip()
    ):
        raise ChatwootProtocolError("followup_template_invalid_metadata")
    if (
        expected_language is not None
        and language.strip() != expected_language.strip()
    ):
        raise ChatwootProtocolError("followup_template_language_mismatch")

    components = template.get("components")
    if not isinstance(components, list):
        raise ChatwootProtocolError("followup_template_invalid_body")
    bodies = [
        component
        for component in components
        if isinstance(component, dict) and component.get("type") == "BODY"
    ]
    if len(bodies) != 1:
        raise ChatwootProtocolError("followup_template_invalid_body")
    body = bodies[0].get("text")
    if not isinstance(body, str) or not body.strip():
        raise ChatwootProtocolError("followup_template_invalid_body")
    # Nombre, producto y cupon: ni uno mas ni uno menos. Con un marcador de mas
    # Meta rechaza el envio por falta de parametros.
    if sorted(set(_PLACEHOLDER_RE.findall(body))) != ["1", "2", "3"]:
        raise ChatwootProtocolError("followup_template_unexpected_placeholders")

    # Exactamente un boton, y tiene que ser de URL dinamica sobre el checkout
    # de Hotmart. Sin el boton, el cupon queda en un texto que hay que copiar a
    # mano, que es justo lo que el seguimiento viene a evitar.
    button_groups = [
        component
        for component in components
        if isinstance(component, dict) and component.get("type") == "BUTTONS"
    ]
    if len(button_groups) != 1:
        raise ChatwootProtocolError("followup_template_button_missing")
    buttons = button_groups[0].get("buttons")
    if not isinstance(buttons, list) or len(buttons) != 1:
        raise ChatwootProtocolError("followup_template_button_missing")
    button = buttons[0]
    if (
        not isinstance(button, dict)
        or button.get("type") != "URL"
        or button.get("url") != FOLLOWUP_BUTTON_URL
    ):
        raise ChatwootProtocolError("followup_template_button_not_dynamic_checkout")

    return FollowupTemplate(
        name=wanted,
        language=language.strip(),
        category=category.strip(),
        body=body,
    )


def followup_button_suffix(checkout_url_final: str, coupon_code: str) -> str:
    """La parte variable del boton: el link emitido mas ``offDiscount``.

    Medido el 2026-09-28 en el checkout real de Johanna: con ``offDiscount`` el
    campo del cupon (``#COUPON``) aparece desplegado y con el codigo ya escrito.
    El link que emitio la base no se toca: el cupon se agrega al final.
    """
    if not isinstance(coupon_code, str) or not COUPON_CODE_RE.fullmatch(coupon_code):
        raise ValueError("followup_coupon_code_invalid")
    if (
        not isinstance(checkout_url_final, str)
        or not checkout_url_final.startswith(HOTMART_CHECKOUT_PREFIX)
    ):
        raise ValueError("followup_checkout_url_invalid")
    suffix = (
        checkout_url_final[len(HOTMART_CHECKOUT_PREFIX):]
        + "&offDiscount="
        + coupon_code
    )
    if (
        len(suffix) > MAX_BUTTON_SUFFIX_CHARS
        or not _BUTTON_SUFFIX_RE.fullmatch(suffix)
    ):
        raise ValueError("followup_checkout_url_invalid")
    return suffix


@dataclass(frozen=True)
class FollowupCandidate:
    """Una conversacion lista para el seguimiento con cupon."""

    conversation_id: int
    last_inbound_message_id: int
    last_outbound_message_id: int
    inbound_age_seconds: int
    phone: str
    email: str | None
    contact_name: str
    regime: str

    @property
    def command_key(self) -> str:
        return f"followup:{self.conversation_id}:{self.last_outbound_message_id}"

    @property
    def external_user_id(self) -> str:
        return self.phone.lstrip("+")


@dataclass(frozen=True)
class FollowupDecision:
    """El veredicto sobre una conversacion: el candidato o por que no lo es."""

    candidate: FollowupCandidate | None
    skip_reason: str | None

    @property
    def eligible(self) -> bool:
        return self.candidate is not None


def _skip(reason: str) -> FollowupDecision:
    return FollowupDecision(candidate=None, skip_reason=reason)


def _sender_type(message: dict[str, object]) -> str | None:
    sender = message.get("sender")
    if not isinstance(sender, dict):
        return None
    value = sender.get("type")
    return value if isinstance(value, str) else None


def _content(message: dict[str, object]) -> str:
    value = message.get("content")
    return value if isinstance(value, str) else ""


def _followup_regime(conversational: Sequence[dict[str, object]]) -> str:
    """En cual de los tres regimenes esta la conversacion (solo para auditar).

    Todos reciben la misma plantilla; el regimen queda en la fila para poder
    medir despues cual convierte. La API de mensajes de Chatwoot no devuelve
    ``template_params`` (medido el 2026-09-28 sobre el inbox 9), asi que la
    plantilla de pago fallido se reconoce por su texto: la de Johanna y la de
    ATT1 dicen las dos "no pudo completarse".
    """
    outgoing = [message for message in conversational if message.get("message_type") != 0]
    if any(_PAYMENT_FAILED_MARKER in _content(message) for message in outgoing):
        return REGIME_PAYMENT_FAILED
    if any(_HOTMART_LINK_RE.search(_content(message)) for message in outgoing):
        return REGIME_LINK_SENT
    return REGIME_WENT_QUIET


def evaluate_followup_candidate(
    details: object,
    messages: Sequence[object],
    *,
    expected_inbox_id: int,
    now_epoch: int,
    min_inbound_age_seconds: int = DEFAULT_MIN_INBOUND_AGE_SECONDS,
    max_inbound_age_seconds: int = DEFAULT_MAX_INBOUND_AGE_SECONDS,
    allowed_phone: str | None = None,
) -> FollowupDecision:
    """Decidir si una conversacion recibe el seguimiento con cupon.

    Devuelve siempre un veredicto explicito: o el candidato completo, o el
    motivo por el que se salteo. La compra, la derivacion sin atender y el
    opt-out durable los chequea la base al reservar; aca se descarta lo que
    Chatwoot ya muestra.
    """
    if (
        not isinstance(expected_inbox_id, int)
        or isinstance(expected_inbox_id, bool)
        or expected_inbox_id <= 0
        or not isinstance(now_epoch, int)
        or isinstance(now_epoch, bool)
        or now_epoch <= 0
        or min_inbound_age_seconds < 0
        or max_inbound_age_seconds < min_inbound_age_seconds
    ):
        raise ValueError("invalid followup evaluation configuration")
    if not isinstance(details, dict):
        raise ChatwootProtocolError("invalid_conversation_payload")

    conversation_id = details.get("id")
    if (
        not isinstance(conversation_id, int)
        or isinstance(conversation_id, bool)
        or conversation_id <= 0
        or details.get("inbox_id") != expected_inbox_id
    ):
        raise ChatwootProtocolError("invalid_conversation_scope")

    if details.get("status") != "open":
        return _skip("conversation_not_open")
    if details.get("muted") is True:
        return _skip("conversation_muted")
    if details.get("snoozed_until") not in (None, ""):
        return _skip("conversation_snoozed")

    labels = details.get("labels")
    if labels is not None and not isinstance(labels, list):
        raise ChatwootProtocolError("invalid_conversation_labels")
    label_values = tuple(label for label in (labels or []) if isinstance(label, str))
    if len(label_values) != len(labels or []):
        raise ChatwootProtocolError("invalid_conversation_labels")
    if "automation_opted_out" in label_values:
        return _skip("contact_opted_out")
    # Pausada = en manos del equipo (una derivacion o una persona que escribio).
    if "automation_paused" in label_values:
        return _skip("conversation_paused")

    meta = details.get("meta")
    if not isinstance(meta, dict):
        raise ChatwootProtocolError("invalid_conversation_payload")
    sender = meta.get("sender")
    if not isinstance(sender, dict):
        raise ChatwootProtocolError("invalid_conversation_payload")
    if sender.get("blocked") is not False:
        return _skip("contact_blocked_or_unknown")

    phone = None
    for key in ("phone_number", "identifier"):
        value = sender.get(key)
        if isinstance(value, str) and _E164_RE.fullmatch(value.strip()):
            phone = value.strip()
            break
    if phone is None:
        return _skip("contact_phone_unreadable")
    if allowed_phone is not None and phone.lstrip("+") != allowed_phone.lstrip("+"):
        return _skip("target_not_allowed")

    raw_email = sender.get("email")
    email = (
        raw_email.strip()
        if isinstance(raw_email, str) and _EMAIL_RE.fullmatch(raw_email.strip())
        else None
    )
    raw_name = sender.get("name")
    contact_name = " ".join(raw_name.split()) if isinstance(raw_name, str) else ""
    if not contact_name:
        return _skip("contact_name_unusable")

    last_message = _last_conversational_message(messages)
    if last_message is None:
        return _skip("no_conversational_history")
    # El turno es del agente cuando el lead escribio ultimo: eso no es un
    # seguimiento (y si nadie contesta, es la reactivacion).
    if last_message.get("message_type") == 0:
        return _skip("last_message_inbound")
    # Un saliente de una persona del equipo pausa la conversacion. Si la
    # etiqueta todavia no esta, igual es del equipo, no del agente.
    if _sender_type(last_message) != "agent_bot":
        return _skip("last_message_not_from_agent")

    conversational = [
        message
        for message in messages
        if isinstance(message, dict)
        and message.get("private") is False
        and message.get("message_type") in (0, 1, 3)
    ]
    inbound = [
        message
        for message in conversational
        if message.get("message_type") == 0 and _sender_type(message) == "contact"
    ]
    # La regla de Dan: a quien nunca nos contesto no se le manda seguimiento.
    if not inbound:
        return _skip("never_replied")
    last_inbound = max(inbound, key=lambda message: message["id"])  # type: ignore[arg-type,return-value]
    last_inbound_id = last_inbound["id"]
    assert isinstance(last_inbound_id, int)
    if any(
        _sender_type(message) == "user" and message["id"] > last_inbound_id  # type: ignore[operator]
        for message in conversational
        if message.get("message_type") != 0
    ):
        return _skip("team_replied")

    created_at = last_inbound.get("created_at")
    if (
        not isinstance(created_at, int)
        or isinstance(created_at, bool)
        or created_at <= 0
    ):
        raise ChatwootProtocolError("invalid_message_timestamp")
    inbound_age_seconds = now_epoch - created_at
    if inbound_age_seconds < min_inbound_age_seconds:
        return _skip("inbound_too_recent")
    if inbound_age_seconds > max_inbound_age_seconds:
        return _skip("inbound_too_old")

    last_outbound_id = last_message.get("id")
    assert isinstance(last_outbound_id, int)
    return FollowupDecision(
        candidate=FollowupCandidate(
            conversation_id=conversation_id,
            last_inbound_message_id=last_inbound_id,
            last_outbound_message_id=last_outbound_id,
            inbound_age_seconds=inbound_age_seconds,
            phone=phone,
            email=email,
            contact_name=contact_name,
            regime=_followup_regime(conversational),
        ),
        skip_reason=None,
    )


class ConversationFollowupSweeper:
    """Barrer el inbox y mandar el seguimiento con cupon a quien corresponde."""

    def __init__(
        self,
        *,
        chatwoot: object,
        supabase: object,
        account_id: int,
        inbox_id: int,
        template_name: str,
        coupon_code: str,
        product_name: str,
        expected_template_language: str | None = None,
        scan_interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
        min_inbound_age_seconds: int = DEFAULT_MIN_INBOUND_AGE_SECONDS,
        max_inbound_age_seconds: int = DEFAULT_MAX_INBOUND_AGE_SECONDS,
        max_sends_per_scan: int = 10,
        max_pages: int = 5,
        allowed_phone: str | None = None,
        clock: Callable[[], float] = time.time,
        ulid_factory: Callable[[], str] = generate_issuance_ulid,
    ) -> None:
        if (
            not isinstance(account_id, int)
            or isinstance(account_id, bool)
            or account_id <= 0
            or not isinstance(inbox_id, int)
            or isinstance(inbox_id, bool)
            or inbox_id <= 0
            or not isinstance(template_name, str)
            or not template_name.strip()
            or not isinstance(coupon_code, str)
            or not COUPON_CODE_RE.fullmatch(coupon_code)
            or not isinstance(product_name, str)
            or not product_name.strip()
            or not math.isfinite(scan_interval_seconds)
            or scan_interval_seconds <= 0
            or min_inbound_age_seconds < 0
            or max_inbound_age_seconds < min_inbound_age_seconds
            or not isinstance(max_sends_per_scan, int)
            or isinstance(max_sends_per_scan, bool)
            or max_sends_per_scan < 1
            or not 1 <= max_pages <= 20
        ):
            raise ValueError("invalid conversation followup configuration")
        self._chatwoot = chatwoot
        self._supabase = supabase
        self._account_id = account_id
        self._inbox_id = inbox_id
        self._template_name = template_name.strip()
        self._coupon_code = coupon_code
        self._product_name = product_name.strip()
        self._expected_template_language = expected_template_language
        self._scan_interval_seconds = scan_interval_seconds
        self._min_inbound_age_seconds = min_inbound_age_seconds
        self._max_inbound_age_seconds = max_inbound_age_seconds
        self._max_sends_per_scan = max_sends_per_scan
        self._max_pages = max_pages
        self._allowed_phone = allowed_phone
        self._clock = clock
        self._ulid_factory = ulid_factory
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_scan_state = "never"
        self._has_completed_scan = False
        self._last_sent_count = 0
        self._last_scan_summary = "never"
        self._last_attempt_failed = False
        self._last_outcome: str | None = None

    @property
    def last_scan_state(self) -> str:
        return self._last_scan_state

    @property
    def last_scan_summary(self) -> str:
        """Conteos y motivos del ultimo barrido, sin datos de nadie."""
        return self._last_scan_summary

    @property
    def last_sent_count(self) -> int:
        return self._last_sent_count

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._last_scan_state = "never"
            self._has_completed_scan = False
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

    async def run_once(self) -> int:
        """Un barrido completo. Devuelve cuantos seguimientos salieron."""
        inbox_payload = await self._chatwoot.get_inbox(inbox_id=self._inbox_id)
        template = parse_followup_template(
            inbox_payload,
            template_name=self._template_name,
            expected_language=self._expected_template_language,
        )
        conversations = await self._chatwoot.list_open_conversations_with_messages(
            expected_inbox_id=self._inbox_id,
            max_pages=self._max_pages,
        )
        now_epoch = int(self._clock())
        sent = 0
        failed = False
        motivos: Counter[str] = Counter()
        for entry in conversations:
            if sent >= self._max_sends_per_scan:
                break
            details = entry.get("conversation")
            messages = entry.get("messages")
            if not isinstance(details, dict) or not isinstance(messages, list):
                raise ChatwootProtocolError("invalid_conversation_payload")
            decision = evaluate_followup_candidate(
                details,
                messages,
                expected_inbox_id=self._inbox_id,
                now_epoch=now_epoch,
                min_inbound_age_seconds=self._min_inbound_age_seconds,
                max_inbound_age_seconds=self._max_inbound_age_seconds,
                allowed_phone=self._allowed_phone,
            )
            if decision.candidate is None:
                motivos[decision.skip_reason or "unknown"] += 1
                continue
            if await self._follow_up(decision.candidate, template):
                sent += 1
            else:
                motivos[self._last_outcome or "not_sent"] += 1
                failed = failed or self._last_attempt_failed
        self._last_sent_count = sent
        self._has_completed_scan = True
        self._last_scan_state = "error" if failed else "healthy"
        self._last_scan_summary = _scan_summary(
            scanned=len(conversations), sent=sent, motivos=motivos
        )
        logger.info("conversation_followup_scan %s", self._last_scan_summary)
        return sent

    async def _still_waiting(self, candidate: FollowupCandidate) -> bool:
        """Releer la conversacion justo antes de reservar.

        El barrido junta el inbox entero y recien despues manda: si el lead
        escribio en el medio, el ultimo mensaje ya no es el nuestro y el turno
        es del agente, no del cupon.
        """
        messages = await self._chatwoot.get_conversation_messages(
            conversation_id=candidate.conversation_id,
            limit=30,
        )
        latest = _last_conversational_message(messages)
        return (
            latest is not None
            and latest.get("id") == candidate.last_outbound_message_id
        )

    async def _follow_up(
        self,
        candidate: FollowupCandidate,
        template: FollowupTemplate,
    ) -> bool:
        """Reservar, autorizar el link, mandar y cerrar. True solo si salio."""
        self._last_attempt_failed = False
        self._last_outcome = None
        try:
            if not await self._still_waiting(candidate):
                self._last_outcome = "lead_wrote_meanwhile"
                return False
        except Exception as exc:
            self._last_attempt_failed = True
            self._last_outcome = "recheck_failed"
            logger.warning(
                "conversation_followup_recheck_failed conversation=%s error_type=%s",
                candidate.conversation_id,
                type(exc).__name__,
            )
            return False

        greeting = await resolve_greeting_name(
            candidate.contact_name, store=self._supabase  # type: ignore[arg-type]
        )
        command_key = candidate.command_key
        try:
            claim = await self._supabase.claim_conversation_followup(
                external_conversation_id=candidate.conversation_id,
                chatwoot_account_id=self._account_id,
                chatwoot_inbox_id=self._inbox_id,
                external_user_id=candidate.external_user_id,
                contact_email=candidate.email,
                command_key=command_key,
                regime=candidate.regime,
                template_name=template.name,
                template_language=template.language,
                coupon_code=self._coupon_code,
                last_inbound_message_id=candidate.last_inbound_message_id,
                last_outbound_message_id=candidate.last_outbound_message_id,
                inbound_age_seconds=candidate.inbound_age_seconds,
                issuance_ulid=self._ulid_factory(),
            )
        except Exception as exc:
            self._last_attempt_failed = True
            self._last_outcome = "claim_failed"
            logger.warning(
                "conversation_followup_claim_failed conversation=%s error_type=%s",
                candidate.conversation_id,
                type(exc).__name__,
            )
            return False
        if claim.outcome != "claimed":
            # 'replayed' tambien corta: una reserva viva con este command_key
            # es un envio que ya salio o que quedo en vuelo.
            self._last_outcome = claim.outcome
            return False
        assert claim.checkout_issuance_id is not None
        assert claim.checkout_url_final is not None

        # La reserva ya esta tomada: de aca en mas todo camino cierra la fila.
        try:
            suffix = followup_button_suffix(
                claim.checkout_url_final, self._coupon_code
            )
            params = template.params(
                first_name=greeting.name,
                product_name=self._product_name,
                coupon_code=self._coupon_code,
                button_suffix=suffix,
            )
            content = template.render(
                first_name=greeting.name,
                product_name=self._product_name,
                coupon_code=self._coupon_code,
            )
        except ValueError as exc:
            self._last_attempt_failed = True
            self._last_outcome = "template_params_invalid"
            await self._settle(command_key, status="failed", failure_reason=str(exc))
            return False

        issuance_context = {
            "external_user_id": candidate.external_user_id,
            "chatwoot_account_id": self._account_id,
            "chatwoot_inbox_id": self._inbox_id,
            "chatwoot_conversation_id": candidate.conversation_id,
            "trigger_external_message_id": str(candidate.last_outbound_message_id),
        }
        try:
            authorization = await self._supabase.authorize_chatwoot_checkout_issuance_v2(
                issuance_id=claim.checkout_issuance_id,
                now=_now_iso(),
                **issuance_context,
            )
        except Exception as exc:
            self._last_attempt_failed = True
            self._last_outcome = "issuance_authorize_failed"
            await self._settle(
                command_key, status="failed", failure_reason=type(exc).__name__
            )
            return False
        if authorization.outcome != "request_started":
            self._last_outcome = f"issuance_{authorization.outcome}"
            await self._settle(
                command_key,
                status="failed",
                failure_reason=f"issuance_{authorization.outcome}",
            )
            return False

        try:
            result = await self._chatwoot.send_followup_template(
                conversation_id=candidate.conversation_id,
                content=content,
                command_key=command_key,
                template_params=params,
            )
        except Exception as exc:
            # Resultado incierto: el link queda 'delivery_unknown' y la base no
            # deja volver a mandarlo. Mandar dos veces un cupon es peor que no
            # mandarlo.
            self._last_attempt_failed = True
            self._last_outcome = "send_failed"
            await self._finalize_issuance(
                claim.checkout_issuance_id,
                status="delivery_unknown",
                message_id=None,
                failure_code="chatwoot_followup_send_unconfirmed",
            )
            await self._settle(
                command_key, status="failed", failure_reason=type(exc).__name__
            )
            logger.warning(
                "conversation_followup_send_failed conversation=%s error_type=%s",
                candidate.conversation_id,
                type(exc).__name__,
            )
            return False

        message_id = result.get("message_id") if isinstance(result, dict) else None
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            self._last_attempt_failed = True
            self._last_outcome = "invalid_sent_message"
            await self._finalize_issuance(
                claim.checkout_issuance_id,
                status="delivery_unknown",
                message_id=None,
                failure_code="chatwoot_followup_invalid_result",
            )
            await self._settle(
                command_key, status="failed", failure_reason="invalid_sent_message"
            )
            return False

        await self._finalize_issuance(
            claim.checkout_issuance_id,
            status="accepted_by_chatwoot",
            message_id=message_id,
            failure_code=None,
        )
        await self._settle(command_key, status="sent", provider_message_id=message_id)
        logger.info(
            "conversation_followup_sent conversation=%s regime=%s age_seconds=%s greeting=%s",
            candidate.conversation_id,
            candidate.regime,
            candidate.inbound_age_seconds,
            greeting.source,
        )
        return True

    async def _finalize_issuance(
        self,
        issuance_id: str,
        *,
        status: str,
        message_id: int | None,
        failure_code: str | None,
    ) -> None:
        try:
            await self._supabase.finalize_chatwoot_checkout_issuance_v2(
                issuance_id=issuance_id,
                status=status,
                chatwoot_message_id=message_id,
                failure_code=failure_code,
                now=_now_iso(),
            )
        except Exception:
            # La emision queda 'request_started', que tambien bloquea un
            # reenvio. Es el lado seguro.
            logger.warning("conversation_followup_issuance_finalize_failed status=%s", status)

    async def _settle(
        self,
        command_key: str,
        *,
        status: str,
        provider_message_id: int | None = None,
        failure_reason: str | None = None,
    ) -> None:
        try:
            await self._supabase.settle_conversation_followup(
                command_key=command_key,
                status=status,
                provider_message_id=provider_message_id,
                failure_reason=(failure_reason or "")[:200] or None,
            )
        except Exception:
            # Perder el cierre deja la fila en 'claimed', que bloquea un
            # reenvio. Es el lado seguro.
            logger.warning("conversation_followup_settle_failed status=%s", status)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                self._last_scan_state = "error"
                logger.warning(
                    "conversation_followup_scan_failed error_type=%s",
                    type(exc).__name__,
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._scan_interval_seconds,
                )
            except TimeoutError:
                pass


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
