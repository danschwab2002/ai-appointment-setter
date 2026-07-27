from bridge.filtering import classify_chatwoot_event
import pytest


def test_accepts_only_configured_whatsapp_jid() -> None:
    payload = {
        "event": "message_created",
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": {
                "source_id": "5492916424279@s.whatsapp.net",
            }
        },
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="5492916424279@s.whatsapp.net",
    )

    assert decision.accepted is True
    assert decision.sender_jid == "5492916424279@s.whatsapp.net"
    assert decision.reason == "accepted"


@pytest.mark.parametrize(
    ("changes", "expected_reason"),
    [
        ({"event": "message_updated"}, "unsupported_event"),
        ({"message_type": "outgoing"}, "not_incoming"),
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
                "source_id": "5492916424279@s.whatsapp.net",
            }
        },
        **changes,
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="5492916424279@s.whatsapp.net",
    )

    assert decision.accepted is False
    assert decision.reason == expected_reason