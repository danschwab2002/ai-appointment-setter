"""Tests del barredor de reactivacion.

El criterio y la plantilla se prueban contra los payloads capturados de
produccion el 2026-09-23 (``tests/fixtures/chatwoot_*_20260923.json``), no
contra payloads inventados: el shape real de Chatwoot es justamente lo que una
suite escrita de memoria se equivoca.
"""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from bridge.chatwoot import ChatwootProtocolError, TeamMessageTimestampError
from bridge.reactivation import (
    ConversationReactivationSweeper,
    ReactivationTemplate,
    evaluate_reactivation_candidate,
    parse_reactivation_template,
    reactivation_first_name,
)


FIXTURES = Path(__file__).parent / "fixtures"
INBOX_FIXTURE = json.loads(
    (FIXTURES / "chatwoot_inbox_9_message_templates_20260923.json").read_text(
        encoding="utf-8"
    )
)
CONVERSATION_FIXTURE = json.loads(
    (
        FIXTURES / "chatwoot_conversation_outside_window_20260923.json"
    ).read_text(encoding="utf-8")
)

INBOX_ID = 9
# El ultimo inbound del fixture (id 2079) quedo a las 1790090674; el ultimo del
# equipo (id 2077) a las 1790090638. Este reloj los deja 28 h y 28 h atras.
NOW = 1790090674 + 101_000


def _conversation() -> dict:
    return copy.deepcopy(CONVERSATION_FIXTURE["conversation"])


def _messages() -> list:
    return copy.deepcopy(CONVERSATION_FIXTURE["messages"]["payload"])


def _evaluate(details=None, messages=None, **overrides):
    return evaluate_reactivation_candidate(
        _conversation() if details is None else details,
        _messages() if messages is None else messages,
        expected_inbox_id=overrides.pop("expected_inbox_id", INBOX_ID),
        now_epoch=overrides.pop("now_epoch", NOW),
        **overrides,
    )


# --------------------------------------------------------------------------
# La plantilla


def test_parses_the_approved_template_captured_from_production() -> None:
    template = parse_reactivation_template(
        INBOX_FIXTURE, template_name="johanna_reactivacion_01"
    )
    assert template.name == "johanna_reactivacion_01"
    assert template.language == "es_EC"
    assert template.category == "MARKETING"
    assert template.body.startswith("Hola, {{1}}.")


def test_rendered_body_replaces_the_placeholder_with_the_first_name() -> None:
    template = parse_reactivation_template(
        INBOX_FIXTURE, template_name="johanna_reactivacion_01"
    )
    rendered = template.render("Patricia")
    assert "{{1}}" not in rendered
    assert rendered.startswith("Hola, Patricia.")
    assert template.params("Patricia") == {
        "name": "johanna_reactivacion_01",
        "category": "MARKETING",
        "language": "es_EC",
        "processed_params": {"body": {"1": "Patricia"}},
    }


def test_refuses_a_template_that_meta_did_not_approve() -> None:
    payload = copy.deepcopy(INBOX_FIXTURE)
    payload["message_templates"][0]["status"] = "PENDING"
    with pytest.raises(ChatwootProtocolError) as error:
        parse_reactivation_template(
            payload, template_name="johanna_reactivacion_01"
        )
    assert str(error.value) == "reactivation_template_not_approved"


def test_refuses_a_template_that_no_longer_exists_in_the_inbox() -> None:
    payload = copy.deepcopy(INBOX_FIXTURE)
    payload["message_templates"] = [
        template
        for template in payload["message_templates"]
        if template["name"] != "johanna_reactivacion_01"
    ]
    with pytest.raises(ChatwootProtocolError) as error:
        parse_reactivation_template(
            payload, template_name="johanna_reactivacion_01"
        )
    assert str(error.value) == "reactivation_template_not_found"


def test_refuses_a_template_with_two_placeholders() -> None:
    # johanna_interes_precheckout_01 tiene {{1}} y {{2}}: mandarla con un solo
    # parametro la hace fallar en Meta y el lead no recibe nada.
    with pytest.raises(ChatwootProtocolError) as error:
        parse_reactivation_template(
            INBOX_FIXTURE, template_name="johanna_interes_precheckout_01"
        )
    assert str(error.value) == "reactivation_template_unexpected_placeholders"


