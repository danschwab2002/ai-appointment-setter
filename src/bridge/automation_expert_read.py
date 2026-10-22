"""Las lecturas del Automation Expert (automation-expert-read-v1).

Contrato: docs/contracts/automation-expert-read-v1.md. Este modulo es puro: no
hace I/O. Recibe lo que devuelven las tres funciones de la base y los mensajes
de Chatwoot que lee la ruta, y arma las respuestas del contrato:

- los flujos, proyectados con la configuracion de hoy en el formato del
  esquema (docs/contracts/flow-definition-v1.md), con la procedencia de cada
  valor;
- la busqueda de un lead por numero de conversacion, link o nombre;
- la historia, que junta la base y Chatwoot en una sola linea de tiempo.

Nada de lo que sale lleva un telefono, un mail, el nombre completo, el
contenido de un mensaje ni la URL de un link. De Chatwoot se copian campo por
campo la hora, el tipo de autor, el estado de entrega y si es una nota
privada; el resto se descarta aca adentro y no se escribe en ningun log.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

CONTRACT = "automation-expert-read-v1"
CONTRACT_VERSION = "1.1.0"
FLOW_KEYS = ("precheckout", "carrito", "pago_fallido", "reactivacion", "descuento")
HISTORY_DEFAULT_LIMIT = 50
HISTORY_MAX_LIMIT = 100
MAX_CANDIDATES = 5
MAX_FLOWS_BODY_BYTES = 256 * 1024
MAX_HISTORY_BODY_BYTES = 512 * 1024
CHATWOOT_MAX_MESSAGES_PER_CONVERSATION = 200
CHATWOOT_READ_TIMEOUT_SECONDS = 5.0

REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_:]{0,79}$")
_FLOW_KEY_SHAPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+")
_PHONE_SEPARATORS = frozenset(" \t\n\r\u00a0-.()+")
_CONVERSATION_RE = re.compile(r"#?([0-9]{1,6})")
_SEVEN_DIGITS_RE = re.compile(r"[0-9]{7}")
_SCOPE_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127} v[1-9][0-9]{0,8}$")
_META_ERROR_CODE_RE = re.compile(r"^(\d{3,6})(?:\D|$)")
_EVENT_TYPE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_SHORT_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_RELEASE_RE = re.compile(r"^[0-9a-f]{7,40}$")
_LANDING_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_OFFER_RE = re.compile(r"^[A-Za-z0-9]{4,32}$")
_HANDOFF_MARKER = "[supportmagician-handoff:"

MOMENT_KINDS = (
    "form_received", "first_contact_planned", "first_contact_not_planned", "hotmart_event",
    "rejected_on_entry", "entered_flow", "merged_into_open_case", "scheduled", "deferred_by_hours",
    "stopped", "start_authorized", "start_rejected", "accepted_by_chatwoot",
    "delivery_uncertain_or_failed", "meta_delivery", "replied_and_adopted", "agent_replied",
    "conversation_activity", "link_issued", "coupon_sent", "coupon_not_sent", "reactivation",
    "handed_off", "handoff_attended", "resumed", "opted_out", "purchased", "expired", "closed",
    "other",
)
_KINDS_FROM_DATABASE = frozenset(MOMENT_KINDS) - {"meta_delivery", "conversation_activity", "coupon_not_sent"}
_EVIDENCE_TABLES = frozenset({
    "purchase_intents", "portable_precheckout_first_contact_plans", "webhook_events", "recovery_cases",
    "conversation_events", "scheduled_actions", "pilot_outbound_request_authorizations",
    "flow_engine_request_authorizations", "followup_delivery_attempts", "agent_turn_provenance",
    "checkout_link_issuances",
    "conversation_followup_events", "conversation_reactivation_events", "human_handoff_requests",
    "conversation_resume_events", "contact_opt_out_events", "chatwoot.messages",
})

# Los motivos (contrato, «Los motivos»). La traduccion de los codigos de hoy;
# los que no tienen motivo salen con reason en null y su frase.
_REASON_BY_CODE = {
    "business_window_closed": "fuera_de_horario",
    "pilot_audience_intent_unresolved": "no_entra:permiso",
    "precheckout_intent_not_live": "ya_no_entra",
    "precheckout_authorization_lost": "ya_no_entra",
    "intent_purchased": "compro",
    "purchase_by_identity": "compro",
    "intent_purchase_ambiguous": "compro",
    "contact_opted_out": "baja",
    "pilot_audience_consented_intent_prior_opt_out": "baja",
    "stop_receiving_messages": "baja",
    "precheckout_contact_already_planned": "ya_en_curso",
    "precheckout_conversation_handoff": "tomada_por_el_equipo",
    "human_takeover_active": "tomada_por_el_equipo",
    "expired": "vencio",
    "expired_after_worker_crash": "vencio",
    "accepted_by_chatwoot": "completo",
    "policy_exhausted": "completo",
    "pilot_daily_budget_exhausted": "tope_del_flujo",
    "pilot_total_budget_exhausted": "tope_del_flujo",
    "delivery_unknown": "envio_incierto",
    "precheckout_prior_opt_out": "baja",
    "contact_blocked": "baja",
    "purchase_detected": "compro",
    "reevaluation_expired": "vencio",
    "no_business_window_before_expiry": "sin_horario",
    # Los del cupon, deducidos con el mismo criterio que su barrido.
    "last_message_inbound": "contesto",
    "last_message_not_from_agent": "tomada_por_el_equipo",
    "team_replied": "tomada_por_el_equipo",
    "inbound_too_old": "vencio",
}

# La frase fija de cada codigo. Un codigo que no esta aca sale con la frase y
# el motivo en null, y suma el hueco unknown_reason_code.
EXPLANATIONS = {
    "business_window_closed": "Le tocaba salir fuera del horario del flujo y esperó a la próxima apertura.",
    "superseded_by_provider_event": "Llegó un evento de Hotmart de un flujo que le gana, y este paso se canceló.",
    "pilot_runtime_not_armed": "El flujo no estaba prendido cuando llegó el evento.",
    "pilot_audience_intent_unresolved": "No se encontró un formulario con permiso de WhatsApp de esta persona para este evento.",
    "pilot_audience_consented_intent_prior_opt_out": "La persona se había dado de baja antes de que llegara el evento.",
    "pilot_contact_not_in_cohort": "La persona no estaba en la cohorte de prueba del flujo.",
    "pilot_scope_version_mismatch": "El paso era de una versión del flujo que ya no está activa, y no arrancó.",
    "pilot_daily_budget_exhausted": "Se había gastado el cupo del día del flujo.",
    "pilot_total_budget_exhausted": "Se había gastado el cupo total del flujo.",
    "precheckout_intent_not_live": "El formulario dejó de estar vigente para el primer contacto.",
    "precheckout_authorization_lost": "La persona dejó de tener un permiso vigente para el primer contacto.",
    "precheckout_contact_already_planned": "La persona ya tenía un primer contacto en curso o reciente.",
    "precheckout_conversation_handoff": "Una persona del equipo tenía la conversación.",
    "precheckout_contact_ambiguous": "Los datos del formulario llevaban a más de un contacto, y no se programó.",
    "first_contact_scheduled": "Se programó el primer contacto.",
    "intent_purchased": "La persona compró.",
    "purchase_by_identity": "Hay una compra aprobada con los datos de esta persona.",
    "intent_purchase_ambiguous": "Hay una compra aprobada que puede ser de esta persona, y no se le escribe.",
    "contact_opted_out": "La persona se dio de baja.",
    "stop_receiving_messages": "La persona pidió no recibir más mensajes.",
    "human_takeover_active": "Una persona del equipo tomó la conversación, y el flujo quedó en pausa.",
    "expired": "Venció el plazo del paso sin que saliera.",
    "expired_after_worker_crash": "Venció el plazo del paso sin que saliera.",
    "accepted_by_chatwoot": "Chatwoot aceptó la plantilla: salió. Que llegó lo dice Meta.",
    "policy_exhausted": "Ya salieron todos los pasos del flujo.",
    "eligible_for_execution": "Nada lo frena: le toca salir.",
    "pilot_request_start_authorized": "El piloto autorizó el arranque y gastó cupo del día.",
    # 20261022000300: el mismo momento cuando lo autoriza el motor de flujos.
    "flow_engine_request_start_authorized": "El motor autorizó el arranque y gastó cupo del día.",
    "precheckout_binding_unavailable": "La instancia no tenía una configuración comercial activa para el primer contacto.",
    "precheckout_prior_opt_out": "La persona se había dado de baja antes del formulario.",
    "contact_blocked": "El contacto está bloqueado.",
    "purchase_detected": "Se detectó una compra de la persona.",
    "reevaluation_expired": "Venció el plazo del paso sin que saliera.",
    "no_business_window_before_expiry": "No quedaba ninguna apertura del horario antes de que venciera el paso.",
    "coupon_send_failed": "El cupón no salió: el envío falló, y el barrido puede volver a intentarlo.",
    "reactivation_send_failed": "La reactivación no salió: el envío falló.",
    "cart_abandonment": "Llegó un carrito abandonado.",
    "payment_failure": "Llegó un pago fallido.",
    "precheckout_intent": "Llegó un formulario.",
    "create_recovery_case_failed": "No arrancó por una falla al crear el caso; la causa queda en el log.",
    "payment_failure_correlation_unresolved": "No se pudo atar el pago fallido a un formulario de esta persona.",
    "purchase_correlation_case_not_found": "La compra no tenía un caso abierto del recuperador.",
    "repeated_payment_failure": "Otro pago fallido se sumó al recorrido que ya estaba en curso.",
    "delivery_unknown": "No se sabe si el envío salió.",
    "link_sent_no_purchase": "Salió el cupón porque se le mandó el link y no compró.",
    "went_quiet": "Salió el cupón porque la persona dejó de contestar.",
    "payment_failed": "Salió el cupón después de un pago rechazado.",
    "policy_requires_human": "El agente derivó porque la política pide a una persona.",
    "explicit_human_request": "La persona pidió hablar con alguien del equipo.",
    "commercial_exception": "El agente derivó por una excepción comercial.",
    "inbound_after_quiet_period": "La persona volvió a escribir después del silencio, y el agente retomó.",
    "operator_request": "Alguien del equipo devolvió la conversación al agente.",
    "last_message_inbound": "La persona escribió después del último mensaje del agente.",
    "last_message_not_from_agent": "Lo último de la conversación lo escribió alguien del equipo.",
    "team_replied": "Alguien del equipo escribió después del último mensaje de la persona.",
    "never_replied": "La persona nunca contestó, y a quien no contestó no se le manda el cupón.",
    "inbound_too_recent": "Todavía no pasó la espera mínima desde el último mensaje de la persona.",
    "inbound_too_old": "Pasó la espera máxima desde el último mensaje de la persona.",
    "no_conversational_history": "La conversación no tiene mensajes públicos.",
}

# Lo que Meta contesta cuando no entrega una plantilla. Solo el codigo en
# digitos sale de Chatwoot; la frase es nuestra.
META_ERROR_EXPLANATIONS = {
    "131049": "Meta no la entregó para cuidar la interacción de la persona: le llegan muchos mensajes de marketing.",
    "131026": "Meta no la pudo entregar: el número no tiene WhatsApp o tiene una versión que no la recibe.",
    "130472": "Meta no la entregó porque el número está en un experimento de Meta.",
    "131042": "Meta no la entregó por un problema de pago de la cuenta de WhatsApp.",
    "131047": "Meta no la entregó porque pasaron más de 24 horas desde el último mensaje de la persona.",
    "131051": "Meta no la entregó porque el tipo de mensaje no está soportado.",
    "132000": "Meta la rechazó porque las variables no coinciden con la plantilla aprobada.",
    "132001": "Meta la rechazó porque la plantilla no existe en ese idioma.",
}

GAP_DESCRIPTIONS = {
    "start_rejection_not_persisted": (
        "por qué no arrancó el envío", "el motivo del rechazo queda solo en el log del bridge"),
    "case_creation_failure_not_persisted": (
        "por qué no se pudo crear el caso", "la causa queda solo en el log del bridge"),
    "chatwoot_not_read": (
        "la entrega de las plantillas y la actividad de la conversación", "Chatwoot no se pudo leer"),
    "meta_failure_reason_not_persisted": (
        "por qué Meta no entregó la plantilla", "Chatwoot no guardó el código de Meta"),
    "unanswered_reply_not_persisted": (
        "por qué el agente no contestó", "la base no lo guarda"),
    "agent_release_not_persisted": (
        "con qué versión del prompt y del conocimiento contestó el agente", "la base no lo guardó en esos turnos"),
    "followup_skip_not_persisted": (
        "por qué el cupón no salió", "el barrido no guarda lo que saltea"),
    "hotmart_ignored_not_persisted": (
        "los eventos que el bridge ignoró", "no quedan en la base"),
    "unattached_hotmart_event": (
        "eventos de Hotmart que podrían ser de esta persona", "sus datos llevan a más de un contacto"),
    "payment_slip_not_attached": (
        "si imprimió una ficha de pago o se le venció", "la base guarda las fichas sin atarlas a nadie; se mira en Hotmart"),
    "purchase_channel_not_persisted": (
        "con qué link o por qué canal compró", "no compró con un link nuestro"),
    "env_value_unversioned": (
        "desde cuándo vale lo que vale", "sale de una variable o del manifiesto, que no tienen historia"),
    "unknown_reason_code": (
        "el motivo de un momento", "el código no está en el catálogo o no cumple la forma"),
    "unknown_event_kind": (
        "qué fue un momento", "el tipo no está en el catálogo"),
}


def code_ok(value: object) -> bool:
    """La forma de los motivos, sin siete digitos seguidos (un telefono no pasa)."""
    return (isinstance(value, str) and REASON_CODE_RE.match(value) is not None
            and _SEVEN_DIGITS_RE.search(value) is None)


def _is_name_word(word: str) -> bool:
    """La guarda de las palabras de nombre: solo letras, apostrofos y guiones.

    Caracter por caracter con str.isalpha: un superindice o un numero en circulo
    no es una letra, aunque la clase [^\W\d_] de re lo deje pasar.
    """
    return bool(word) and word[0].isalpha() and all(
        character.isalpha() or character in "'’-" for character in word)


class LookupRejected(ValueError):
    """El pedido no cumple el contrato: 422 con un codigo cerrado."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InvalidReadEvidence(ValueError):
    """La base o Chatwoot devolvieron algo con otra forma: 503."""


