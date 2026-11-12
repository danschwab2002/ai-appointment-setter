"""E-292: el link del agente sale siempre en un mensaje propio, el ultimo.

Condicion de Dan del 2026-11-02 (decision P-07, punto 4): el link va como
«mensaje independiente»; lo demas que el agente diga en el mismo turno va en
globos normales. Con ``PAYMENT_LINK_TEMPLATE_NAME``:

- el texto del agente pasa por el divisor de respuestas, como cualquier turno,
  y sus globos salen antes, en orden;
- el mensaje del link es la plantilla o, si no se puede usar, la URL sola;
- el lote mide lo mismo en los dos casos, asi que un reintento que cambia de
  plantilla a URL no repite ningun globo.

El catalogo es el del inbox 9 capturado el 2026-10-01
(``chatwoot_message_templates_inbox_9_20261001.json``), con la plantilla del
link armada como en ``test_checkout_delivery.py``. Los mensajes de Chatwoot
siguen la forma de los de ``test_chatwoot.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from bridge.chatwoot import ChatwootClient
from bridge.checkout_delivery import CheckoutDeliveryError, deliver_checkout_issuance_v2
from bridge.supabase import SupabaseError
from test_checkout_delivery import LINK_BODY, LINK_TEMPLATE, URL, FakeSupabase, _catalog


TEXT = (
    "Claro, el acceso es de por vida. Te cuento que el pago se puede hacer con "
    "tarjeta o en efectivo. Te dejo el link para completar tu compra:"
)
# E-486 (D12): el texto va en dos globos como mucho, y el link en el tercero.
PARTS = (
    "Claro, el acceso es de por vida.",
    "Te cuento que el pago se puede hacer con tarjeta o en efectivo. "
    "Te dejo el link para completar tu compra:",
)
# Un divisor que todavia devuelve tres globos: ya no se acepta.
THREE_PARTS = (
    "Claro, el acceso es de por vida.",
    "Te cuento que el pago se puede hacer con tarjeta o en efectivo.",
    "Te dejo el link para completar tu compra:",
)
JID = "12025550123@s.whatsapp.net"


class PartsControl:
    """Chatwoot de prueba: cada parte sale con su id y registra lo que llega."""

    def __init__(self, catalog=None, *, inbox_error=None, blocked_part=None):
        self.catalog = catalog if catalog is not None else _catalog()
        self.inbox_error = inbox_error
        self.blocked_part = blocked_part
        self.sends: list[dict[str, object]] = []
        self.authorized_parts: list[int] = []

    async def get_inbox(self, *, inbox_id):
        if self.inbox_error is not None:
            raise self.inbox_error
        return self.catalog

    async def send_agent_bot_reply(self, **kwargs):
        self.sends.append(kwargs)
        index = kwargs.get("part_index", 1)
        if index == self.blocked_part:
            return {"status": "blocked", "reason": "conversation_advanced"}
        authorizer = kwargs.get("pre_send_authorizer")
        if authorizer is not None:
            assert await authorizer() is True
            self.authorized_parts.append(index)
        return {"status": "sent", "message_id": 7100 + index}


def _splitter(parts, calls=None):
    async def split(text):
        if calls is not None:
            calls.append(text)
        return parts

    return split


def _deliver(db, control, *, sleeps, splitter, template=LINK_TEMPLATE, preamble=TEXT):
    async def fake_sleep(seconds):
        sleeps.append(seconds)

    return asyncio.run(deliver_checkout_issuance_v2(
        supabase=db,
        control_client=control,
        commercial_case_id="00000000-0000-0000-0000-000000000300",
        external_user_id="12025550123",
        chatwoot_account_id=1,
        chatwoot_inbox_id=9,
        chatwoot_conversation_id=9101,
        trigger_message_id=501,
        delivery_id="delivery-1",
        preamble=preamble,
        expected_jid=JID,
        link_template_name=template,
        link_template_language="es_EC" if template is not None else None,
        part_delay_seconds=2.5,
        part_sleep=fake_sleep,
        preamble_splitter=splitter,
    ))


def test_two_text_bubbles_then_the_template_alone() -> None:
    db = FakeSupabase()
    control = PartsControl()
    sleeps: list[float] = []
    calls: list[str] = []

    result = _deliver(db, control, sleeps=sleeps, splitter=_splitter(PARTS, calls))

    assert calls == [TEXT]
    assert [s["content"] for s in control.sends] == [*PARTS, f"{LINK_BODY}\n{URL}"]
    assert [(s["part_index"], s["part_count"]) for s in control.sends] == [
        (1, 3), (2, 3), (3, 3),
    ]
    assert [s["prior_parts"] for s in control.sends] == [
        (), PARTS[:1], PARTS,
    ]
    # Solo el mensaje del link autoriza la emision, y es el que la cierra.
    assert control.authorized_parts == [3]
    assert all("pre_send_authorizer" not in s for s in control.sends[:2])
    assert all("template_params" not in s for s in control.sends[:2])
    assert control.sends[2]["template_params"]["name"] == LINK_TEMPLATE
    # Ningun globo del texto lleva la URL.
    assert all("pay.hotmart.com" not in s["content"] for s in control.sends[:2])
    assert sleeps == [2.5, 2.5]
    assert result.outcome == "sent"
    assert result.chatwoot_message_id == 7103
    assert db.calls[-1][1]["chatwoot_message_id"] == 7103


def test_the_fallback_url_goes_alone_after_the_split_text() -> None:
    db = FakeSupabase()
    control = PartsControl(_catalog(with_link_template=False))
    sleeps: list[float] = []

    result = _deliver(db, control, sleeps=sleeps, splitter=_splitter(PARTS))

    assert [s["content"] for s in control.sends] == [*PARTS, URL]
    assert control.sends[-1]["part_index"] == control.sends[-1]["part_count"] == 3
    assert "template_params" not in control.sends[-1]
    assert control.authorized_parts == [3]
    assert result.chatwoot_message_id == 7103


@pytest.mark.parametrize(
    "parts",
    [
        # El divisor reescribio una palabra: no se acepta.
        ("Claro, el acceso es para siempre.", *PARTS[1:]),
        # Un manifiesto viejo de cuatro globos no deja lugar para el link.
        ("Claro,", "el acceso es de por vida.", *THREE_PARTS[1:]),
        # E-486 (D12): tres globos de texto tampoco.
        THREE_PARTS,
        (),
    ],
)
def test_parts_that_do_not_rebuild_the_text_go_as_one_bubble(parts) -> None:
    db = FakeSupabase()
    control = PartsControl()

    _deliver(db, control, sleeps=[], splitter=_splitter(parts))

    assert [s["content"] for s in control.sends] == [TEXT, f"{LINK_BODY}\n{URL}"]
    assert [(s["part_index"], s["part_count"]) for s in control.sends] == [(1, 2), (2, 2)]


def test_without_the_template_name_nothing_changes() -> None:
    # Hoy ninguna aliada tiene la variable: el texto y el link en un solo
    # mensaje, sin leer el catalogo y sin llamar al divisor.
    db = FakeSupabase()
    control = PartsControl(inbox_error=AssertionError("no se lee el catalogo"))
    calls: list[str] = []

    result = _deliver(
        db, control, sleeps=[], splitter=_splitter(PARTS, calls), template=None
    )

    assert calls == []
    (only,) = control.sends
    assert only["content"] == f"{TEXT}\n{URL}"
    assert "part_count" not in only
    assert result.outcome == "sent"


def test_a_blocked_text_bubble_stops_before_the_link() -> None:
    db = FakeSupabase()
    control = PartsControl(blocked_part=2)
    sleeps: list[float] = []

    result = _deliver(db, control, sleeps=sleeps, splitter=_splitter(PARTS))

    assert result.outcome == "blocked"
    assert result.reason == "conversation_advanced"
    assert [s["part_index"] for s in control.sends] == [1, 2]
    assert control.authorized_parts == []
    assert [name for name, _ in db.calls] == ["reserve"]
    assert sleeps == [2.5]


def test_a_url_in_the_agent_text_is_still_rejected_before_any_bubble() -> None:
    db = FakeSupabase()
    control = PartsControl()

    result = _deliver(
        db,
        control,
        sleeps=[],
        splitter=_splitter(("Mira", "https://pay.hotmart.com/otro")),
        preamble="Mira https://pay.hotmart.com/otro",
    )

    assert result.outcome == "blocked"
    assert result.reason == "payment_link_reply_rejected"
    assert control.sends == []


# --- Un reintento que cambia de plantilla a URL, contra el cliente real ------


class ConversationTransport(httpx.AsyncBaseTransport):
    """Chatwoot con estado: guarda lo que se postea y lo devuelve al releer."""

    def __init__(self) -> None:
        self.catalog_available = True
        self.posts: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = [{
            "id": 501,
            "conversation_id": 2,
            "message_type": 0,
            "private": False,
            "content": "Pasame el link",
            "sender": {"type": "contact", "id": 20},
        }]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/conversations/2"):
            return httpx.Response(200, json={
                "id": 2,
                "inbox_id": 9,
                "status": "open",
                "can_reply": True,
                "meta": {"sender": {"identifier": JID, "blocked": False}, "assignee": None},
            })
        if request.method == "GET" and path.endswith("/inboxes/9"):
            if not self.catalog_available:
                return httpx.Response(503, json={"error": "unavailable"})
            return httpx.Response(200, json=_catalog())
        if request.method == "GET" and path.endswith("/labels"):
            return httpx.Response(200, json={"payload": []})
        if request.method == "GET" and path.endswith("/messages"):
            return httpx.Response(200, json={"payload": list(self.messages)})
        assert request.method == "POST" and path.endswith("/messages")
        body = json.loads(request.content)
        self.posts.append(body)
        message = {
            "id": 600 + len(self.posts),
            "conversation_id": 2,
            "message_type": 1,
            "private": False,
            "content": body["content"],
            "content_attributes": body["content_attributes"],
            "sender": {"type": "agent_bot", "id": 1},
        }
        self.messages.append(message)
        return httpx.Response(200, json=message)


class AuthorizeFailsOnce(FakeSupabase):
    """La primera autorizacion se cae: el link no sale y la fila sigue reservada."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def authorize_chatwoot_checkout_issuance_v2(self, **kwargs):
        if not self.failed:
            self.failed = True
            raise SupabaseError("supabase_unavailable")
        return await super().authorize_chatwoot_checkout_issuance_v2(**kwargs)