def test_refuses_a_template_without_placeholders() -> None:
    with pytest.raises(ChatwootProtocolError) as error:
        parse_reactivation_template(
            INBOX_FIXTURE, template_name="hello_world"
        )
    assert str(error.value) == "reactivation_template_unexpected_placeholders"


def test_refuses_a_template_whose_language_is_not_the_declared_one() -> None:
    with pytest.raises(ChatwootProtocolError) as error:
        parse_reactivation_template(
            INBOX_FIXTURE,
            template_name="johanna_reactivacion_01",
            expected_language="es_AR",
        )
    assert str(error.value) == "reactivation_template_language_mismatch"


def test_rendering_with_an_empty_name_is_rejected() -> None:
    template = ReactivationTemplate(
        name="t", language="es_EC", category="MARKETING", body="Hola, {{1}}."
    )
    with pytest.raises(ValueError):
        template.render("   ")
    with pytest.raises(ValueError):
        template.params("")


# --------------------------------------------------------------------------
# El primer nombre


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Los seis nombres reales del inbox 9 al 2026-09-23.
        ("Patricia García", "Patricia"),
        ("Andres felipe galvis loaiza", "Andres"),
        ("Chayin", "Chayin"),
        ("Ángel Crown", "Ángel"),
        ("Marcia Aidegart Narváez Lara", "Marcia"),
        ("Mau", "Mau"),
        # Normalizacion de mayusculas.
        ("maria", "Maria"),
        ("MARIA", "Maria"),
        ("McCarthy", "McCarthy"),
        ("  Ana   Lucia  ", "Ana"),
    ],
)
def test_first_name_of_real_contact_names(raw: str, expected: str) -> None:
    assert reactivation_first_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "+593999000126",
        "593999000126",
        "Cliente 2",
        "A",
        "x" * 41,
        "...",
        "🙂",
        12345,
    ],
)
def test_unusable_contact_names_have_no_first_name(raw) -> None:
    assert reactivation_first_name(raw) is None


# --------------------------------------------------------------------------
# El criterio


def test_the_captured_conversation_is_a_candidate() -> None:
    decision = _evaluate()
    assert decision.eligible
    candidate = decision.candidate
    assert candidate is not None
    assert candidate.conversation_id == 126
    assert candidate.last_inbound_message_id == 2079
    assert candidate.first_name == "Mau"
    assert candidate.phone == "+593999000126"
    assert candidate.command_key == "reactivate:126:2079"
    assert candidate.inbound_age_seconds == NOW - 1790090674
    assert candidate.quiet_seconds == NOW - 1790090638


def test_a_conversation_inside_the_service_window_is_skipped() -> None:
    details = _conversation()
    details["can_reply"] = True
    decision = _evaluate(details)
    assert not decision.eligible
    assert decision.skip_reason == "inside_service_window"


def test_a_missing_can_reply_is_not_treated_as_outside_the_window() -> None:
    details = _conversation()
    del details["can_reply"]
    assert _evaluate(details).skip_reason == "inside_service_window"


def test_a_resolved_conversation_is_skipped() -> None:
    details = _conversation()
    details["status"] = "resolved"
    assert _evaluate(details).skip_reason == "conversation_not_open"


def test_an_opted_out_contact_is_never_reactivated() -> None:
    details = _conversation()
    details["labels"] = ["automation_paused", "automation_opted_out"]
    assert _evaluate(details).skip_reason == "contact_opted_out"


def test_a_paused_conversation_is_still_a_candidate() -> None:
    # La pausa es exactamente el caso: las 6 conversaciones que esperaban
    # respuesta el 23/09 estaban pausadas. Excluirlas dejaria al barredor sin
    # nadie a quien reactivar.
    details = _conversation()
    assert details["labels"] == ["automation_paused"]
    assert _evaluate(details).eligible


def test_a_conversation_without_labels_is_also_a_candidate() -> None:
    details = _conversation()
    details["labels"] = []
    assert _evaluate(details).eligible


def test_a_blocked_contact_is_skipped() -> None:
    details = _conversation()
    details["meta"]["sender"]["blocked"] = True
    assert _evaluate(details).skip_reason == "contact_blocked_or_unknown"


def test_an_unknown_block_state_is_skipped() -> None:
    details = _conversation()
    details["meta"]["sender"]["blocked"] = None
    assert _evaluate(details).skip_reason == "contact_blocked_or_unknown"


