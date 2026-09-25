import json
from pathlib import Path

from bridge.filtering import classify_chatwoot_event
import pytest


def _scoped_incoming_payload(*, account_id: int = 1, inbox_id: int = 6) -> dict[str, object]:
    return {
        "event": "message_created",
        "id": 200,
        "message_type": "incoming",
        "private": False,
        "account": {"id": account_id},
        "inbox": {"id": inbox_id},
        "conversation": {
            "inbox_id": inbox_id,
            "meta": {
                "sender": {"identifier": "12025550123@s.whatsapp.net"},
            },
        },
    }


def test_accepts_only_the_configured_account_and_inbox() -> None:
    decision = classify_chatwoot_event(
        _scoped_incoming_payload(),
        allowed_jid="12025550123@s.whatsapp.net",
        expected_account_id=1,
        expected_inbox_id=6,
    )

    assert decision.accepted is True
    assert decision.reason == "accepted"


def test_scoped_inbound_mode_accepts_canonical_sender_from_exact_scope() -> None:
    payload = _scoped_incoming_payload()
    conversation = payload["conversation"]
    assert isinstance(conversation, dict)
    metadata = conversation["meta"]
    assert isinstance(metadata, dict)
    metadata["sender"] = {"identifier": "12025550999"}

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
        expected_account_id=1,
        expected_inbox_id=6,
        allow_any_scoped_sender=True,
    )

    assert decision.accepted is True
    assert decision.sender_jid == "12025550999@s.whatsapp.net"


@pytest.mark.parametrize("identifier", ["", "not-a-phone", "+12025550999", True, None])
def test_scoped_inbound_mode_rejects_malformed_sender(identifier: object) -> None:
    payload = _scoped_incoming_payload()
    conversation = payload["conversation"]
    assert isinstance(conversation, dict)
    metadata = conversation["meta"]
    assert isinstance(metadata, dict)
    metadata["sender"] = {"identifier": identifier}

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
        expected_account_id=1,
        expected_inbox_id=6,
        allow_any_scoped_sender=True,
    )

    assert decision.accepted is False
    assert decision.reason == "sender_not_allowed"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (_scoped_incoming_payload(account_id=2), "account_not_allowed"),
        (_scoped_incoming_payload(inbox_id=1), "inbox_not_allowed"),
        (
            {
                **_scoped_incoming_payload(),
                "conversation": {
                    "inbox_id": 1,
                    "meta": {
                        "sender": {
                            "identifier": "12025550123@s.whatsapp.net",
                        }
                    },
                },
            },
            "inbox_not_allowed",
        ),
        (
            {key: value for key, value in _scoped_incoming_payload().items() if key != "inbox"},
            "inbox_not_allowed",
        ),
        (
            {
                key: value
                for key, value in _scoped_incoming_payload().items()
                if key != "account"
            },
            "account_not_allowed",
        ),
    ],
)
def test_rejects_same_sender_from_another_or_malformed_scope(
    payload: dict[str, object], reason: str
) -> None:
    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
        expected_account_id=1,
        expected_inbox_id=6,
    )

    assert decision.accepted is False
    assert decision.action == "ignore"
    assert decision.reason == reason


@pytest.mark.parametrize(
    ("expected_account_id", "expected_inbox_id"),
    [(None, 6), (1, None), (True, 6), (1, 0)],
)
def test_rejects_an_incomplete_or_invalid_runtime_scope(
    expected_account_id: object,
    expected_inbox_id: object,
) -> None:
    decision = classify_chatwoot_event(
        _scoped_incoming_payload(),
        allowed_jid="12025550123@s.whatsapp.net",
        expected_account_id=expected_account_id,
        expected_inbox_id=expected_inbox_id,
    )

    assert decision.accepted is False
    assert decision.action == "ignore"
    assert decision.reason == "scope_configuration_incomplete"


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("account", "account_not_allowed"),
        ("inbox", "inbox_not_allowed"),
        ("conversation", "inbox_not_allowed"),
    ],
)
def test_rejects_boolean_payload_ids_that_compare_equal_to_one(
    location: str,
    reason: str,
) -> None:
    payload = _scoped_incoming_payload(account_id=1, inbox_id=1)
    if location == "account":
        payload["account"] = {"id": True}
    elif location == "inbox":
        payload["inbox"] = {"id": True}
    else:
        conversation = payload["conversation"]
        assert isinstance(conversation, dict)
        conversation["inbox_id"] = True

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
        expected_account_id=1,
        expected_inbox_id=1,
    )

    assert decision.accepted is False
    assert decision.action == "ignore"
    assert decision.reason == reason