def test_a_retry_that_loses_the_template_does_not_repeat_the_text(tmp_path: Path) -> None:
    transport = ConversationTransport()
    client = ChatwootClient(
        base_url="https://chatwoot.example.test",
        account_id=1,
        access_token="control-token",
        allowed_jid=JID,
        agent_bot_access_token="agent-bot-token",
        agent_bot_id=1,
        reply_dir=tmp_path,
        transport=transport,
    )
    db = AuthorizeFailsOnce()

    def attempt():
        return asyncio.run(deliver_checkout_issuance_v2(
            supabase=db,
            control_client=client,
            commercial_case_id="00000000-0000-0000-0000-000000000300",
            external_user_id="12025550123",
            chatwoot_account_id=1,
            chatwoot_inbox_id=9,
            chatwoot_conversation_id=2,
            trigger_message_id=501,
            delivery_id="delivery-1",
            preamble=TEXT,
            expected_jid=JID,
            link_template_name=LINK_TEMPLATE,
            link_template_language="es_EC",
            preamble_splitter=_splitter(PARTS),
        ))

    # Primer intento: con la plantilla, los tres globos salen y la emision no
    # se puede autorizar. El link no salio.
    with pytest.raises(CheckoutDeliveryError) as raised:
        attempt()
    assert raised.value.code == "checkout_issuance_authorize_failed"
    assert [post["content"] for post in transport.posts] == list(PARTS)

    # Reintento: el catalogo ya no se puede leer, el link sale como URL. Los
    # globos del texto ya estan en Chatwoot con la misma huella: no se repiten.
    transport.catalog_available = False
    result = attempt()

    assert result.outcome == "sent"
    assert [post["content"] for post in transport.posts] == [*PARTS, URL]
    batch = hashlib.sha256(b"2:501").hexdigest()
    assert [post["content_attributes"] for post in transport.posts] == [
        {
            "appointment_setter_reply_hash": hashlib.sha256(
                f"{batch}:{index}:3".encode()
            ).hexdigest(),
            "appointment_setter_reply_batch_hash": batch,
            "appointment_setter_reply_part_index": index,
            "appointment_setter_reply_part_count": 3,
        }
        for index in (1, 2, 3)
    ]
    assert "template_params" not in transport.posts[-1]
    assert db.calls[-1][0] == "finalize"
    assert db.calls[-1][1]["chatwoot_message_id"] == 603
    # La fila de la emision es la misma en los dos intentos.
    reserves = [kwargs for name, kwargs in db.calls if name == "reserve"]
    assert len(reserves) == 2