def test_a_contact_without_a_readable_phone_is_skipped() -> None:
    details = _conversation()
    details["meta"]["sender"]["phone_number"] = "not-a-phone"
    details["meta"]["sender"]["identifier"] = None
    assert _evaluate(details).skip_reason == "contact_phone_unreadable"


def test_the_identifier_is_used_when_the_phone_number_is_absent() -> None:
    details = _conversation()
    details["meta"]["sender"]["phone_number"] = None
    details["meta"]["sender"]["identifier"] = "+593999000126"
    assert _evaluate(details).eligible


def test_a_phone_outside_the_allowed_jid_is_skipped() -> None:
    assert (
        _evaluate(allowed_phone="12025550123").skip_reason
        == "target_not_allowed"
    )


def test_the_allowed_jid_matches_regardless_of_the_plus_sign() -> None:
    assert _evaluate(allowed_phone="593999000126").eligible


def test_a_contact_without_a_usable_name_is_skipped() -> None:
    details = _conversation()
    details["meta"]["sender"]["name"] = "+593999000126"
    assert _evaluate(details).skip_reason == "contact_name_unusable"


def test_a_conversation_whose_last_message_is_ours_is_skipped() -> None:
    messages = _messages()
    messages.append(
        {
            "id": 2999,
            "message_type": 1,
            "private": False,
            "content": "ya te respondo",
            "created_at": 1790090700,
            "sender": {"id": 4, "type": "user"},
        }
    )
    assert (
        _evaluate(messages=messages).skip_reason == "last_message_not_inbound"
    )


def test_a_later_system_activity_does_not_hide_the_last_inbound() -> None:
    messages = _messages()
    messages.append(
        {
            "id": 3000,
            "message_type": 2,
            "private": False,
            "content": "Conversation was assigned",
            "created_at": 1790090800,
        }
    )
    assert _evaluate(messages=messages).eligible


def test_a_private_note_does_not_count_as_an_answer() -> None:
    messages = _messages()
    messages.append(
        {
            "id": 3001,
            "message_type": 1,
            "private": True,
            "content": "nota interna",
            "created_at": 1790090900,
            "sender": {"id": 4, "type": "user"},
        }
    )
    assert _evaluate(messages=messages).eligible


def test_the_order_is_the_integer_id_and_not_created_at() -> None:
    # Un mensaje nuestro con id menor pero created_at posterior no puede
    # convertirse en "el ultimo": es el mismo criterio canonico del monitor de
    # conversaciones estancadas.
    messages = _messages()
    messages.append(
        {
            "id": 10,
            "message_type": 1,
            "private": False,
            "content": "viejo con fecha nueva",
            "created_at": 1790099999,
            "sender": {"id": 4, "type": "user"},
        }
    )
    assert _evaluate(messages=messages).eligible


def test_an_unknown_public_message_type_fails_closed() -> None:
    messages = _messages()
    messages.append(
        {
            "id": 3002,
            "message_type": 7,
            "private": False,
            "content": "?",
            "created_at": 1790090950,
        }
    )
    with pytest.raises(ChatwootProtocolError) as error:
        _evaluate(messages=messages)
    assert str(error.value) == "invalid_message_type"


def test_a_boolean_message_type_fails_closed() -> None:
    messages = _messages()
    messages[-1]["message_type"] = True
    with pytest.raises(ChatwootProtocolError):
        _evaluate(messages=messages)


def test_an_inbound_inside_the_minimum_age_is_skipped() -> None:
    assert (
        _evaluate(now_epoch=1790090674 + 60).skip_reason == "inbound_too_recent"
    )


def test_an_inbound_older_than_the_maximum_age_is_skipped() -> None:
    assert (
        _evaluate(now_epoch=1790090674 + 5_000_000).skip_reason
        == "inbound_too_old"
    )