def test_accepts_only_configured_whatsapp_jid() -> None:
    payload = {
        "event": "message_created",
        "id": 100,
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


def test_accepts_waba_digit_source_id_for_the_configured_whatsapp_jid() -> None:
    payload = {
        "event": "message_created",
        "id": 102,
        "message_type": "incoming",
        "private": False,
        "conversation": {"contact_inbox": {"source_id": "12025550123"}},
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
    )

    assert decision.accepted is True
    assert decision.sender_jid == "12025550123@s.whatsapp.net"
    assert decision.reason == "accepted"


def test_accepts_waba_e164_phone_number_when_identifier_is_canonical_null() -> None:
    payload = {
        "event": "message_created",
        "id": 104,
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": {},
            "meta": {
                "sender": {
                    "identifier": None,
                    "phone_number": "+12025550123",
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


@pytest.mark.parametrize(
    ("location", "value"),
    [
        ("identifier", ""),
        ("identifier", False),
        ("identifier", "+12025550123"),
        ("source_id", False),
        ("source_id", "+12025550123"),
    ],
)
def test_rejects_waba_phone_fallback_when_an_explicit_identity_is_malformed(
    location: str,
    value: object,
) -> None:
    sender: dict[str, object] = {"phone_number": "+12025550123"}
    contact_inbox: dict[str, object] = {}
    if location == "identifier":
        sender[location] = value
        contact_inbox["source_id"] = "12025550123"
    else:
        contact_inbox[location] = value
    payload = {
        "event": "message_created",
        "id": 105,
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": contact_inbox,
            "meta": {"sender": sender},
        },
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
    )

    assert decision.accepted is False
    assert decision.reason == "sender_not_allowed"


@pytest.mark.parametrize(
    "phone_number",
    ["12025550123", "12025550123@s.whatsapp.net", " +12025550123"],
)
def test_waba_phone_fallback_requires_canonical_e164(phone_number: str) -> None:
    payload = {
        "event": "message_created",
        "id": 106,
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": {},
            "meta": {
                "sender": {
                    "identifier": None,
                    "phone_number": phone_number,
                }
            },
        },
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
    )

    assert decision.accepted is False
    assert decision.reason == "sender_not_allowed"


def test_rejects_malformed_contact_inbox_before_phone_fallback() -> None:
    payload = {
        "event": "message_created",
        "id": 107,
        "message_type": "incoming",
        "private": False,
        "conversation": {
            "contact_inbox": [],
            "meta": {
                "sender": {
                    "identifier": None,
                    "phone_number": "+12025550123",
                }
            },
        },
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
    )

    assert decision.accepted is False
    assert decision.reason == "sender_not_allowed"


@pytest.mark.parametrize(
    "source_id",
    ["+12025550123", "12025550124", "12025550123@c.us"],
)
def test_rejects_noncanonical_waba_source_id_variants(source_id: str) -> None:
    payload = {
        "event": "message_created",
        "id": 103,
        "message_type": "incoming",
        "private": False,
        "conversation": {"contact_inbox": {"source_id": source_id}},
    }

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="12025550123@s.whatsapp.net",
    )

    assert decision.accepted is False
    assert decision.reason == "sender_not_allowed"


def test_accepts_evolution_identifier_in_chatwoot_sender_metadata() -> None:
    payload = {
        "event": "message_created",
        "id": 101,
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


@pytest.mark.parametrize("message_id", [None, True, "102", -1])
def test_rejects_incoming_messages_without_a_canonical_message_id(
    message_id: object,
) -> None:
    decision = classify_chatwoot_event(
        {
            "event": "message_created",
            "id": message_id,
            "message_type": "incoming",
            "private": False,
            "conversation": {
                "contact_inbox": {
                    "source_id": "12025550123@s.whatsapp.net",
                }
            },
        },
        allowed_jid="12025550123@s.whatsapp.net",
    )

    assert decision.accepted is False
    assert decision.action == "ignore"
    assert decision.reason == "invalid_message_id"

# ---------------------------------------------------------------------------
# Identidad en modo scoped cuando el payload sale del show de la API y no del
# webhook. Fixture capturado el 24/09/2026 12:38 UTC (conv 158, inbox 9, WABA).
# ---------------------------------------------------------------------------


def _captured_api_show_conversation_158() -> dict[str, object]:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "chatwoot_conversation_api_show_conv_158_20260924.json"
        ).read_text(encoding="utf-8")
    )
    conversation = fixture["conversation"]
    assert isinstance(conversation, dict)
    return conversation


