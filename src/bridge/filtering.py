"""Deterministic filtering for Chatwoot webhook events."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EventDecision:
    accepted: bool
    reason: str
    sender_jid: str | None


def classify_chatwoot_event(
    payload: dict[str, Any], *, allowed_jid: str
) -> EventDecision:
    """Classify an event before any agent can be invoked."""
    conversation = payload.get("conversation") or {}
    contact_inbox = conversation.get("contact_inbox") or {}
    sender_jid = contact_inbox.get("source_id")

    if payload.get("event") != "message_created":
        return EventDecision(False, "unsupported_event", sender_jid)
    if payload.get("message_type") != "incoming":
        return EventDecision(False, "not_incoming", sender_jid)
    if payload.get("private") is not False:
        return EventDecision(False, "private_message", sender_jid)
    if sender_jid != allowed_jid:
        return EventDecision(False, "sender_not_allowed", sender_jid)
    return EventDecision(True, "accepted", sender_jid)