def test_a_team_that_answered_recently_keeps_the_conversation() -> None:
    """El silencio del equipo es la barrera de atras, y caza lo que el orden no.

    El assignee no sirve como senal: las derivaciones de este inbox van a un
    team sin asignado individual, asi que la conversacion figura sin humano
    incluso mientras una persona la esta contestando. Lo que se mide es el
    silencio real.

    En el caso normal esta barrera es redundante con el criterio de "el ultimo
    conversacional es del lead": si el equipo respondio despues, el ultimo ya no
    es del lead. Donde no es redundante es cuando el orden por ID y las fechas
    no coinciden, que es exactamente este caso: un mensaje del equipo con ID
    menor y fecha reciente. Sin esta barrera, la conversacion se reactivaria
    mientras alguien la esta atendiendo.
    """
    messages = _messages()
    messages.append(
        {
            "id": 2050,
            "message_type": 1,
            "private": False,
            "content": "ya lo veo",
            "created_at": NOW - 600,
            "sender": {"id": 4, "type": "user"},
        }
    )
    decision = _evaluate(messages=messages)
    assert decision.skip_reason == "team_recently_active"


def test_a_conversation_the_team_never_answered_is_a_candidate() -> None:
    messages = [
        message
        for message in _messages()
        if (message.get("sender") or {}).get("type") != "user"
    ]
    decision = _evaluate(messages=messages)
    assert decision.eligible
    assert decision.candidate is not None
    assert decision.candidate.quiet_seconds is None


def test_an_unreadable_team_timestamp_fails_closed() -> None:
    messages = _messages()
    messages[3]["created_at"] = "hace un rato"
    with pytest.raises(TeamMessageTimestampError):
        _evaluate(messages=messages)


def test_a_conversation_from_another_inbox_is_a_protocol_error() -> None:
    details = _conversation()
    details["inbox_id"] = 11
    with pytest.raises(ChatwootProtocolError) as error:
        _evaluate(details)
    assert str(error.value) == "invalid_conversation_scope"


def test_a_snoozed_conversation_is_skipped() -> None:
    details = _conversation()
    details["snoozed_until"] = 1790200000
    assert _evaluate(details).skip_reason == "conversation_snoozed"


# --------------------------------------------------------------------------
# El barredor


class _ChatwootFalso:
    def __init__(self, *, conversations=None, send_error=None, message_id=777):
        self.conversations = conversations
        self.send_error = send_error
        self.message_id = message_id
        self.sent: list[dict] = []
        self.inbox_calls = 0

    async def get_inbox(self, *, inbox_id: int) -> dict:
        self.inbox_calls += 1
        return copy.deepcopy(INBOX_FIXTURE)

    async def list_open_conversations_with_messages(
        self, *, expected_inbox_id: int, max_pages: int
    ) -> list[dict]:
        if self.conversations is not None:
            return copy.deepcopy(self.conversations)
        return [
            {"conversation": _conversation(), "messages": _messages()},
        ]

    async def send_reactivation_template(
        self, *, conversation_id, content, command_key, template_params
    ) -> dict:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(
            {
                "conversation_id": conversation_id,
                "content": content,
                "command_key": command_key,
                "template_params": template_params,
            }
        )
        return {"status": "sent", "message_id": self.message_id}


class _Claim:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome


class _SupabaseFalso:
    def __init__(self, *, claim_outcome="claimed", claim_error=None):
        self.claim_outcome = claim_outcome
        self.claim_error = claim_error
        self.claims: list[dict] = []
        self.settlements: list[dict] = []

    async def claim_conversation_reactivation(self, **kwargs):
        if self.claim_error is not None:
            raise self.claim_error
        self.claims.append(kwargs)
        return _Claim(self.claim_outcome)

    async def settle_conversation_reactivation(self, **kwargs):
        self.settlements.append(kwargs)
        return _Claim("settled")


def _sweeper(chatwoot, supabase, **overrides):
    return ConversationReactivationSweeper(
        chatwoot=chatwoot,
        supabase=supabase,
        inbox_id=INBOX_ID,
        template_name="johanna_reactivacion_01",
        clock=lambda: float(overrides.pop("now", NOW)),
        **overrides,
    )


def test_one_scan_reserves_sends_and_settles() -> None:
    chatwoot = _ChatwootFalso()
    supabase = _SupabaseFalso()
    sent = asyncio.run(_sweeper(chatwoot, supabase).run_once())

    assert sent == 1
    assert len(supabase.claims) == 1
    claim = supabase.claims[0]
    assert claim["external_conversation_id"] == 126
    assert claim["command_key"] == "reactivate:126:2079"
    assert claim["reason_code"] == "outside_service_window"
    assert claim["template_name"] == "johanna_reactivacion_01"
    assert claim["template_language"] == "es_EC"
    assert claim["last_inbound_message_id"] == 2079

    assert len(chatwoot.sent) == 1
    assert chatwoot.sent[0]["content"].startswith("Hola, Mau.")
    assert chatwoot.sent[0]["template_params"]["processed_params"] == {
        "body": {"1": "Mau"}
    }

    assert supabase.settlements == [
        {
            "command_key": "reactivate:126:2079",
            "status": "sent",
            "provider_message_id": 777,
            "failure_reason": None,
        }
    ]


