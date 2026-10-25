"""F-034, D8: al derivar, una nota privada con el link que recibio la persona.

Cuando el agente deriva, quien del equipo toma la conversacion no ve que link
le mandamos: las plantillas del carrito dejan en Chatwoot solo el texto, sin el
link ni el cupon (``chatwoot_followup_candidate_inbox_11_20261010.json``). La
nota sale de la base (``read_handoff_note_checkout_link_v1``, migracion
``20261025000300``), es privada, va una vez por derivacion y nunca frena la
derivacion. Sin link, no hay nota (D-091-1, default para que lo revise Dan).

Datos:

* el entrante de la captura 200 del inbox 9, por el mismo camino que el aviso
  (``test_handoff_notice.py``);
* el link es el que mando el agente en la conversacion de ATT1 capturada el
  10/10 (``chatwoot_followup_candidate_inbox_11_20261010.json``, ya
  anonimizado);
* el historial real con notas privadas de derivacion es el de la conversacion
  173 del inbox 9 (``chatwoot_conversation_messages_inbox_9_conv_173_20260926.json``):
  la nota nueva convive con la del worker de la proyeccion.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from bridge.app import Settings, create_app
from bridge.chatwoot import ChatwootClient, ChatwootHandoffDeliveryUnknownError
from bridge.handoff_link_note import (
    HANDOFF_LINK_NOTE_MARKER_PREFIX,
    handoff_link_note_body,
    handoff_link_note_marker,
)
from bridge.supabase import HandoffNoteCheckoutLink, SupabaseError
from test_att1_production_settings import _start
from test_webhook import StubChatwootClient, StubShadowProcessor, _post, _signed_headers
from test_whatsapp_inbound_equivalence import (
    FORM_ID,
    SECRET,
    _manifest_settings,
    _Supabase,
    _text_webhook,
)

FIXTURES = Path(__file__).parent / "fixtures"
LINK_FIXTURE = FIXTURES / "chatwoot_followup_candidate_inbox_11_20261010.json"
HISTORY_173 = FIXTURES / "chatwoot_conversation_messages_inbox_9_conv_173_20260926.json"
HANDOFF_REQUEST_ID = "6f1c2a4e-9b7d-4c1e-8f3a-2d5b7e9a0c11"
COUPON = "ATT1A1K7M2Q9"
HANDOFF = {
    "decision": "handoff",
    "qualification_status": "needs_human",
    "reason_code": "refund_request",
    "reply": "Te paso con una persona del equipo.",
}


def _captured_link() -> str:
    text = LINK_FIXTURE.read_text(encoding="utf-8")
    [url] = sorted(
        set(
            re.findall(
                r"https://pay\.hotmart\.com/D98014973Y\?off=[A-Za-z0-9]+"
                r"&checkoutMode=10&src=hermes&sck=[A-Za-z0-9_~%-]+~hermes~v1~[0-9A-Z]{26}",
                text,
            )
        )
    )
    return url


def _link(*, coupon: str | None = None, kind: str = "inbound_request") -> HandoffNoteCheckoutLink:
    return HandoffNoteCheckoutLink(
        checkout_url_final=_captured_link(),
        source_kind=kind,
        accepted_at=datetime(2026, 10, 20, 15, 1, tzinfo=UTC),
        coupon_code=coupon,
    )


class _NoteChatwoot(StubChatwootClient):
    def __init__(self, *, note_error: Exception | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.notes: list[dict[str, object]] = []
        self.note_error = note_error

    async def ensure_marked_private_note(self, **kwargs: object) -> bool:
        self.events.append("note")
        self.notes.append(kwargs)
        if self.note_error is not None:
            raise self.note_error
        return True


class _NoteSupabase(_Supabase):
    def __init__(
        self,
        *,
        link: HandoffNoteCheckoutLink | None,
        read_error: Exception | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.link = link
        self.read_error = read_error
        self.link_reads: list[dict[str, object]] = []

    async def request_inbound_human_handoff(self, **kwargs: object) -> object:
        self.handoff_calls.append(kwargs)
        return type(
            "HandoffResult",
            (),
            {"outcome": "requested", "handoff_request_id": HANDOFF_REQUEST_ID},
        )()

    async def read_handoff_note_checkout_link(
        self, **kwargs: object
    ) -> HandoffNoteCheckoutLink | None:
        self.link_reads.append(kwargs)
        if self.read_error is not None:
            raise self.read_error
        return self.link


def _settings(tmp_path: Path, *, enabled: bool = True) -> Settings:
    return replace(_manifest_settings(tmp_path), handoff_link_note_enabled=enabled)


def _derive(
    tmp_path: Path,
    *,
    link: HandoffNoteCheckoutLink | None = None,
    settings: Settings | None = None,
    read_error: Exception | None = None,
    note_error: Exception | None = None,
    deliveries: int = 1,
) -> tuple[_NoteChatwoot, _NoteSupabase]:
    supabase = _NoteSupabase(link=link, read_error=read_error, identities=(FORM_ID,))
    webhook, history = _text_webhook("Quiero que me devuelvan el dinero")
    chatwoot = _NoteChatwoot(messages=list(history), note_error=note_error)
    app = create_app(
        settings or _settings(tmp_path),
        chatwoot_client=chatwoot,
        shadow_processor=StubShadowProcessor(HANDOFF),
        supabase_client=supabase,  # type: ignore[arg-type]
    )
    raw_body = json.dumps(webhook, separators=(",", ":")).encode("utf-8")
    for attempt in range(deliveries):
        response = _post(
            app,
            raw_body,
            _signed_headers(raw_body, secret=SECRET, delivery=f"d8-200-{attempt}"),
        )
        assert response.status_code == 202
        asyncio.run(app.state.chatwoot_worker.run_once())
    return chatwoot, supabase


# --------------------------------------------------------------- el texto


def test_the_note_carries_the_link_and_asks_for_no_other() -> None:
    body = handoff_link_note_body(_link())
    assert _captured_link() in body
    assert "2026-10-20 15:01 UTC" in body
    assert "no otro" in body and "cupón" in body
    assert "offDiscount" not in body
    assert len(body) <= 1800


def test_a_cart_link_goes_with_its_coupon_like_the_button() -> None:
    body = handoff_link_note_body(_link(coupon=COUPON, kind="recovery_first_touch"))
    assert f"{_captured_link()}&offDiscount={COUPON}" in body
    assert f"Ya trae su cupón personal {COUPON}." in body


def test_the_marker_is_one_per_handoff() -> None:
    assert handoff_link_note_marker(HANDOFF_REQUEST_ID) == (
        f"{HANDOFF_LINK_NOTE_MARKER_PREFIX}{HANDOFF_REQUEST_ID}]"
    )
    with pytest.raises(ValueError):
        handoff_link_note_marker("handoff-1")


# ------------------------------------------------- con el flag: la nota sale


def test_a_handoff_writes_one_private_note_after_the_pause(tmp_path: Path) -> None:
    chatwoot, supabase = _derive(tmp_path, link=_link())

    assert len(supabase.handoff_calls) == 1
    [read] = supabase.link_reads
    assert read["chatwoot_account_id"] == 1
    assert read["chatwoot_inbox_id"] == 9
    [note] = chatwoot.notes
    assert note["idempotency_marker"] == handoff_link_note_marker(HANDOFF_REQUEST_ID)
    assert note["expected_inbox_id"] == 9
    assert _captured_link() in str(note["note_body"])
    # Despues de la derivacion durable y de la pausa; nada a la persona.
    assert chatwoot.events == ["label:automation_paused", "note"]
    assert chatwoot.reply_calls == []


def test_a_retried_webhook_reuses_the_same_marker(tmp_path: Path) -> None:
    # El webhook se reintenta: la nota se pide con el mismo marcador, y
    # ensure_marked_private_note no escribe una segunda (test de abajo).
    chatwoot, supabase = _derive(tmp_path, link=_link(), deliveries=2)

    markers = {note["idempotency_marker"] for note in chatwoot.notes}
    assert markers == {handoff_link_note_marker(HANDOFF_REQUEST_ID)}


def test_without_a_link_there_is_no_note(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # D-091-1 (default): la persona nunca recibio un link nuestro.
    caplog.set_level("INFO")
    chatwoot, supabase = _derive(tmp_path, link=None)

    assert chatwoot.notes == []
    assert len(supabase.handoff_calls) == 1
    assert chatwoot.events == ["label:automation_paused"]
    assert "handoff_link_note_skipped reason=no_link" in caplog.text


@pytest.mark.parametrize(
    ("read_error", "note_error"),
    [
        (SupabaseError("handoff_note_checkout_link_read_failed: HTTP 503"), None),
        (None, ChatwootHandoffDeliveryUnknownError("flow_private_note_delivery_unknown")),
        (None, httpx.ConnectError("chatwoot down")),
    ],
)
def test_a_failed_note_never_stops_the_handoff(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    read_error: Exception | None,
    note_error: Exception | None,
) -> None:
    caplog.set_level("INFO")
    chatwoot, supabase = _derive(
        tmp_path, link=_link(), read_error=read_error, note_error=note_error
    )

    assert len(supabase.handoff_calls) == 1
    assert chatwoot.events[0] == "label:automation_paused"
    assert "handoff_link_note_not_sent" in caplog.text
    assert chatwoot.reply_calls == []


# ------------------------------------------------- sin el flag: como hasta hoy


def test_without_the_flag_there_is_no_read_and_no_note(tmp_path: Path) -> None:
    chatwoot, supabase = _derive(
        tmp_path, link=_link(), settings=_settings(tmp_path, enabled=False)
    )

    assert supabase.link_reads == []
    assert chatwoot.notes == []
    assert len(supabase.handoff_calls) == 1
    assert chatwoot.events == ["label:automation_paused"]


def test_the_flag_is_off_by_default() -> None:
    assert Settings.__dataclass_fields__["handoff_link_note_enabled"].default is False


def test_att1_production_starts_with_the_flag_from_its_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings, _ = _start(
        monkeypatch, tmp_path, extra={"HANDOFF_LINK_NOTE_ENABLED": "true"}
    )
    assert settings.handoff_link_note_enabled is True

    settings, _ = _start(monkeypatch, tmp_path / "sin")
    assert settings.handoff_link_note_enabled is False


def test_the_flag_without_the_handoff_admission_does_not_start(tmp_path: Path) -> None:
    settings = Settings(
        webhook_secret=SECRET,
        allowed_jid="5210000000200@s.whatsapp.net",
        capture_dir=tmp_path / "captures",
        max_age_seconds=300,
        handoff_link_note_enabled=True,
    )
    with pytest.raises(ValueError, match="HANDOFF_LINK_NOTE_ENABLED requires"):
        create_app(settings, chatwoot_client=StubChatwootClient())


# ------------------------------------- el cliente de Chatwoot: privada y una vez


def _chatwoot_with_history(posted: list[dict[str, Any]]) -> ChatwootClient:
    """La conversacion 173 real: ya tiene las notas de derivacion del worker."""
    history = json.loads(HISTORY_173.read_text(encoding="utf-8"))
    messages = list(history["payload"])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/conversations/173"):
            return httpx.Response(200, json={"id": 173, "inbox_id": 9})
        if request.method == "GET" and path.endswith("/conversations/173/messages"):
            if "before" in request.url.params:
                return httpx.Response(200, json={"payload": []})
            return httpx.Response(200, json={"payload": messages})
        if request.method == "POST" and path.endswith("/conversations/173/messages"):
            body = json.loads(request.content)
            posted.append(body)
            created = {
                "id": 99001,
                "conversation_id": 173,
                "inbox_id": 9,
                "message_type": 1,
                "private": body["private"],
                "content": body["content"],
            }
            messages.append(created)
            return httpx.Response(200, json=created)
        raise AssertionError(f"unexpected request {request.method} {path}")

    return ChatwootClient(
        base_url="https://chatwoot.example",
        account_id=1,
        access_token="token",
        transport=httpx.MockTransport(handler),
    )


def test_the_client_writes_a_private_note_once_per_marker() -> None:
    posted: list[dict[str, Any]] = []
    client = _chatwoot_with_history(posted)
    body = handoff_link_note_body(_link())
    marker = handoff_link_note_marker(HANDOFF_REQUEST_ID)

    for _ in range(2):
        assert asyncio.run(
            client.ensure_marked_private_note(
                conversation_id=173,
                expected_inbox_id=9,
                note_body=body,
                idempotency_marker=marker,
            )
        )

    [post] = posted
    assert post["private"] is True
    assert post["message_type"] == "outgoing"
    assert post["content"] == f"{body}\n\n{marker}"


def test_the_client_still_rejects_an_unknown_marker() -> None:
    client = _chatwoot_with_history([])
    with pytest.raises(ValueError, match="marker"):
        asyncio.run(
            client.ensure_marked_private_note(
                conversation_id=173,
                expected_inbox_id=9,
                note_body="x",
                idempotency_marker="[supportmagician-otra:1]",
            )
        )