def _stalled_monitor_payload_from_api_show(
    conversation: dict[str, object],
) -> dict[str, object]:
    """El mismo payload minimo que arma ``_stalled_conversation_candidate``."""
    messages = conversation["messages"]
    assert isinstance(messages, list)
    latest = max(
        (message for message in messages if message["message_type"] == 0),
        key=lambda message: message["id"],
    )
    metadata = conversation["meta"]
    assert isinstance(metadata, dict)
    return {
        "event": "message_created",
        "id": latest["id"],
        "content": latest["content"],
        "created_at": latest["created_at"],
        "message_type": "incoming",
        "private": False,
        "sender": latest["sender"],
        "account": {"id": 1},
        "inbox": {"id": 9},
        "conversation": {
            "id": conversation["id"],
            "inbox_id": 9,
            "status": "open",
            "can_reply": True,
            "labels": list(conversation["labels"]),
            "meta": {"sender": metadata["sender"], "assignee": None},
            "contact_inbox": conversation.get("contact_inbox"),
        },
        "_stalled_monitor": {"version": 1},
    }


def test_scoped_mode_accepts_the_e164_phone_when_the_api_show_carries_no_other_identity() -> None:
    """El show de la API no trae contact_inbox y meta.sender.identifier es null.

    Con los remitentes acotados por scope, el monitor de estancadas arma este
    payload y el clasificador lo descartaba con sender_not_allowed: el
    24/09/2026 12:33 UTC el monitor recien prendido barrio sano, publico
    healthy y dejo sin readmitir a un lead que llevaba 13 h esperando. La
    unica identidad disponible en el show es phone_number en E.164, la misma
    que el worker ya acepta antes de enviar y que usa el barredor de
    reactivacion.
    """
    conversation = _captured_api_show_conversation_158()
    assert "contact_inbox" not in conversation
    metadata = conversation["meta"]
    assert isinstance(metadata, dict)
    assert metadata["sender"]["identifier"] is None

    decision = classify_chatwoot_event(
        _stalled_monitor_payload_from_api_show(conversation),
        allowed_jid="542916424279@s.whatsapp.net",  # el numero de prueba, no el lead
        agent_bot_id=1,
        expected_account_id=1,
        expected_inbox_id=9,
        allow_any_scoped_sender=True,
    )

    assert decision.accepted is True
    assert decision.reason == "accepted"
    assert decision.sender_jid == "573000000158@s.whatsapp.net"


@pytest.mark.parametrize(
    "phone_number",
    ["573000000158", " +573000000158", "+0573000000158", "+57", "", None, 573000000158],
)
def test_scoped_mode_phone_fallback_requires_canonical_e164(
    phone_number: object,
) -> None:
    conversation = _captured_api_show_conversation_158()
    metadata = conversation["meta"]
    assert isinstance(metadata, dict)
    metadata["sender"]["phone_number"] = phone_number

    decision = classify_chatwoot_event(
        _stalled_monitor_payload_from_api_show(conversation),
        allowed_jid="542916424279@s.whatsapp.net",
        agent_bot_id=1,
        expected_account_id=1,
        expected_inbox_id=9,
        allow_any_scoped_sender=True,
    )

    assert decision.accepted is False
    assert decision.reason == "sender_not_allowed"


def test_exact_scope_still_rejects_a_foreign_e164_phone() -> None:
    """Sin scope acotado el telefono solo vale si es el JID configurado."""
    conversation = _captured_api_show_conversation_158()

    decision = classify_chatwoot_event(
        _stalled_monitor_payload_from_api_show(conversation),
        allowed_jid="542916424279@s.whatsapp.net",
        agent_bot_id=1,
        expected_account_id=1,
        expected_inbox_id=9,
        allow_any_scoped_sender=False,
    )

    assert decision.accepted is False
    assert decision.reason == "sender_not_allowed"


def test_scoped_mode_keeps_the_source_id_over_the_phone_when_both_are_present() -> None:
    """El webhook trae contact_inbox.source_id; el telefono no lo pisa."""
    conversation = _captured_api_show_conversation_158()
    payload = _stalled_monitor_payload_from_api_show(conversation)
    payload_conversation = payload["conversation"]
    assert isinstance(payload_conversation, dict)
    payload_conversation["contact_inbox"] = {"source_id": "573000000999"}

    decision = classify_chatwoot_event(
        payload,
        allowed_jid="542916424279@s.whatsapp.net",
        agent_bot_id=1,
        expected_account_id=1,
        expected_inbox_id=9,
        allow_any_scoped_sender=True,
    )

    assert decision.accepted is True
    assert decision.sender_jid == "573000000999@s.whatsapp.net"