def test_the_reservation_happens_before_the_send() -> None:
    chatwoot = _ChatwootFalso()
    supabase = _SupabaseFalso(claim_outcome="replayed")
    sent = asyncio.run(_sweeper(chatwoot, supabase).run_once())
    assert sent == 0
    assert chatwoot.sent == []
    assert supabase.settlements == []


def test_the_limit_reached_in_the_database_stops_the_send() -> None:
    chatwoot = _ChatwootFalso()
    supabase = _SupabaseFalso(claim_outcome="blocked_reactivation_limit")
    assert asyncio.run(_sweeper(chatwoot, supabase).run_once()) == 0
    assert chatwoot.sent == []


def test_an_opted_out_contact_in_the_database_stops_the_send() -> None:
    chatwoot = _ChatwootFalso()
    supabase = _SupabaseFalso(claim_outcome="blocked_contact")
    assert asyncio.run(_sweeper(chatwoot, supabase).run_once()) == 0
    assert chatwoot.sent == []


def test_a_conversation_chatwoot_knows_and_supabase_does_not_is_skipped() -> None:
    chatwoot = _ChatwootFalso()
    supabase = _SupabaseFalso(claim_outcome="not_found")
    assert asyncio.run(_sweeper(chatwoot, supabase).run_once()) == 0
    assert chatwoot.sent == []


def test_a_failed_send_releases_the_reservation() -> None:
    chatwoot = _ChatwootFalso(send_error=ChatwootProtocolError("boom"))
    supabase = _SupabaseFalso()
    sweeper = _sweeper(chatwoot, supabase)
    assert asyncio.run(sweeper.run_once()) == 0
    assert supabase.settlements == [
        {
            "command_key": "reactivate:126:2079",
            "status": "failed",
            "provider_message_id": None,
            "failure_reason": "ChatwootProtocolError",
        }
    ]
    assert sweeper.last_scan_state == "error"


def test_an_invalid_send_response_releases_the_reservation() -> None:
    chatwoot = _ChatwootFalso(message_id=0)
    supabase = _SupabaseFalso()
    assert asyncio.run(_sweeper(chatwoot, supabase).run_once()) == 0
    assert supabase.settlements[0]["status"] == "failed"
    assert supabase.settlements[0]["failure_reason"] == "invalid_sent_message"


def test_a_failed_reservation_never_sends() -> None:
    chatwoot = _ChatwootFalso()
    supabase = _SupabaseFalso(claim_error=RuntimeError("supabase caido"))
    assert asyncio.run(_sweeper(chatwoot, supabase).run_once()) == 0
    assert chatwoot.sent == []
    assert supabase.settlements == []


def test_a_template_that_lost_its_approval_stops_the_whole_scan() -> None:
    class _SinAprobar(_ChatwootFalso):
        async def get_inbox(self, *, inbox_id: int) -> dict:
            payload = copy.deepcopy(INBOX_FIXTURE)
            payload["message_templates"][0]["status"] = "PAUSED"
            return payload

    chatwoot = _SinAprobar()
    supabase = _SupabaseFalso()
    with pytest.raises(ChatwootProtocolError):
        asyncio.run(_sweeper(chatwoot, supabase).run_once())
    assert chatwoot.sent == []
    assert supabase.claims == []


def test_the_scan_stops_at_the_configured_send_budget() -> None:
    conversations = []
    for index, conversation_id in enumerate((126, 143, 136, 63)):
        details = _conversation()
        details["id"] = conversation_id
        details["meta"]["sender"]["id"] = 200 + index
        messages = _messages()
        for message in messages:
            message["conversation_id"] = conversation_id
            message["id"] = message["id"] + 100 * (index + 1)
        conversations.append({"conversation": details, "messages": messages})

    chatwoot = _ChatwootFalso(conversations=conversations)
    supabase = _SupabaseFalso()
    sent = asyncio.run(
        _sweeper(chatwoot, supabase, max_sends_per_scan=2).run_once()
    )
    assert sent == 2
    assert len(chatwoot.sent) == 2


