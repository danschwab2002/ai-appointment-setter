"""Deterministic filtering for Chatwoot webhook events."""

from dataclasses import dataclass
import re
from typing import Any, Literal


EventAction = Literal[
    "capture_incoming",
    "pause_automation",
    "record_automation",
    "ignore",
]

_SCOPE_UNSET = object()
_WHATSAPP_JID_SUFFIX = "@s.whatsapp.net"
_WHATSAPP_DIGITS = re.compile(r"[1-9][0-9]{6,14}")
_WHATSAPP_E164 = re.compile(r"\+[1-9][0-9]{6,14}")


@dataclass(frozen=True)
class EventDecision:
    accepted: bool
    reason: str
    sender_jid: str | None
    action: EventAction


def _json_object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _canonical_positive_id(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value


def matches_allowed_whatsapp_identity(
    observed_identity: object,
    *,
    allowed_jid: object,
    allow_e164: bool = False,
) -> bool:
    """Match Evolution JIDs and official-WABA digit source IDs fail-closed."""
    if not isinstance(observed_identity, str) or not isinstance(allowed_jid, str):
        return False
    if observed_identity == allowed_jid:
        return True
    if not allowed_jid.endswith(_WHATSAPP_JID_SUFFIX):
        return False
    allowed_digits = allowed_jid[: -len(_WHATSAPP_JID_SUFFIX)]
    if not allowed_digits or not allowed_digits.isdigit():
        return False
    return observed_identity == allowed_digits or (
        allow_e164 and observed_identity == f"+{allowed_digits}"
    )


def matches_allowed_whatsapp_phone(
    observed_phone: object,
    *,
    allowed_jid: object,
) -> bool:
    """Match only a canonical E.164 phone fallback to the configured JID."""
    return (
        isinstance(observed_phone, str)
        and _WHATSAPP_E164.fullmatch(observed_phone) is not None
        and matches_allowed_whatsapp_identity(
            observed_phone,
            allowed_jid=allowed_jid,
            allow_e164=True,
        )
    )


def _canonical_whatsapp_jid(observed_identity: object) -> str | None:
    if not isinstance(observed_identity, str):
        return None
    digits = observed_identity.removesuffix(_WHATSAPP_JID_SUFFIX)
    if _WHATSAPP_DIGITS.fullmatch(digits) is None:
        return None
    return f"{digits}{_WHATSAPP_JID_SUFFIX}"


def _canonical_whatsapp_jid_from_e164(observed_phone: object) -> str | None:
    """El JID de digitos que corresponde a un telefono E.164 canonico, o None.

    Es el mismo valor que el webhook trae en ``contact_inbox.source_id`` para
    WhatsApp Cloud (los digitos sin ``+``). Solo acepta E.164 estricto: sin
    espacios, sin cero inicial, sin sufijo; cualquier otra cosa falla cerrado.
    """
    if not isinstance(observed_phone, str):
        return None
    if _WHATSAPP_E164.fullmatch(observed_phone) is None:
        return None
    return f"{observed_phone[1:]}{_WHATSAPP_JID_SUFFIX}"


@dataclass(frozen=True)
class LabelRemoval:
    """Un ``conversation_updated`` en el que una persona saco una etiqueta."""

    conversation_id: int
    sender_jid: str | None


def _label_list_value(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(
        isinstance(label, str) for label in value
    ):
        return None
    return value


def classify_chatwoot_label_removal(
    payload: object,
    *,
    label: str,
    expected_inbox_id: int | None,
    allowed_jid: str | None,
    allow_any_scoped_sender: bool = False,
    expected_account_id: int | None = None,
) -> LabelRemoval | None:
    """El cambio de etiqueta que saca ``label``, o None.

    Forma del emisor (Chatwoot v4.13.0, ``WebhookListener#conversation_updated``):
    el ``webhook_data`` de la conversacion en la raiz (``id``, ``inbox_id``,
    ``labels``, ``contact_inbox``, ``meta``...) mas ``changed_attributes`` =
    ``[{atributo: {previous_value, current_value}}]``. Solo cuenta un cambio
    de ``label_list`` cuyo valor anterior tenia ``label`` y el actual no: es
    lo que hace una persona al sacar la etiqueta a mano o al correr el macro
    de reanudacion. Cualquier forma inesperada devuelve None (falla cerrado).

    La identidad se resuelve como en ``classify_chatwoot_event``: con
    remitentes acotados por scope, el JID canonico del ``source_id`` (o del
    ``identifier``, o del telefono E.164 si no hay ninguno); si no, tiene que
    coincidir con ``allowed_jid``.

    La cuenta: si el payload la trae (``account_id`` en la raiz o
    ``account.id``) y no es ``expected_account_id``, se ignora. Si no la trae
    alcanza el inbox, que en Chatwoot es un id global; el snapshot del cliente
    de control se lee igual con su cuenta.
    """
    event = _json_object(payload)
    if event.get("event") != "conversation_updated":
        return None
    expected = _canonical_positive_id(expected_inbox_id)
    if expected is None or _canonical_positive_id(event.get("inbox_id")) != expected:
        return None
    conversation_id = _canonical_positive_id(event.get("id"))
    if conversation_id is None:
        return None
    account_id = _canonical_positive_id(expected_account_id)
    for observed_account in (
        event.get("account_id"),
        _json_object(event.get("account")).get("id"),
    ):
        if (
            account_id is not None
            and observed_account is not None
            and _canonical_positive_id(observed_account) != account_id
        ):
            return None
    changes = event.get("changed_attributes")
    if not isinstance(changes, list):
        return None
    removed = False
    for change in changes:
        if not isinstance(change, dict) or "label_list" not in change:
            continue
        label_change = _json_object(change.get("label_list"))
        previous = _label_list_value(label_change.get("previous_value"))
        current = _label_list_value(label_change.get("current_value"))
        if previous is None or current is None:
            return None
        if label in previous and label not in current:
            removed = True
    if not removed:
        return None
    contact_inbox_value = event.get("contact_inbox")
    if contact_inbox_value is not None and not isinstance(contact_inbox_value, dict):
        return None
    contact_inbox = _json_object(contact_inbox_value)
    sender = _json_object(_json_object(event.get("meta")).get("sender"))
    identifier = sender.get("identifier")
    source_id = contact_inbox.get("source_id")
    sender_jid = (
        identifier
        if identifier is not None
        else source_id if source_id is not None else None
    )
    sender_phone = sender.get("phone_number")
    if allow_any_scoped_sender:
        canonical = _canonical_whatsapp_jid(sender_jid)
        if canonical is None and sender_jid is None:
            canonical = _canonical_whatsapp_jid_from_e164(sender_phone)
        if canonical is None:
            return None
        return LabelRemoval(conversation_id=conversation_id, sender_jid=canonical)
    using_phone_fallback = sender_jid is None and matches_allowed_whatsapp_phone(
        sender_phone, allowed_jid=allowed_jid
    )
    if not matches_allowed_whatsapp_identity(
        sender_phone if using_phone_fallback else sender_jid,
        allowed_jid=allowed_jid,
        allow_e164=using_phone_fallback,
    ):
        return None
    return LabelRemoval(conversation_id=conversation_id, sender_jid=None)


def classify_chatwoot_event(
    payload: object,
    *,
    allowed_jid: str | None,
    agent_bot_id: int | None = None,
    expected_account_id: int | None | object = _SCOPE_UNSET,
    expected_inbox_id: int | None | object = _SCOPE_UNSET,
    allow_any_scoped_sender: bool = False,
) -> EventDecision:
    """Classify an event before any agent can be invoked."""
    event = _json_object(payload)
    conversation = _json_object(event.get("conversation"))
    contact_inbox_value = conversation.get("contact_inbox")
    metadata_value = conversation.get("meta")
    contact_inbox = _json_object(contact_inbox_value)
    metadata = _json_object(metadata_value)
    sender_value = metadata.get("sender")
    sender = _json_object(sender_value)
    malformed_identity_container = (
        contact_inbox_value is not None and not isinstance(contact_inbox_value, dict)
    )
    identifier_present = "identifier" in sender and sender.get("identifier") is not None
    source_present = (
        "source_id" in contact_inbox
        and contact_inbox.get("source_id") is not None
    )
    sender_jid = (
        sender.get("identifier")
        if identifier_present
        else contact_inbox.get("source_id") if source_present else None
    )
    sender_phone = sender.get("phone_number")
    using_phone_fallback = (
        not identifier_present
        and not source_present
        and matches_allowed_whatsapp_phone(
            sender_phone,
            allowed_jid=allowed_jid,
        )
    )
    observed_identity = sender_phone if using_phone_fallback else sender_jid

    if event.get("event") != "message_created":
        return EventDecision(False, "unsupported_event", sender_jid, "ignore")
    if event.get("private") is not False:
        return EventDecision(False, "private_message", sender_jid, "ignore")
    if malformed_identity_container:
        return EventDecision(False, "sender_not_allowed", sender_jid, "ignore")
    if expected_account_id is not _SCOPE_UNSET:
        if (
            not isinstance(expected_account_id, int)
            or isinstance(expected_account_id, bool)
            or expected_account_id <= 0
        ):
            return EventDecision(
                False, "scope_configuration_incomplete", sender_jid, "ignore"
            )
        account = _json_object(event.get("account"))
        if _canonical_positive_id(account.get("id")) != expected_account_id:
            return EventDecision(False, "account_not_allowed", sender_jid, "ignore")
    if expected_inbox_id is not _SCOPE_UNSET:
        if (
            not isinstance(expected_inbox_id, int)
            or isinstance(expected_inbox_id, bool)
            or expected_inbox_id <= 0
        ):
            return EventDecision(
                False, "scope_configuration_incomplete", sender_jid, "ignore"
            )
        inbox = _json_object(event.get("inbox"))
        if (
            _canonical_positive_id(inbox.get("id")) != expected_inbox_id
            or _canonical_positive_id(conversation.get("inbox_id"))
            != expected_inbox_id
        ):
            return EventDecision(False, "inbox_not_allowed", sender_jid, "ignore")
    if allow_any_scoped_sender:
        if (
            _canonical_positive_id(expected_account_id) is None
            or _canonical_positive_id(expected_inbox_id) is None
        ):
            return EventDecision(
                False, "scope_configuration_incomplete", sender_jid, "ignore"
            )
        canonical_sender_jid = _canonical_whatsapp_jid(sender_jid)
        if canonical_sender_jid is None and sender_jid is None:
            # El show de la API (GET /conversations/{id}) no trae contact_inbox
            # en la raiz y meta.sender.identifier llega null en WhatsApp Cloud:
            # la unica identidad es el telefono E.164. El monitor de estancadas
            # arma su payload desde ese show, y con los remitentes acotados por
            # scope esto descartaba a todos con sender_not_allowed (24/09/2026,
            # fixture chatwoot_conversation_api_show_conv_158_20260924.json).
            # Solo aplica cuando no hay identifier ni source_id: un identifier
            # o source_id presente pero malformado sigue fallando cerrado.
            canonical_sender_jid = _canonical_whatsapp_jid_from_e164(sender_phone)
        if canonical_sender_jid is None:
            return EventDecision(False, "sender_not_allowed", sender_jid, "ignore")
        sender_jid = canonical_sender_jid
    else:
        if not matches_allowed_whatsapp_identity(
            observed_identity,
            allowed_jid=allowed_jid,
            allow_e164=using_phone_fallback,
        ):
            return EventDecision(False, "sender_not_allowed", sender_jid, "ignore")
        sender_jid = allowed_jid

    message_type = event.get("message_type")
    event_sender = _json_object(event.get("sender"))
    sender_type = str(event_sender.get("type", "")).lower()
    if message_type == "outgoing" and sender_type == "user":
        return EventDecision(
            False,
            "human_outgoing",
            sender_jid,
            "pause_automation",
        )
    if (
        message_type == "outgoing"
        and sender_type == "agent_bot"
        and agent_bot_id is not None
        and event_sender.get("id") == agent_bot_id
    ):
        return EventDecision(
            False,
            "automation_outgoing",
            sender_jid,
            "record_automation",
        )
    if message_type == "outgoing":
        return EventDecision(
            False,
            "unknown_outgoing_actor",
            sender_jid,
            "pause_automation",
        )
    if message_type != "incoming":
        return EventDecision(False, "not_incoming", sender_jid, "ignore")
    message_id = event.get("id")
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or message_id < 0
    ):
        return EventDecision(False, "invalid_message_id", sender_jid, "ignore")
    return EventDecision(True, "accepted", sender_jid, "capture_incoming")
