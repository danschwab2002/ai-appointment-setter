"""Deterministic filtering for Chatwoot webhook events."""

from dataclasses import dataclass
from typing import Any, Literal


EventAction = Literal[
    "capture_incoming",
    "pause_automation",
    "record_automation",
    "ignore",
]


@dataclass(frozen=True)
class EventDecision:
    accepted: bool
    reason: str
    sender_jid: str | None
    action: EventAction


def _json_object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def classify_chatwoot_event(
    payload: object, *, allowed_jid: str, agent_bot_id: int | None = None
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
    if sender_jid != allowed_jid:
        return EventDecision(False, "sender_not_allowed", sender_jid, "ignore")

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