class ResponseTooLarge(ValueError):
    """La respuesta no entra en el limite: 503, nunca recortada en silencio."""


@dataclass(frozen=True)
class ScopeRef:
    key: str
    version: int


@dataclass(frozen=True)
class InstanceMark:
    """La marca de instancia. La arma el bridge con su manifiesto, nunca el pedido."""

    tenant_ref: str
    ally_ref: str
    funnel_ref: str
    chatwoot_account_id: int
    chatwoot_inbox_id: int
    hotmart_product_id: str
    inbound_scope_key: str
    agent_scope_ref: str
    time_zone: str
    product_version: str
    recovery_scope: ScopeRef | None
    precheckout_scope: ScopeRef | None

    def to_rpc(self) -> dict[str, object]:
        scopes = []
        if self.recovery_scope is not None:
            scopes.append({"flow_group": "recuperacion", "key": self.recovery_scope.key,
                           "version": self.recovery_scope.version})
        if self.precheckout_scope is not None:
            scopes.append({"flow_group": "precheckout", "key": self.precheckout_scope.key,
                           "version": self.precheckout_scope.version})
        return {
            "tenant_ref": self.tenant_ref,
            "ally_ref": self.ally_ref,
            "funnel_ref": self.funnel_ref,
            "chatwoot_account_id": self.chatwoot_account_id,
            "chatwoot_inbox_id": self.chatwoot_inbox_id,
            "hotmart_product_id": self.hotmart_product_id,
            "inbound_scope_key": self.inbound_scope_key,
            "agent_scope_ref": self.agent_scope_ref,
            "time_zone": self.time_zone,
            "scopes": scopes,
        }

    def envelope(self) -> dict[str, str]:
        return {
            "tenant_ref": self.tenant_ref,
            "ally_ref": self.ally_ref,
            "funnel_ref": self.funnel_ref,
            "chatwoot_inbox_id": str(self.chatwoot_inbox_id),
            "hotmart_product_id": self.hotmart_product_id,
            "product_version": self.product_version,
        }


@dataclass(frozen=True)
class FlowSettings:
    """Lo que la proyeccion de los flujos lee de las variables, de una lista cerrada."""

    manifest_flows: Mapping[str, bool]
    templates: Mapping[str, tuple[str, str, tuple[str, ...]]]
    time_zone: str
    precheckout_enabled: bool
    pilot_enabled: bool
    recovery_enabled: bool
    payment_failure_enabled: bool
    coupon_enabled: bool
    coupon_template: tuple[str, str] | None
    coupon_code: str | None
    coupon_product_name: str | None
    coupon_min_age_seconds: int
    coupon_max_age_seconds: int
    coupon_send_hours: tuple[int, int] | None
    coupon_test_phone_set: bool
    reactivation_enabled: bool
    reactivation_template: tuple[str, str] | None
    reactivation_min_age_seconds: int
    reactivation_max_age_seconds: int
    stalled_monitor_enabled: bool
    resume_enabled: bool
    precheckout_test_phone_set: bool
    allowed_jid_set: bool


@dataclass(frozen=True)
class Lookup:
    conversation: int | None = None
    issuance: str | None = None
    name: str | None = None


