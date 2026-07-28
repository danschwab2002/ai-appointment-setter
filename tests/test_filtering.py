from bridge.filtering import classify_chatwoot_event
import pytest


def test_accepts_only_configured_whatsapp_jid() -> None:
    payload = {
        "event": "message_created",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            }
        },
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
    )

    assert decision.accepted is True
    assert decision.sender_jid == "12025550123@s.whatsapp.net"
    assert decision.reason == "accepted"


def test_accepts_evolution_identifier_in_chatwoot_sender_metadata() -> None:
    payload = {
        "event": "message_created",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": {
                "source_id": "chatwoot-internal-source-id",
            },
            "meta": {
                "sender": {
                    "identifier": "12025550123@s.whatsapp.net",
                }
            },
        },
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
    )

    assert decision.accepted is True
    assert decision.sender_jid == "12025550123@s.whatsapp.net"
    assert decision.reason == "accepted"


def test_classifies_a_public_outgoing_user_message_as_human_takeover() -> None:
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "private": False,
        "sender": {
            "id": 1,
            "type": "user",
        },
        "conversation": {
            "meta": {
                "sender": {
                    "identifier": "12025550123@s.whatsapp.net",
                }
            },
        },
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
    )

    assert decision.action == "pause_automation"
    assert decision.accepted is False
    assert decision.reason == "human_outgoing"


def test_classifies_the_configured_agent_bot_as_automation_outgoing() -> None:
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "private": False,
        "sender": {
            "id": 1,
            "type": "agent_bot",
        },
        "conversation": {
            "meta": {
                "sender": {
                    "identifier": "12025550123@s.whatsapp.net",
                }
            },
        },
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
        agent_bot_id=1,
    )

    assert decision.action == "record_automation"
    assert decision.accepted is False
    assert decision.reason == "automation_outgoing"


def test_fails_closed_for_an_unknown_outgoing_actor() -> None:
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "private": False,
        "sender": {
            "id": 2,
            "type": "agent_bot",
        },
        "conversation": {
            "meta": {
                "sender": {
                    "identifier": "12025550123@s.whatsapp.net",
                }
            },
        },
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
        agent_bot_id=1,
    )

    assert decision.action == "pause_automation"
    assert decision.accepted is False
    assert decision.reason == "unknown_outgoing_actor"


@pytest.mark.parametrize("malformed_sender", [[], "user", 1, True])
def test_fails_closed_for_a_malformed_outgoing_sender(
    malformed_sender: object,
) -> None:
    payload = {
        "event": "message_created",
        "message_type": "outgoing",
        "private": False,
        "sender": malformed_sender,
        "conversation": {
            "meta": {
                "sender": {
                    "identifier": "12025550123@s.whatsapp.net",
                }
            },
        },
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
        agent_bot_id=1,
    )

    assert decision.action == "pause_automation"
    assert decision.accepted is False
    assert decision.reason == "unknown_outgoing_actor"


@pytest.mark.parametrize(
    ("changes", "expected_reason"),
    [
        ({"event": "message_updated"}, "unsupported_event"),
        ({"message_type": "outgoing"}, "unknown_outgoing_actor"),
        ({"private": True}, "private_message"),
    ],
)
def test_rejects_events_that_are_not_public_incoming_messages(
    changes: dict[str, object], expected_reason: str
) -> None:
    payload = {
        "event": "message_created",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": {
                "source_id": "12025550123@s.whatsapp.net",
            }
        },
        **changes,
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
    )

    assert decision.accepted is False
    assert decision.reason == expected_reason