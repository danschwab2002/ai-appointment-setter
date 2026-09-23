"""Reactivar la conversacion que se cayo fuera de la ventana de 24 h.

El sistema de reanudacion (``resume_paused_conversation``) levanta la pausa de
una conversacion cuando el equipo lleva horas sin atenderla, pero su disparador
vive dentro del worker del webhook de Chatwoot: corre unicamente cuando entra un
mensaje del lead. Si el lead ya escribio y esta esperando, no va a escribir de
nuevo. Este modulo produce ese mensaje entrante que falta: manda una plantilla
aprobada de Meta a quien quedo esperando fuera de la ventana de servicio, para
que responda y el resto del sistema arranque solo.

Tres piezas, separadas a proposito:

* ``parse_reactivation_template`` valida el catalogo de plantillas que publica
  Chatwoot y devuelve el cuerpo con el que se arma el mensaje.
* ``evaluate_reactivation_candidate`` es el criterio, puro y sin red: recibe el
  detalle de la conversacion y su historial y decide si se reactiva o por que
  no. Se testea contra payloads capturados de produccion.
* ``ConversationReactivationSweeper`` es el worker recurrente que junta las dos
  cosas, reserva el envio en Supabase y lo cierra con lo que Chatwoot responda.

Ver ``docs/contracts/conversation-reactivation-v1.md``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from bridge.chatwoot import (
    ChatwootProtocolError,
    TeamMessageTimestampError,
    seconds_since_last_team_message,
)


logger = logging.getLogger(__name__)

# La ventana de servicio de Meta dura 24 h desde el ultimo mensaje del usuario.
# Chatwoot la publica como can_reply, asi que el barredor no la calcula: la lee.
# Este piso existe igual porque can_reply es booleano y un margen propio evita
# competir con el monitor de conversaciones estancadas, que atiende el otro lado
# de esa misma frontera.
DEFAULT_MIN_INBOUND_AGE_SECONDS = 86_400
# A los 30 dias, reactivar deja de ser retomar una conversacion y pasa a ser
# escribirle a un desconocido sobre algo que ya no recuerda.
DEFAULT_MAX_INBOUND_AGE_SECONDS = 2_592_000
DEFAULT_SCAN_INTERVAL_SECONDS = 900.0
DEFAULT_MAX_REACTIVATIONS = 1

_E164_RE = re.compile(r"\+[1-9]\d{6,14}")
_DIGITS_RE = re.compile(r"[0-9]")
_NAME_WORD_RE = re.compile(r"^[^\W\d_](?:[^\W\d_]|['’-])*$", re.UNICODE)
_PLACEHOLDER_RE = re.compile(r"\{\{(\d+)\}\}")

MIN_FIRST_NAME_CHARS = 2
MAX_FIRST_NAME_CHARS = 40


@dataclass(frozen=True)
class ReactivationTemplate:
    """Una plantilla aprobada de Meta, con un unico marcador en el cuerpo."""

    name: str
    language: str
    category: str
    body: str

    def render(self, first_name: str) -> str:
        """El texto que se ve en Chatwoot, con el marcador ya reemplazado."""
        if not first_name.strip():
            raise ValueError("reactivation_template_parameter_empty")
        return self.body.replace("{{1}}", first_name)

    def params(self, first_name: str) -> dict[str, object]:
        """``template_params`` tal como los espera la API de Chatwoot."""
        if not first_name.strip():
            raise ValueError("reactivation_template_parameter_empty")
        return {
            "name": self.name,
            "category": self.category,
            "language": self.language,
            "processed_params": {"body": {"1": first_name}},
        }


def parse_reactivation_template(
    inbox_payload: object,
    *,
    template_name: str,
    expected_language: str | None = None,
) -> ReactivationTemplate:
    """Extraer una plantilla aprobada del catalogo que publica Chatwoot.

    Falla cerrado ante cualquier duda. Una plantilla que Meta todavia no aprobo,
    que tiene dos marcadores, o que dejo de existir, no se manda: se lee del
    inbox en cada barrido justamente para que una baja en Meta corte el envio en
    vez de producir mensajes rechazados.
    """
    if not isinstance(template_name, str) or not template_name.strip():
        raise ValueError("reactivation_template_name_missing")
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
        raise ChatwootProtocolError("reactivation_template_not_found")
    template = matches[0]

    if template.get("status") != "APPROVED":
        raise ChatwootProtocolError("reactivation_template_not_approved")
    language = template.get("language")
    category = template.get("category")
    if (
        not isinstance(language, str)
        or not language.strip()
        or not isinstance(category, str)
        or not category.strip()
    ):
        raise ChatwootProtocolError("reactivation_template_invalid_metadata")
    if (
        expected_language is not None
        and language.strip() != expected_language.strip()
    ):
        raise ChatwootProtocolError("reactivation_template_language_mismatch")

    components = template.get("components")
    if not isinstance(components, list):
        raise ChatwootProtocolError("reactivation_template_invalid_body")
    bodies = [
        component
        for component in components
        if isinstance(component, dict) and component.get("type") == "BODY"
    ]
    if len(bodies) != 1:
        raise ChatwootProtocolError("reactivation_template_invalid_body")
    body = bodies[0].get("text")
    if not isinstance(body, str) or not body.strip():
        raise ChatwootProtocolError("reactivation_template_invalid_body")

    # Exactamente un marcador, y tiene que ser {{1}}. Con cero, el nombre no
    # entra a ningun lado; con dos, Meta rechaza el envio por falta de
    # parametros y el lead nunca recibe nada.
    placeholders = sorted(set(_PLACEHOLDER_RE.findall(body)))
    if placeholders != ["1"]:
        raise ChatwootProtocolError("reactivation_template_unexpected_placeholders")

    return ReactivationTemplate(
        name=wanted,
        language=language.strip(),
        category=category.strip(),
        body=body,
    )


def reactivation_first_name(raw_name: object) -> str | None:
    """El primer nombre con el que se puede saludar, o ``None``.

    Chatwoot guarda como nombre de contacto el push name de WhatsApp, que puede
    ser un nombre completo en minusculas, un apodo, un numero de telefono o un
    string vacio. Medido el 23/09 sobre las 6 conversaciones que esperaban
    respuesta: "Patricia Garcia", "Andres felipe galvis loaiza", "Chayin",
    "Angel Crown", "Marcia Aidegart Narvaez Lara" y "Mau". Saludar con el nombre
    completo queda mal en cuatro de seis; saludar con un telefono queda mal
    siempre. Sin un primer nombre usable no se manda la plantilla: la variable
    vacia hace fallar el envio en Meta.
    """
    if not isinstance(raw_name, str):
        return None
    collapsed = " ".join(raw_name.split())
    if not collapsed:
        return None
    if _DIGITS_RE.search(collapsed):
        return None
    first = collapsed.split(" ")[0]
    if not MIN_FIRST_NAME_CHARS <= len(first) <= MAX_FIRST_NAME_CHARS:
        return None
    if not _NAME_WORD_RE.match(first):
        return None
    if unicodedata.category(first[0]) not in {"Ll", "Lu", "Lt", "Lo"}:
        return None
    # "andres" y "ANDRES" se saludan igual de mal. Una palabra mixta
    # ("McCarthy", "Ángel") se deja como el contacto la escribio.
    if first.islower() or first.isupper():
        return first[0].upper() + first[1:].lower()
    return first


@dataclass(frozen=True)
class ReactivationCandidate:
    """Una conversacion lista para recibir la plantilla de reactivacion."""

    conversation_id: int
    last_inbound_message_id: int
    inbound_age_seconds: int
    quiet_seconds: int | None
    first_name: str
    phone: str

    @property
    def command_key(self) -> str:
        return f"reactivate:{self.conversation_id}:{self.last_inbound_message_id}"


@dataclass(frozen=True)
class ReactivationDecision:
    """El veredicto sobre una conversacion: el candidato o por que no lo es."""

    candidate: ReactivationCandidate | None
    skip_reason: str | None

    @property
    def eligible(self) -> bool:
        return self.candidate is not None


def _skip(reason: str) -> ReactivationDecision:
    return ReactivationDecision(candidate=None, skip_reason=reason)


def _canonical_phone(sender: dict[str, object]) -> str | None:
    """El telefono E.164 del contacto, si Chatwoot lo publica sin ambiguedad."""
    for key in ("phone_number", "identifier"):
        value = sender.get(key)
        if isinstance(value, str) and _E164_RE.fullmatch(value.strip()):
            return value.strip()
    return None


def _last_conversational_message(
    messages: Sequence[object],
) -> dict[str, object] | None:
    """El ultimo mensaje publico conversacional, por ID entero.

    Mismo criterio canonico que el monitor de conversaciones estancadas: el
    orden lo da el ID de Chatwoot y no ``created_at``, las actividades de
    sistema (``message_type`` 2) no participan, y un tipo publico desconocido o
    malformado invalida todo el historial en vez de ignorarse.
    """
    latest: dict[str, object] | None = None
    latest_id = -1
    for message in messages:
        if not isinstance(message, dict):
            raise ChatwootProtocolError("invalid_messages_payload")
        if message.get("private") is not False:
            continue
        message_type = message.get("message_type")
        if not isinstance(message_type, int) or isinstance(message_type, bool):
            raise ChatwootProtocolError("invalid_message_type")
        if message_type == 2:
            continue
        if message_type not in (0, 1, 3):
            raise ChatwootProtocolError("invalid_message_type")
        message_id = message.get("id")
        if (
            not isinstance(message_id, int)
            or isinstance(message_id, bool)
            or message_id <= 0
        ):
            raise ChatwootProtocolError("invalid_message_id")
        if message_id > latest_id:
            latest_id = message_id
            latest = message
    return latest


def evaluate_reactivation_candidate(
    details: object,
    messages: Sequence[object],
    *,
    expected_inbox_id: int,
    now_epoch: int,
    min_inbound_age_seconds: int = DEFAULT_MIN_INBOUND_AGE_SECONDS,
    max_inbound_age_seconds: int = DEFAULT_MAX_INBOUND_AGE_SECONDS,
    quiet_seconds_threshold: int = 28_800,
    allowed_phone: str | None = None,
) -> ReactivationDecision:
    """Decidir si una conversacion recibe la plantilla de reactivacion.

    Devuelve siempre un veredicto explicito: o el candidato completo, o el
    motivo por el que se salteo. Ningun camino devuelve un candidato a medias.
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
        or quiet_seconds_threshold < 0
    ):
        raise ValueError("invalid reactivation evaluation configuration")
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
    # can_reply es la ventana de servicio de 24 h de Meta tal como la ve
    # Chatwoot. Con true todavia entra texto libre y la conversacion le
    # corresponde al monitor de estancadas, no a una plantilla de marketing.
    if details.get("can_reply") is not False:
        return _skip("inside_service_window")
    if details.get("muted") is True:
        return _skip("conversation_muted")
    if details.get("snoozed_until") not in (None, ""):
        return _skip("conversation_snoozed")

    labels = details.get("labels")
    if labels is not None and not isinstance(labels, list):
        raise ChatwootProtocolError("invalid_conversation_labels")
    label_values = tuple(
        label for label in (labels or []) if isinstance(label, str)
    )
    if len(label_values) != len(labels or []):
        raise ChatwootProtocolError("invalid_conversation_labels")
    if "automation_opted_out" in label_values:
        return _skip("contact_opted_out")

    meta = details.get("meta")
    if not isinstance(meta, dict):
        raise ChatwootProtocolError("invalid_conversation_payload")
    sender = meta.get("sender")
    if not isinstance(sender, dict):
        raise ChatwootProtocolError("invalid_conversation_payload")
    # Solo un false explicito habilita: ausente o nulo es desconocido, y no se
    # le escribe a un contacto cuyo bloqueo no se pudo leer.
    if sender.get("blocked") is not False:
        return _skip("contact_blocked_or_unknown")

    phone = _canonical_phone(sender)
    if phone is None:
        return _skip("contact_phone_unreadable")
    if allowed_phone is not None and phone.lstrip("+") != allowed_phone.lstrip(
        "+"
    ):
        return _skip("target_not_allowed")

    first_name = reactivation_first_name(sender.get("name"))
    if first_name is None:
        return _skip("contact_name_unusable")

    last_message = _last_conversational_message(messages)
    if last_message is None:
        return _skip("no_conversational_history")
    if last_message.get("message_type") != 0:
        return _skip("last_message_not_inbound")
    last_sender = last_message.get("sender")
    if (
        not isinstance(last_sender, dict)
        or last_sender.get("type") != "contact"
    ):
        return _skip("last_message_not_from_contact")
    content = last_message.get("content")
    if not isinstance(content, str) or not content.strip():
        return _skip("last_message_empty")

    created_at = last_message.get("created_at")
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

    # El assignee no sirve como senal: las derivaciones de este inbox van a un
    # team sin asignado individual, asi que la conversacion figura sin humano
    # incluso mientras una persona la esta contestando. Lo que se mide es el
    # silencio real del equipo. Si un mensaje del equipo tiene fecha ilegible,
    # seconds_since_last_team_message falla cerrado y la conversacion se saltea.
    quiet_seconds = seconds_since_last_team_message(
        [message for message in messages if isinstance(message, dict)],
        now_epoch=now_epoch,
    )
    if quiet_seconds is not None and quiet_seconds < quiet_seconds_threshold:
        return _skip("team_recently_active")

    message_id = last_message.get("id")
    assert isinstance(message_id, int)
    return ReactivationDecision(
        candidate=ReactivationCandidate(
            conversation_id=conversation_id,
            last_inbound_message_id=message_id,
            inbound_age_seconds=inbound_age_seconds,
            quiet_seconds=quiet_seconds,
            first_name=first_name,
            phone=phone,
        ),
        skip_reason=None,
    )


