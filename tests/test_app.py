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
