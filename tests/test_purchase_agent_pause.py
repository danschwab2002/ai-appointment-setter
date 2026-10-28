"""E-100: al comprar, el agente deja de atender y la persona pasa al equipo.

La replica de «Ventas maribel» de GHL (relevamiento de E-093): con la compra
aprobada, la conversacion del comprador queda con la etiqueta
``automation_paused``, asignada al team de derivacion y con una nota privada;
la reanudacion por silencio del equipo no la devuelve al agente.

Datos:

* la compra es el evento real de ATT1 capturado el 12/10
  (``hotmart_cash_payment_approved_att1_20261012.json``, anonimizado: el
  telefono viene sin el 1 de Mexico);
* la conversacion es el show real de la API de Chatwoot de la conversacion 158
  del inbox 9 (``chatwoot_conversation_api_show_conv_158_20260924.json``): sin
  ``assignee`` ni ``team`` en ``meta`` cuando nadie la tiene;
* el historial con notas privadas es el real de la conversacion 173 del inbox 9
  (``chatwoot_conversation_messages_inbox_9_conv_173_20260926.json``).

Las conversaciones del comprador las busca la base
(``read_purchase_agent_pause_conversations_v1``, migracion 20261025000400); su
SQL se prueba en ``tests/sql/followup_engine/validate_purchase_agent_pause.mjs``.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import bridge.app as app_module
from bridge.app import Settings, _resume_paused_conversation
from bridge.chatwoot import ChatwootAssignmentConflictError, ChatwootClient, ChatwootProtocolError
from bridge.hotmart import parse_hotmart_purchase_payload
from bridge.purchase_agent_pause import (
    PURCHASE_PAUSE_NOTE_MARKER_PREFIX,
    PurchaseAgentPause,
    purchase_pause_note_body,
    purchase_pause_note_marker,
    purchase_pause_note_present,
)
from bridge.supabase import (
    ConversationResumeResult,
    PurchaseAgentPauseLookup,
    PurchaseCorrelationResult,
    SupabaseClient,
    SupabaseError,
)
from bridge.worker import ResolutionWorker
from test_att1_production_settings import _start

FIXTURES = Path(__file__).parent / "fixtures"
PURCHASE_FIXTURE = FIXTURES / "hotmart_cash_payment_approved_att1_20261012.json"
SHOW_158 = FIXTURES / "chatwoot_conversation_api_show_conv_158_20260924.json"
HISTORY_173 = FIXTURES / "chatwoot_conversation_messages_inbox_9_conv_173_20260926.json"
EVENT_ID = "6b0f3c1e-2a4d-4e8f-9c7b-1d2e3f4a5b6c"
ATT1_PRODUCT = 5071808


def _purchase_payload() -> dict[str, Any]:
    captured = json.loads(PURCHASE_FIXTURE.read_text(encoding="utf-8"))
    payload = copy.deepcopy(captured["eventos"][0]["payload"])
    assert payload["event"] == "PURCHASE_APPROVED"
    assert payload["data"]["product"]["id"] == ATT1_PRODUCT
    return payload


def _purchase() -> Any:
    purchase = parse_hotmart_purchase_payload(_purchase_payload())
    assert purchase is not None
    return purchase


# ------------------------------------------------------------ texto y marcador


def test_the_note_says_the_day_of_the_purchase_in_the_instance_zone() -> None:
    # 01:30 UTC del 21/10 es todavia el 20/10 en Ciudad de Mexico.
    body = purchase_pause_note_body(
        datetime(2026, 10, 21, 1, 30, tzinfo=UTC), time_zone="America/Mexico_City"
    )
    assert body.startswith("Compró el 20/10/2026: la atiende el equipo.")
    assert "automation_paused" in body
    assert len(body) < 1800


def test_the_marker_is_one_per_purchase() -> None:
    assert purchase_pause_note_marker(EVENT_ID.upper()) == (
        f"{PURCHASE_PAUSE_NOTE_MARKER_PREFIX}{EVENT_ID}]"
    )
    with pytest.raises(ValueError):
        purchase_pause_note_marker("evento; drop")


def test_only_a_private_note_with_the_marker_counts() -> None:
    history = json.loads(HISTORY_173.read_text(encoding="utf-8"))["payload"]
    assert purchase_pause_note_present(history) is False
    marker = purchase_pause_note_marker(EVENT_ID)
    public = {"private": False, "message_type": 1, "content": f"x {marker}"}
    assert purchase_pause_note_present([*history, public]) is False
    note = {"private": True, "message_type": 1, "content": f"Compró\n\n{marker}"}
    assert purchase_pause_note_present([*history, note]) is True


# ------------------------------------------------------- la pausa del comprador


class _LookupSupabase:
    def __init__(self, lookup: PurchaseAgentPauseLookup | Exception) -> None:
        self.lookup = lookup
        self.reads: list[dict[str, object]] = []

    async def read_purchase_agent_pause_conversations(self, **kwargs: object):
        self.reads.append(kwargs)
        if isinstance(self.lookup, Exception):
            raise self.lookup
        return self.lookup


class _PauseChatwoot:
    def __init__(self, *, fail_on: set[int] | None = None, note_error: bool = False) -> None:
        self.fail_on = fail_on or set()
        self.note_error = note_error
        self.paused: list[dict[str, object]] = []
        self.notes: list[dict[str, object]] = []

    async def pause_purchased_conversation(self, **kwargs: object) -> str:
        if kwargs["conversation_id"] in self.fail_on:
            raise httpx.ConnectError("caido")
        self.paused.append(kwargs)
        return "team_assigned"

    async def ensure_marked_private_note(self, **kwargs: object) -> bool:
        self.notes.append(kwargs)
        if self.note_error:
            raise ChatwootProtocolError("invalid_json")
        return True


def _pause(supabase: Any, chatwoot: Any, *, team_id: int | None = 3) -> PurchaseAgentPause:
    return PurchaseAgentPause(
        supabase=supabase,
        chatwoot=chatwoot,
        chatwoot_account_id=2,
        chatwoot_inbox_id=11,
        inbound_scope_key="att1-inbound",
        team_id=team_id,
        time_zone="America/Mexico_City",
    )


def test_a_found_buyer_is_paused_assigned_and_noted_in_each_conversation() -> None:
    supabase = _LookupSupabase(PurchaseAgentPauseLookup("found", (40, 44)))
    chatwoot = _PauseChatwoot()
    purchase = _purchase()

    outcome = asyncio.run(
        _pause(supabase, chatwoot).pause_buyer(webhook_event_id=EVENT_ID, purchase=purchase)
    )

    assert outcome == "paused"
    assert supabase.reads == [{
        "chatwoot_account_id": 2,
        "chatwoot_inbox_id": 11,
        "inbound_scope_key": "att1-inbound",
        "buyer_email": purchase.buyer_email,
        "buyer_phone": purchase.buyer_phone,
    }]
    assert [call["conversation_id"] for call in chatwoot.paused] == [40, 44]
    assert all(call["team_id"] == 3 and call["expected_inbox_id"] == 11 for call in chatwoot.paused)
    assert [note["idempotency_marker"] for note in chatwoot.notes] == [
        purchase_pause_note_marker(EVENT_ID)
    ] * 2
    assert chatwoot.notes[0]["note_body"].startswith("Compró el ")


@pytest.mark.parametrize("outcome", ["not_found", "ambiguous"])
def test_without_a_single_buyer_nothing_is_touched(outcome: str) -> None:
    chatwoot = _PauseChatwoot()
    result = asyncio.run(
        _pause(_LookupSupabase(PurchaseAgentPauseLookup(outcome, ())), chatwoot).pause_buyer(
            webhook_event_id=EVENT_ID, purchase=_purchase()
        )
    )
    assert result == outcome
    assert chatwoot.paused == [] and chatwoot.notes == []


def test_a_failed_read_or_conversation_never_raises() -> None:
    chatwoot = _PauseChatwoot()
    assert asyncio.run(
        _pause(_LookupSupabase(SupabaseError("x_failed: HTTP 500")), chatwoot).pause_buyer(
            webhook_event_id=EVENT_ID, purchase=_purchase()
        )
    ) == "failed"

    chatwoot = _PauseChatwoot(fail_on={40}, note_error=True)
    assert asyncio.run(
        _pause(_LookupSupabase(PurchaseAgentPauseLookup("found", (40, 44))), chatwoot).pause_buyer(
            webhook_event_id=EVENT_ID, purchase=_purchase()
        )
    ) == "paused"
    assert [call["conversation_id"] for call in chatwoot.paused] == [44]


def test_a_purchase_without_email_or_phone_reads_nothing() -> None:
    supabase = _LookupSupabase(PurchaseAgentPauseLookup("found", (40,)))
    purchase = replace(_purchase(), buyer_email=None, buyer_phone=None)
    assert asyncio.run(
        _pause(supabase, _PauseChatwoot()).pause_buyer(
            webhook_event_id=EVENT_ID, purchase=purchase
        )
    ) == "no_contact"
    assert supabase.reads == []


# ------------------------------------------------------------------ el worker


class _WorkerSupabase(_LookupSupabase):
    def __init__(self) -> None:
        super().__init__(PurchaseAgentPauseLookup("found", (40,)))
        self.applied: list[dict[str, object]] = []

    async def apply_hotmart_purchase_approved(self, **kwargs: object):
        self.applied.append(kwargs)
        return PurchaseCorrelationResult(
            outcome="applied", recovery_case_id=None, matched_by="phone"
        )


def _worker(supabase: Any, chatwoot: Any, *, with_pause: bool = True, product: int = ATT1_PRODUCT):
    return ResolutionWorker(
        supabase=supabase,
        commercial_ally_config=SimpleNamespace(hotmart_product_id=product, funnel_ref="att1"),
        purchase_agent_pause=_pause(supabase, chatwoot) if with_pause else None,
    )


def _event() -> dict[str, object]:
    return {"id": EVENT_ID, "event_type": "PURCHASE_APPROVED", "payload": _purchase_payload()}


def test_the_worker_pauses_the_buyer_after_applying_the_purchase() -> None:
    supabase, chatwoot = _WorkerSupabase(), _PauseChatwoot()
    asyncio.run(_worker(supabase, chatwoot)._process_one(_event()))

    assert len(supabase.applied) == 1
    assert len(supabase.reads) == 1
    assert [call["conversation_id"] for call in chatwoot.paused] == [40]


def test_without_the_flag_the_purchase_is_as_today() -> None:
    supabase, chatwoot = _WorkerSupabase(), _PauseChatwoot()
    asyncio.run(_worker(supabase, chatwoot, with_pause=False)._process_one(_event()))

    assert len(supabase.applied) == 1
    assert supabase.reads == [] and chatwoot.paused == []


def test_a_purchase_of_another_product_does_not_pause() -> None:
    supabase, chatwoot = _WorkerSupabase(), _PauseChatwoot()
    asyncio.run(_worker(supabase, chatwoot, product=999)._process_one(_event()))

    assert len(supabase.applied) == 1
    assert supabase.reads == [] and chatwoot.paused == []


@pytest.mark.parametrize("native_outcome", ["admitida", "reintentar"])
def test_the_pause_is_skipped_only_for_the_buyer_a_native_flow_took(native_outcome: str) -> None:
    # E-165: con el flujo nativo que arranco (o mientras la admision no contesto), no se pausa.
    supabase, chatwoot = _WorkerSupabase(), _PauseChatwoot()
    worker = _worker(supabase, chatwoot)
    asyncio.run(
        worker._pause_agent_for_purchase(
            EVENT_ID, _purchase_payload(), _purchase(), native_outcome=native_outcome
        )
    )
    assert supabase.reads == [] and chatwoot.paused == []


@pytest.mark.parametrize("native_outcome", ["sin_arranque", "descartada:sin_telefono"])
def test_a_buyer_no_native_flow_took_is_paused(native_outcome: str) -> None:
    # E-165: fuera de la cohorte, sin flujo o sin poder entrar nunca, se pausa como sin flujo nativo.
    supabase, chatwoot = _WorkerSupabase(), _PauseChatwoot()
    worker = _worker(supabase, chatwoot)
    asyncio.run(
        worker._pause_agent_for_purchase(
            EVENT_ID, _purchase_payload(), _purchase(), native_outcome=native_outcome
        )
    )
    assert [call["conversation_id"] for call in chatwoot.paused] == [40]


# ------------------------------------------------- el cliente de Chatwoot real


def _show_158() -> dict[str, Any]:
    conversation = json.loads(SHOW_158.read_text(encoding="utf-8"))["conversation"]
    assert "assignee" not in conversation["meta"] and "team" not in conversation["meta"]
    return conversation


def _client(
    *, conversation: dict[str, Any], requests: list[tuple[str, str, Any]], team_after_post: int | None = 3
) -> ChatwootClient:
    labels: list[str] = list(conversation.get("labels", []))
    state = {"conversation": conversation}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, path, body))
        if request.method == "GET" and path.endswith("/conversations/158"):
            return httpx.Response(200, json=state["conversation"])
        if request.method == "GET" and path.endswith("/conversations/158/labels"):
            return httpx.Response(200, json={"payload": labels})
        if request.method == "POST" and path.endswith("/macros/7/execute"):
            labels.append("automation_paused")
            return httpx.Response(200, json={})
        if request.method == "POST" and path.endswith("/conversations/158/assignments"):
            if team_after_post is not None:
                updated = copy.deepcopy(state["conversation"])
                updated["meta"]["team"] = {"id": team_after_post, "name": "maribel"}
                state["conversation"] = updated
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected request {request.method} {path}")

    return ChatwootClient(
        base_url="https://chatwoot.example",
        account_id=1,
        access_token="token",
        pause_macro_id=7,
        confirmation_attempts=2,
        confirmation_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )


def test_the_client_labels_then_assigns_the_team_of_an_unowned_conversation() -> None:
    requests: list[tuple[str, str, Any]] = []
    client = _client(conversation=_show_158(), requests=requests)

    result = asyncio.run(
        client.pause_purchased_conversation(conversation_id=158, expected_inbox_id=9, team_id=3)
    )

    assert result == "team_assigned"
    writes = [(method, path.rsplit("/", 2)[-2:], body) for method, path, body in requests if method == "POST"]
    assert writes == [
        ("POST", ["7", "execute"], {"conversation_ids": [158]}),
        ("POST", ["158", "assignments"], {"team_id": 3}),
    ]


def test_the_client_leaves_a_person_already_assigned() -> None:
    conversation = _show_158()
    conversation["meta"]["assignee"] = {"id": 4, "name": "Mariana"}
    conversation["meta"]["assignee_type"] = "User"
    requests: list[tuple[str, str, Any]] = []
    client = _client(conversation=conversation, requests=requests)

    assert asyncio.run(
        client.pause_purchased_conversation(conversation_id=158, expected_inbox_id=9, team_id=3)
    ) == "existing_human"
    assert not any(path.endswith("/assignments") for _, path, _ in requests)


def test_the_client_without_team_only_labels() -> None:
    requests: list[tuple[str, str, Any]] = []
    client = _client(conversation=_show_158(), requests=requests)
    assert asyncio.run(
        client.pause_purchased_conversation(conversation_id=158, expected_inbox_id=9, team_id=None)
    ) == "no_team"
    assert [path for method, path, _ in requests if method == "POST"] == [
        "/api/v1/accounts/1/macros/7/execute"
    ]


def test_the_client_refuses_a_conversation_of_another_inbox_before_writing() -> None:
    requests: list[tuple[str, str, Any]] = []
    client = _client(conversation=_show_158(), requests=requests)
    with pytest.raises(ChatwootProtocolError, match="invalid_conversation_authority"):
        asyncio.run(
            client.pause_purchased_conversation(conversation_id=158, expected_inbox_id=11, team_id=3)
        )
    assert [method for method, _, _ in requests] == ["GET"]


def test_another_team_is_a_conflict_after_the_label() -> None:
    conversation = _show_158()
    conversation["meta"]["team"] = {"id": 9, "name": "otro"}
    requests: list[tuple[str, str, Any]] = []
    client = _client(conversation=conversation, requests=requests)
    with pytest.raises(ChatwootAssignmentConflictError):
        asyncio.run(
            client.pause_purchased_conversation(conversation_id=158, expected_inbox_id=9, team_id=3)
        )
    assert any(path.endswith("/macros/7/execute") for _, path, _ in requests)


def test_the_client_accepts_the_purchase_note_marker() -> None:
    history = json.loads(HISTORY_173.read_text(encoding="utf-8"))
    messages = list(history["payload"])
    posted: list[dict[str, Any]] = []

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
            created = {**body, "id": 99002, "conversation_id": 173, "message_type": 1}
            messages.append(created)
            return httpx.Response(200, json=created)
        raise AssertionError(f"unexpected request {request.method} {path}")

    client = ChatwootClient(
        base_url="https://chatwoot.example", account_id=1, access_token="token",
        transport=httpx.MockTransport(handler),
    )
    marker = purchase_pause_note_marker(EVENT_ID)
    body = purchase_pause_note_body(datetime(2026, 10, 25, 18, tzinfo=UTC), time_zone="America/Mexico_City")
    for _ in range(2):
        assert asyncio.run(client.ensure_marked_private_note(
            conversation_id=173, expected_inbox_id=9, note_body=body, idempotency_marker=marker,
        ))
    [post] = posted
    assert post["private"] is True and post["content"] == f"{body}\n\n{marker}"
    assert purchase_pause_note_present(messages) is True


# ------------------------------------------------ la lectura de la base (cliente)


def _supabase(rows: list[dict[str, Any]], seen: list[dict[str, Any]]) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/rpc/read_purchase_agent_pause_conversations_v1"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=rows)

    return SupabaseClient(
        base_url="https://supabase.example", service_role_key="key",
        transport=httpx.MockTransport(handler),
    )


def _read(rows: list[dict[str, Any]], seen: list[dict[str, Any]] | None = None):
    return asyncio.run(_supabase(rows, seen if seen is not None else []).read_purchase_agent_pause_conversations(
        chatwoot_account_id=2, chatwoot_inbox_id=11, inbound_scope_key="att1-inbound",
        buyer_email="persona.prueba1@example.com", buyer_phone="525500000001",
    ))


def test_the_read_parses_found_ambiguous_and_empty() -> None:
    seen: list[dict[str, Any]] = []
    found = _read([
        {"outcome": "found", "chatwoot_conversation_id": 44, "last_activity_at": "2026-10-22T10:00:00+00:00"},
        {"outcome": "found", "chatwoot_conversation_id": 40, "last_activity_at": "2026-10-20T10:00:00+00:00"},
    ], seen)
    assert found == PurchaseAgentPauseLookup("found", (44, 40))
    assert seen == [{
        "p_chatwoot_account_id": 2, "p_chatwoot_inbox_id": 11, "p_inbound_scope_key": "att1-inbound",
        "p_buyer_email": "persona.prueba1@example.com", "p_buyer_phone": "525500000001",
    }]
    assert _read([{"outcome": "ambiguous", "chatwoot_conversation_id": None, "last_activity_at": None}]) == (
        PurchaseAgentPauseLookup("ambiguous", ())
    )
    assert _read([]) == PurchaseAgentPauseLookup("not_found", ())


@pytest.mark.parametrize("rows", [
    [{"outcome": "found", "chatwoot_conversation_id": 0, "last_activity_at": None}],
    [{"outcome": "found", "chatwoot_conversation_id": 4, "last_activity_at": None, "x": 1}],
    [{"outcome": "otro", "chatwoot_conversation_id": 4, "last_activity_at": None}],
    [{"outcome": "found", "chatwoot_conversation_id": 4, "last_activity_at": None},
     {"outcome": "ambiguous", "chatwoot_conversation_id": None, "last_activity_at": None}],
])
def test_the_read_rejects_an_unexpected_shape(rows: list[dict[str, Any]]) -> None:
    with pytest.raises(SupabaseError):
        _read(rows)


# ----------------------------------------- la reanudacion no la devuelve sola


class _ResumeClient:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self._messages = messages
        self.label_cleared = False

    async def get_canonical_conversation_snapshot(self, **_: object):
        return SimpleNamespace(
            status="open", can_reply=True, labels=("automation_paused",),
            human_assignee_present=False,
        )

    async def get_conversation_messages(self, **_: object):
        return self._messages

    async def clear_conversation_label(self, **_: object) -> bool:
        self.label_cleared = True
        return True


class _ResumeSupabase:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def resume_paused_conversation(self, **kwargs: object):
        self.calls.append(kwargs)
        return ConversationResumeResult(
            outcome="already_active", conversation_id="c", commercial_case_id="k",
            resume_event_id=None,
        )


def _resume(messages: list[dict[str, object]]) -> tuple[bool, _ResumeClient, _ResumeSupabase]:
    client, supabase = _ResumeClient(messages), _ResumeSupabase()
    resumed = asyncio.run(_resume_paused_conversation(
        control_client=client,  # type: ignore[arg-type]
        supabase=supabase,  # type: ignore[arg-type]
        settings=SimpleNamespace(
            chatwoot_inbox_id=11, conversation_resume_quiet_seconds=28_800,
            conversation_resume_max=10,
        ),  # type: ignore[arg-type]
        conversation_id=40,
        message_id=9001,
    ))
    return resumed, client, supabase


def test_a_paused_buyer_is_not_handed_back_to_the_agent_by_the_team_silence() -> None:
    # Sin nadie del equipo escribiendo, la reanudacion devolvia la conversacion
    # al agente en el primer mensaje del comprador (quiet_seconds None).
    resumed, client, supabase = _resume([])
    assert resumed is True and client.label_cleared is True

    note = {
        "private": True, "message_type": 1,
        "content": f"Compró el 25/10/2026\n\n{purchase_pause_note_marker(EVENT_ID)}",
    }
    resumed, client, supabase = _resume([note])
    assert resumed is False
    assert client.label_cleared is False
    assert supabase.calls == []


# ------------------------------------------------------------ el flag del runtime


def test_the_flag_is_off_by_default() -> None:
    assert Settings.__dataclass_fields__["purchase_agent_pause_enabled"].default is False


def test_att1_production_starts_with_the_flag_and_hands_the_pause_to_the_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[dict[str, object]] = []
    real = app_module.ResolutionWorker

    def capture(**kwargs: object) -> ResolutionWorker:
        seen.append(kwargs)
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(app_module, "ResolutionWorker", capture)
    settings, _ = _start(
        monkeypatch, tmp_path, "carrito", extra={"PURCHASE_AGENT_PAUSE_ENABLED": "true"}
    )
    assert settings.purchase_agent_pause_enabled is True
    [kwargs] = seen
    pause = kwargs["purchase_agent_pause"]
    assert isinstance(pause, PurchaseAgentPause)
    assert pause.chatwoot_inbox_id == settings.instance_manifest.chatwoot_inbox_id
    assert pause.team_id == settings.instance_manifest.handoff_team_id

    seen.clear()
    settings, _ = _start(monkeypatch, tmp_path / "sin", "carrito")
    assert settings.purchase_agent_pause_enabled is False
    assert seen[0]["purchase_agent_pause"] is None


def test_the_flag_without_the_purchase_worker_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="PURCHASE_AGENT_PAUSE_ENABLED requires"):
        _start(monkeypatch, tmp_path, extra={"PURCHASE_AGENT_PAUSE_ENABLED": "true"})