def test_a_skipped_conversation_does_not_mark_the_scan_as_failed() -> None:
    details = _conversation()
    details["can_reply"] = True
    chatwoot = _ChatwootFalso(
        conversations=[{"conversation": details, "messages": _messages()}]
    )
    supabase = _SupabaseFalso()
    sweeper = _sweeper(chatwoot, supabase)
    assert asyncio.run(sweeper.run_once()) == 0
    assert sweeper.last_scan_state == "healthy"
    assert sweeper.has_completed_scan


def test_an_unreadable_team_timestamp_skips_only_that_conversation() -> None:
    broken = _conversation()
    broken["id"] = 143
    broken_messages = _messages()
    broken_messages[3]["created_at"] = None
    for message in broken_messages:
        message["conversation_id"] = 143
        message["id"] += 500
    chatwoot = _ChatwootFalso(
        conversations=[
            {"conversation": broken, "messages": broken_messages},
            {"conversation": _conversation(), "messages": _messages()},
        ]
    )
    supabase = _SupabaseFalso()
    assert asyncio.run(_sweeper(chatwoot, supabase).run_once()) == 1
    assert chatwoot.sent[0]["conversation_id"] == 126


@pytest.mark.parametrize(
    "kwargs",
    [
        {"inbox_id": 0},
        {"template_name": "   "},
        {"scan_interval_seconds": 0},
        {"max_reactivations": 0},
        {"max_sends_per_scan": 0},
        {"max_pages": 0},
        {"max_pages": 21},
        {"min_inbound_age_seconds": -1},
        {"max_inbound_age_seconds": 10, "min_inbound_age_seconds": 20},
    ],
)
def test_invalid_sweeper_configuration_is_rejected(kwargs) -> None:
    base = {
        "chatwoot": _ChatwootFalso(),
        "supabase": _SupabaseFalso(),
        "inbox_id": INBOX_ID,
        "template_name": "johanna_reactivacion_01",
    }
    base.update(kwargs)
    with pytest.raises(ValueError):
        ConversationReactivationSweeper(**base)


# --------------------------------------------------------------------------
# El resumen del barrido


def test_a_scan_that_sends_nothing_says_why() -> None:
    """Un barrido mudo no se distingue de uno sin trabajo.

    El 2026-09-23 el barredor salteo las 25 conversaciones del inbox con
    `target_not_allowed` y publico `healthy`, porque saltear a todos no es una
    falla. El resumen es lo que hace visible la diferencia.
    """
    details = _conversation()
    details["can_reply"] = True
    chatwoot = _ChatwootFalso(
        conversations=[
            {"conversation": _conversation(), "messages": _messages()},
            {"conversation": details, "messages": _messages()},
        ]
    )
    supabase = _SupabaseFalso()
    sweeper = _sweeper(chatwoot, supabase, allowed_phone="12025550123")
    assert asyncio.run(sweeper.run_once()) == 0
    resumen = sweeper.last_scan_summary
    assert "scanned=2" in resumen
    assert "sent=0" in resumen
    assert "target_not_allowed=1" in resumen
    assert "inside_service_window=1" in resumen


def test_the_summary_of_a_successful_scan_counts_the_sends() -> None:
    chatwoot = _ChatwootFalso()
    sweeper = _sweeper(chatwoot, _SupabaseFalso())
    assert asyncio.run(sweeper.run_once()) == 1
    assert "scanned=1" in sweeper.last_scan_summary
    assert "sent=1" in sweeper.last_scan_summary


def test_the_summary_never_leaks_contact_data() -> None:
    # Solo conteos y nombres de motivo, que son constantes del codigo.
    chatwoot = _ChatwootFalso()
    sweeper = _sweeper(chatwoot, _SupabaseFalso())
    asyncio.run(sweeper.run_once())
    resumen = sweeper.last_scan_summary
    for filtrado in ("Mau", "593999000126", "+593", "asistente virtual"):
        assert filtrado not in resumen


def test_the_summary_before_the_first_scan_is_never() -> None:
    sweeper = _sweeper(_ChatwootFalso(), _SupabaseFalso())
    assert sweeper.last_scan_summary == "never"