def envelope(
    instance: InstanceMark,
    *,
    data: object,
    gaps: Sequence[Mapping[str, object]],
    generated_at: datetime,
    database_at: object,
    bridge_git_sha: str | None,
    page: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "instance": instance.envelope(),
        "source": "projection",
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "database_at": database_at if isinstance(database_at, str) else None,
        "bridge_git_sha": bridge_git_sha if bridge_git_sha and _RELEASE_RE.match(bridge_git_sha) else None,
        "gaps": list(gaps),
        "data": data,
        "page": dict(page) if page is not None else {"limit": None, "next_cursor": None},
    }


def checked_size(body: Mapping[str, object], *, limit: int) -> dict[str, object]:
    """La respuesta entra en el limite o falla: nada se recorta en silencio."""
    size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > limit:
        raise ResponseTooLarge(f"response_bytes={size}")
    return dict(body)


# ---------------------------------------------------------------------------
# La busqueda.
# ---------------------------------------------------------------------------

def looks_like_identifier(value: str) -> bool:
    """Si el texto tiene forma de telefono o de mail (contrato, «Datos personales»).

    Se sacan los separadores y siete caracteres numericos seguidos son un
    telefono, contando cualquier digito de Unicode (superindices incluidos).
    """
    if _EMAIL_RE.search(value):
        return True
    run = 0
    for character in value:
        if character in _PHONE_SEPARATORS:
            continue
        run = run + 1 if character.isnumeric() else 0
        if run >= 7:
            return True
    return False


def parse_lookup(body: object) -> Lookup:
    """Valida el cuerpo de la busqueda: exactamente una de tres llaves."""
    if not isinstance(body, dict):
        raise LookupRejected("invalid_lookup")
    keys = [key for key in ("conversation", "issuance", "name") if body.get(key) is not None]
    unknown = set(body) - {"conversation", "issuance", "name"}
    # Un telefono o un mail en el numero o en el nombre se rechaza antes que la
    # forma. El link no: su identificador es un ULID con su forma exacta.
    for key in ("conversation", "name"):
        value = body.get(key)
        if isinstance(value, str) and looks_like_identifier(value):
            raise LookupRejected("identifier_not_allowed")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1_000_000:
            raise LookupRejected("identifier_not_allowed")
    if unknown or len(keys) != 1:
        raise LookupRejected("invalid_lookup")
    key = keys[0]
    value = body[key]
    if key == "conversation":
        if isinstance(value, str):
            match = _CONVERSATION_RE.fullmatch(value.strip())
            if match is None:
                raise LookupRejected("invalid_lookup")
            value = int(match.group(1))
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 999_999:
            raise LookupRejected("invalid_lookup")
        return Lookup(conversation=value)
    if key == "issuance":
        if not isinstance(value, str) or not _ULID_RE.match(value.strip()):
            raise LookupRejected("invalid_lookup")
        return Lookup(issuance=value.strip())
    if not isinstance(value, str):
        raise LookupRejected("invalid_lookup")
    name = " ".join(value.split())
    if not 3 <= len(name) <= 80 or not all(
        _is_name_word(word.replace(" ", "")) for word in name.split(" ")
    ):
        raise LookupRejected("invalid_lookup")
    return Lookup(name=name)


