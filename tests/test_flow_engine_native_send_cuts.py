"""El primer toque de un flujo nativo respeta los cortes de G3 (E-204).

Una accion nativa (``anchor_type = flow_engine_event``, la bienvenida tras la
compra con ``entry_permission = "compra"``) sale por el mismo camino que el
legado: ``DurableDispatcher`` -> ``ChatwootMessageSender.send_first_touch``
(``src/bridge/worker.py``, el ``else`` de ``followup_conversation_id`` que
arma ``first_touch_kwargs`` con ``trigger_kind=action.anchor_type`` y llama a
``self._sender.send_first_touch``) -> ``ChatwootClient.send_first_message``.
Estos tests lo prueban a nivel del despachador real, con el sender real y un
``ChatwootClient`` real sobre ``httpx.MockTransport``:

* G3a: la persona escribio dentro de las 2 h -> ``contact_active_conversation``
  y ningun POST de mensaje;
* G3b: la conversacion abierta la tiene el equipo (asignada, con equipo o con
  ``automation_paused``: asi se ven la derivacion, la toma humana y la pausa
  por compra) -> ``contact_conversation_with_team``, sin POST;
* el caso sano: sin conversacion viva, la bienvenida sale (para que los
  anteriores no pasen por no mandar nunca).

Cada corte tiene su variante con la guarda apagada, que espera el envio: el
freno lo pone la guarda y no otra cosa del recorrido nativo.

Datos: los dobles de la base y del inbox 11 son los de
``test_flow_engine_native_dispatch.py``. El contacto y su conversacion son las
capturas de Maribel de ``test_first_touch_active_conversation.py`` (contacto
767, conversacion 6, la imagen sin texto de las 02:38:06Z) con el inbox
cambiado al 11 del doble; la ventana es la de Maribel (7200 s) y el reuso
prendido, como Maribel.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from test_durable_dispatcher_approved_template import (
    ACCOUNT_ID,
    ALLY,
    BOUNDARY,
    FINAL_NOW,
    INBOX_ID,
    NOW,
    PHONE,
    TEMPLATE,
)
from test_flow_engine_native_dispatch import (
    LABEL_MACRO_ID,
    WELCOME,
    _NativeAuthority,
    _NativeChatwoot,
    _step,
)

from bridge.messaging import (
    FIRST_TOUCH_CONTACT_ACTIVE_CONVERSATION,
    FIRST_TOUCH_CONTACT_CONVERSATION_WITH_TEAM,
    ChatwootMessageSender,
    FinalMetaEffectGate,
)
from bridge.product_label import ProductLabeler
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

CONTACT = 767
# 2026-10-28T03:20:00Z y la imagen sin texto en la 6, 42 min antes.
SENT_AT = 1793157600
LAST_INBOUND_6 = 1793155086
MARIBEL_WINDOW = 7200
PREFIX = f"/api/v1/accounts/{ACCOUNT_ID}"


def _listing(change: Callable[[dict[str, Any]], object] | None = None) -> dict[str, Any]:
    """La lista del contacto a las 03:20:00Z (solo la 6), en el inbox del doble."""
    listing = copy.deepcopy(CONVERSATIONS)
    listing.pop("_fixture")
    listing["payload"] = [c for c in listing["payload"] if c["id"] == 6]
    for conversation in listing["payload"]:
        conversation["inbox_id"] = INBOX_ID
        if change is not None:
            change(conversation)
    return listing


class _LiveContactChatwoot(_NativeChatwoot):
    """El inbox 11 del doble, con el contacto ya creado y su conversacion 6."""

    def __init__(self, listing: dict[str, Any]) -> None:
        super().__init__()
        self.listing = listing

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == f"{PREFIX}/contacts/search":
            self.requests.append(("GET", path, None))
            return httpx.Response(200, json={"payload": [{
                "id": CONTACT,
                "phone_number": f"+{PHONE}",
                "blocked": False,
                "contact_inboxes": [{"source_id": PHONE, "inbox": {"id": INBOX_ID}}],
            }]})
        if request.method == "GET" and path == f"{PREFIX}/contacts/{CONTACT}/conversations":
            self.requests.append(("GET", path, None))
            return httpx.Response(200, json=self.listing)
        if request.method == "GET" and path == f"{PREFIX}/conversations/6/messages":
            self.requests.append(("GET", path, None))
            payload = copy.deepcopy(MESSAGES_6)
            payload.pop("_fixture")
            return httpx.Response(200, json=payload)
        return super().handler(request)

    def message_posts(self) -> list[dict[str, Any]]:
        return [
            body
            for method, path, body in self.requests
            if method == "POST" and path.endswith("/messages") and body is not None
        ]


def _dispatcher(
    authority: _NativeAuthority,
    chatwoot: _LiveContactChatwoot,
    tmp_path: Path,
    *,
    guard: int,
    reuse: bool,
    now: int = SENT_AT,
) -> DurableDispatcher:
    client = chatwoot.client()
    return DurableDispatcher(
        supabase=authority,  # type: ignore[arg-type]
        worker_id="att1-dispatcher",
        chatwoot=client,
        chatwoot_account_id=ACCOUNT_ID,
        chatwoot_inbox_id=INBOX_ID,
        recovery_agent=None,
        sender=ChatwootMessageSender(
            chatwoot=client,
            inbox_id=INBOX_ID,
            allowed_jid=None,
            dynamic_recipient_enabled=True,
            template=TEMPLATE,
            active_conversation_guard_seconds=guard,
            reuse_open_conversation=reuse,
            clock=lambda: float(now),
        ),
        allowed_jid=None,
        commercial_ally_config=ALLY,
        portable_recipient_enabled=True,
        pilot_boundary=BOUNDARY,
        clock=lambda: FINAL_NOW,
        final_meta_effect_gate=FinalMetaEffectGate(enabled=True, evidence_dir=tmp_path / "meta-effects"),
        waba_template=TEMPLATE,
        approved_template_direct=True,
        flow_engine_enabled=True,
        native_client_labeler=ProductLabeler(client=client, label="cliente", macro_id=LABEL_MACRO_ID),
    )


def _welcome_authority() -> _NativeAuthority:
    return _NativeAuthority(step=_step(entry_permission="compra"))


def _run(dispatcher: DurableDispatcher) -> None:
    asyncio.run(dispatcher.dispatch_due(now=NOW))


def _assert_blocked(authority: _NativeAuthority, chatwoot: _LiveContactChatwoot) -> None:
    assert chatwoot.message_posts() == []
    assert chatwoot.posts(f"{PREFIX}/conversations") == []
    assert authority.acceptances == []
    [closed] = authority.finalizations
    assert closed["outcome"] == "rejected"
    assert closed["reason_code"] == "sender_rejected_before_delivery"
    # El recorrido nativo llego hasta el envio: no corto antes por otra cosa.
    assert authority.engine_calls[:2] == ["context", "request_started"]


def _assert_sent(authority: _NativeAuthority, chatwoot: _LiveContactChatwoot) -> list[dict[str, Any]]:
    templates = [post for post in chatwoot.message_posts() if "template_params" in post]
    [message] = templates
    assert message["template_params"]["name"] == WELCOME
    assert authority.acceptances
    return templates


# ------------------------------------------------------------- G3a, el freno


@pytest.mark.parametrize("guard_on", [True, False], ids=["guarda prendida", "guarda apagada"])
def test_the_native_welcome_stops_when_the_person_wrote_within_two_hours(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, guard_on: bool
) -> None:
    authority = _welcome_authority()
    chatwoot = _LiveContactChatwoot(_listing())

    with caplog.at_level("INFO", logger="bridge"):
        _run(_dispatcher(
            authority, chatwoot, tmp_path,
            guard=MARIBEL_WINDOW if guard_on else 0, reuse=False,
        ))

    if guard_on:
        _assert_blocked(authority, chatwoot)
        assert f"reason={FIRST_TOUCH_CONTACT_ACTIVE_CONVERSATION}" in caplog.text
        assert ("GET", f"{PREFIX}/conversations/6/messages", None) in chatwoot.requests
    else:
        # Sin la guarda, la misma persona recibe la bienvenida en otra conversacion.
        _assert_sent(authority, chatwoot)


# ------------------------------------------------ G3b, la tiene el equipo


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["meta"].update(assignee={"id": 5, "type": "user"}),
        lambda c: c["meta"].update(team={"id": 2}),
        lambda c: c.update(labels=["automation_paused"]),
    ],
    ids=["asignada (toma humana)", "con equipo (derivacion)", "pausada (pausa por compra)"],
)
@pytest.mark.parametrize("reuse_on", [True, False], ids=["reuso prendido", "reuso apagado"])
def test_the_native_welcome_stops_when_the_team_has_the_open_conversation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, change: Callable[[dict[str, Any]], object],
    reuse_on: bool,
) -> None:
    authority = _welcome_authority()
    chatwoot = _LiveContactChatwoot(_listing(change))

    # Fuera de la ventana de la imagen: lo que frena es el equipo, no G3a.
    with caplog.at_level("INFO", logger="bridge"):
        _run(_dispatcher(
            authority, chatwoot, tmp_path,
            guard=MARIBEL_WINDOW, reuse=reuse_on, now=LAST_INBOUND_6 + MARIBEL_WINDOW + 1,
        ))

    if reuse_on:
        _assert_blocked(authority, chatwoot)
        assert f"reason={FIRST_TOUCH_CONTACT_CONVERSATION_WITH_TEAM}" in caplog.text
    else:
        _assert_sent(authority, chatwoot)


# ------------------------------------------------------------ el caso sano


def test_without_a_live_conversation_the_native_welcome_goes_out(tmp_path: Path) -> None:
    # La 6 resuelta y el ultimo entrante fuera de la ventana: abre la 200 y sale.
    authority = _welcome_authority()
    chatwoot = _LiveContactChatwoot(_listing(lambda c: c.update(status="resolved")))

    _run(_dispatcher(
        authority, chatwoot, tmp_path,
        guard=MARIBEL_WINDOW, reuse=True, now=LAST_INBOUND_6 + MARIBEL_WINDOW + 1,
    ))

    [message] = _assert_sent(authority, chatwoot)
    assert ("GET", f"{PREFIX}/contacts/{CONTACT}/conversations", None) in chatwoot.requests
    assert len(chatwoot.posts(f"{PREFIX}/conversations")) == 1
    assert "recovery_first_touch_hash" in message["content_attributes"]


def test_with_reuse_on_the_native_welcome_goes_to_the_open_conversation(tmp_path: Path) -> None:
    # Sin equipo y fuera de la ventana: no abre otra, va a la 6.
    authority = _welcome_authority()
    chatwoot = _LiveContactChatwoot(_listing())
    original = chatwoot.handler

    def conversation_6(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"{PREFIX}/conversations/6/messages":
            body = json.loads(request.content)
            chatwoot.requests.append(("POST", request.url.path, body))
            return httpx.Response(200, json={
                "id": 4700, "conversation_id": 6, "message_type": 1, "private": False,
                "content": body["content"], "content_attributes": body["content_attributes"],
            })
        return original(request)

    chatwoot.handler = conversation_6  # type: ignore[method-assign]

    _run(_dispatcher(
        authority, chatwoot, tmp_path,
        guard=MARIBEL_WINDOW, reuse=True, now=LAST_INBOUND_6 + MARIBEL_WINDOW + 1,
    ))

    assert chatwoot.posts(f"{PREFIX}/conversations") == []
    [message] = [
        body for method, path, body in chatwoot.requests
        if method == "POST" and path == f"{PREFIX}/conversations/6/messages"
    ]
    assert message["template_params"]["name"] == WELCOME
    assert authority.acceptances