def _scan_summary(
    *, scanned: int, sent: int, motivos: "Counter[str]", top: int = 4
) -> str:
    """Resumir un barrido sin filtrar contenido ni identidad de nadie.

    Solo lleva conteos y nombres de motivo, que son constantes del codigo.
    """
    partes = [f"scanned={scanned}", f"sent={sent}"]
    for motivo, cuenta in motivos.most_common(top):
        partes.append(f"{motivo}={cuenta}")
    restantes = len(motivos) - min(len(motivos), top)
    if restantes > 0:
        partes.append(f"other_reasons={restantes}")
    return " ".join(partes)


class ConversationReactivationSweeper:
    """Barrer el inbox y mandar la plantilla a quien quedo fuera de ventana."""

    def __init__(
        self,
        *,
        chatwoot: object,
        supabase: object,
        inbox_id: int,
        template_name: str,
        expected_template_language: str | None = None,
        scan_interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
        min_inbound_age_seconds: int = DEFAULT_MIN_INBOUND_AGE_SECONDS,
        max_inbound_age_seconds: int = DEFAULT_MAX_INBOUND_AGE_SECONDS,
        quiet_seconds_threshold: int = 28_800,
        max_reactivations: int = DEFAULT_MAX_REACTIVATIONS,
        max_sends_per_scan: int = 10,
        max_pages: int = 5,
        allowed_phone: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            not isinstance(inbox_id, int)
            or isinstance(inbox_id, bool)
            or inbox_id <= 0
            or not isinstance(template_name, str)
            or not template_name.strip()
            or not math.isfinite(scan_interval_seconds)
            or scan_interval_seconds <= 0
            or min_inbound_age_seconds < 0
            or max_inbound_age_seconds < min_inbound_age_seconds
            or quiet_seconds_threshold < 0
            or not isinstance(max_reactivations, int)
            or isinstance(max_reactivations, bool)
            or max_reactivations < 1
            or not isinstance(max_sends_per_scan, int)
            or isinstance(max_sends_per_scan, bool)
            or max_sends_per_scan < 1
            or not 1 <= max_pages <= 20
        ):
            raise ValueError("invalid conversation reactivation configuration")
        self._chatwoot = chatwoot
        self._supabase = supabase
        self._inbox_id = inbox_id
        self._template_name = template_name.strip()
        self._expected_template_language = expected_template_language
        self._scan_interval_seconds = scan_interval_seconds
        self._min_inbound_age_seconds = min_inbound_age_seconds
        self._max_inbound_age_seconds = max_inbound_age_seconds
        self._quiet_seconds_threshold = quiet_seconds_threshold
        self._max_reactivations = max_reactivations
        self._max_sends_per_scan = max_sends_per_scan
        self._max_pages = max_pages
        self._allowed_phone = allowed_phone
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_scan_state = "never"
        self._has_completed_scan = False
        self._last_sent_count = 0
        self._last_scan_summary = "never"

    @property
    def last_scan_state(self) -> str:
        return self._last_scan_state

    @property
    def last_scan_summary(self) -> str:
        """Una linea con lo que hizo el ultimo barrido y por que salteo.

        Un barrido que saltea a las 25 conversaciones del inbox y uno que no
        tiene a nadie a quien escribir terminan igual: sin envios y sin error.
        Sin este resumen, los dos publican `healthy` y son indistinguibles
        desde afuera. Paso el 2026-09-23: el barredor salteaba todo con
        `target_not_allowed` y `/ready` decia `healthy`.
        """
        return self._last_scan_summary

    @property
    def has_completed_scan(self) -> bool:
        return self._has_completed_scan

    @property
    def last_sent_count(self) -> int:
        return self._last_sent_count

    @property
    def ready(self) -> bool:
        return (
            self._task is not None
            and not self._task.done()
            and self._has_completed_scan
            and self._last_scan_state == "healthy"
        )

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
        """Un barrido completo. Devuelve cuantas plantillas salieron."""
        inbox_payload = await self._chatwoot.get_inbox(inbox_id=self._inbox_id)
        template = parse_reactivation_template(
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
            try:
                decision = evaluate_reactivation_candidate(
                    details,
                    messages,
                    expected_inbox_id=self._inbox_id,
                    now_epoch=now_epoch,
                    min_inbound_age_seconds=self._min_inbound_age_seconds,
                    max_inbound_age_seconds=self._max_inbound_age_seconds,
                    quiet_seconds_threshold=self._quiet_seconds_threshold,
                    allowed_phone=self._allowed_phone,
                )
            except TeamMessageTimestampError:
                motivos["team_message_timestamp_invalid"] += 1
                logger.warning(
                    "conversation_reactivation_skipped reason=%s",
                    "team_message_timestamp_invalid",
                )
                continue
            if decision.candidate is None:
                motivos[decision.skip_reason or "unknown"] += 1
                logger.debug(
                    "conversation_reactivation_skipped reason=%s",
                    decision.skip_reason,
                )
                continue
            if await self._reactivate(decision.candidate, template):
                sent += 1
            else:
                motivos["not_reactivated"] += 1
                failed = failed or self._last_attempt_failed
        self._last_sent_count = sent
        self._has_completed_scan = True
        self._last_scan_state = "error" if failed else "healthy"
        self._last_scan_summary = _scan_summary(
            scanned=len(conversations), sent=sent, motivos=motivos
        )
        logger.info(
            "conversation_reactivation_scan %s", self._last_scan_summary
        )
        return sent

    async def _reactivate(
        self,
        candidate: ReactivationCandidate,
        template: ReactivationTemplate,
    ) -> bool:
        """Reservar, mandar y cerrar. Devuelve True solo si la plantilla salio."""
        self._last_attempt_failed = False
        command_key = candidate.command_key
        try:
            claim = await self._supabase.claim_conversation_reactivation(
                external_conversation_id=candidate.conversation_id,
                command_key=command_key,
                reason_code="outside_service_window",
                template_name=template.name,
                template_language=template.language,
                last_inbound_message_id=candidate.last_inbound_message_id,
                inbound_age_seconds=candidate.inbound_age_seconds,
                quiet_seconds=candidate.quiet_seconds,
                max_reactivations=self._max_reactivations,
            )
        except Exception:
            self._last_attempt_failed = True
            logger.warning(
                "conversation_reactivation_claim_failed conversation=%s",
                candidate.conversation_id,
            )
            return False
        if claim.outcome != "claimed":
            logger.info(
                "conversation_reactivation_not_claimed conversation=%s outcome=%s",
                candidate.conversation_id,
                claim.outcome,
            )
            return False

        # La reserva ya esta tomada: de aca en mas todo camino cierra la fila,
        # porque una reserva 'claimed' colgada bloquea el reintento para siempre.
        try:
            result = await self._chatwoot.send_reactivation_template(
                conversation_id=candidate.conversation_id,
                content=template.render(candidate.first_name),
                command_key=command_key,
                template_params=template.params(candidate.first_name),
            )
        except Exception as exc:
            self._last_attempt_failed = True
            await self._settle(
                command_key,
                status="failed",
                failure_reason=type(exc).__name__,
            )
            logger.warning(
                "conversation_reactivation_send_failed conversation=%s error_type=%s",
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
            await self._settle(
                command_key,
                status="failed",
                failure_reason="invalid_sent_message",
            )
            return False

        await self._settle(
            command_key, status="sent", provider_message_id=message_id
        )
        logger.info(
            "conversation_reactivated conversation=%s age_seconds=%s quiet_seconds=%s",
            candidate.conversation_id,
            candidate.inbound_age_seconds,
            candidate.quiet_seconds,
        )
        return True

    async def _settle(
        self,
        command_key: str,
        *,
        status: str,
        provider_message_id: int | None = None,
        failure_reason: str | None = None,
    ) -> None:
        try:
            await self._supabase.settle_conversation_reactivation(
                command_key=command_key,
                status=status,
                provider_message_id=provider_message_id,
                failure_reason=failure_reason,
            )
        except Exception:
            # La plantilla ya salio (o ya fallo): perder el cierre deja la fila
            # en 'claimed', que bloquea un reenvio. Es el lado seguro.
            logger.warning(
                "conversation_reactivation_settle_failed status=%s", status
            )

    _last_attempt_failed = False

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                self._last_scan_state = "error"
                logger.warning(
                    "conversation_reactivation_scan_failed error_type=%s",
                    type(exc).__name__,
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._scan_interval_seconds,
                )
            except TimeoutError:
                pass


ReactivationScanner = Callable[[], Awaitable[int]]
