"""El primer toque no pisa una conversacion viva (G3 de Maribel, E-177).

Lo que paso, medido el 2026-10-28 (G3 de E-173): la persona lleno el formulario
a las 02:19:54Z, converso con el agente en la conversacion 6 hasta las 02:38:06Z
(su ultimo mensaje, una imagen sin texto) y a las 03:20:00Z el primer contacto
del formulario salio en una conversacion nueva, la 9, con la 6 todavia abierta.

Dos piezas en ``ChatwootMessageSender.send_first_touch``, apagadas por default:

* ``FIRST_TOUCH_ACTIVE_CONVERSATION_GUARD_SECONDS`` (0 = apagado): si la persona
  escribio dentro de la ventana, el envio se frena con
  ``contact_active_conversation``;
* ``FIRST_TOUCH_REUSE_OPEN_CONVERSATION_ENABLED``: la plantilla sale en la
  conversacion abierta del contacto, salvo que la tenga el equipo
  (``contact_conversation_with_team``).

Datos capturados, no inventados: ``GET /contacts/767/conversations`` y
``GET /conversations/6/messages`` de Maribel, tomados el 2026-10-29 en solo
lectura desde el contenedor del bridge y redactados adentro (ningun texto,
nombre, telefono ni mail salio). La lista se recorta a como estaba a las
03:20:00Z: sin la 9, que nacio en ese momento. El telefono es sintetico.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_att1_production_settings import OUTBOUND_FLOWS, _start

from bridge.chatwoot import ChatwootClient, ChatwootProtocolError
from bridge.messaging import (
    FIRST_TOUCH_CONTACT_ACTIVE_CONVERSATION,
    FIRST_TOUCH_CONTACT_CONVERSATION_WITH_TEAM,
    ChatwootMessageSender,
    FirstTouchResult,
)
from bridge.recovery_agent import FollowupMessageProposal
from bridge.supabase import (
    ChatwootAuthorityContext,
    DeliveryAttempt,
    FollowupExecutionContext,
    ReevaluationDecision,
    ScheduledAction,
)
from bridge.worker import DurableDispatcher

FIXTURES = Path(__file__).parent / "fixtures"
CONVERSATIONS = json.loads(
    (FIXTURES / "chatwoot_contact_conversations_inbox_13_contact_767_20261029.json")
    .read_text(encoding="utf-8")
)
MESSAGES_6 = json.loads(
    (FIXTURES / "chatwoot_conversation_messages_inbox_13_conv_6_20261029.json")
    .read_text(encoding="utf-8")
)

ACCOUNT = 3
INBOX = 13
CONTACT = 767
PHONE = "520000000767"
# 2026-10-28T03:20:00Z, cuando salio el primer contacto en la 9.
SENT_AT = 1793157600
# 2026-10-28T02:38:06Z, la imagen sin texto en la 6: 42 min antes.
LAST_INBOUND_6 = 1793155086
MARIBEL_WINDOW = 7200
NEW_CONVERSATION = 900
PREFIX = f"/api/v1/accounts/{ACCOUNT}"


def _listing_at_sent_time() -> dict[str, object]:
    """La lista como estaba a las 03:20:00Z: solo la 6."""
    listing = copy.deepcopy(CONVERSATIONS)
    listing.pop("_fixture")
    listing["payload"] = [
        conversation
        for conversation in listing["payload"]
        if conversation["id"] == 6
    ]
    assert [c["status"] for c in listing["payload"]] == ["open"]
    return listing


class _MaribelInbox:
    """Chatwoot del inbox 13 con el contacto 767 ya creado (escribio el 28/10)."""

    def __init__(self, listing: dict[str, object] | None = None) -> None:
        self.listing = listing if listing is not None else _listing_at_sent_time()
        self.listing_status = 200
        self.requests: list[tuple[str, str]] = []
        self.posts: list[tuple[str, dict[str, object]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        body = json.loads(request.content) if request.content else None
        if request.method == "POST":
            assert isinstance(body, dict)
            self.posts.append((path, body))
        if request.method == "GET" and path == f"{PREFIX}/contacts/search":
            return httpx.Response(200, json={"payload": [{
                "id": CONTACT,
                "phone_number": f"+{PHONE}",
                "blocked": False,
                "contact_inboxes": [{"source_id": PHONE, "inbox": {"id": INBOX}}],
            }]})
        if request.method == "GET" and path == f"{PREFIX}/contacts/{CONTACT}/conversations":
            return httpx.Response(self.listing_status, json=self.listing)
        if request.method == "GET" and path == f"{PREFIX}/conversations/6/messages":
            payload = copy.deepcopy(MESSAGES_6)
            payload.pop("_fixture")
            return httpx.Response(200, json=payload)
        if request.method == "POST" and path == f"{PREFIX}/conversations":
            return httpx.Response(200, json={"id": NEW_CONVERSATION})
        if request.method == "POST" and path.startswith(f"{PREFIX}/conversations/"):
            assert body is not None
            conversation_id = int(path.split("/")[-2])
            return httpx.Response(200, json={
                "id": 4700,
                "conversation_id": conversation_id,
                "message_type": 1,
                "private": False,
                "content": body["content"],
                "content_attributes": body["content_attributes"],
            })
        return httpx.Response(404, json={"error": "not_emulated"})

    def client(self) -> ChatwootClient:
        return ChatwootClient(
            base_url="https://chatwoot.test",
            account_id=ACCOUNT,
            access_token="test-token",
            agent_bot_access_token="bot-token",
            agent_bot_id=3,
            transport=httpx.MockTransport(self.handler),
        )

    def sender(
        self,
        *,
        guard: int = 0,
        reuse: bool = False,
        now: int = SENT_AT,
    ) -> ChatwootMessageSender:
        return ChatwootMessageSender(
            chatwoot=self.client(),
            inbox_id=INBOX,
            allowed_jid=None,
            dynamic_recipient_enabled=True,
            active_conversation_guard_seconds=guard,
            reuse_open_conversation=reuse,
            clock=lambda: float(now),
        )

    def created_conversations(self) -> list[dict[str, object]]:
        return [body for path, body in self.posts if path == f"{PREFIX}/conversations"]

    def sent_to(self) -> list[int]:
        return [
            int(path.split("/")[-2])
            for path, _ in self.posts
            if path.endswith("/messages")
        ]

    def read_listing(self) -> bool:
        return ("GET", f"{PREFIX}/contacts/{CONTACT}/conversations") in self.requests


def _first_touch(sender: ChatwootMessageSender) -> FirstTouchResult:
    return asyncio.run(sender.send_first_touch(
        phone=PHONE,
        buyer_name="Contacto de prueba",
        buyer_email=None,
        content="Texto de la plantilla aprobada",
        delivery_id="evt-g3",
    ))


# ------------------------------------------------------------ la lectura nueva


def test_the_captured_listing_reads_both_conversations_of_the_inbox() -> None:
    listing = copy.deepcopy(CONVERSATIONS)
    listing.pop("_fixture")
    inbox = _MaribelInbox(listing)

    conversations = asyncio.run(inbox.client().list_contact_conversations(
        contact_id=CONTACT, inbox_id=INBOX,
    ))

    # De la mas activa a la menos; sin pedir historial no se lee ningun mensaje.
    assert [c.conversation_id for c in conversations] == [9, 6]
    assert [c.status for c in conversations] == ["open", "open"]
    assert all(not c.with_team for c in conversations)
    assert all(c.last_inbound_at is None for c in conversations)
    assert ("GET", f"{PREFIX}/conversations/6/messages") not in inbox.requests


def test_the_last_inbound_is_the_image_without_text() -> None:
    inbox = _MaribelInbox()

    [conversation] = asyncio.run(inbox.client().list_contact_conversations(
        contact_id=CONTACT,
        inbox_id=INBOX,
        messages_since_epoch=SENT_AT - MARIBEL_WINDOW,
    ))

    assert conversation.conversation_id == 6
    assert conversation.last_inbound_at == LAST_INBOUND_6


def test_a_conversation_without_activity_in_the_window_is_not_read() -> None:
    inbox = _MaribelInbox()

    [conversation] = asyncio.run(inbox.client().list_contact_conversations(
        contact_id=CONTACT,
        inbox_id=INBOX,
        messages_since_epoch=LAST_INBOUND_6 + 1,
    ))

    assert conversation.last_inbound_at is None
    assert ("GET", f"{PREFIX}/conversations/6/messages") not in inbox.requests


def test_other_inboxes_are_left_out() -> None:
    listing = _listing_at_sent_time()
    other = copy.deepcopy(listing["payload"][0])
    other.update(id=77, inbox_id=11)
    listing["payload"].append(other)

    conversations = asyncio.run(_MaribelInbox(listing).client().list_contact_conversations(
        contact_id=CONTACT, inbox_id=INBOX,
    ))

    assert [c.conversation_id for c in conversations] == [6]


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda c: c["meta"]["sender"].update(id=1), "contact_conversation_scope_mismatch"),
        (lambda c: c.update(status="archived"), "invalid_contact_conversation"),
        (lambda c: c.update(last_activity_at=None), "invalid_contact_conversation"),
        (lambda c: c.update(labels=None), "invalid_contact_conversation"),
        (lambda c: c.update(id=True), "invalid_contact_conversation"),
    ],
)
def test_a_malformed_conversation_fails_closed(change, error: str) -> None:
    listing = _listing_at_sent_time()
    change(listing["payload"][0])

    with pytest.raises(ChatwootProtocolError, match=error):
        asyncio.run(_MaribelInbox(listing).client().list_contact_conversations(
            contact_id=CONTACT, inbox_id=INBOX,
        ))


def test_a_listing_that_may_be_cut_fails_closed() -> None:
    listing = _listing_at_sent_time()
    template = listing["payload"][0]
    listing["payload"] = [
        {**copy.deepcopy(template), "id": 1000 + index} for index in range(20)
    ]

    with pytest.raises(ChatwootProtocolError, match="contact_conversations_truncated"):
        asyncio.run(_MaribelInbox(listing).client().list_contact_conversations(
            contact_id=CONTACT, inbox_id=INBOX,
        ))


def test_assignment_team_and_pause_mean_the_team_has_it() -> None:
    listing = _listing_at_sent_time()
    base = listing["payload"][0]
    assigned = copy.deepcopy(base)
    assigned["id"] = 10
    assigned["meta"]["assignee"] = {"id": 5, "type": "user"}
    team = copy.deepcopy(base)
    team["id"] = 11
    team["meta"]["team"] = {"id": 2}
    paused = copy.deepcopy(base)
    paused["id"] = 12
    paused["labels"] = ["automation_paused"]
    listing["payload"] = [base, assigned, team, paused]

    conversations = asyncio.run(_MaribelInbox(listing).client().list_contact_conversations(
        contact_id=CONTACT, inbox_id=INBOX,
    ))

    assert {c.conversation_id: c.with_team for c in conversations} == {
        6: False, 10: True, 11: True, 12: True,
    }


# ---------------------------------------------------------------- G3a, el freno


def test_the_guard_stops_the_first_touch_of_conversation_9() -> None:
    inbox = _MaribelInbox()

    result = _first_touch(inbox.sender(guard=MARIBEL_WINDOW))

    assert result == FirstTouchResult(
        "blocked", None, None, FIRST_TOUCH_CONTACT_ACTIVE_CONVERSATION
    )
    assert inbox.created_conversations() == []
    assert inbox.sent_to() == []


def test_the_guard_also_stops_when_reuse_is_on() -> None:
    # El freno va primero: no se manda ni en la 6.
    inbox = _MaribelInbox()

    result = _first_touch(inbox.sender(guard=MARIBEL_WINDOW, reuse=True))

    assert result.reason == FIRST_TOUCH_CONTACT_ACTIVE_CONVERSATION
    assert inbox.posts == []


def test_an_inbound_older_than_the_window_does_not_stop_it() -> None:
    inbox = _MaribelInbox()

    result = _first_touch(
        inbox.sender(guard=MARIBEL_WINDOW, now=LAST_INBOUND_6 + MARIBEL_WINDOW + 1)
    )

    assert result.status == "sent"
    assert result.conversation_id == NEW_CONVERSATION
    assert len(inbox.created_conversations()) == 1


def test_a_shorter_window_lets_it_out_as_today() -> None:
    # 42 min despues de la imagen, una ventana de 30 min ya no frena.
    inbox = _MaribelInbox()

    result = _first_touch(inbox.sender(guard=1800))

    assert result.status == "sent"
    assert inbox.sent_to() == [NEW_CONVERSATION]


def test_a_conversation_where_only_the_agent_wrote_does_not_stop_it() -> None:
    inbox = _MaribelInbox()
    original = inbox.handler

    def only_outgoing(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path.endswith("/conversations/6/messages"):
            body = response.json()
            body["payload"] = [m for m in body["payload"] if m["message_type"] != 0]
            return httpx.Response(200, json=body)
        return response

    inbox.handler = only_outgoing  # type: ignore[method-assign]

    result = _first_touch(inbox.sender(guard=MARIBEL_WINDOW))

    assert result.status == "sent"


# ------------------------------------------------------- G3b, la reutilizacion


def test_reuse_sends_in_conversation_6_and_opens_nothing() -> None:
    inbox = _MaribelInbox()

    result = _first_touch(inbox.sender(reuse=True))

    assert result.status == "sent"
    assert result.conversation_id == 6
    assert inbox.created_conversations() == []
    assert inbox.sent_to() == [6]
    [(_, body)] = inbox.posts
    # El marcador del primer toque se conserva.
    assert set(body["content_attributes"]) == {"recovery_first_touch_hash"}
    # Sin ventana no se lee el historial.
    assert ("GET", f"{PREFIX}/conversations/6/messages") not in inbox.requests


def test_reuse_picks_the_most_active_open_conversation() -> None:
    listing = copy.deepcopy(CONVERSATIONS)
    listing.pop("_fixture")
    inbox = _MaribelInbox(listing)

    result = _first_touch(inbox.sender(reuse=True))

    assert result.conversation_id == 9


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["meta"].update(assignee={"id": 5, "type": "user"}),
        lambda c: c["meta"].update(team={"id": 2}),
        lambda c: c.update(labels=["automation_paused"]),
    ],
)
def test_reuse_stops_when_the_team_has_the_open_conversation(change) -> None:
    listing = _listing_at_sent_time()
    change(listing["payload"][0])
    inbox = _MaribelInbox(listing)

    result = _first_touch(inbox.sender(reuse=True))

    assert result == FirstTouchResult(
        "blocked", None, None, FIRST_TOUCH_CONTACT_CONVERSATION_WITH_TEAM
    )
    assert inbox.posts == []


def test_reuse_without_an_open_conversation_opens_one_as_today() -> None:
    listing = _listing_at_sent_time()
    listing["payload"][0]["status"] = "resolved"
    inbox = _MaribelInbox(listing)

    result = _first_touch(inbox.sender(reuse=True))

    assert result.status == "sent"
    assert result.conversation_id == NEW_CONVERSATION
    assert inbox.created_conversations() == [
        {"inbox_id": INBOX, "contact_id": CONTACT, "source_id": PHONE}
    ]


def test_a_chatwoot_error_reading_the_conversations_sends_nothing() -> None:
    inbox = _MaribelInbox()
    inbox.listing_status = 500

    result = _first_touch(inbox.sender(guard=MARIBEL_WINDOW, reuse=True))

    assert result == FirstTouchResult("failed", None, None, "chatwoot_http_error")
    assert inbox.posts == []


# -------------------------------------------------- apagado, exactamente lo de hoy


def test_with_both_off_it_is_the_first_touch_of_today() -> None:
    # La regresion de ATT1 (y de Maribel hasta prenderlo): no lee las
    # conversaciones del contacto y abre otra, como el 28/10 a las 03:20Z.
    inbox = _MaribelInbox()

    result = _first_touch(inbox.sender())

    assert result.status == "sent"
    assert result.conversation_id == NEW_CONVERSATION
    assert not inbox.read_listing()
    assert inbox.requests == [
        ("GET", f"{PREFIX}/contacts/search"),
        ("POST", f"{PREFIX}/conversations"),
        ("POST", f"{PREFIX}/conversations/{NEW_CONVERSATION}/messages"),
    ]


def test_a_contact_created_now_has_nothing_to_read() -> None:
    created: list[str] = []

    class _NewContactInbox(_MaribelInbox):
        def handler(self, request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET" and path == f"{PREFIX}/contacts/search":
                self.requests.append((request.method, path))
                if not created:
                    return httpx.Response(200, json={"payload": []})
            if request.method == "POST" and path == f"{PREFIX}/contacts":
                self.requests.append((request.method, path))
                created.append(path)
                return httpx.Response(200, json={"payload": {"id": CONTACT}})
            return super().handler(request)

    inbox = _NewContactInbox()

    result = _first_touch(inbox.sender(guard=MARIBEL_WINDOW, reuse=True))

    assert result.status == "sent"
    assert result.conversation_id == NEW_CONVERSATION
    assert not inbox.read_listing()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"active_conversation_guard_seconds": -1},
        {"active_conversation_guard_seconds": True},
        {"active_conversation_guard_seconds": 7200.0},
        {"reuse_open_conversation": "true"},
    ],
)
def test_invalid_settings_are_refused(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ChatwootMessageSender(
            chatwoot=_MaribelInbox().client(),
            inbox_id=INBOX,
            allowed_jid=None,
            dynamic_recipient_enabled=True,
            **kwargs,  # type: ignore[arg-type]
        )


# ------------------------------------------------------------- el cableado


def test_the_bridge_wires_both_into_the_dispatcher_sender(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, app = _start(
        monkeypatch,
        tmp_path,
        *OUTBOUND_FLOWS,
        extra={
            "FIRST_TOUCH_ACTIVE_CONVERSATION_GUARD_SECONDS": "7200",
            "FIRST_TOUCH_REUSE_OPEN_CONVERSATION_ENABLED": "true",
        },
    )
    sender = app.state.durable_dispatcher._sender  # type: ignore[attr-defined]

    assert settings.first_touch_active_conversation_guard_seconds == 7200
    assert settings.first_touch_reuse_open_conversation_enabled is True
    assert sender._active_conversation_guard_seconds == 7200
    assert sender._reuse_open_conversation is True


def test_by_default_both_stay_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, app = _start(monkeypatch, tmp_path, *OUTBOUND_FLOWS)
    sender = app.state.durable_dispatcher._sender  # type: ignore[attr-defined]

    assert settings.first_touch_active_conversation_guard_seconds == 0
    assert settings.first_touch_reuse_open_conversation_enabled is False
    assert sender._active_conversation_guard_seconds == 0
    assert sender._reuse_open_conversation is False


@pytest.mark.parametrize("raw", ["-1", "86401", "dos horas"])
def test_an_invalid_window_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    with pytest.raises(ValueError):
        _start(
            monkeypatch,
            tmp_path,
            *OUTBOUND_FLOWS,
            extra={"FIRST_TOUCH_ACTIVE_CONVERSATION_GUARD_SECONDS": raw},
        )


# --------------------------------------------- el despachador deja el motivo


def test_the_dispatcher_closes_the_block_as_today_and_logs_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # El carrito (la politica de un toque de Maribel): el remitente es el mismo
    # para los tres flujos portables, y el cierre no depende del ancla.
    finalizations: list[dict[str, object]] = []
    action = ScheduledAction(
        action_id="action-g3", recovery_case_id="case-g3",
        followup_sequence_id="sequence-g3", action_type="first_contact_review",
        status="pending", due_at="2026-10-28T03:19:54+00:00",
        expires_at="2026-11-04T02:19:54+00:00", expected_case_version=1,
        policy_key="maribel-recuperacion-un-toque", policy_version=1,
        step_key="first_contact", anchor_type="cart_abandonment",
        anchor_subject_internal_id="event-g3",
        anchor_observed_at="2026-10-28T02:19:54+00:00",
        lease_owner="dispatcher-test", lease_generation=1,
        lease_expires_at="2026-10-28T03:25:00+00:00",
        idempotency_key="cart_recovery:first_contact:case-g3",
    )
    attempt = DeliveryAttempt(
        attempt_id="attempt-g3", action_id=action.action_id,
        idempotency_key=action.idempotency_key, attempt_number=1,
        channel="whatsapp", mode="freeform", phase="reserved",
        lease_generation=1, expected_case_version=1,
        expected_sequence_revision=1,
    )

    class SupabaseStub:
        async def claim_due_followup_actions(self, **_: object) -> list[ScheduledAction]:
            return [action]

        async def get_followup_chatwoot_context(self, **_: object) -> ChatwootAuthorityContext:
            return ChatwootAuthorityContext(
                action.action_id, action.action_type, None, None, None, None
            )

        async def reevaluate_followup_action(self, **_: object) -> ReevaluationDecision:
            return ReevaluationDecision(
                action.action_id, "execute", "eligible_for_execution", 1, 1
            )

        async def reserve_followup_delivery_attempt(self, **_: object) -> DeliveryAttempt:
            return attempt

        async def get_followup_execution_context(self, **_: object) -> FollowupExecutionContext:
            return FollowupExecutionContext(
                action.action_id, action.action_type, action.step_key,
                action.recovery_case_id, "contact-g3", "event-g3", "Contacto",
                None, PHONE, "Curso", None, None, "new",
            )

        async def mark_followup_request_started(self, **_: object) -> DeliveryAttempt:
            return DeliveryAttempt(**{**attempt.__dict__, "phase": "request_started"})

        async def finalize_followup_delivery_attempt(self, **kwargs: object) -> object:
            finalizations.append(kwargs)
            return SimpleNamespace(status="permanent_failed")

    class AgentStub:
        async def request_followup_message(self, **_: object) -> FollowupMessageProposal:
            return FollowupMessageProposal("primer_contacto", "Mensaje aprobado")

    class SenderStub:
        async def send_first_touch(self, **_: object) -> FirstTouchResult:
            return FirstTouchResult(
                "blocked", None, None, FIRST_TOUCH_CONTACT_ACTIVE_CONVERSATION
            )

    dispatcher = DurableDispatcher(
        supabase=SupabaseStub(),  # type: ignore[arg-type]
        worker_id="dispatcher-test",
        recovery_agent=AgentStub(),  # type: ignore[arg-type]
        sender=SenderStub(),  # type: ignore[arg-type]
        allowed_jid=f"{PHONE}@s.whatsapp.net",
        clock=lambda: "2026-10-28T03:20:00+00:00",
    )

    with caplog.at_level(logging.INFO, logger="bridge.worker"):
        asyncio.run(dispatcher.dispatch_due(now="2026-10-28T03:20:00+00:00"))

    [finalized] = finalizations
    assert finalized["outcome"] == "rejected"
    assert finalized["reason_code"] == "sender_rejected_before_delivery"
    [line] = [r.getMessage() for r in caplog.records if "followup_sender_blocked" in r.getMessage()]
    assert line == (
        "followup_sender_blocked reason=contact_active_conversation "
        "policy_key=maribel-recuperacion-un-toque action_id=action-g3"
    )
    assert PHONE not in caplog.text
