"""Deterministic filtering for Chatwoot webhook events."""

from dataclasses import dataclass
from typing import Any, Literal


EventAction = Literal[
    "capture_incoming",
    "pause_automation",
    "record_automation",
    "ignore",
]

_SCOPE_UNSET = object()
_WHATSAPP_JID_SUFFIX = "@s.whatsapp.net"


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


def classify_chatwoot_event(
    payload: object,
    *,
    allowed_jid: str,
    agent_bot_id: int | None = None,
    expected_account_id: int | None | object = _SCOPE_UNSET,
    expected_inbox_id: int | None | object = _SCOPE_UNSET,
) -> EventDecision:
    """Classify an event before any agent can be invoked."""
    event = _json_object(payload)
    conversation = _json_object(event.get("conversation"))
    contact_inbox = _json_object(conversation.get("contact_inbox"))
    metadata = _json_object(conversation.get("meta"))
    sender = _json_object(metadata.get("sender"))
    sender_jid = sender.get("identifier") or contact_inbox.get("source_id")

    if event.get("event") != "message_created":
        return EventDecision(False, "unsupported_event", sender_jid, "ignore")
    if event.get("private") is not False:
        return EventDecision(False, "private_message", sender_jid, "ignore")
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
    if not matches_allowed_whatsapp_identity(sender_jid, allowed_jid=allowed_jid):
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
