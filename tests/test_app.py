"""HTTP contract tests for sanitary Johanna funnel observations."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from bridge.app import Settings, create_app

SECRET = "johanna-funnel-observability-secret"
EVENT_ID = "01K4N9YQ2T7W3H5J8M6P0R1SVC"
SESSION_ID = "01K4N9YQ2T7W3H5J8M6P0R1SVD"


class _FakeSupabase:
    def __init__(self, outcome: str = "inserted") -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def admit_johanna_funnel_event(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(outcome=self.outcome, event_id=EVENT_ID)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "webhook_secret": "unused",
        "allowed_jid": None,
        "capture_dir": Path("/tmp/johanna-funnel-observability-tests"),
        "max_age_seconds": 300,
        "lead_precheckout_secret": SECRET,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _settings_from_env(monkeypatch: pytest.MonkeyPatch) -> Settings:
    required_environment = {
        "CHATWOOT_WEBHOOK_SECRET": "unused",
        "CHATWOOT_AGENT_BOT_ID": "1",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_CONTROL_API_ACCESS_TOKEN": "unused",
        "CHATWOOT_PAUSE_MACRO_ID": "1",
        "CHATWOOT_INBOX_ID": "9",
    }
    for name, value in required_environment.items():
        monkeypatch.setenv(name, value)
    return Settings.from_env()


def _payload() -> dict[str, object]:
    return {
        "version": "1.0.0",
        "event_id": EVENT_ID,
        "event_type": "page_view",
        "occurred_at": datetime.now(UTC).isoformat(),
        "anonymous_session_id": SESSION_ID,
        "landing_ref": "ads-a",
        "offer_ref": "bxjge6zq",
        "utm": {
            "source": "meta",
            "medium": "paid_social",
            "campaign": "anxiety_vsl",
            "content": "creative_a",
            "term": None,
        },
    }


def _post(
    app: object,
    payload: object,
    *,
    signature: str | None = None,
    content_type: str = "application/json; charset=utf-8",
) -> httpx.Response:
    body = (
        payload
        if isinstance(payload, bytes)
        else json.dumps(payload, separators=(",", ":")).encode()
    )
    digest = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/webhooks/johanna-funnel-events",
                content=body,
                headers={
                    "Content-Type": content_type,
                    "X-Lancemos-Signature": signature or digest,
                },
            )

    return asyncio.run(send())


def test_signed_closed_event_is_admitted_without_pii_or_payload_envelope() -> None:
    supabase = _FakeSupabase()
    response = _post(create_app(_settings(), supabase_client=supabase), _payload())  # type: ignore[arg-type]

    assert response.status_code == 202
    assert response.json() == {"status": "received", "event_id": EVENT_ID}
    assert supabase.calls == [{
        "version": "1.0.0",
        "event_id": EVENT_ID,
        "event_type": "page_view",
        "occurred_at": supabase.calls[0]["occurred_at"],
        "anonymous_session_id": SESSION_ID,
        "landing_ref": "ads-a",
        "offer_ref": "bxjge6zq",
        "utm_source": "meta",
        "utm_medium": "paid_social",
        "utm_campaign": "anxiety_vsl",
        "utm_content": "creative_a",
        "utm_term": None,
    }]


def test_observability_ignores_removed_dedicated_secret_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOHANNA_FUNNEL_OBSERVABILITY_SECRET", SECRET)
    monkeypatch.delenv("LEAD_PRECHECKOUT_SECRET", raising=False)
    supabase = _FakeSupabase()
    response = _post(
        create_app(
            _settings_from_env(monkeypatch),
            supabase_client=supabase,  # type: ignore[arg-type]
        ),
        _payload(),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "johanna_funnel_not_enabled"
    assert supabase.calls == []


def test_existing_lead_secret_enables_observability_independently_of_lead_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JOHANNA_FUNNEL_OBSERVABILITY_SECRET", raising=False)
    monkeypatch.setenv("LEAD_PRECHECKOUT_SECRET", SECRET)
    monkeypatch.setenv("LEAD_PRECHECKOUT_ENABLED", "false")
    supabase = _FakeSupabase()

    response = _post(
        create_app(
            _settings_from_env(monkeypatch),
            supabase_client=supabase,  # type: ignore[arg-type]
        ),
        _payload(),
    )

    assert response.status_code == 202
    assert len(supabase.calls) == 1


def test_exact_replay_and_semantic_conflict_have_distinct_http_outcomes() -> None:
    duplicate = _post(
        create_app(_settings(), supabase_client=_FakeSupabase("duplicate")),  # type: ignore[arg-type]
        _payload(),
    )
    conflict = _post(
        create_app(_settings(), supabase_client=_FakeSupabase("semantic_conflict")),  # type: ignore[arg-type]
        _payload(),
    )

    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate", "event_id": EVENT_ID}
    assert conflict.status_code == 409
    assert conflict.json() == {"status": "conflict", "event_id": EVENT_ID}


def test_invalid_signature_is_rejected_before_json_and_rpc() -> None:
    supabase = _FakeSupabase()
    response = _post(
        create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
        b"not-json",
        signature="sha256=" + "0" * 64,
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_johanna_funnel_signature"
    assert supabase.calls == []


def test_closed_schema_rejects_pii_tracking_and_arbitrary_payload_before_rpc() -> None:
    for field, value in (
        ("email", "private@example.test"),
        ("phone", "+12025550123"),
        ("name", "Private Person"),
        ("fbclid", "tracking-id"),
        ("payload", {"anything": "goes"}),
    ):
        payload = _payload()
        payload[field] = value
        supabase = _FakeSupabase()

        response = _post(
            create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
            payload,
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "invalid_johanna_funnel_event"
        assert supabase.calls == []


def test_event_enums_ulids_pair_and_bounded_utm_fail_closed() -> None:
    mutations = (
        ("event_type", "purchase"),
        ("event_id", "not-a-ulid"),
        ("anonymous_session_id", "01k4n9yq2t7w3h5j8m6p0r1svd"),
        ("offer_ref", "mgbgpp19"),
    )
    for field, value in mutations:
        payload = _payload()
        payload[field] = value
        supabase = _FakeSupabase()
        response = _post(
            create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
            payload,
        )
        assert response.status_code == 400
        assert supabase.calls == []

    payload = _payload()
    payload["utm"]["campaign"] = "x" * 129  # type: ignore[index]
    supabase = _FakeSupabase()
    response = _post(
        create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
        payload,
    )
    assert response.status_code == 400
    assert supabase.calls == []


def test_event_older_than_the_maximum_dashboard_window_is_rejected() -> None:
    payload = _payload()
    payload["occurred_at"] = "2000-01-01T00:00:00Z"
    supabase = _FakeSupabase()

    response = _post(
        create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
        payload,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_johanna_funnel_event"
    assert supabase.calls == []


def test_event_more_than_five_minutes_in_the_future_is_rejected() -> None:
    payload = _payload()
    payload["occurred_at"] = (datetime.now(UTC) + timedelta(minutes=6)).isoformat()
    supabase = _FakeSupabase()

    response = _post(
        create_app(_settings(), supabase_client=supabase),  # type: ignore[arg-type]
        payload,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_johanna_funnel_event"
    assert supabase.calls == []


# ── Levantar la pausa: a quien si y a quien no ─────────────────────────

from bridge.app import _normalize_chatwoot_history, _resume_paused_conversation
from bridge.chatwoot import ChatwootProtocolError
from bridge.supabase import ConversationResumeResult, SupabaseError


AHORA = 1_700_000_000


def _mensajes_con_humano(hace_segundos: int) -> list[dict[str, object]]:
    return [
        {
            "message_type": 1,
            "private": False,
            "content": "yo sigo",
            "sender": {"type": "user", "id": 4},
            "created_at": AHORA - hace_segundos,
        }
    ]


class _ClienteFalso:
    def __init__(self, *, labels=("automation_paused",), status="open",
                 can_reply=True, assignee=False, mensajes=None):
        self._snapshot = SimpleNamespace(
            status=status,
            can_reply=can_reply,
            labels=tuple(labels),
            human_assignee_present=assignee,
        )
        self._mensajes = mensajes if mensajes is not None else []
        self.etiqueta_sacada = False

    async def get_canonical_conversation_snapshot(self, **_: object):
        return self._snapshot

    async def get_conversation_messages(self, **_: object):
        return self._mensajes

    async def clear_conversation_label(self, **_: object) -> bool:
        self.etiqueta_sacada = True
        return True


class _SupabaseFalso:
    def __init__(self, outcome: str = "resumed"):
        self.outcome = outcome
        self.llamadas: list[dict[str, object]] = []

    async def resume_paused_conversation(self, **kwargs: object):
        self.llamadas.append(kwargs)
        return ConversationResumeResult(
            outcome=self.outcome,
            conversation_id="uuid-conv",
            commercial_case_id="uuid-case",
            resume_event_id="uuid-event",
        )


def _settings_resume(quiet: int = 28_800) -> SimpleNamespace:
    return SimpleNamespace(
        chatwoot_inbox_id=9,
        conversation_resume_quiet_seconds=quiet,
        conversation_resume_max=3,
    )


def _correr(cliente, supabase, settings=None) -> bool:
    return asyncio.run(
        _resume_paused_conversation(
            control_client=cliente,
            supabase=supabase,
            settings=settings or _settings_resume(),
            conversation_id=124,
            message_id=8899,
        )
    )


def test_resume_lifts_both_layers_when_the_team_went_quiet(monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)
    cliente = _ClienteFalso(mensajes=_mensajes_con_humano(40_000))
    supabase = _SupabaseFalso()

    assert _correr(cliente, supabase) is True
    assert cliente.etiqueta_sacada is True
    assert supabase.llamadas[0]["quiet_seconds"] == 40_000
    assert supabase.llamadas[0]["command_key"] == "resume:124:8899"
    assert supabase.llamadas[0]["reason_code"] == "inbound_after_quiet_period"


def test_resume_waits_while_the_team_is_still_answering(monkeypatch) -> None:
    # Mariana escribio hace dos horas: el agente no se mete en el medio.
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)
    cliente = _ClienteFalso(mensajes=_mensajes_con_humano(7_200))
    supabase = _SupabaseFalso()

    assert _correr(cliente, supabase) is False
    assert supabase.llamadas == []
    assert cliente.etiqueta_sacada is False


def test_resume_proceeds_when_no_person_ever_wrote(monkeypatch) -> None:
    # Derivada y nunca atendida: no hay silencio que medir, y el agente es
    # mejor que el vacio.
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)
    supabase = _SupabaseFalso()
    assert _correr(_ClienteFalso(mensajes=[]), supabase) is True
    assert supabase.llamadas[0]["quiet_seconds"] is None


def test_resume_respects_the_opt_out_label(monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)
    cliente = _ClienteFalso(
        labels=("automation_paused", "automation_opted_out"),
        mensajes=_mensajes_con_humano(90_000),
    )
    supabase = _SupabaseFalso()
    assert _correr(cliente, supabase) is False
    assert supabase.llamadas == []


def test_resume_skips_an_assigned_or_closed_conversation(monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)
    for cliente in (
        _ClienteFalso(assignee=True, mensajes=_mensajes_con_humano(90_000)),
        _ClienteFalso(status="resolved", mensajes=_mensajes_con_humano(90_000)),
        _ClienteFalso(can_reply=False, mensajes=_mensajes_con_humano(90_000)),
        _ClienteFalso(labels=(), mensajes=_mensajes_con_humano(90_000)),
    ):
        supabase = _SupabaseFalso()
        assert _correr(cliente, supabase) is False
        assert supabase.llamadas == []


def test_resume_reports_false_when_the_durable_layer_refuses(monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)
    for outcome in ("blocked_contact", "blocked_pending_handoff",
                    "blocked_resume_limit", "not_found"):
        cliente = _ClienteFalso(mensajes=_mensajes_con_humano(90_000))
        assert _correr(cliente, _SupabaseFalso(outcome)) is False
        assert cliente.etiqueta_sacada is False


def test_resume_does_not_claim_success_if_the_label_survives(monkeypatch) -> None:
    # Sin sacar la etiqueta, la guarda pre-envio bloquea igual: no se reintenta
    # la admision para no fabricar una respuesta que nunca sale.
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)

    class _ClienteQueFalla(_ClienteFalso):
        async def clear_conversation_label(self, **_: object) -> bool:
            raise ChatwootProtocolError("macro_label_not_cleared")

    cliente = _ClienteQueFalla(mensajes=_mensajes_con_humano(90_000))
    assert _correr(cliente, _SupabaseFalso()) is False


def test_resume_fails_closed_when_supabase_errors(monkeypatch) -> None:
    monkeypatch.setattr("bridge.app.time.time", lambda: AHORA)

    class _SupabaseQueFalla(_SupabaseFalso):
        async def resume_paused_conversation(self, **_: object):
            raise SupabaseError("conversation_resume_failed: HTTP 500")

    cliente = _ClienteFalso(mensajes=_mensajes_con_humano(90_000))
    assert _correr(cliente, _SupabaseQueFalla()) is False
    assert cliente.etiqueta_sacada is False


def test_history_hides_team_messages_unless_resume_is_enabled() -> None:
    historia: list[dict[str, object]] = [
        {
            "message_type": 0,
            "private": False,
            "content": "Envíame el enlace",
            "sender": {"type": "contact", "id": 1},
            "created_at": 10,
            "id": 1,
        },
        {
            "message_type": 1,
            "private": False,
            "content": "aquí tienes el enlace https://pay.hotmart.com/X",
            "sender": {"type": "user", "id": 4},
            "created_at": 20,
            "id": 2,
        },
    ]

    sin_flag = _normalize_chatwoot_history(historia, agent_bot_id=1)
    assert [m["actor"] for m in sin_flag] == ["prospect"]

    con_flag = _normalize_chatwoot_history(
        historia, agent_bot_id=1, include_team_messages=True
    )
    assert [m["actor"] for m in con_flag] == ["prospect", "human_agent"]
    assert "pay.hotmart.com" in con_flag[1]["text"]