def parse_lead_ref(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise LookupRejected("invalid_lead_ref") from exc


def parse_limit(value: object) -> int:
    if value is None:
        return HISTORY_DEFAULT_LIMIT
    try:
        limit = int(str(value))
    except ValueError as exc:
        raise LookupRejected("limit_out_of_range") from exc
    if not 1 <= limit <= HISTORY_MAX_LIMIT:
        raise LookupRejected("limit_out_of_range")
    return limit


def parse_flow_key(value: str) -> str:
    if not _FLOW_KEY_SHAPE_RE.match(value):
        raise LookupRejected("invalid_flow_key")
    return value


def first_name(inferred: object, fallback: object) -> str | None:
    """El nombre de pila de la cadena del saludo, solo si pasa la guarda.

    El inferido con confianza puede tener hasta tres palabras ("María del
    Carmen"); el respaldo es siempre una sola, la primera, para que nunca caiga
    al nombre completo, ni siquiera con un espacio que la base no partio.
    """
    if isinstance(inferred, str):
        words = inferred.split()
        if words and len(words) <= 3 and all(_is_name_word(word) for word in words):
            name = " ".join(words)
            if 2 <= len(name) <= 40:
                return name
    if isinstance(fallback, str):
        words = fallback.split()
        if words and _is_name_word(words[0]) and 2 <= len(words[0]) <= 40:
            return words[0]
    return None


def lead_label(name: str | None, conversations: Sequence[int]) -> str:
    """«<nombre de pila> · conversación #N». Sin nombre, solo el numero."""
    number = max(conversations) if conversations else None
    if name and number is not None:
        return f"{name} · conversación #{number}"
    if number is not None:
        return f"conversación #{number}"
    return name or "lead sin conversación"


def _conversation_numbers(value: object) -> list[int]:
    if not isinstance(value, list):
        raise InvalidReadEvidence("invalid_conversations")
    numbers = []
    for number in value:
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 10**17:
            raise InvalidReadEvidence("invalid_conversation_number")
        numbers.append(number)
    return sorted(set(numbers))


def _public_base(url: str | None) -> str | None:
    """La URL de Chatwoot para los links: https, sin credenciales ni parametros."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        return None
    return f"https://{parts.netloc}{parts.path.rstrip('/')}"


def conversation_links(
    numbers: Sequence[int], *, chatwoot_public_url: str | None, account_id: int,
) -> list[dict[str, object]]:
    base = _public_base(chatwoot_public_url)
    return [
        {"number": number,
         "url": f"{base}/app/accounts/{account_id}/conversations/{number}" if base else None}
        for number in numbers
    ]


def build_lookup(
    raw: object, *, chatwoot_public_url: str | None, account_id: int,
) -> dict[str, object] | None:
    """La respuesta de la busqueda, o None si no hay ningun lead (404)."""
    if not isinstance(raw, dict):
        raise InvalidReadEvidence("invalid_lookup_shape")
    result = raw.get("result")
    if result == "not_found":
        return None
    if result == "too_many_candidates":
        return {"result": "too_many_candidates", "lead_ref": None, "candidates": []}
    if result not in ("found", "ambiguous"):
        raise InvalidReadEvidence("invalid_lookup_result")
    raw_candidates = raw.get("candidates")
    if not isinstance(raw_candidates, list) or not 1 <= len(raw_candidates) <= MAX_CANDIDATES:
        raise InvalidReadEvidence("invalid_candidates")
    candidates = []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            raise InvalidReadEvidence("invalid_candidate")
        lead_ref = candidate.get("lead_ref")
        try:
            lead_ref = str(UUID(str(lead_ref)))
        except ValueError as exc:
            raise InvalidReadEvidence("invalid_lead_ref") from exc
        numbers = _conversation_numbers(candidate.get("conversations"))
        name = first_name(candidate.get("first_name_inferred"), candidate.get("first_name_fallback"))
        last_activity = candidate.get("last_activity_at")
        candidates.append({
            "lead_ref": lead_ref,
            "label": lead_label(name, numbers),
            "conversations": conversation_links(
                numbers, chatwoot_public_url=chatwoot_public_url, account_id=account_id),
            "last_activity_at": last_activity if isinstance(last_activity, str) else None,
        })
    if result == "found" and len(candidates) != 1:
        raise InvalidReadEvidence("found_with_many_candidates")
    return {
        "result": result,
        "lead_ref": candidates[0]["lead_ref"] if result == "found" else None,
        "candidates": candidates,
    }


# ---------------------------------------------------------------------------
# Los flujos.
# ---------------------------------------------------------------------------

_PRIORITY = {
    "carrito": {"le_gana_a": ["precheckout"], "no_arranca_mientras": ["pago_fallido"]},
    "pago_fallido": {"le_gana_a": ["precheckout"], "no_arranca_mientras": []},
    "precheckout": {"le_gana_a": [], "no_arranca_mientras": []},
    "descuento": {"le_gana_a": [], "no_arranca_mientras": []},
    "reactivacion": {"le_gana_a": [], "no_arranca_mientras": []},
}
_PRIORITY_REFS = {
    ("carrito", "le_gana_a"): "20261001000200_portable_precheckout_first_contact.sql:296-313",
    ("carrito", "no_arranca_mientras"): "20260930000300_pilot_scope_audience_mode.sql:837-846",
    ("pago_fallido", "le_gana_a"): "20261001000200_portable_precheckout_first_contact.sql:296-313",
}
_FLOW_NAMES = {
    "precheckout": "Primer contacto tras el formulario",
    "carrito": "Carrito abandonado",
    "pago_fallido": "Pago fallido",
    "descuento": "Seguimiento con cupón",
    "reactivacion": "Reactivación de quien quedó esperando",
}
_RUNTIME_STATE = {"armed": "on", "paused": "paused", "inactive": "off", "closed": "off"}
_SCHEMA_REF = "docs/contracts/flow-definition-v1.md «Los siete flujos de validación»"


def _duration(seconds: object, unit: str) -> str | None:
    """Una duracion en la unidad con la que la escribe el esquema.

    `vence` va en dias, una espera en minutos y un silencio en horas (en dias
    desde una semana). Si no divide justo, baja de unidad.
    """
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
        return None
    if unit == "silence" and seconds >= 7 * 86_400:
        unit = "d"
    elif unit == "silence":
        unit = "h"
    for name, size in (("d", 86_400), ("h", 3_600), ("m", 60)):
        if unit == name and seconds % size == 0:
            return f"{seconds // size}{name}"
        if unit == name:
            unit = {"d": "h", "h": "m", "m": "m"}[name]
    return f"{seconds // 60}m"


def _business_hours(policy: Mapping[str, object] | None, zone: str) -> dict[str, str] | str | None:
    if policy is None:
        return None
    windows = policy.get("business_windows")
    if not isinstance(windows, list) or len(windows) != 1 or not isinstance(windows[0], dict):
        return None
    window = windows[0]
    start, end = window.get("start"), window.get("end")
    if not all(isinstance(value, str) and re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", value)
               for value in (start, end)):
        return None
    return {"zona": zone, "desde": start, "hasta": end}


def _template_step(
    template: tuple[str, str, tuple[str, ...]] | None,
) -> dict[str, object] | None:
    if template is None:
        return None
    name, language, variables = template
    return {"plantilla": name, "idioma": language, "variables": list(variables)}


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _scope_by_group(raw: Mapping[str, object], group: str) -> dict[str, object] | None:
    scopes = raw.get("scopes")
    if not isinstance(scopes, list):
        raise InvalidReadEvidence("invalid_scopes")
    for scope in scopes:
        if isinstance(scope, dict) and scope.get("flow_group") == group:
            return scope
    return None


def _policy(raw: Mapping[str, object], scope: Mapping[str, object] | None) -> dict[str, object] | None:
    if scope is None or not scope.get("bound_to_instance"):
        return None
    policies = raw.get("policies")
    if not isinstance(policies, list):
        raise InvalidReadEvidence("invalid_policies")
    for policy in policies:
        if (isinstance(policy, dict) and policy.get("policy_key") == scope.get("policy_key")
                and policy.get("version") == scope.get("policy_version")):
            return policy
    return None


def build_flows(
    raw: object, *, instance: InstanceMark, settings: FlowSettings,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Los cinco flujos, lo que corre en codigo, los eventos sin flujo y lo comun."""
    if not isinstance(raw, dict):
        raise InvalidReadEvidence("invalid_flows_shape")
    observed = raw.get("observed")
    if not isinstance(observed, dict) or not isinstance(observed.get("flows"), dict):
        raise InvalidReadEvidence("invalid_observed")
    gaps: list[dict[str, object]] = []
    recovery = _scope_by_group(raw, "recuperacion")
    precheckout = _scope_by_group(raw, "precheckout")
    zone = settings.time_zone
    flows: list[dict[str, object]] = []

    for flow_key in FLOW_KEYS:
        provenance: dict[str, dict[str, str]] = {
            "/definition/version": {"from": "code", "ref": _SCHEMA_REF},
            "/definition/estado": {"from": "code", "ref": _SCHEMA_REF},
        }
        manifest_on = bool(settings.manifest_flows.get(flow_key, False))
        provenance["/status"] = {"from": "manifest", "ref": f"[flujos].{flow_key}"}
        scope = recovery if flow_key in ("carrito", "pago_fallido") else (
            precheckout if flow_key == "precheckout" else None)
        policy = _policy(raw, scope)
        version: dict[str, str] | None = None
        status: dict[str, object]
        usage: dict[str, object] | None = None
        definition: dict[str, object] = {
            "esquema": "setter-flujo/v1",
            "flujo": flow_key,
            "nombre": _FLOW_NAMES[flow_key],
            "version": 1,
            "estado": "publicada",
        }

        if flow_key in ("carrito", "pago_fallido", "precheckout"):
            flow_flag = {
                "precheckout": settings.precheckout_enabled,
                "carrito": settings.recovery_enabled and settings.pilot_enabled,
                "pago_fallido": settings.payment_failure_enabled and settings.pilot_enabled,
            }[flow_key]
            switch_on = manifest_on and flow_flag
            bound = scope is not None and scope.get("bound_to_instance") is True
            if bound:
                version = {
                    "scope": f"{scope['scope_key']} v{scope['version']}",
                    "policy": f"{scope['policy_key']} v{scope['policy_version']}",
                }
                provenance["/version"] = {"from": "database", "ref": f"pilot_scope_versions {version['scope']}"}
            if not switch_on:
                status = {"state": "off", "detail": "flag_off", "since": None}
                provenance["/status"] = {"from": "env", "ref": "[flujos] y el flag del flujo"}
            elif not bound:
                status = {"state": "not_configured", "detail": None, "since": None}
            else:
                runtime = scope.get("runtime_state")
                state = _RUNTIME_STATE.get(runtime) if isinstance(runtime, str) else None
                status = {
                    "state": state or "off",
                    "detail": runtime if code_ok(runtime) else None,
                    "since": scope.get("runtime_changed_at"),
                }
                provenance["/status"] = {"from": "database", "ref": "pilot_runtime_controls"}
            if bound:
                usage = {
                    "local_date": scope.get("local_budget_date"),
                    "starts_today": _int(scope.get("starts_today")),
                    "daily_cap": _int(scope.get("max_outbound_request_starts_per_day")),
                    "starts_total": _int(scope.get("starts_total")),
                    "total_cap": _int(scope.get("max_outbound_request_starts_total")),
                    "pool": scope.get("scope_key"),
                }
                provenance["/usage/daily_cap"] = {"from": "database", "ref": f"pilot_scope_versions {version['scope']}"}
            slot = flow_key
            template = settings.templates.get(slot)
            steps: list[dict[str, object]] = []
            if flow_key == "precheckout" and policy is not None:
                wait = _duration(policy.get("grace_period_seconds"), "m")
                if wait and wait != "0m":
                    steps.append({"esperar": wait})
            step = _template_step(template)
            if step is not None:
                steps.append(step)
                provenance["/definition/pasos"] = {"from": "manifest", "ref": f"[plantillas].{slot}"}
            definition.update({
                "dispara": {"evento": {"carrito": "carrito", "pago_fallido": "pago_fallido",
                                       "precheckout": "intencion"}[flow_key]},
                "entra": {"permiso": "formulario"},
                "pasos": steps,
                "termina": {"si": ["compra"], "vence": _duration(policy.get("expires_after_seconds"), "d") if policy else None},
                "horario": _business_hours(policy, zone),
                "topes": {
                    "bolsa": scope.get("scope_key") if bound else None,
                    "por_dia": _int(scope.get("max_outbound_request_starts_per_day")) if bound else None,
                    "total": _int(scope.get("max_outbound_request_starts_total")) if bound else None,
                    "repeticion": (
                        {"una_por": "persona", "otra_despues_de": "24h"} if flow_key == "precheckout"
                        else {"una_por": "persona_y_oferta", "otra_despues_de": "0m"}),
                },
                "prioridad": _PRIORITY[flow_key],
            })
            if policy is not None:
                provenance["/definition/horario"] = {"from": "database", "ref": f"followup_policy_versions {version['policy']}"}
                provenance["/definition/termina/vence"] = {"from": "database", "ref": f"followup_policy_versions {version['policy']}"}
            if bound:
                provenance["/definition/topes"] = {"from": "database", "ref": f"pilot_scope_versions {version['scope']}"}
            provenance["/definition/topes/repeticion"] = {"from": "code", "ref": _SCHEMA_REF}
        elif flow_key == "descuento":
            switch_on = manifest_on and settings.coupon_enabled
            status = {"state": "on" if switch_on else "off",
                      "detail": "test_single_phone" if switch_on and settings.coupon_test_phone_set else None,
                      "since": None}
            provenance["/status"] = {"from": "env", "ref": "CONVERSATION_FOLLOWUP_ENABLED y [flujos].descuento"}
            gaps.append(_gap("env_value_unversioned", f"flow:{flow_key}"))
            hours = settings.coupon_send_hours
            step = None
            if settings.coupon_template is not None:
                name, language = settings.coupon_template
                step = {"plantilla": name, "idioma": language, "variables": ["nombre", "producto"],
                        "boton": {"link": "checkout", "cupon": settings.coupon_code}}
            definition.update({
                "dispara": {"silencio": {
                    "lo_ultimo": "agente",
                    "desde": _duration(settings.coupon_min_age_seconds, "silence"),
                    "hasta": _duration(settings.coupon_max_age_seconds, "silence"),
                    "conversaciones": ["abierta", "resuelta"],
                }},
                "entra": {"permiso": "conversacion", "contesto_a": ["carrito", "pago_fallido", "precheckout"]},
                "pasos": [step] if step else [],
                "termina": {"si": ["compra", "contesta"]},
                "horario": ({"zona": zone, "desde": f"{hours[0]:02d}:00", "hasta": f"{hours[1] - 1:02d}:59"}
                            if hours else "siempre"),
                "topes": {"por_dia": "sin_tope", "total": "sin_tope",
                          "repeticion": {"una_por": "conversacion", "otra_despues_de": "nunca"}},
                "prioridad": _PRIORITY[flow_key],
            })
            provenance["/definition"] = {"from": "env", "ref": "CONVERSATION_FOLLOWUP_* y [plantillas].descuento"}
        else:
            switch_on = manifest_on and settings.reactivation_enabled
            status = {"state": "on" if switch_on else "off", "detail": None, "since": None}
            provenance["/status"] = {"from": "env", "ref": "CONVERSATION_REACTIVATION_ENABLED y [flujos].reactivacion"}
            gaps.append(_gap("env_value_unversioned", f"flow:{flow_key}"))
            step = None
            if settings.reactivation_template is not None:
                name, language = settings.reactivation_template
                step = {"plantilla": name, "idioma": language, "variables": ["nombre"]}
            definition.update({
                "dispara": {"silencio": {
                    "lo_ultimo": "persona",
                    "desde": _duration(settings.reactivation_min_age_seconds, "silence"),
                    "hasta": _duration(settings.reactivation_max_age_seconds, "silence"),
                    "conversaciones": ["abierta"],
                }},
                "entra": {"permiso": "conversacion"},
                "pasos": [step] if step else [],
                "termina": {"si": []},
                "horario": "siempre",
                "topes": {"por_dia": "sin_tope", "total": "sin_tope",
                          "repeticion": {"una_por": "conversacion", "otra_despues_de": "nunca"}},
                "prioridad": _PRIORITY[flow_key],
            })
            provenance["/definition"] = {"from": "env", "ref": "CONVERSATION_REACTIVATION_*"}

        priority = dict(_PRIORITY[flow_key])
        priority["derived"] = ["le_ganan"]
        for field_name in ("le_gana_a", "no_arranca_mientras"):
            ref = _PRIORITY_REFS.get((flow_key, field_name))
            if ref is not None:
                provenance[f"/priority/{field_name}"] = {"from": "code", "ref": ref}
        flows.append({
            "flow_key": flow_key,
            "version": version,
            "status": status,
            "priority": priority,
            "usage": usage,
            "observed": _observed(observed, flow_key),
            "source": "projection",
            "definition": definition,
            "provenance": provenance,
        })
        if version is None and flow_key in ("carrito", "pago_fallido", "precheckout") and status["state"] != "off":
            gaps.append(_gap("env_value_unversioned", f"flow:{flow_key}"))

    states = {flow["flow_key"]: flow["status"]["state"] for flow in flows}  # type: ignore[index]
    for flow in flows:
        flow["priority"]["le_ganan"] = [  # type: ignore[index]
            other for other in FLOW_KEYS
            if flow["flow_key"] in _PRIORITY[other]["le_gana_a"] and states[other] != "off"
        ]

    without_flow = raw.get("events_without_flow")
    if not isinstance(without_flow, list):
        raise InvalidReadEvidence("invalid_events_without_flow")
    events = []
    for event in without_flow:
        if not isinstance(event, dict) or not _EVENT_TYPE_RE.match(str(event.get("event_type"))):
            raise InvalidReadEvidence("invalid_event_without_flow")
        events.append({
            "event_type": event["event_type"],
            "what_happens": "El bridge lo guarda en failed con unsupported_persisted_event_type y no dispara nada.",
            "count_window": _int(event.get("count_window")),
            "window_days": 7,
            "last_received_at": event.get("last_received_at"),
            "provenance": {"from": "code", "ref": "src/bridge/worker.py:2395-2402"},
        })

    cap = raw.get("proactive_contact_cap")
    cap_configured = isinstance(cap, dict) and cap.get("configured") is True
    data = {
        "flows": flows,
        "in_code": [
            {"key": "inbound", "name": "El agente que contesta",
             "status": "on" if settings.manifest_flows.get("inbound") else "off",
             "provenance": {"from": "manifest", "ref": "[flujos].inbound"}},
            {"key": "stalled_monitor", "name": "El monitor de conversaciones trabadas",
             "status": "on" if settings.stalled_monitor_enabled else "off",
             "provenance": {"from": "env", "ref": "CHATWOOT_STALLED_MONITOR_ENABLED"}},
            {"key": "resume", "name": "La reanudación de conversaciones pausadas",
             "status": "on" if settings.resume_enabled else "off",
             "provenance": {"from": "env", "ref": "CONVERSATION_RESUME_ENABLED"}},
        ],
        "events_without_flow": events,
        "shared": {
            "gate": None,
            "gate_note": (
                "La puerta común no existe todavía: la construye la 1b. Cada flujo tiene hoy sus "
                "propias barreras, y la pausa del piloto no frena el cupón ni la reactivación."),
            "per_person_cap": {
                "esquema": "puerta.tope_por_persona",
                "value": (_int(cap.get("max_request_starts")) if cap_configured else "sin_tope"),
                "window_seconds": _int(cap.get("request_window_seconds")) if cap_configured else None,
                "provenance": {"from": "database", "ref": "pilot_proactive_contact_caps"},
            },
            "test_settings": {
                "coupon_test_phone_set": settings.coupon_test_phone_set,
                "precheckout_test_phone_set": settings.precheckout_test_phone_set,
                "allowed_jid_set": settings.allowed_jid_set,
            },
        },
        "local_date": raw.get("local_date"),
    }
    return data, _dedupe(gaps)


def _observed(observed: Mapping[str, object], flow_key: str) -> dict[str, object]:
    window = observed.get("window_days")
    if flow_key in ("descuento", "reactivacion"):
        counts = observed.get(flow_key)
        if not isinstance(counts, dict):
            raise InvalidReadEvidence("invalid_observed_counts")
        return {"window_days": window, "sent": _int(counts.get("sent")), "failed": _int(counts.get("failed"))}
    counts = observed["flows"].get(flow_key)  # type: ignore[index]
    if not isinstance(counts, dict):
        raise InvalidReadEvidence("invalid_observed_flow")
    result: dict[str, object] = {"window_days": window}
    for field_name in ("triggers", "cases_opened", "starts", "accepted_by_chatwoot", "deferred_by_hours",
                       "median_trigger_to_accept_seconds", "superseded", "median_form_to_winning_event_seconds"):
        if field_name in counts:
            result[field_name] = _int(counts.get(field_name))
    for field_name in ("rejected_on_entry", "stopped", "first_contact_plans"):
        value = counts.get(field_name)
        if isinstance(value, dict):
            cleaned = {}
            for code, count in value.items():
                short = code.split("pilot_scope_rejected:", 1)[-1]
                if code_ok(short) and _int(count) is not None:
                    cleaned[short] = count
            result[field_name] = cleaned
    return result


# ---------------------------------------------------------------------------
# La historia.
# ---------------------------------------------------------------------------

def _gap(code: str, scope: str, *, what: str | None = None, why: str | None = None) -> dict[str, object]:
    default_what, default_why = GAP_DESCRIPTIONS[code]
    return {"code": code, "scope": scope, "what": what or default_what, "why": why or default_why}


def _dedupe(gaps: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[object, object, object]] = set()
    result = []
    for gap in gaps:
        key = (gap["code"], gap["scope"], gap["what"])
        if key not in seen:
            seen.add(key)
            result.append(dict(gap))
    return result


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise InvalidReadEvidence("invalid_moment_time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidReadEvidence("invalid_moment_time") from exc
    if parsed.tzinfo is None:
        raise InvalidReadEvidence("naive_moment_time")
    return parsed.astimezone(timezone.utc)


def _flow_version(value: object) -> dict[str, str | None] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise InvalidReadEvidence("invalid_flow_version")
    result: dict[str, str | None] = {}
    for field_name in ("scope", "policy"):
        label = value.get(field_name)
        result[field_name] = label if isinstance(label, str) and _SCOPE_LABEL_RE.match(label) else None
    return result


# Las claves de extra que pueden salir, con su validacion.
def _clean_extra(kind: str, extra: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in extra.items():
        if value is None:
            continue
        if key in ("count", "inbound_age_seconds", "conversation", "chatwoot_message_id"):
            if _int(value) is not None:
                result[key] = value
        elif key in ("first_at", "last_at", "due_at", "expires_at", "winner_at", "local_budget_date"):
            if isinstance(value, str) and len(value) <= 40:
                result[key] = value
        elif key in ("whatsapp_contact_authorized", "release_recorded", "with_checkout_link"):
            if isinstance(value, bool):
                result[key] = value
        elif key == "winner_flow_key":
            if value in FLOW_KEYS:
                result[key] = value
        elif key == "event_type":
            if isinstance(value, str) and _EVENT_TYPE_RE.match(value):
                result[key] = value
        elif key == "landing_ref":
            if isinstance(value, str) and _LANDING_RE.match(value) and not _SEVEN_DIGITS_RE.search(value):
                result[key] = value
        elif key == "offer_ref":
            if isinstance(value, str) and _OFFER_RE.match(value) and not _SEVEN_DIGITS_RE.search(value):
                result[key] = value
        elif key == "model":
            if isinstance(value, str) and _MODEL_RE.match(value) and not _SEVEN_DIGITS_RE.search(value):
                result[key] = value
        elif key == "bridge_release":
            if isinstance(value, str) and _RELEASE_RE.match(value):
                result[key] = value
        elif key == "link_source":
            if value in ("template", "agent", "coupon"):
                result[key] = value
        elif key in ("event_role", "processing_status", "correlation_outcome", "matched_by", "audience_mode",
                     "step_key", "anchor_type", "status", "outcome", "source_kind", "offer_resolution",
                     "attribution_resolution", "lifecycle_state", "case_status", "control_state",
                     "detail_reason_code"):
            if code_ok(value):
                result[key] = value
    return result


def _reason(code: str | None, kind: str, extra: Mapping[str, object]) -> str | None:
    if code is None:
        return None
    if code == "superseded_by_provider_event":
        winner = extra.get("winner_flow_key")
        return f"le_gano:{winner}" if winner in FLOW_KEYS else None
    if code == "pilot_runtime_not_armed":
        return "flujo_pausado" if extra.get("control_state") == "paused" else None
    return _REASON_BY_CODE.get(code)


@dataclass
class _Moment:
    at: datetime
    order: int
    data: dict[str, object]

    @property
    def key(self) -> tuple[str, int, str, str, str]:
        evidence = self.data["evidence"]
        assert isinstance(evidence, dict)
        return (self.at.isoformat(), self.order, str(evidence["table"]), str(evidence["id"]),
                str(self.data["kind"]))


def _moment_from_database(
    raw: object, *, zone: ZoneInfo, gaps: list[dict[str, object]],
) -> _Moment:
    if not isinstance(raw, dict):
        raise InvalidReadEvidence("invalid_moment")
    at = _parse_time(raw.get("at"))
    kind = raw.get("kind")
    if kind not in _KINDS_FROM_DATABASE:
        gaps.append(_gap("unknown_event_kind", "lead"))
        kind = "other"
    flow_key = raw.get("flow_key")
    if flow_key is not None and flow_key not in FLOW_KEYS:
        raise InvalidReadEvidence("invalid_flow_key")
    evidence = raw.get("evidence")
    if (not isinstance(evidence, dict) or evidence.get("table") not in _EVIDENCE_TABLES
            or not isinstance(evidence.get("id"), str)):
        raise InvalidReadEvidence("invalid_evidence")
    try:
        UUID(evidence["id"])
    except ValueError as exc:
        raise InvalidReadEvidence("invalid_evidence_id") from exc
    certainty = raw.get("certainty")
    if certainty not in ("stored", "inferred"):
        raise InvalidReadEvidence("invalid_certainty")
    attached_by = raw.get("attached_by")
    if not isinstance(attached_by, str) or not _SHORT_CODE_RE.match(attached_by):
        raise InvalidReadEvidence("invalid_attached_by")
    extra_raw = raw.get("extra") or {}
    if not isinstance(extra_raw, dict):
        raise InvalidReadEvidence("invalid_extra")
    extra = _clean_extra(str(kind), extra_raw)
    code = raw.get("reason_code")
    if raw.get("reason_code_rejected") is True or (code is not None and not code_ok(code)):
        code = None
        gaps.append(_gap("unknown_reason_code", "lead"))
    explanation = EXPLANATIONS.get(code) if isinstance(code, str) else None
    if isinstance(code, str) and explanation is None:
        gaps.append(_gap("unknown_reason_code", "lead"))
    decision = raw.get("decision")
    data: dict[str, object] = {
        "at": at.isoformat(),
        "local_at": at.astimezone(zone).isoformat(),
        "kind": kind,
        "flow_key": flow_key,
        "flow_version": _flow_version(raw.get("flow_version")),
        "reason": _reason(code, str(kind), extra),
        "reason_code": code,
        "explanation": explanation,
        "certainty": certainty,
        "attached_by": attached_by,
        "evidence": {"table": evidence["table"], "id": evidence["id"]},
    }
    if decision in ("cancel", "pause", "escalate", "defer"):
        data["decision"] = decision
    if kind == "stopped" and "decision" not in data:
        data["decision"] = "cancel"
    for key, value in extra.items():
        data[key] = value
    return _Moment(at, 0, data)


def _actor(message: Mapping[str, object], *, agent_bot_id: int) -> str:
    message_type = message.get("message_type")
    sender = message.get("sender")
    sender_type = sender.get("type") if isinstance(sender, dict) else None
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    if message_type == 0:
        return "prospect" if sender_type == "contact" else "other"
    if message.get("private") is True:
        # La nota de la derivacion la publica el bridge con el token de control,
        # que es de un usuario: la marca sola no alcanza.
        content = message.get("content")
        if isinstance(content, str) and _HANDOFF_MARKER in content and sender_type == "user":
            return "system"
        return "human_agent"
    if message_type == 1 and sender_type == "agent_bot" and sender_id == agent_bot_id:
        return "ai_agent"
    if message_type in (1, 3) and sender_type == "user":
        return "human_agent"
    return "other"


def _delivery_status(message: Mapping[str, object]) -> str | None:
    if message.get("private") is True or message.get("message_type") not in (1, 3):
        return None
    status = message.get("status")
    return status if status in ("sent", "delivered", "read", "failed") else None


def _meta_error_code(message: Mapping[str, object]) -> str | None:
    """Solo el codigo de Meta en digitos, nunca el texto (contrato 1.1.0)."""
    attributes = message.get("content_attributes")
    if not isinstance(attributes, dict):
        return None
    error = attributes.get("external_error")
    if not isinstance(error, str):
        return None
    match = _META_ERROR_CODE_RE.match(error.strip())
    return match.group(1) if match else None


@dataclass(frozen=True)
class ChatwootRead:
    """Lo que la ruta leyo de una conversacion: los mensajes o por que no."""

    conversation: int
    messages: Sequence[Mapping[str, object]] | None
    failure: str | None = None


def project_chatwoot(
    reads: Sequence[ChatwootRead],
    *,
    inbox_id: int,
    agent_bot_id: int,
    templates: Mapping[int, _Moment],
    zone: ZoneInfo,
    gaps: list[dict[str, object]],
) -> tuple[list[_Moment], dict[int, list[Mapping[str, object]]]]:
    """La parte de Chatwoot: meta_delivery de cada plantilla y la actividad.

    Copia campo por campo; el contenido, los adjuntos, el remitente y el meta se
    quedan aca. Una conversacion entra entera o no entra.
    """
    moments: list[_Moment] = []
    readable: dict[int, list[Mapping[str, object]]] = {}
    seen_templates: set[int] = set()
    for read in reads:
        if read.messages is None:
            gaps.append(_gap("chatwoot_not_read", "lead", what=f"la conversación #{read.conversation}",
                             why=read.failure or "Chatwoot no contestó"))
            continue
        messages = list(read.messages)
        if any(not isinstance(message, dict) or message.get("inbox_id") != inbox_id for message in messages):
            gaps.append(_gap("chatwoot_not_read", "lead", what=f"la conversación #{read.conversation}",
                             why="trae mensajes de otra bandeja"))
            continue
        bad_id = any(not isinstance(message.get("id"), int) or isinstance(message.get("id"), bool)
                     or not isinstance(message.get("created_at"), int) for message in messages)
        if bad_id:
            gaps.append(_gap("chatwoot_not_read", "lead", what=f"la conversación #{read.conversation}",
                             why="Chatwoot devolvió mensajes con otra forma"))
            continue
        readable[read.conversation] = messages
        for message in messages:
            message_id = message["id"]
            if message.get("message_type") == 2:
                continue
            template = templates.get(message_id)  # type: ignore[arg-type]
            if template is not None:
                seen_templates.add(message_id)  # type: ignore[arg-type]
                status = _delivery_status(message)
                data: dict[str, object] = {
                    "at": template.data["at"],
                    "local_at": template.data["local_at"],
                    "kind": "meta_delivery",
                    "flow_key": template.data.get("flow_key"),
                    "flow_version": template.data.get("flow_version"),
                    "reason": None,
                    "reason_code": None,
                    "explanation": _DELIVERY_EXPLANATIONS.get(status) if status else None,
                    "certainty": "stored",
                    "attached_by": "conversation",
                    "evidence": {"table": "chatwoot.messages", "id": str(message_id)},
                    "delivery_status": status,
                    "conversation": read.conversation,
                }
                if status == "failed":
                    code = _meta_error_code(message)
                    if code is not None:
                        data["meta_error_code"] = code
                        data["explanation"] = META_ERROR_EXPLANATIONS.get(code, _DELIVERY_EXPLANATIONS["failed"])
                    else:
                        gaps.append(_gap("meta_failure_reason_not_persisted", "lead"))
                moments.append(_Moment(template.at, 1, data))
                continue
            created = datetime.fromtimestamp(int(message["created_at"]), tz=timezone.utc)  # type: ignore[arg-type]
            actor = _actor(message, agent_bot_id=agent_bot_id)
            activity: dict[str, object] = {
                "at": created.isoformat(),
                "local_at": created.astimezone(zone).isoformat(),
                "kind": "conversation_activity",
                "flow_key": None,
                "flow_version": None,
                "reason": None,
                "reason_code": None,
                "explanation": None,
                "certainty": "stored",
                "attached_by": "conversation",
                "evidence": {"table": "chatwoot.messages", "id": str(message_id)},
                "actor_type": actor,
                "conversation": read.conversation,
            }
            if message.get("private") is True:
                activity["private"] = True
            else:
                status = _delivery_status(message)
                if status is not None:
                    activity["delivery_status"] = status
                    if status == "failed":
                        code = _meta_error_code(message)
                        if code is not None:
                            activity["meta_error_code"] = code
            moments.append(_Moment(created, 2, activity))
    if readable and all(read.messages is not None for read in reads):
        for message_id in set(templates) - seen_templates:
            gaps.append(_gap(
                "chatwoot_not_read", "lead", what="la entrega de una plantilla",
                why="su mensaje no está en las conversaciones leídas"))
    return moments, readable


_DELIVERY_EXPLANATIONS = {
    "sent": ("Meta no confirmó la entrega: no dice que llegó ni que falló. En ATT1, antes del "
             "2026-10-05 a las 11:41Z este estado no distingue nada."),
    "delivered": "Meta la entregó.",
    "read": "Meta la entregó y la persona la leyó.",
    "failed": "Meta no la entregó.",
}


def _last_public_at(messages: Sequence[Mapping[str, object]]) -> datetime | None:
    conversational = [
        message for message in messages
        if message.get("private") is False and message.get("message_type") in (0, 1, 3)
    ]
    if not conversational:
        return None
    last = max(conversational, key=lambda message: message["id"])  # type: ignore[arg-type,return-value]
    return datetime.fromtimestamp(int(last["created_at"]), tz=timezone.utc)  # type: ignore[arg-type]


def _coupon_view(
    messages: Sequence[Mapping[str, object]], *, now: datetime, min_age: int, max_age: int,
) -> tuple[str, datetime | None]:
    """El mismo criterio que el barrido del cupon (followup_discount.py:469-517)."""
    conversational = [
        message for message in messages
        if message.get("private") is False and message.get("message_type") in (0, 1, 3)
    ]
    if not conversational:
        return "no_conversational_history", None
    last = max(conversational, key=lambda message: message["id"])  # type: ignore[arg-type,return-value]
    if last.get("message_type") == 0:
        return "last_message_inbound", None
    sender = last.get("sender")
    if not isinstance(sender, dict) or sender.get("type") != "agent_bot":
        return "last_message_not_from_agent", None
    inbound = [
        message for message in conversational
        if message.get("message_type") == 0
        and isinstance(message.get("sender"), dict) and message["sender"].get("type") == "contact"  # type: ignore[union-attr]
    ]
    if not inbound:
        return "never_replied", None
    last_inbound = max(inbound, key=lambda message: message["id"])  # type: ignore[arg-type,return-value]
    if any(
        isinstance(message.get("sender"), dict) and message["sender"].get("type") == "user"  # type: ignore[union-attr]
        and message["id"] > last_inbound["id"]  # type: ignore[operator]
        for message in conversational if message.get("message_type") != 0
    ):
        return "team_replied", None
    since = datetime.fromtimestamp(int(last_inbound["created_at"]), tz=timezone.utc)  # type: ignore[arg-type]
    age = (now - since).total_seconds()
    if age < min_age:
        return "inbound_too_recent", since
    if age > max_age:
        return "inbound_too_old", since
    return "eligible_for_execution", since


def _coupon_window(since: datetime, *, min_age: int, max_age: int, zone: ZoneInfo,
                   send_hours: tuple[int, int] | None) -> tuple[datetime, datetime]:
    opens = since + timedelta(seconds=min_age)
    closes = since + timedelta(seconds=max_age)
    if send_hours is not None:
        local = opens.astimezone(zone)
        start, end = send_hours
        if local.hour < start:
            opens = local.replace(hour=start, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        elif local.hour >= end:
            next_day = (local + timedelta(days=1)).replace(hour=start, minute=0, second=0, microsecond=0)
            opens = next_day.astimezone(timezone.utc)
    return opens, closes


def encode_cursor(key: tuple[str, int, str, str, str]) -> str:
    raw = json.dumps(list(key), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str | None) -> tuple[str, int, str, str, str] | None:
    if value is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,400}", value):
        raise LookupRejected("invalid_cursor")
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise LookupRejected("invalid_cursor") from exc
    if (not isinstance(decoded, list) or len(decoded) != 5 or not isinstance(decoded[0], str)
            or not isinstance(decoded[1], int) or decoded[2] not in _EVIDENCE_TABLES
            or not isinstance(decoded[3], str) or len(decoded[3]) > 64
            or decoded[4] not in MOMENT_KINDS):
        raise LookupRejected("invalid_cursor")
    try:
        _parse_time(decoded[0])
    except InvalidReadEvidence as exc:
        raise LookupRejected("invalid_cursor") from exc
    return (decoded[0], decoded[1], decoded[2], decoded[3], decoded[4])


def build_history(
    raw: object,
    *,
    reads: Sequence[ChatwootRead],
    instance: InstanceMark,
    settings: FlowSettings,
    agent_bot_id: int,
    chatwoot_public_url: str | None,
    now: datetime,
    limit: int,
    cursor: tuple[str, int, str, str, str] | None,
) -> tuple[dict[str, object], list[dict[str, object]], str | None]:
    """La historia de un lead: la base y Chatwoot en una sola linea de tiempo."""
    if not isinstance(raw, dict):
        raise InvalidReadEvidence("invalid_history_shape")
    zone = ZoneInfo(instance.time_zone)
    gaps: list[dict[str, object]] = [_gap("payment_slip_not_attached", "instance")]
    lead_raw = raw.get("lead")
    if not isinstance(lead_raw, dict):
        raise InvalidReadEvidence("invalid_lead")
    try:
        lead_ref = str(UUID(str(lead_raw.get("lead_ref"))))
    except ValueError as exc:
        raise InvalidReadEvidence("invalid_lead_ref") from exc
    numbers = _conversation_numbers(lead_raw.get("conversations"))
    identity_raw = lead_raw.get("identity")
    if not isinstance(identity_raw, dict):
        raise InvalidReadEvidence("invalid_identity")
    identity = {}
    for field_name in ("has_phone", "has_email", "whatsapp_authorized", "opted_out"):
        value = identity_raw.get(field_name)
        if not isinstance(value, bool):
            raise InvalidReadEvidence("invalid_identity_flag")
        identity[field_name] = value
    name = first_name(lead_raw.get("first_name_inferred"), lead_raw.get("first_name_fallback"))

    raw_moments = raw.get("moments")
    if not isinstance(raw_moments, list):
        raise InvalidReadEvidence("invalid_moments")
    database = [_moment_from_database(item, zone=zone, gaps=gaps) for item in raw_moments]
    templates: dict[int, _Moment] = {}
    for moment in database:
        message_id = moment.data.pop("chatwoot_message_id", None)
        if moment.data["kind"] in ("accepted_by_chatwoot", "coupon_sent", "reactivation") and _int(message_id):
            templates[int(message_id)] = moment  # type: ignore[arg-type]
        if moment.data["kind"] == "start_rejected" and moment.data["reason_code"] is None:
            gaps.append(_gap("start_rejection_not_persisted", "lead"))

    counts = raw.get("gap_counts")
    if not isinstance(counts, dict):
        raise InvalidReadEvidence("invalid_gap_counts")
    for code in ("unattached_hotmart_event", "case_creation_failure_not_persisted",
                 "purchase_channel_not_persisted", "agent_release_not_persisted"):
        if (_int(counts.get(code)) or 0) > 0:
            gaps.append(_gap(code, "lead"))

    chatwoot, readable = project_chatwoot(
        reads, inbox_id=instance.chatwoot_inbox_id, agent_bot_id=agent_bot_id,
        templates=templates, zone=zone, gaps=gaps)

    # El cupon: lo que todavia puede pasar y por que no salio, por conversacion.
    now_raw = raw.get("now")
    if not isinstance(now_raw, dict):
        raise InvalidReadEvidence("invalid_now")
    now_state: dict[str, object] = {}
    for field_name in ("purchased", "opted_out", "pending_handoff", "human_takeover"):
        value = now_raw.get(field_name)
        if not isinstance(value, bool):
            raise InvalidReadEvidence("invalid_now_flag")
        now_state[field_name] = value
    now_state["live_actions"] = _int(now_raw.get("live_actions"))
    # Lo que el barrido del cupon frena antes de mirar los mensajes.
    coupon_block = (
        "intent_purchased" if now_state["purchased"]
        else "contact_opted_out" if now_state["opted_out"]
        else "human_takeover_active" if now_state["pending_handoff"] or now_state["human_takeover"]
        else None
    )
    upcoming_coupon: list[dict[str, object]] = []
    coupon_not_sent: list[_Moment] = []
    adopted = raw.get("adopted_conversations")
    if not isinstance(adopted, list):
        raise InvalidReadEvidence("invalid_adopted_conversations")
    coupon_on = bool(settings.manifest_flows.get("descuento")) and settings.coupon_enabled
    for conversation in adopted:
        if not isinstance(conversation, dict) or _int(conversation.get("conversation")) is None:
            raise InvalidReadEvidence("invalid_adopted_conversation")
        number = int(conversation["conversation"])  # type: ignore[arg-type]
        if conversation.get("coupon_sent") is True or not coupon_on:
            continue
        messages = readable.get(number)
        if messages is None:
            last_turn = conversation.get("last_agent_turn_at")
            if isinstance(last_turn, str):
                since = _parse_time(last_turn)
                opens, closes = _coupon_window(
                    since, min_age=settings.coupon_min_age_seconds, max_age=settings.coupon_max_age_seconds,
                    zone=zone, send_hours=settings.coupon_send_hours)
                if now <= closes:
                    upcoming_coupon.append(_coupon_upcoming(
                        number, opens, closes, "cancel" if coupon_block else "unknown", zone,
                        reason_code=coupon_block))
            gaps.append(_gap("followup_skip_not_persisted", "lead", what=f"el cupón de la conversación #{number}"))
            continue
        code, since = _coupon_view(messages, now=now, min_age=settings.coupon_min_age_seconds,
                                   max_age=settings.coupon_max_age_seconds)
        if code in ("inbound_too_recent", "eligible_for_execution") and since is not None:
            opens, closes = _coupon_window(
                since, min_age=settings.coupon_min_age_seconds, max_age=settings.coupon_max_age_seconds,
                zone=zone, send_hours=settings.coupon_send_hours)
            decision = ("cancel" if coupon_block
                        else "unknown" if settings.coupon_test_phone_set else "execute")
            upcoming_coupon.append(_coupon_upcoming(
                number, opens, closes, decision, zone, reason_code=coupon_block))
            continue
        # Una hora estable, para que el momento no cambie de pagina: el cierre
        # de la ventana si ya paso, o el ultimo mensaje publico.
        if code == "inbound_too_old" and since is not None:
            at = since + timedelta(seconds=settings.coupon_max_age_seconds)
        else:
            at = _last_public_at(messages) or now
        coupon_not_sent.append(_Moment(at, 3, {
            "at": at.isoformat(),
            "local_at": at.astimezone(zone).isoformat(),
            "kind": "coupon_not_sent",
            "flow_key": "descuento",
            "flow_version": None,
            "reason": _REASON_BY_CODE.get(code),
            "reason_code": code,
            "explanation": EXPLANATIONS.get(code),
            "certainty": "inferred",
            "attached_by": "conversation",
            "evidence": {"table": "chatwoot.messages", "id": f"conversation:{number}"},
            "conversation": number,
        }))

    upcoming = _upcoming(raw.get("upcoming"), zone=zone) + upcoming_coupon
    if len(upcoming) > 20:
        raise ResponseTooLarge(f"upcoming={len(upcoming)}")
    all_moments = sorted(database + chatwoot + coupon_not_sent, key=lambda moment: moment.key)
    if cursor is not None:
        all_moments = [moment for moment in all_moments if moment.key > cursor]
    page = all_moments[:limit]
    next_cursor = encode_cursor(page[-1].key) if len(all_moments) > limit and page else None

    data = {
        "lead": {
            "lead_ref": lead_ref,
            "label": lead_label(name, numbers),
            "conversations": conversation_links(
                numbers, chatwoot_public_url=chatwoot_public_url, account_id=instance.chatwoot_account_id),
            "identity": identity,
        },
        "past": [moment.data for moment in page],
        "upcoming": upcoming,
        "now": now_state,
    }
    return data, _dedupe(gaps), next_cursor


def _coupon_upcoming(number: int, opens: datetime, closes: datetime, decision: str,
                     zone: ZoneInfo, *, reason_code: str | None = None) -> dict[str, object]:
    return {
        "kind": "coupon_window",
        "flow_key": "descuento",
        "conversation": number,
        "due_at": opens.isoformat(),
        "local_due_at": opens.astimezone(zone).isoformat(),
        "expires_at": closes.isoformat(),
        "checked_before_sending": ["compra", "contesta", "tomada_por_el_equipo", "horario"],
        "expected": {
            "decision": decision,
            "reason": _REASON_BY_CODE.get(reason_code) if reason_code else None,
            "reason_code": reason_code,
            "explanation": EXPLANATIONS.get(reason_code) if reason_code else None,
            "certainty": "inferred",
        },
    }


def _upcoming(raw: object, *, zone: ZoneInfo) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        raise InvalidReadEvidence("invalid_upcoming")
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise InvalidReadEvidence("invalid_upcoming_item")
        flow_key = item.get("flow_key")
        if flow_key is not None and flow_key not in FLOW_KEYS:
            raise InvalidReadEvidence("invalid_upcoming_flow")
        due = _parse_time(item.get("due_at"))
        expected = item.get("expected")
        if not isinstance(expected, dict):
            raise InvalidReadEvidence("invalid_expected")
        decision = expected.get("decision")
        code = expected.get("reason_code")
        if decision not in ("cancel", "pause", "defer", "expire", "execute"):
            decision = "unknown"
        if not code_ok(code):
            code = None
        context = {
            "winner_flow_key": expected.get("winner_flow_key") if expected.get("winner_flow_key") in FLOW_KEYS else None,
            "control_state": expected.get("control_state") if code_ok(expected.get("control_state")) else None,
        }
        result.append({
            "kind": "scheduled_step",
            "flow_key": flow_key,
            "flow_version": _flow_version(item.get("flow_version")),
            "step_key": item.get("step_key") if isinstance(item.get("step_key"), str) else None,
            "due_at": due.isoformat(),
            "local_due_at": due.astimezone(zone).isoformat(),
            "expires_at": item.get("expires_at") if isinstance(item.get("expires_at"), str) else None,
            "checked_before_sending": ["vence", "baja", "compra", "tomada_por_el_equipo", "horario"],
            "expected": {
                "decision": decision,
                "reason": _reason(code, "upcoming", context) if code else None,
                "reason_code": code,
                "explanation": EXPLANATIONS.get(code) if code else None,
                "certainty": "inferred",
                **{key: value for key, value in context.items() if value is not None},
            },
        })
    return result


def flow_settings_from(
    *,
    manifest_flows: Mapping[str, bool],
    templates: Mapping[str, Any],
    time_zone: str,
    settings: Any,
) -> FlowSettings:
    """Lee de Settings solo la lista cerrada de valores de los flujos."""
    template_map: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for slot, template in templates.items():
        template_map[slot] = (template.name, template.language, tuple(template.parameters))
    coupon_template = None
    if "descuento" in template_map:
        name, language, _ = template_map["descuento"]
        coupon_template = (name, language)
    elif settings.conversation_followup_template_name:
        coupon_template = (settings.conversation_followup_template_name,
                           settings.conversation_followup_template_language or "es")
    reactivation_template = None
    if settings.conversation_reactivation_template_name:
        reactivation_template = (settings.conversation_reactivation_template_name,
                                 settings.conversation_reactivation_template_language or "es")
    return FlowSettings(
        manifest_flows=dict(manifest_flows),
        templates=template_map,
        time_zone=time_zone,
        precheckout_enabled=bool(settings.portable_precheckout_first_contact_enabled),
        pilot_enabled=bool(settings.pilot_boundary_enabled),
        recovery_enabled=bool(settings.portable_hotmart_recovery_enabled),
        payment_failure_enabled=bool(settings.portable_hotmart_payment_failure_enabled),
        coupon_enabled=bool(settings.conversation_followup_enabled),
        coupon_template=coupon_template,
        coupon_code=settings.conversation_followup_coupon_code,
        coupon_product_name=settings.conversation_followup_product_name,
        coupon_min_age_seconds=int(settings.conversation_followup_min_age_seconds),
        coupon_max_age_seconds=int(settings.conversation_followup_max_age_seconds),
        coupon_send_hours=settings.conversation_followup_send_hours,
        coupon_test_phone_set=settings.conversation_followup_only_phone is not None,
        reactivation_enabled=bool(settings.conversation_reactivation_enabled),
        reactivation_template=reactivation_template,
        reactivation_min_age_seconds=int(settings.conversation_reactivation_min_age_seconds),
        reactivation_max_age_seconds=int(settings.conversation_reactivation_max_age_seconds),
        stalled_monitor_enabled=bool(settings.chatwoot_stalled_monitor_enabled),
        resume_enabled=bool(settings.conversation_resume_enabled),
        precheckout_test_phone_set=settings.precheckout_test_phone_e164 is not None,
        allowed_jid_set=settings.allowed_jid is not None,
    )
