"""Parte 3 de E-539: resolver en Chatwoot cierra la conversacion del setter.

Diagnostico (E-539, 13/11/2026): con ``lock_to_single_conversation = false`` en
las bandejas 11 y 13, cuando la persona vuelve a escribir Chatwoot abre otra
conversacion y la resuelta queda ``paused_human`` para siempre en el setter
(25 de 50 ``paused_human`` el 13/11). El cambio de estado llega como
``conversation_updated`` (Maribel conv 260, 2026-11-12 23:57:04Z,
``chatwoot_webhook_ignored reason=unsupported_event``) y no se procesaba ni se
capturaba.

DEUDA DE FIXTURE (declarada): el bridge nunca capturo un cambio de estado, y la
entrega real de la 260 cayo antes de la captura. Los payloads de aca se derivan
del precedente mas cercano del repo, marcado: el ``webhook.conversation``
capturado de la conversacion 173
(``chatwoot_paused_lead_label_removed_by_human_inbox_9_conv_173_20260925.json``,
el ``webhook_data`` de la raiz de un ``conversation_updated`` de Chatwoot
v4.13.0, el mismo que usa E-571) mas ``changed_attributes`` con ``status`` en
la forma de ``BaseListener#extract_changed_attributes``, igual que
``label_list``. Con el flag prendido el evento se captura antes de procesarse
(como el de la etiqueta), y el clasificador falla cerrado ante otra forma:
antes de prender ``CHATWOOT_RESOLVED_CLOSE_ENABLED`` en una instancia hay que
sumar uno real anonimizado con su test.

Lo que la base hace de verdad lo prueba
``tests/sql/followup_engine/validate_close_resolved_conversation.mjs``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import httpx
import pytest

import bridge.app as app_module
from bridge.app import Settings
from bridge.filtering import (
    ConversationResolved,
    classify_chatwoot_conversation_resolved,
    classify_chatwoot_label_removal,
)
from bridge.supabase import (
    ConversationCloseResult,
    ConversationResumeResult,
    SupabaseClient,
    SupabaseError,
)
from test_app import (
    AHORA_173,
    JID_DEL_LEAD_173,
    NUMERO_DE_PRUEBA,
    _Chatwoot173,
    _fixture_173,
)
from test_chatwoot_label_removal_resume import _app, _enviar

ENTREGA = "c3a1f0de-0000-4000-8000-000000000173"  # valor de prueba
CLAVE = (
    "close:173:resolved:"
    + hashlib.sha256(ENTREGA.encode("utf-8")).hexdigest()[:32]
)


def _resuelta(
    previo: str = "open", actual: str = "resolved", **extra: object
) -> dict[str, object]:
    """`conversation_updated` derivado del `webhook_data` capturado de la 173."""
    conversation = dict(_fixture_173()["webhook"]["conversation"])
    conversation["status"] = actual
    return {
        **conversation,
        "event": "conversation_updated",
        "changed_attributes": [
            {"status": {"previous_value": previo, "current_value": actual}}
        ],
        **extra,
    }


def _clasificar(payload: object, *, scoped: bool = True, allowed: str = NUMERO_DE_PRUEBA):
    return classify_chatwoot_conversation_resolved(
        payload,
        expected_inbox_id=9,
        allowed_jid=allowed,
        allow_any_scoped_sender=scoped,
    )


# ------------------------------------------------------------- el clasificador


def test_a_resolution_is_recognized_with_the_lead_identity() -> None:
    assert _clasificar(_resuelta()) == ConversationResolved(
        conversation_id=173, sender_jid=JID_DEL_LEAD_173
    )
    assert _clasificar(_resuelta(previo="pending")) is not None


@pytest.mark.parametrize(
    "payload",
    [
        # Reabrir no es resolver.
        _resuelta(previo="resolved", actual="open"),
        # Ya estaba resuelta: no hay cambio.
        _resuelta(previo="resolved", actual="resolved"),
        _resuelta(actual="snoozed"),
        # El estado de la raiz contradice el cambio.
        {**_resuelta(), "status": "open"},
        # Sin status en los cambios (un cambio de etiqueta).
        {
            **_resuelta(),
            "changed_attributes": [
                {"label_list": {"previous_value": ["automation_paused"], "current_value": []}}
            ],
        },
        _resuelta(inbox_id=8),
        {**_resuelta(), "event": "message_created"},
        # Forma rota: falla cerrado.
        {**_resuelta(), "changed_attributes": [{"status": "resolved"}]},
        {**_resuelta(), "changed_attributes": [{"status": {"previous_value": None, "current_value": "resolved"}}]},
        {**_resuelta(), "changed_attributes": "status"},
        {**_resuelta(), "id": True},
        {**_resuelta(), "contact_inbox": "x"},
        "no es un objeto",
    ],
)
def test_anything_else_is_not_a_resolution(payload: object) -> None:
    assert _clasificar(payload) is None


def test_a_resolution_from_another_account_is_ignored() -> None:
    def clasificar(payload: object):
        return classify_chatwoot_conversation_resolved(
            payload,
            expected_inbox_id=9,
            expected_account_id=1,
            allowed_jid=NUMERO_DE_PRUEBA,
            allow_any_scoped_sender=True,
        )

    assert clasificar({**_resuelta(), "account_id": 2}) is None
    assert clasificar({**_resuelta(), "account": {"id": 2}}) is None
    assert clasificar({**_resuelta(), "account_id": 1}) is not None
    assert clasificar(_resuelta()) is not None


def test_without_scoped_senders_only_the_allowed_identity_counts() -> None:
    assert _clasificar(_resuelta(), scoped=False) is None
    assert _clasificar(_resuelta(), scoped=False, allowed=JID_DEL_LEAD_173) == (
        ConversationResolved(conversation_id=173, sender_jid=None)
    )


def test_the_shared_identity_helper_keeps_the_label_removal_identical() -> None:
    """La identidad salio a un helper comun: la de la etiqueta no cambia."""
    base = dict(_fixture_173()["webhook"]["conversation"])
    payload = {
        **base,
        "event": "conversation_updated",
        "changed_attributes": [
            {"label_list": {"previous_value": ["automation_paused"], "current_value": []}}
        ],
    }
    removal = classify_chatwoot_label_removal(
        payload, label="automation_paused", expected_inbox_id=9,
        allowed_jid=NUMERO_DE_PRUEBA, allow_any_scoped_sender=True,
    )
    assert removal is not None and removal.sender_jid == JID_DEL_LEAD_173
    assert classify_chatwoot_label_removal(
        payload, label="automation_paused", expected_inbox_id=9,
        allowed_jid=NUMERO_DE_PRUEBA, allow_any_scoped_sender=False,
    ) is None
    assert classify_chatwoot_label_removal(
        payload, label="automation_paused", expected_inbox_id=9,
        allowed_jid=JID_DEL_LEAD_173, allow_any_scoped_sender=False,
    ) is not None


# ------------------------------------------------------------ el handler entero


class _SupabaseResuelta:
    def __init__(self, outcome: str = "closed") -> None:
        self.outcome = outcome
        self.cierres: list[dict[str, object]] = []
        self.reanudaciones: list[dict[str, object]] = []

    async def admit_inbound_commercial_case(self, **_: object) -> object:
        raise AssertionError("un cambio de estado no abre ni admite un caso")

    async def resume_paused_conversation(self, **_: object):
        raise AssertionError("resolver no reanuda nada")

    async def resume_paused_conversation_by_operator(self, **kwargs: object):
        self.reanudaciones.append(kwargs)
        return ConversationResumeResult(
            outcome="resumed", conversation_id="c", commercial_case_id="k", resume_event_id="e"
        )

    async def close_resolved_conversation(self, **kwargs: object):
        self.cierres.append(kwargs)
        return ConversationCloseResult(
            outcome=self.outcome, conversation_id="conversation-173", close_event_id="event-173"
        )

    async def has_chatwoot_opt_out_stop(self, **_: object) -> bool:
        return False


def _resuelta_en_chatwoot() -> _Chatwoot173:
    fixture = _fixture_173()
    fixture["conversation"] = {**fixture["conversation"], "status": "resolved"}
    return _Chatwoot173(fixture)


@pytest.mark.parametrize(
    "payload",
    [_resuelta(), _resuelta(previo="resolved", actual="open"), _resuelta(actual="snoozed")],
)
def test_with_the_flag_off_nothing_changes(tmp_path, monkeypatch, payload) -> None:
    """Apagado (el default), un cambio de estado se ignora como en origin/main.

    Misma respuesta (200, ``unsupported_event``), sin captura a disco, sin
    trabajo durable, sin leer Chatwoot y sin llamar a la base.
    """
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _resuelta_en_chatwoot()
    supabase = _SupabaseResuelta()
    app = _app(tmp_path, transport, supabase)
    [response] = _enviar(app, payload, (ENTREGA,))
    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "unsupported_event"}
    assert supabase.cierres == []
    assert supabase.reanudaciones == []
    assert transport.requests == []
    assert list(tmp_path.rglob("*.json")) == []


def test_the_flag_needs_the_durable_admission(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _resuelta_en_chatwoot()
    supabase = _SupabaseResuelta()
    app = _app(
        tmp_path,
        transport,
        supabase,
        chatwoot_resolved_close_enabled=True,
        chatwoot_cut_b_admission_enabled=False,
        chatwoot_cut_b_agent_enabled=False,
        chatwoot_scoped_inbound_senders_enabled=False,
        human_handoff_admission_enabled=False,
        human_handoff_projection_enabled=False,
    )
    [response] = _enviar(app, _resuelta(), (ENTREGA,))
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert supabase.cierres == []


def test_a_resolution_closes_the_setter_conversation(
    tmp_path, monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _resuelta_en_chatwoot()
    supabase = _SupabaseResuelta()
    app = _app(tmp_path, transport, supabase, chatwoot_resolved_close_enabled=True)
    caplog.set_level(logging.INFO)
    primera, repetida = _enviar(app, _resuelta(), (ENTREGA, ENTREGA))
    assert primera.status_code == 202
    assert primera.json() == {
        "status": "accepted",
        "reason": "conversation_resolved",
        "delivery_id": ENTREGA,
    }
    assert repetida.json()["status"] == "duplicate"
    assert supabase.cierres == [{"external_conversation_id": 173, "command_key": CLAVE}]
    assert supabase.reanudaciones == []
    assert transport.macro_executed is False
    # Con el flag prendido se captura, para conseguir el fixture real.
    captured = list(tmp_path.glob("*.json"))
    assert len(captured) == 1
    assert json.loads(captured[0].read_text())["changed_attributes"][0]["status"][
        "current_value"
    ] == "resolved"
    assert any(
        "chatwoot_resolved_close conversation=173 outcome=closed" in m
        for m in caplog.messages
    )
    assert not [m for m in caplog.messages if "chatwoot_work_failed" in m]


def test_with_the_flag_on_reopen_and_snooze_are_still_ignored(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _Chatwoot173(_fixture_173())
    supabase = _SupabaseResuelta()
    app = _app(tmp_path, transport, supabase, chatwoot_resolved_close_enabled=True)
    for i, payload in enumerate(
        [_resuelta(previo="resolved", actual="open"), _resuelta(actual="snoozed")]
    ):
        [response] = _enviar(app, payload, (f"otra-entrega-000{i}",))
        assert response.status_code == 200
        assert response.json() == {"status": "ignored", "reason": "unsupported_event"}
    assert supabase.cierres == []
    assert transport.requests == []


def test_a_reopened_conversation_is_not_closed(tmp_path, monkeypatch, caplog) -> None:
    """La reabrieron entre el evento y el trabajo: se relee y no se cierra."""
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _Chatwoot173(_fixture_173())  # abierta
    supabase = _SupabaseResuelta()
    app = _app(tmp_path, transport, supabase, chatwoot_resolved_close_enabled=True)
    caplog.set_level(logging.INFO)
    [response] = _enviar(app, _resuelta(), (ENTREGA,))
    assert response.status_code == 202
    assert supabase.cierres == []
    assert any("reason=status_open" in m for m in caplog.messages)


def test_another_contact_is_never_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _resuelta_en_chatwoot()
    supabase = _SupabaseResuelta()
    app = _app(tmp_path, transport, supabase, chatwoot_resolved_close_enabled=True)
    payload = _resuelta()
    payload["contact_inbox"] = {**payload["contact_inbox"], "source_id": "5210000000999"}  # type: ignore[dict-item]
    payload["meta"] = {**payload["meta"], "sender": {**payload["meta"]["sender"], "identifier": None}}  # type: ignore[index]
    [response] = _enviar(app, payload, (ENTREGA,))
    assert response.status_code == 202
    assert supabase.cierres == []


def test_a_failed_close_is_retried_by_the_worker(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _resuelta_en_chatwoot()
    supabase = _SupabaseResuelta()

    async def caido(**kwargs: object):
        supabase.cierres.append(kwargs)
        raise SupabaseError("conversation_close_resolved_failed: HTTP 503")

    supabase.close_resolved_conversation = caido  # type: ignore[method-assign]
    app = _app(tmp_path, transport, supabase, chatwoot_resolved_close_enabled=True)
    caplog.set_level(logging.WARNING)
    [response] = _enviar(app, _resuelta(), (ENTREGA,))
    assert response.status_code == 202
    assert len(supabase.cierres) == 1
    fallos = [m for m in caplog.messages if "chatwoot_work_failed" in m]
    assert fallos and "LabelRemovalResumeTransientError" in fallos[0]
    assert "status=admitted" in fallos[0]


def test_a_pending_projection_is_retried(tmp_path, monkeypatch, caplog) -> None:
    """El proyector trabaja: se reintenta en vez de dejar la paused_human muerta."""
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _resuelta_en_chatwoot()
    supabase = _SupabaseResuelta(outcome="blocked_pending_projection")
    app = _app(tmp_path, transport, supabase, chatwoot_resolved_close_enabled=True)
    caplog.set_level(logging.WARNING)
    [response] = _enviar(app, _resuelta(), (ENTREGA,))
    assert response.status_code == 202
    fallos = [m for m in caplog.messages if "chatwoot_work_failed" in m]
    assert fallos and "status=admitted" in fallos[0]


@pytest.mark.parametrize("outcome", ["not_paused", "already_closed", "replayed", "not_found"])
def test_the_base_decides_and_nothing_else_happens(tmp_path, monkeypatch, caplog, outcome) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _resuelta_en_chatwoot()
    supabase = _SupabaseResuelta(outcome=outcome)
    app = _app(tmp_path, transport, supabase, chatwoot_resolved_close_enabled=True)
    caplog.set_level(logging.INFO)
    [response] = _enviar(app, _resuelta(), (ENTREGA,))
    assert response.status_code == 202
    assert len(supabase.cierres) == 1
    assert any(f"outcome={outcome}" in m for m in caplog.messages)
    assert not [m for m in caplog.messages if "chatwoot_work_failed" in m]


def test_one_event_that_removes_the_label_and_resolves_closes_without_resuming(
    tmp_path, monkeypatch, caplog
) -> None:
    """Los dos flags prendidos y un mismo conversation_updated con las dos cosas."""
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA_173)
    transport = _resuelta_en_chatwoot()
    supabase = _SupabaseResuelta()
    app = _app(
        tmp_path,
        transport,
        supabase,
        chatwoot_resolved_close_enabled=True,
        chatwoot_label_removal_resume_enabled=True,
    )
    payload = _resuelta()
    payload["labels"] = []
    payload["changed_attributes"] = [
        *payload["changed_attributes"],  # type: ignore[misc]
        {"label_list": {"previous_value": ["automation_paused"], "current_value": []}},
    ]
    caplog.set_level(logging.INFO)
    [response] = _enviar(app, payload, (ENTREGA,))
    assert response.json()["reason"] == "conversation_label_removal"
    # Resuelta: no esta 'open', la reanudacion no llega a la base.
    assert supabase.reanudaciones == []
    assert supabase.cierres == [{"external_conversation_id": 173, "command_key": CLAVE}]


# ------------------------------------------------------------ el cliente de la base


def _cliente(row: object, requests: list[httpx.Request], *, status: int = 200) -> SupabaseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json=[row] if row is not None else [])

    return SupabaseClient(
        base_url="https://supabase.example.test",
        service_role_key="service-role",
        transport=httpx.MockTransport(handler),
    )


def test_the_close_client_sends_only_the_conversation_and_the_key() -> None:
    requests: list[httpx.Request] = []
    client = _cliente(
        {"outcome": "closed", "closed_conversation_id": "c", "close_event_id": "e"}, requests
    )
    result = asyncio.run(
        client.close_resolved_conversation(external_conversation_id=173, command_key=CLAVE)
    )
    assert result == ConversationCloseResult(outcome="closed", conversation_id="c", close_event_id="e")
    assert result.closed
    assert requests[0].url.path == "/rest/v1/rpc/close_resolved_conversation_v1"
    assert json.loads(requests[0].content) == {
        "p_external_conversation_id": 173,
        "p_command_key": CLAVE,
    }


@pytest.mark.parametrize(
    ("row", "status"),
    [({"outcome": "deleted"}, 200), (None, 200), ({"outcome": "closed"}, 503), ({"outcome": "closed"}, 404)],
)
def test_an_unknown_answer_from_the_base_is_an_error(row: object, status: int) -> None:
    client = _cliente(row, [], status=status)
    with pytest.raises(SupabaseError):
        asyncio.run(
            client.close_resolved_conversation(external_conversation_id=173, command_key=CLAVE)
        )


# ------------------------------------------------------------------ el arranque


def test_the_flag_is_off_by_default_and_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    field = Settings.__dataclass_fields__["chatwoot_resolved_close_enabled"]
    assert field.default is False
    assert "chatwoot_resolved_close_enabled" in app_module.PORTABLE_RUNTIME_BOOLEAN_CAPABILITIES
    for name, value in {
        "CHATWOOT_WEBHOOK_SECRET": "test-secret",
        "ALLOWED_WHATSAPP_JID": "12025550123@s.whatsapp.net",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "test-control-token",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CHATWOOT_RESOLVED_CLOSE_ENABLED", raising=False)
    assert Settings.from_env().chatwoot_resolved_close_enabled is False
    monkeypatch.setenv("CHATWOOT_RESOLVED_CLOSE_ENABLED", " TRUE ")
    assert Settings.from_env().chatwoot_resolved_close_enabled is True
