from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from bridge.daily_feedback_service import (
    DailyFeedbackScheduler,
    DailyFeedbackSchedulerSettings,
    DailyFeedbackService,
    DailyFeedbackWebSettings,
    SlackIdentity,
    SlackOpenIdError,
    SlackOidcProvider,
    SupabaseDailyFeedbackRepository,
    _review_page,
    create_daily_feedback_review_app,
    validate_collection_lease_seconds,
    validate_internal_http_origin,
)
from bridge.daily_feedback_export import (
    DailyReviewPackage,
    ReviewConversation,
    ReviewMessage,
)


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.page: dict[str, object] = {
            "status": "item",
            "local_date": "2026-09-10",
            "item_count": 2,
            "decided_count": 0,
            "retention_expires_at": "2026-09-13T23:00:00Z",
            "item": {
                "item_id": "33333333-3333-4333-8333-333333333333",
                "position": 1,
                "display_label": "Conversación 01",
                "apparent_objective": "Revisar <objetivo>",
                "observed_outcome": "El prospecto continuó",
                "release_id": "release_lineage_unavailable",
                "release_version": 0,
                "messages": [
                    {
                        "actor": "prospect",
                        "occurred_at": "2026-09-10T12:00:00Z",
                        "text": "¿Qué incluye? <script>alert(1)</script>",
                    },
                    {
                        "actor": "agent",
                        "occurred_at": "2026-09-10T12:01:00Z",
                        "text": "Te explico el contenido.",
                    },
                ],
            },
        }

    async def rpc(self, name: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, payload))
        if name == "begin_daily_feedback_oidc_v1":
            return {"status": "created"}
        if name == "complete_daily_feedback_oidc_v1":
            return {
                "status": "authenticated",
                "return_path": "/daily-feedback/review/22222222-2222-4222-8222-222222222222",
                "public_ref": "22222222-2222-4222-8222-222222222222",
            }
        if name in {"get_daily_feedback_review_page_v1", "get_daily_feedback_review_page_v2"}:
            return self.page
        if name == "close_daily_feedback_batch_v1":
            return {"status": "closed", "decided_count": 1, "unreviewed_count": 1, "item_count": 2}
        if name == "record_daily_feedback_decision_v1":
            return {
                "status": "recorded",
                "decided_count": 1,
                "item_count": 2,
                "batch_complete": False,
            }
        raise AssertionError(name)


class FakeOidc:
    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return "https://slack.com/openid/connect/authorize?" + f"state={state}&redirect_uri={redirect_uri}"

    async def authenticate(self, *, code: str, redirect_uri: str) -> SlackIdentity:
        assert code == "authorization-code"
        assert redirect_uri == "https://reviews.example.test/daily-feedback/auth/slack/callback"
        return SlackIdentity(
            issuer="https://slack.com",
            subject="https://slack.com/user_id/U12345678",
            team_id="T12345678",
            user_id="U12345678",
        )


@dataclass(frozen=True)
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


def _client(
    *,
    oidc_client: SlackOidcProvider | None = None,
    brand_name: str | None = None,
) -> tuple[TestClient, FakeRepository]:
    repository = FakeRepository()
    brand = {} if brand_name is None else {"brand_name": brand_name}
    service = DailyFeedbackService(
        repository=repository,
        oidc_client=oidc_client or FakeOidc(),
        settings=DailyFeedbackWebSettings(
            public_origin="https://reviews.example.test",
            session_hmac_key=b"s" * 32,
            slack_team_id="T12345678",
            **brand,
        ),
        clock=Clock(datetime(2026, 9, 10, 23, 0, tzinfo=UTC)),
    )
    return TestClient(create_daily_feedback_review_app(service), base_url="https://reviews.example.test"), repository


def test_production_web_settings_default_to_host_prefixed_cookies() -> None:
    settings = DailyFeedbackWebSettings(
        public_origin="https://reviews.example.test",
        session_hmac_key=b"s" * 32,
        slack_team_id="T12345678",
    )

    assert settings.session_cookie_name == "__Host-daily_feedback_session"
    assert settings.oidc_state_cookie_name == "__Host-daily_feedback_oidc"


def test_review_requires_slack_auth_without_exposing_the_batch() -> None:
    client, _ = _client()

    response = client.get(
        "/review/22222222-2222-4222-8222-222222222222",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "Iniciar sesión con Slack" in response.text
    assert "Conversación 01" not in response.text
    assert '<body class="app-shell">' not in response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert response.headers["referrer-policy"] == "no-referrer"


def test_slack_oidc_creates_a_db_backed_session_and_returns_to_a_clean_url() -> None:
    client, repository = _client()
    batch_ref = "22222222-2222-4222-8222-222222222222"

    started = client.get(
        f"/auth/slack/start?batch_ref={batch_ref}",
        follow_redirects=False,
    )
    assert started.status_code == 303
    target = urlsplit(started.headers["location"])
    state = parse_qs(target.query)["state"][0]
    assert target.scheme == "https" and target.netloc == "slack.com"
    assert "__Host-daily_feedback_oidc" in started.cookies
    begin_name, begin_payload = repository.calls[-1]
    assert begin_name == "begin_daily_feedback_oidc_v1"
    assert begin_payload["p_state_hash"] == hashlib.sha256(state.encode()).hexdigest()
    assert begin_payload["p_public_ref"] == batch_ref

    completed = client.get(
        f"/auth/slack/callback?code=authorization-code&state={state}",
        follow_redirects=False,
    )

    assert completed.status_code == 303
    assert completed.headers["location"] == f"/daily-feedback/review/{batch_ref}"
    assert "__Host-daily_feedback_session" in completed.cookies
    complete_name, complete_payload = repository.calls[-1]
    assert complete_name == "complete_daily_feedback_oidc_v1"
    assert complete_payload["p_oidc_issuer"] == "https://slack.com"
    assert complete_payload["p_oidc_subject"] == "https://slack.com/user_id/U12345678"
    assert complete_payload["p_slack_team_id"] == "T12345678"
    assert complete_payload["p_slack_user_id"] == "U12345678"
    assert "session" not in completed.headers["location"]


def test_slack_callback_reports_a_safe_provider_failure_reference() -> None:
    class RejectedOidc(FakeOidc):
        async def authenticate(self, *, code: str, redirect_uri: str) -> SlackIdentity:
            raise SlackOpenIdError("OIDC-TOKEN-BAD-REDIRECT-URI")

    client, _ = _client(oidc_client=RejectedOidc())
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]

    response = client.get(
        f"/auth/slack/callback?code=authorization-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "OIDC-TOKEN-BAD-REDIRECT-URI" in response.text


def test_slack_callback_does_not_expose_unexpected_exception_text() -> None:
    class BrokenOidc(FakeOidc):
        async def authenticate(self, *, code: str, redirect_uri: str) -> SlackIdentity:
            raise RuntimeError("sensitive-provider-response")

    client, _ = _client(oidc_client=BrokenOidc())
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]

    response = client.get(
        f"/auth/slack/callback?code=authorization-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "OIDC-UNEXPECTED" in response.text
    assert "sensitive-provider-response" not in response.text


def test_slack_callback_does_not_reflect_a_well_shaped_unlisted_reference() -> None:
    class UnlistedOidc(FakeOidc):
        async def authenticate(self, *, code: str, redirect_uri: str) -> SlackIdentity:
            raise SlackOpenIdError("OIDC-SENSITIVE-FIXED-SHAPE-VALUE")

    client, _ = _client(oidc_client=UnlistedOidc())
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]

    response = client.get(
        f"/auth/slack/callback?code=authorization-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "OIDC-UNEXPECTED" in response.text
    assert "OIDC-SENSITIVE-FIXED-SHAPE-VALUE" not in response.text


def test_slack_callback_revalidates_a_mutated_reference_at_the_render_boundary() -> None:
    class MutatedOidc(FakeOidc):
        async def authenticate(self, *, code: str, redirect_uri: str) -> SlackIdentity:
            error = SlackOpenIdError("OIDC-TOKEN-REJECTED")
            error.reference = "sensitive-provider-response"
            raise error

    client, _ = _client(oidc_client=MutatedOidc())
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]

    response = client.get(
        f"/auth/slack/callback?code=authorization-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "OIDC-UNEXPECTED" in response.text
    assert "sensitive-provider-response" not in response.text


def test_slack_callback_rejects_allowlist_equal_str_subclasses() -> None:
    class ReflectingStr(str):
        def __str__(self) -> str:
            return "sensitive-reflected-payload"

    class MutatedOidc(FakeOidc):
        async def authenticate(self, *, code: str, redirect_uri: str) -> SlackIdentity:
            error = SlackOpenIdError("OIDC-TOKEN-REJECTED")
            error.reference = ReflectingStr("OIDC-TOKEN-REJECTED")
            raise error

    client, _ = _client(oidc_client=MutatedOidc())
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]

    response = client.get(
        f"/auth/slack/callback?code=authorization-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "OIDC-UNEXPECTED" in response.text
    assert "sensitive-reflected-payload" not in response.text


def test_slack_callback_reports_state_failure_without_echoing_state() -> None:
    client, _ = _client()
    state = "s" * 32

    response = client.get(
        f"/auth/slack/callback?code=authorization-code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "OIDC-STATE" in response.text
    assert state not in response.text


def test_host_prefixed_auth_cookies_use_the_required_root_path_when_mounted() -> None:
    repository = FakeRepository()
    service = DailyFeedbackService(
        repository=repository,
        oidc_client=FakeOidc(),
        settings=DailyFeedbackWebSettings(
            public_origin="https://reviews.example.test",
            session_hmac_key=b"s" * 32,
            slack_team_id="T12345678",
            session_cookie_name="__Host-daily_feedback_session",
            oidc_state_cookie_name="__Host-daily_feedback_oidc",
        ),
        clock=Clock(datetime(2026, 9, 10, 23, 0, tzinfo=UTC)),
    )
    outer = FastAPI()
    outer.mount("/daily-feedback", create_daily_feedback_review_app(service))
    client = TestClient(outer, base_url="https://reviews.example.test")

    started = client.get(
        "/daily-feedback/auth/slack/start?batch_ref=22222222-2222-4222-8222-222222222222",
        follow_redirects=False,
    )

    cookie = started.headers["set-cookie"]
    assert "__Host-daily_feedback_oidc=" in cookie
    assert "Path=/;" in cookie
    assert "Secure" in cookie and "HttpOnly" in cookie
    assert "Path=/daily-feedback" not in cookie


def test_supabase_repository_rejects_unlisted_daily_feedback_rpc_names() -> None:
    from bridge.daily_feedback_service import SupabaseDailyFeedbackRepository

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status": "unexpected"})

    repository = SupabaseDailyFeedbackRepository(
        base_url="https://project.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ValueError, match="daily_feedback_rpc_not_allowed"):
        __import__("asyncio").run(
            repository.rpc("complete_daily_feedback_backdoor_v1", {})
        )
    assert calls == 0


def test_supabase_repository_allows_durable_readiness_rpc() -> None:
    from bridge.daily_feedback_service import SupabaseDailyFeedbackRepository

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/v1/rpc/get_daily_feedback_readiness_v1"
        return httpx.Response(200, json={"status": "ok", "delivery_unknown_count": 0})

    repository = SupabaseDailyFeedbackRepository(
        base_url="https://project.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
    )
    result = __import__("asyncio").run(
        repository.rpc(
            "get_daily_feedback_readiness_v1",
            {
                "p_tenant_ref": "lancemos",
                "p_scope_ref": "psicologajohanna-agent-bot-19",
                "p_now": "2026-09-11T18:00:00Z",
            },
        )
    )
    assert result == {"status": "ok", "delivery_unknown_count": 0}


def test_review_renders_only_the_current_escaped_conversation() -> None:
    client, repository = _client()
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.get(
        f"/auth/slack/callback?code=authorization-code&state={state}",
        follow_redirects=False,
    )

    response = client.get(f"/review/{batch_ref}")

    assert response.status_code == 200
    assert "1 de 2" in response.text
    assert response.text.count('class="conversation"') == 1
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert "correct_with_feedback" in response.text
    rpc_name, rpc_payload = repository.calls[-1]
    assert rpc_name == "get_daily_feedback_review_page_v2"
    assert rpc_payload["p_public_ref"] == batch_ref
    assert isinstance(rpc_payload["p_session_hash"], str)
    # Sin parametros, el filtro por defecto: derivacion, respuesta de la IA e inbound.
    assert rpc_payload["p_facets"] == ["handoff", "model_reply", "inbound"]


def test_review_uses_chatwoot_inspired_operational_visual_system() -> None:
    client, _ = _client()
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.get(f"/auth/slack/callback?code=authorization-code&state={state}", follow_redirects=False)

    response = client.get(f"/review/{batch_ref}")

    assert response.status_code == 200
    assert '<body class="app-shell">' in response.text
    assert '<div class="workspace-mark" aria-hidden="true">' in response.text
    assert 'class="topbar"' in response.text
    assert 'class="review-layout"' in response.text
    assert 'class="chat-thread"' in response.text
    assert 'class="message message--prospect"' in response.text
    assert 'class="message message--agent"' in response.text
    assert 'class="review-panel"' in response.text
    assert "--cw-bg:#111214" in response.text
    assert "--cw-accent:#1976d2" in response.text
    assert "--cw-subtle:#898b95" in response.text
    assert "textarea::placeholder{color:#898b95}" in response.text
    assert "font-family:Inter,ui-sans-serif" in response.text
    assert 'role="progressbar"' in response.text
    assert 'aria-valuemin="1"' in response.text
    assert 'aria-valuemax="2"' in response.text
    assert 'aria-valuenow="1"' in response.text
    assert "Georgia" not in response.text
    assert "--accent:#176b4b" not in response.text


def test_review_makes_correction_feedback_an_explicit_progressive_choice() -> None:
    client, _ = _client()
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.get(f"/auth/slack/callback?code=authorization-code&state={state}", follow_redirects=False)

    response = client.get(f"/review/{batch_ref}")

    assert response.status_code == 200
    assert ">Está correcta</button>" in response.text
    assert '<button type="button" class="decision-option" data-reveal-feedback' in response.text
    assert ">Necesita corrección</button>" in response.text
    assert "Correcta con feedback" not in response.text
    assert response.text.index("Está correcta") < response.text.index("Necesita corrección")
    assert response.text.index("Necesita corrección") < response.text.index("Omitir por ahora")
    assert response.text.index("Omitir por ahora") < response.text.index("<textarea")
    assert '<div class="feedback-panel" hidden' in response.text
    assert '<textarea id="feedback" name="verbatim_feedback" maxlength="4000" required disabled' in response.text
    assert 'placeholder="Describí el problema y la respuesta esperada…"' in response.text
    assert 'data-reveal-feedback aria-expanded="false" aria-controls="feedback-panel"' in response.text
    assert "aria-pressed" not in response.text
    assert '<button class="primary save-correction" type="submit" name="decision" value="correct_with_feedback" disabled>Guardar corrección</button>' in response.text
    assert '<input type="hidden" name="verbatim_feedback" value="" data-empty-feedback>' in response.text
    assert 'type="submit" name="decision" value="correct" data-direct-decision>Está correcta</button>' in response.text
    assert 'type="submit" name="decision" value="skip" data-direct-decision>Omitir por ahora</button>' in response.text
    assert ">Omitir por ahora</button>" in response.text
    assert "<script>" in response.text
    assert "onclick=" not in response.text
    rendered_script = response.text.split("<script>", 1)[1].split("</script>", 1)[0]
    script_hash = base64.b64encode(
        hashlib.sha256(rendered_script.encode("utf-8")).digest()
    ).decode("ascii")
    content_security_policy = response.headers["content-security-policy"]
    assert f"script-src 'sha256-{script_hash}'" in content_security_policy
    assert "script-src 'unsafe-inline'" not in content_security_policy
    contract = Path("docs/contracts/daily-feedback-production-v1.md").read_text(
        encoding="utf-8"
    )
    assert f"Content-Security-Policy: {content_security_policy}" in contract


def test_decision_requires_origin_and_session_bound_csrf_then_preserves_feedback() -> None:
    client, repository = _client()
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.get(f"/auth/slack/callback?code=authorization-code&state={state}", follow_redirects=False)
    page = client.get(f"/review/{batch_ref}")
    csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    command_id = page.text.split('name="command_id" value="', 1)[1].split('"', 1)[0]
    feedback = "Responder primero; conservar ñ literalmente."

    rejected = client.post(
        f"/review/{batch_ref}/decisions",
        data={
            "csrf_token": csrf,
            "command_id": command_id,
            "item_id": "33333333-3333-4333-8333-333333333333",
            "decision": "correct_with_feedback",
            "verbatim_feedback": feedback,
        },
        headers={"Origin": "https://attacker.example"},
        follow_redirects=False,
    )
    assert rejected.status_code == 403

    accepted = client.post(
        f"/review/{batch_ref}/decisions",
        data={
            "csrf_token": csrf,
            "command_id": command_id,
            "item_id": "33333333-3333-4333-8333-333333333333",
            "decision": "correct_with_feedback",
            "verbatim_feedback": feedback,
        },
        headers={"Origin": "https://reviews.example.test"},
        follow_redirects=False,
    )

    assert accepted.status_code == 303
    assert accepted.headers["location"] == f"/daily-feedback/review/{batch_ref}"
    name, payload = repository.calls[-1]
    assert name == "record_daily_feedback_decision_v1"
    assert payload["p_verbatim_feedback"] == feedback
    assert payload["p_semantic_fingerprint"] == hashlib.sha256(
        (command_id + "\0" + batch_ref + "\0" + "33333333-3333-4333-8333-333333333333" + "\0" + "correct_with_feedback" + "\0" + feedback).encode()
    ).hexdigest()


def test_decision_accepts_browser_verified_same_origin_when_origin_is_omitted() -> None:
    client, repository = _client()
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.get(f"/auth/slack/callback?code=authorization-code&state={state}", follow_redirects=False)
    page = client.get(f"/review/{batch_ref}")
    csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    command_id = page.text.split('name="command_id" value="', 1)[1].split('"', 1)[0]

    accepted = client.post(
        f"/review/{batch_ref}/decisions",
        data={
            "csrf_token": csrf,
            "command_id": command_id,
            "item_id": "33333333-3333-4333-8333-333333333333",
            "decision": "correct_with_feedback",
            "verbatim_feedback": "  Feedback literal con ñ.  ",
        },
        headers={"Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )

    assert accepted.status_code == 303
    assert repository.calls[-1][0] == "record_daily_feedback_decision_v1"
    assert repository.calls[-1][1]["p_verbatim_feedback"] == "  Feedback literal con ñ.  "


def test_decision_accepts_chrome_null_origin_with_verified_same_origin_metadata() -> None:
    client, repository = _client()
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.get(f"/auth/slack/callback?code=authorization-code&state={state}", follow_redirects=False)
    page = client.get(f"/review/{batch_ref}")
    csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    command_id = page.text.split('name="command_id" value="', 1)[1].split('"', 1)[0]

    accepted = client.post(
        f"/review/{batch_ref}/decisions",
        data={
            "csrf_token": csrf,
            "command_id": command_id,
            "item_id": "33333333-3333-4333-8333-333333333333",
            "decision": "correct_with_feedback",
            "verbatim_feedback": "  Feedback literal con ñ.  ",
        },
        headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )

    assert accepted.status_code == 303
    assert repository.calls[-1][0] == "record_daily_feedback_decision_v1"
    assert repository.calls[-1][1]["p_verbatim_feedback"] == "  Feedback literal con ñ.  "


def test_decision_rejects_null_origin_without_same_origin_fetch_metadata() -> None:
    client, repository = _client()
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.get(f"/auth/slack/callback?code=authorization-code&state={state}", follow_redirects=False)
    page = client.get(f"/review/{batch_ref}")
    csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    command_id = page.text.split('name="command_id" value="', 1)[1].split('"', 1)[0]
    calls_before = len(repository.calls)

    rejected = client.post(
        f"/review/{batch_ref}/decisions",
        data={
            "csrf_token": csrf,
            "command_id": command_id,
            "item_id": "33333333-3333-4333-8333-333333333333",
            "decision": "correct_with_feedback",
            "verbatim_feedback": "Feedback",
        },
        headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )

    assert rejected.status_code == 403
    assert len(repository.calls) == calls_before


def test_decision_rejects_missing_origin_without_same_origin_fetch_metadata() -> None:
    client, _ = _client()
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.get(f"/auth/slack/callback?code=authorization-code&state={state}", follow_redirects=False)
    page = client.get(f"/review/{batch_ref}")
    csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    command_id = page.text.split('name="command_id" value="', 1)[1].split('"', 1)[0]

    rejected = client.post(
        f"/review/{batch_ref}/decisions",
        data={
            "csrf_token": csrf,
            "command_id": command_id,
            "item_id": "33333333-3333-4333-8333-333333333333",
            "decision": "correct_with_feedback",
            "verbatim_feedback": "Feedback",
        },
        follow_redirects=False,
    )

    assert rejected.status_code == 403


# Cuerpo crudo que mando Chrome 155 con un Enter en el comentario (ver _capture).
CHROME_CRLF_FORM = json.loads(
    (
        Path(__file__).parent / "fixtures" / "daily_feedback_review_form_chrome_crlf_20261016.json"
    ).read_text(encoding="utf-8")
)


def test_decision_accepts_the_browser_line_break_and_stores_it_as_lf() -> None:
    client, repository = _client()
    batch_ref = "22222222-2222-4222-8222-222222222222"
    item_id = "33333333-3333-4333-8333-333333333333"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.get(f"/auth/slack/callback?code=authorization-code&state={state}", follow_redirects=False)
    page = client.get(f"/review/{batch_ref}")
    csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    command_id = page.text.split('name="command_id" value="', 1)[1].split('"', 1)[0]
    body = (
        CHROME_CRLF_FORM["body"]
        .replace("csrf_token=CSRF", f"csrf_token={quote(csrf, safe='')}")
        .replace("command_id=CMD", f"command_id={command_id}")
        .replace("item_id=ITEM", f"item_id={item_id}")
    )
    assert "%0D%0A" in body

    accepted = client.post(
        f"/review/{batch_ref}/decisions",
        content=body.encode("ascii"),
        headers={
            "Content-Type": CHROME_CRLF_FORM["content_type"],
            "Origin": "https://reviews.example.test",
        },
        follow_redirects=False,
    )

    assert accepted.status_code == 303
    feedback = "faltó compartir el enlace que es este:\nhttps://pay.hotmart.com/X00000000Y?off=2uafw5bg"
    name, payload = repository.calls[-1]
    assert name == "record_daily_feedback_decision_v1"
    assert payload["p_verbatim_feedback"] == feedback
    assert payload["p_semantic_fingerprint"] == hashlib.sha256(
        (command_id + "\0" + batch_ref + "\0" + item_id + "\0" + "correct_with_feedback" + "\0" + feedback).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    ("feedback", "status", "stored"),
    [
        ("Primer renglon\rsegundo", 303, "Primer renglon\nsegundo"),
        ("Con\ttabulador", 303, "Con\ttabulador"),
        ("Nulo\x00adentro", 422, None),
        ("Tab vertical\x0b", 422, None),
        ("Escape\x1b[0m", 422, None),
    ],
)
def test_decision_keeps_rejecting_control_characters_other_than_line_breaks(
    feedback: str, status: int, stored: str | None
) -> None:
    client, repository = _client()
    batch_ref = "22222222-2222-4222-8222-222222222222"
    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.get(f"/auth/slack/callback?code=authorization-code&state={state}", follow_redirects=False)
    page = client.get(f"/review/{batch_ref}")
    csrf = page.text.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    command_id = page.text.split('name="command_id" value="', 1)[1].split('"', 1)[0]

    response = client.post(
        f"/review/{batch_ref}/decisions",
        data={
            "csrf_token": csrf,
            "command_id": command_id,
            "item_id": "33333333-3333-4333-8333-333333333333",
            "decision": "correct_with_feedback",
            "verbatim_feedback": feedback,
        },
        headers={"Origin": "https://reviews.example.test"},
        follow_redirects=False,
    )

    assert response.status_code == status
    decisions = [p for n, p in repository.calls if n == "record_daily_feedback_decision_v1"]
    if stored is None:
        assert "Decisión inválida." in response.text
        assert decisions == []
    else:
        assert decisions[-1]["p_verbatim_feedback"] == stored


class WorkflowRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.batch_id = "55555555-5555-4555-8555-555555555555"

    async def rpc(self, name: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, payload))
        responses = {
            "purge_expired_daily_feedback_v2": {"status": "purged", "count": 0},
            "claim_daily_feedback_collection_v1": {
                "status": "claimed",
                "schedule_id": "44444444-4444-4444-8444-444444444444",
                "tenant_ref": "lancemos",
                "scope_ref": "psicologajohanna-agent-bot-19",
                "chatwoot_account_id": 1,
                "chatwoot_inbox_id": 2,
                "chatwoot_agent_bot_id": 19,
                "local_date": "2026-09-10",
                "window_start": "2026-09-10T00:00:00Z",
                "window_end": "2026-09-10T23:00:00Z",
                "lease_generation": 3,
                "retention_hours": 72,
                "sanitizer_version": "deterministic-redaction-v1",
                "selection_version": "chatwoot-daily-agent-dialogues-v1",
                "renderer_version": "daily-feedback-web-v1",
            },
            "commit_daily_feedback_batch_v1": {
                "status": "committed",
                "batch_id": self.batch_id,
                "public_ref": "66666666-6666-4666-8666-666666666666",
                "item_count": 1,
                "retention_expires_at": "2026-09-13T23:00:00Z",
            },
            "claim_daily_feedback_notification_v1": {
                "status": "claimed",
                "batch_id": self.batch_id,
                "public_ref": "66666666-6666-4666-8666-666666666666",
                "tenant_ref": "lancemos",
                "scope_ref": "psicologajohanna-agent-bot-19",
                "item_count": 1,
                "local_date": "2026-09-10",
                "notification_occurred_at": "2026-09-10T22:55:00Z",
                "retention_expires_at": "2026-09-13T22:55:00Z",
                "lease_generation": 4,
            },
            "mark_daily_feedback_notification_started_v1": {"status": "request_started"},
            "complete_daily_feedback_notification_v1": {"status": "admitted"},
        }
        if name not in responses:
            raise AssertionError(name)
        return responses[name]


class FakeCollector:
    def __init__(self) -> None:
        self.verified = 0
        self.calls: list[dict[str, object]] = []

    def verify_access(self) -> None:
        self.verified += 1

    def collect(self, **kwargs) -> DailyReviewPackage:
        self.calls.append(kwargs)
        return DailyReviewPackage(
            schema_version="daily-feedback-review-package-v1",
            tenant_ref="lancemos",
            scope_ref="psicologajohanna-agent-bot-19",
            window_start=datetime(2026, 9, 10, tzinfo=UTC),
            window_end=datetime(2026, 9, 10, 23, tzinfo=UTC),
            conversations=(
                ReviewConversation(
                    conversation_ref="conv_1234567890abcdef1234",
                    display_label="Conversación 01",
                    messages=(
                        ReviewMessage(actor="prospect", occurred_at=datetime(2026, 9, 10, 12, tzinfo=UTC), text="Quiero conocer el programa", message_ref="msg_1234567890abcdef1234"),
                        ReviewMessage(actor="agent", occurred_at=datetime(2026, 9, 10, 12, 1, tzinfo=UTC), text="Claro, te explico cómo funciona", message_ref="msg_abcdef1234567890abcd"),
                    ),
                    apparent_objective="Revisar la respuesta del agente",
                    observed_outcome="El prospecto continuó la conversación",
                    release_id="release_lineage_unavailable",
                    release_version=0,
                ),
            ),
            retention_hours=72,
            deletion_owner="juan",
            storage_encryption_verified=True,
        )


class FakeProducer:
    def __init__(self) -> None:
        self.verified = 0
        self.commands = []

    async def verify_access(self) -> None:
        self.verified += 1

    async def admit(self, command):
        self.commands.append(command)
        return type("Receipt", (), {"status": "admitted"})()


def test_scheduler_preflight_verifies_chatwoot_and_slack_without_collecting() -> None:
    collector = FakeCollector()
    producer = FakeProducer()
    scheduler = DailyFeedbackScheduler(
        repository=WorkflowRepository(),
        collector=collector,
        producer=producer,
        settings=DailyFeedbackSchedulerSettings(
            worker_id="daily-feedback-worker-1",
            tenant_ref="lancemos",
            scope_ref="psicologajohanna-agent-bot-19",
            chatwoot_account_id=1,
            chatwoot_inbox_id=2,
            chatwoot_agent_bot_id=19,
            deletion_owner="juan",
        ),
        clock=Clock(datetime(2026, 9, 10, 23, 0, tzinfo=UTC)),
    )

    __import__("asyncio").run(scheduler.preflight())

    assert scheduler.healthy is True
    assert collector.verified == 1
    assert collector.calls == []
    assert producer.verified == 1
    assert producer.commands == []


def test_scheduler_uses_one_durable_path_for_collection_commit_and_slack() -> None:
    repository = WorkflowRepository()
    collector = FakeCollector()
    producer = FakeProducer()
    scheduler = DailyFeedbackScheduler(
        repository=repository,
        collector=collector,
        producer=producer,
        settings=DailyFeedbackSchedulerSettings(
            worker_id="daily-feedback-worker-1",
            tenant_ref="lancemos",
            scope_ref="psicologajohanna-agent-bot-19",
            chatwoot_account_id=1,
            chatwoot_inbox_id=2,
            chatwoot_agent_bot_id=19,
            deletion_owner="juan",
        ),
        clock=Clock(datetime(2026, 9, 10, 23, 0, tzinfo=UTC)),
    )

    result = __import__("asyncio").run(scheduler.run_once(force_collection=True))

    assert result == {"collected": True, "notified": True, "purged": 0}
    assert collector.calls == [{
        "tenant_ref": "lancemos",
        "scope_ref": "psicologajohanna-agent-bot-19",
        "window_start": datetime(2026, 9, 10, tzinfo=UTC),
        "window_end": datetime(2026, 9, 10, 23, tzinfo=UTC),
    }]
    assert len(producer.commands) == 1
    command = producer.commands[0]
    assert command.event_id == repository.batch_id
    assert command.event_code == "REV-001"
    assert command.review_ref == "66666666-6666-4666-8666-666666666666"
    assert command.count == 1
    assert command.occurred_at == datetime(2026, 9, 10, 22, 55, tzinfo=UTC)
    assert command.deadline_at == datetime(2026, 9, 13, 22, 55, tzinfo=UTC)
    committed_items = repository.calls[2][1]["p_items"]
    assert isinstance(committed_items, list)
    first_item = committed_items[0]
    assert isinstance(first_item, dict)
    assert first_item["release_id"] == "release_lineage_unavailable"
    assert first_item["release_version"] == 0
    assert sorted(first_item) == [
        "apparent_objective",
        "conversation_ref",
        "display_label",
        "messages",
        "observed_outcome",
        "release_id",
        "release_version",
    ]
    assert [name for name, _ in repository.calls] == [
        "purge_expired_daily_feedback_v2",
        "claim_daily_feedback_collection_v1",
        "commit_daily_feedback_batch_v1",
        "claim_daily_feedback_notification_v1",
        "mark_daily_feedback_notification_started_v1",
        "complete_daily_feedback_notification_v1",
    ]
    for name, payload in repository.calls:
        if name in {
            "purge_expired_daily_feedback_v2",
            "claim_daily_feedback_collection_v1",
            "claim_daily_feedback_notification_v1",
        }:
            assert payload["p_tenant_ref"] == "lancemos"
            assert payload["p_scope_ref"] == "psicologajohanna-agent-bot-19"
    assert repository.calls[0][1]["p_purge_actor_ref"] == "daily-feedback-worker-1"
    assert "p_deletion_owner" not in repository.calls[0][1]


def test_scheduler_rejects_durable_chatwoot_authority_mismatch_before_collection() -> None:
    class MismatchedAuthorityRepository(WorkflowRepository):
        async def rpc(self, name, payload):
            result = await super().rpc(name, payload)
            if name == "claim_daily_feedback_collection_v1":
                return {**result, "chatwoot_inbox_id": 999}
            return result

    collector = FakeCollector()
    scheduler = DailyFeedbackScheduler(
        repository=MismatchedAuthorityRepository(),
        collector=collector,
        producer=FakeProducer(),
        settings=DailyFeedbackSchedulerSettings(
            worker_id="daily-feedback-worker-1",
            tenant_ref="lancemos",
            scope_ref="psicologajohanna-agent-bot-19",
            chatwoot_account_id=1,
            chatwoot_inbox_id=2,
            chatwoot_agent_bot_id=19,
            deletion_owner="juan",
        ),
        clock=Clock(datetime(2026, 9, 10, 23, 0, tzinfo=UTC)),
    )

    with pytest.raises(RuntimeError, match="daily_feedback_collection_claim_scope_mismatch"):
        __import__("asyncio").run(scheduler.run_once(force_collection=True))

    assert collector.calls == []


def test_scheduler_health_fails_closed_after_a_background_iteration_error() -> None:
    async def scenario() -> None:
        import asyncio

        failure_observed = asyncio.Event()
        scheduler = DailyFeedbackScheduler(
            repository=WorkflowRepository(),
            collector=FakeCollector(),
            producer=FakeProducer(),
            settings=DailyFeedbackSchedulerSettings(
                worker_id="daily-feedback-worker-1",
                tenant_ref="lancemos",
                scope_ref="psicologajohanna-agent-bot-19",
                chatwoot_account_id=1,
                chatwoot_inbox_id=2,
                chatwoot_agent_bot_id=19,
                deletion_owner="juan",
                poll_interval_seconds=5,
            ),
            clock=Clock(datetime(2026, 9, 10, 23, 0, tzinfo=UTC)),
        )

        async def fail_iteration(*, force_collection: bool = False) -> dict[str, object]:
            failure_observed.set()
            raise RuntimeError("database unavailable")

        scheduler.run_once = fail_iteration  # type: ignore[method-assign]
        await scheduler.start()
        try:
            await asyncio.wait_for(failure_observed.wait(), timeout=1)
            await asyncio.sleep(0)
            assert scheduler.healthy is False
        finally:
            await scheduler.stop()

    __import__("asyncio").run(scenario())


def test_slack_oidc_uses_authorization_code_then_verifies_userinfo() -> None:
    import httpx

    from bridge.daily_feedback_service import SlackOpenIdClient

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("openid.connect.token"):
            return httpx.Response(200, json={"ok": True, "access_token": "temporary-access"})
        assert request.headers["authorization"] == "Bearer temporary-access"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "sub": "U12345678",
                "https://slack.com/team_id": "T12345678",
                "https://slack.com/user_id": "U12345678",
            },
        )

    client = SlackOpenIdClient(
        client_id="client-id",
        client_secret="client-secret",
        transport=httpx.MockTransport(handler),
    )
    authorization_url = client.authorization_url(
        state="opaque-state",
        redirect_uri="https://reviews.example.test/daily-feedback/auth/slack/callback",
    )
    authorization_query = parse_qs(urlsplit(authorization_url).query)
    assert authorization_query["state"] == ["opaque-state"]
    assert authorization_query["scope"] == ["openid profile"]

    identity = __import__("asyncio").run(
        client.authenticate(
            code="one-time-code",
            redirect_uri="https://reviews.example.test/daily-feedback/auth/slack/callback",
        )
    )

    assert identity.issuer == "https://slack.com"
    assert identity.subject == "https://slack.com/user_id/U12345678"
    assert identity.team_id == "T12345678"
    assert identity.user_id == "U12345678"
    assert [request.url.path for request in requests] == [
        "/api/openid.connect.token",
        "/api/openid.connect.userInfo",
    ]


@pytest.mark.parametrize(
    "slack_subject",
    [None, "not-a-slack-user", "https://slack.com/user_id/U12345678", "U87654321"],
)
def test_slack_oidc_rejects_noncanonical_or_mismatched_subject(
    slack_subject: object,
) -> None:
    from bridge.daily_feedback_service import SlackOpenIdClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid.connect.token"):
            return httpx.Response(200, json={"ok": True, "access_token": "temporary-access"})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "sub": slack_subject,
                "https://slack.com/team_id": "T12345678",
                "https://slack.com/user_id": "U12345678",
            },
        )

    client = SlackOpenIdClient(
        client_id="client-id",
        client_secret="client-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SlackOpenIdError, match="^OIDC-IDENTITY-PAYLOAD$"):
        __import__("asyncio").run(
            client.authenticate(
                code="one-time-code",
                redirect_uri="https://reviews.example.test/daily-feedback/auth/slack/callback",
            )
        )


def test_slack_oidc_classifies_a_bad_redirect_uri_without_exposing_the_payload() -> None:
    from bridge.daily_feedback_service import SlackOpenIdClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("openid.connect.token")
        return httpx.Response(
            200,
            json={"ok": False, "error": "bad_redirect_uri", "detail": "do-not-expose"},
        )

    client = SlackOpenIdClient(
        client_id="client-id",
        client_secret="client-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SlackOpenIdError, match="^OIDC-TOKEN-BAD-REDIRECT-URI$"):
        __import__("asyncio").run(
            client.authenticate(
                code="one-time-code",
                redirect_uri="https://reviews.example.test/daily-feedback/auth/slack/callback",
            )
        )


@pytest.mark.parametrize(
    "token_body",
    [{"ok": "false", "access_token": "token"}, {"access_token": "token"}],
)
def test_slack_oidc_rejects_token_payload_without_exact_true(
    token_body: dict[str, object],
) -> None:
    from bridge.daily_feedback_service import SlackOpenIdClient

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=token_body)

    client = SlackOpenIdClient(
        client_id="client-id",
        client_secret="client-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SlackOpenIdError, match="^OIDC-TOKEN-REJECTED$"):
        __import__("asyncio").run(
            client.authenticate(
                code="one-time-code",
                redirect_uri="https://reviews.example.test/callback",
            )
        )
    assert [request.url.path for request in requests] == ["/api/openid.connect.token"]


def test_slack_oidc_rejects_userinfo_without_exact_true() -> None:
    from bridge.daily_feedback_service import SlackOpenIdClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid.connect.token"):
            return httpx.Response(200, json={"ok": True, "access_token": "temporary-access"})
        return httpx.Response(
            200,
            json={
                "ok": False,
                "sub": "https://slack.com/user_id/U12345678",
                "https://slack.com/team_id": "T12345678",
                "https://slack.com/user_id": "U12345678",
            },
        )

    client = SlackOpenIdClient(
        client_id="client-id",
        client_secret="client-secret",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(SlackOpenIdError, match="^OIDC-USERINFO-REJECTED$"):
        __import__("asyncio").run(
            client.authenticate(
                code="one-time-code",
                redirect_uri="https://reviews.example.test/callback",
            )
        )


def test_supabase_repository_retries_the_exact_rpc_after_transport_uncertainty() -> None:
    import httpx

    from bridge.daily_feedback_service import SupabaseDailyFeedbackRepository

    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if len(bodies) == 1:
            raise httpx.ReadTimeout("ambiguous", request=request)
        return httpx.Response(200, json={"status": "committed"})

    repository = SupabaseDailyFeedbackRepository(
        base_url="https://project.supabase.co",
        service_role_key="not-a-real-secret",
        transport=httpx.MockTransport(handler),
    )
    payload = {
        "p_command_id": "99999999-9999-4999-8999-999999999999",
        "p_semantic_fingerprint": "f" * 64,
    }

    result = __import__("asyncio").run(
        repository.rpc("commit_daily_feedback_batch_v1", payload)
    )

    assert result == {"status": "committed"}
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]


def test_scheduler_closes_the_owned_notification_client() -> None:
    class ClosableProducer(FakeProducer):
        def __init__(self) -> None:
            super().__init__()
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    producer = ClosableProducer()
    scheduler = DailyFeedbackScheduler(
        repository=WorkflowRepository(),
        collector=FakeCollector(),
        producer=producer,
        settings=DailyFeedbackSchedulerSettings(
            worker_id="daily-feedback-worker-1",
            tenant_ref="lancemos",
            scope_ref="psicologajohanna-agent-bot-19",
            chatwoot_account_id=1,
            chatwoot_inbox_id=2,
            chatwoot_agent_bot_id=19,
            deletion_owner="juan",
        ),
    )

    __import__("asyncio").run(scheduler.stop())

    assert producer.closed == 1


# --- La revision en una instancia autohospedada (ATT1) ------------------------------
# La parte del servicio: el origen interno de la base, la marca de la pagina y la
# lease de la recoleccion. Sin los parametros nuevos, todo queda como hoy (Johanna).


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("http://att1-gateway:8080", "http://att1-gateway:8080"),
        ("http://att1-gateway:8080/", "http://att1-gateway:8080"),
        # el nombre completo de un servicio de Swarm (<stack>_<servicio>) lleva guion bajo
        ("http://setter-att1_att1-gateway:8080", "http://setter-att1_att1-gateway:8080"),
        ("http://" + "a" * 63 + ":1", "http://" + "a" * 63 + ":1"),
        ("http://db:65535", "http://db:65535"),
    ],
)
def test_internal_http_origin_accepts_a_single_label_service_with_port(
    origin: str, expected: str
) -> None:
    assert validate_internal_http_origin(origin) == expected


@pytest.mark.parametrize(
    "origin",
    [
        "http://10.0.0.5:8080",  # una IP
        "http://x.host:8080",  # un dominio
        "http://att1-gateway",  # sin puerto
        "http://att1-gateway:",  # con el puerto vacio
        "http://att1-gateway.:8080",  # un nombre absoluto, con el punto final
        "http://[::1]:8080",  # IPv6
        "http://167772165:8080",  # 10.0.0.5 escrita como numero decimal
        "http://0xa000005:8080",  # 10.0.0.5 en hexadecimal
        "http://-gateway:8080",  # empieza con guion
        "http://" + "a" * 64 + ":8080",  # una etiqueta de mas de 63 caracteres
        "https://att1-gateway:8080",  # https no es el origen interno
        "ftp://att1-gateway:8080",
        "http://user:pass@att1-gateway:8080",  # credenciales
        "http://user@att1-gateway:8080",
        "http://@att1-gateway:8080",
        "http://att1-gateway:8080/rest",  # ruta
        "http://att1-gateway:8080//",
        "http://att1-gateway:8080?x=1",  # query
        "http://att1-gateway:8080?",
        "http://att1-gateway:8080#f",  # fragmento
        "http://att1-gateway:8080#",
        "http://att1-gateway:0",  # puerto fuera de rango
        "http://att1-gateway:65536",
        "http://att1-gateway:08080",  # no canonico: urlsplit lo lee como 8080
        "http://att1-gateway:+80",
        "HTTP://att1-gateway:8080",  # urlsplit lo pasa a minusculas
        "http://ATT1-gateway:8080",
        "http://att1-\ngateway:8080",  # urlsplit borra el salto de linea en silencio
        "http://att1-gateway:8080\t",
        " http://att1-gateway:8080",
        # urlsplit levanta su propio error con estos: sale igual el codigo de la regla
        "http://[att1-gateway]:8080",  # entre corchetes, y no es una IP
        "http://[::1:8080",  # un corchete sin cerrar
        "http://att1-gateway]:8080",
        "",
        None,
        b"http://att1-gateway:8080",
    ],
)
def test_internal_http_origin_rejects_everything_else(origin: object) -> None:
    with pytest.raises(ValueError, match="^invalid_supabase_internal_origin$"):
        validate_internal_http_origin(origin)


_ATT1_READINESS = {
    "p_tenant_ref": "lancemos",
    "p_scope_ref": "att1-agent-bot",
    "p_now": "2026-10-10T03:00:00Z",
}


def test_supabase_repository_accepts_internal_http_only_when_explicit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "delivery_unknown_count": 0})

    # Sin el permiso, igual que hoy: el http interno de ATT1 no pasa.
    with pytest.raises(ValueError, match="^invalid_supabase_origin$"):
        SupabaseDailyFeedbackRepository(
            base_url="http://att1-gateway:8080",
            service_role_key="secret",
            transport=httpx.MockTransport(handler),
        )

    repository = SupabaseDailyFeedbackRepository(
        base_url="http://att1-gateway:8080/",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
        allow_internal_http=True,
    )
    result = __import__("asyncio").run(
        repository.rpc("get_daily_feedback_readiness_v1", _ATT1_READINESS)
    )

    assert result == {"status": "ok", "delivery_unknown_count": 0}
    assert [str(request.url) for request in requests] == [
        "http://att1-gateway:8080/rest/v1/rpc/get_daily_feedback_readiness_v1"
    ]


@pytest.mark.parametrize(
    "base_url",
    [
        "http://x.host:8080",
        "http://10.0.0.5:8080",
        "http://att1-gateway",
        "http://user:pass@att1-gateway:8080",
        "http://att1-gateway:8080/rest/v1",
        # el repositorio lee el origen con urlsplit antes de la regla
        "http://[att1-gateway]:8080",
        "http://[::1:8080",
    ],
)
def test_supabase_repository_internal_http_still_requires_the_internal_origin(
    base_url: str,
) -> None:
    with pytest.raises(ValueError, match="^invalid_supabase_internal_origin$"):
        SupabaseDailyFeedbackRepository(
            base_url=base_url,
            service_role_key="secret",
            allow_internal_http=True,
        )


def test_supabase_repository_internal_http_permission_keeps_https_and_needs_a_bool() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "delivery_unknown_count": 0})

    # El permiso no obliga: con https el repositorio sigue como siempre.
    repository = SupabaseDailyFeedbackRepository(
        base_url="https://project.supabase.co",
        service_role_key="secret",
        transport=httpx.MockTransport(handler),
        allow_internal_http=True,
    )
    __import__("asyncio").run(
        repository.rpc("get_daily_feedback_readiness_v1", _ATT1_READINESS)
    )
    assert [str(request.url) for request in requests] == [
        "https://project.supabase.co/rest/v1/rpc/get_daily_feedback_readiness_v1"
    ]

    # Un texto como "false" no es el permiso: tiene que ser un bool.
    with pytest.raises(ValueError, match="^invalid_daily_feedback_supabase_internal_http$"):
        SupabaseDailyFeedbackRepository(
            base_url="http://att1-gateway:8080",
            service_role_key="secret",
            allow_internal_http="false",  # type: ignore[arg-type]
        )


def _captured_review_page(position: int) -> dict[str, object]:
    """La pagina v2 armada con las capturas reales del inbox 9 de Johanna, como en
    test_daily_feedback_review_context. Se importa aca adentro porque ese modulo
    importa este.
    """
    from bridge.daily_feedback_export import apply_conversation_context
    from bridge.daily_feedback_service import _package_items
    from test_daily_feedback_review_context import (
        _claim_v2,
        _collect_v2,
        _supabase_contexts,
    )

    package = apply_conversation_context(_collect_v2(), _supabase_contexts())
    items, _ = _package_items(package, _claim_v2())
    return {
        "status": "item",
        "local_date": "2026-09-26",
        "item_count": len(items),
        "decided_count": 0,
        "item": {
            **items[position],
            "item_id": "33333333-3333-4333-8333-333333333333",
            "position": position + 1,
        },
    }


def test_review_page_renders_the_configured_brand_escaped() -> None:
    page = _captured_review_page(0)
    common = {
        "public_ref": "22222222-2222-4222-8222-222222222222",
        "csrf_token": "csrf",
        "command_id": "44444444-4444-4444-8444-444444444444",
        "display_timezone": "America/Mexico_City",
    }

    nina = _review_page(page, **common, brand_name="Dra. Nina Garza")
    assert '<span class="brand-name">Dra. Nina Garza</span>' in nina
    # La conversacion capturada nombra a Johanna en su contenido; lo que no puede
    # quedar es la marca de Johanna en la barra.
    assert '<span class="brand-name">Johanna</span>' not in nina
    assert nina.count('class="brand-name"') == 1

    hostile = _review_page(page, **common, brand_name="<b>x</b>")
    assert '<span class="brand-name">&lt;b&gt;x&lt;/b&gt;</span>' in hostile
    assert "<b>x</b>" not in hostile

    assert '<span class="brand-name">Johanna</span>' in _review_page(page, **common)


def test_review_route_shows_the_instance_brand_only_on_the_review_page() -> None:
    client, _ = _client(brand_name="Dra. Nina Garza")
    batch_ref = "22222222-2222-4222-8222-222222222222"

    login = client.get(f"/review/{batch_ref}", follow_redirects=False)
    assert login.status_code == 401
    assert "Dra. Nina Garza" not in login.text
    assert "Johanna" not in login.text

    started = client.get(f"/auth/slack/start?batch_ref={batch_ref}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.get(f"/auth/slack/callback?code=authorization-code&state={state}", follow_redirects=False)
    response = client.get(f"/review/{batch_ref}")

    assert response.status_code == 200
    assert '<span class="brand-name">Dra. Nina Garza</span>' in response.text
    assert "Johanna" not in response.text


def _web_settings(**overrides: object) -> DailyFeedbackWebSettings:
    values: dict[str, object] = {
        "public_origin": "https://reviews.example.test",
        "session_hmac_key": b"s" * 32,
        "slack_team_id": "T12345678",
    }
    values.update(overrides)
    return DailyFeedbackWebSettings(**values)  # type: ignore[arg-type]


def test_web_settings_default_to_the_johanna_brand() -> None:
    assert _web_settings().brand_name == "Johanna"


@pytest.mark.parametrize(
    "brand_name",
    ["J", "x" * 60, "Dra. Nina Garza", "<b>x</b>", "Psicóloga Johanna"],
)
def test_web_settings_accept_a_printable_brand_of_1_to_60_characters(
    brand_name: str,
) -> None:
    assert _web_settings(brand_name=brand_name).brand_name == brand_name


@pytest.mark.parametrize(
    "brand_name",
    [
        "",
        "x" * 61,
        "   ",
        "Dra.\nNina",
        "\tJohanna",
        "Jo\x00hanna",
        "Jo\x7fhanna",
        "Jo\u202ehanna",  # el control que da vuelta el texto
        None,
        7,
        b"Johanna",
    ],
)
def test_web_settings_reject_an_empty_long_or_control_brand(brand_name: object) -> None:
    with pytest.raises(ValueError, match="^invalid_daily_feedback_brand_name$"):
        _web_settings(brand_name=brand_name)


def _scheduler_settings(**overrides: object) -> DailyFeedbackSchedulerSettings:
    values: dict[str, object] = {
        "worker_id": "daily-feedback-worker-1",
        "tenant_ref": "lancemos",
        "scope_ref": "psicologajohanna-agent-bot-19",
        "chatwoot_account_id": 1,
        "chatwoot_inbox_id": 2,
        "chatwoot_agent_bot_id": 19,
        "deletion_owner": "juan",
    }
    values.update(overrides)
    return DailyFeedbackSchedulerSettings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(("lease", "expected"), [(None, 120), (30, 30), (900, 900)])
def test_scheduler_sends_the_configured_collection_lease_to_the_claim(
    lease: int | None, expected: int
) -> None:
    repository = WorkflowRepository()
    overrides = {} if lease is None else {"collection_lease_seconds": lease}
    scheduler = DailyFeedbackScheduler(
        repository=repository,
        collector=FakeCollector(),
        producer=FakeProducer(),
        settings=_scheduler_settings(**overrides),
        clock=Clock(datetime(2026, 9, 10, 23, 0, tzinfo=UTC)),
    )

    __import__("asyncio").run(scheduler.run_once(force_collection=True))

    payloads = dict(repository.calls)
    assert payloads["claim_daily_feedback_collection_v1"]["p_lease_seconds"] == expected
    # La de la notificacion es otro tiempo (la admision en Slack) y no cambia.
    assert payloads["claim_daily_feedback_notification_v1"]["p_lease_seconds"] == 120


@pytest.mark.parametrize("lease", [29, 901, 0, -120, 120.0, True, "120", None])
def test_scheduler_settings_reject_a_collection_lease_the_sql_would_refuse(
    lease: object,
) -> None:
    with pytest.raises(
        ValueError, match="^invalid_daily_feedback_collection_lease_seconds$"
    ):
        _scheduler_settings(collection_lease_seconds=lease)
    with pytest.raises(
        ValueError, match="^invalid_daily_feedback_collection_lease_seconds$"
    ):
        validate_collection_lease_seconds(lease)


# --- Filtros de la revision y cierre del lote (2026-10-15) --------------------

from bridge.daily_feedback_service import (  # noqa: E402
    DEFAULT_REVIEW_FACETS,
    REVIEW_CLOSURE_ACKNOWLEDGEMENT,
    parse_review_filter,
    review_filter_suffix,
)

BATCH_REF = "22222222-2222-4222-8222-222222222222"
# La cola que devuelve get_daily_feedback_review_page_v2 (migracion 20261015000100),
# con la mezcla del lote de ATT1 del 2026-10-13: 28 conversaciones, 7 opt-outs.
QUEUE = {
    "pending": 24,
    "matching": 18,
    "faceted": True,
    "filtering": True,
    "facets": {"handoff": 10, "model_reply": 9, "payment_link": 17, "inbound": 3, "opt_out": 7},
}


def _logged_in(page: dict[str, object] | None = None) -> tuple[TestClient, FakeRepository]:
    client, repository = _client()
    if page is not None:
        repository.page = page
    started = client.get(f"/auth/slack/start?batch_ref={BATCH_REF}", follow_redirects=False)
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.get(f"/auth/slack/callback?code=authorization-code&state={state}", follow_redirects=False)
    return client, repository


def _page_with_queue(**queue: object) -> dict[str, object]:
    page = dict(FakeRepository().page)
    page["queue"] = {**QUEUE, **queue}
    return page


def test_parse_review_filter_defaults_all_and_ignores_unknown_values() -> None:
    assert parse_review_filter([]) == DEFAULT_REVIEW_FACETS == ("handoff", "model_reply", "inbound")
    assert parse_review_filter([("ver", "opt_out")]) == ("opt_out",)
    # El orden es el canonico y no se repite.
    assert parse_review_filter([("ver", "inbound"), ("ver", "handoff"), ("ver", "handoff")]) == ("handoff", "inbound")
    assert parse_review_filter([("ver", "todas")]) is None
    assert parse_review_filter([("ver", "opt_out"), ("ver", "todas")]) is None
    # El formulario con todas las casillas desmarcadas es ver todas.
    assert parse_review_filter([("filtro", "1")]) is None
    assert parse_review_filter([("ver", "<script>")]) == DEFAULT_REVIEW_FACETS
    assert review_filter_suffix(DEFAULT_REVIEW_FACETS) == ""
    assert review_filter_suffix(("opt_out",)) == "?ver=opt_out"
    assert review_filter_suffix(None) == "?ver=todas"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", ["handoff", "model_reply", "inbound"]),
        ("?ver=opt_out", ["opt_out"]),
        ("?filtro=1&ver=handoff&ver=opt_out", ["handoff", "opt_out"]),
        ("?ver=todas", None),
        ("?filtro=1", None),
    ],
)
def test_review_asks_the_filtered_page_with_the_chosen_facets(query: str, expected: list[str] | None) -> None:
    client, repository = _logged_in(_page_with_queue())

    response = client.get(f"/review/{BATCH_REF}{query}")

    assert response.status_code == 200
    name, payload = repository.calls[-1]
    assert name == "get_daily_feedback_review_page_v2"
    assert payload["p_facets"] == expected


def test_review_renders_the_filter_menu_with_counts_and_keeps_the_filter_in_the_decision() -> None:
    client, _ = _logged_in(_page_with_queue(matching=6))

    response = client.get(f"/review/{BATCH_REF}?ver=opt_out")

    assert response.status_code == 200
    text = response.text
    assert '<details class="filter-menu"><summary>Ver: opt-out</summary>' in text
    assert '<input type="checkbox" name="ver" value="opt_out" checked>' in text
    assert '<input type="checkbox" name="ver" value="handoff">' in text
    assert '<span>Con derivación</span><span class="filter-count">10</span>' in text
    assert '<span>Opt-out</span><span class="filter-count">7</span>' in text
    assert '<span>Con link de pago de la IA</span><span class="filter-count">17</span>' in text
    assert "6 de 24 pendientes con este filtro" in text
    assert f'href="/daily-feedback/review/{BATCH_REF}?ver=todas">Ver todas</a>' in text
    assert f'action="/daily-feedback/review/{BATCH_REF}/decisions?ver=opt_out"' in text
    # El cierre esta en el panel, plegado, con la casilla obligatoria.
    assert '<details class="close-batch"><summary>Cerrar el reporte sin revisar las que quedan</summary>' in text
    assert '<input type="checkbox" name="acknowledgement" value="acepto" required>' in text
    assert REVIEW_CLOSURE_ACKNOWLEDGEMENT in text
    assert "Quedan 24 sin revisar" in text
    assert f'action="/daily-feedback/review/{BATCH_REF}/close"' in text
    # Las dos formas llevan su propio comando.
    assert text.count('name="command_id"') == 2


def test_review_with_the_default_filter_keeps_clean_urls() -> None:
    client, _ = _logged_in(_page_with_queue())

    text = client.get(f"/review/{BATCH_REF}").text

    assert '<summary>Ver: derivación, respuesta de la IA, inbound</summary>' in text
    assert f'action="/daily-feedback/review/{BATCH_REF}/decisions"' in text
    assert '<input type="checkbox" name="ver" value="opt_out">' in text


def test_review_of_a_batch_built_before_the_filters_says_it_shows_everything() -> None:
    client, _ = _logged_in(_page_with_queue(faceted=False, filtering=False, facets={}))

    text = client.get(f"/review/{BATCH_REF}").text

    assert "Este lote se armó antes de los filtros: se ven todas las conversaciones." in text
    assert 'class="filter-menu"' not in text


def test_review_falls_back_to_the_first_page_when_the_base_lacks_the_migration() -> None:
    client, repository = _logged_in()
    original = repository.rpc

    async def rpc(name: str, payload: dict[str, object]) -> dict[str, object]:
        if name == "get_daily_feedback_review_page_v2":
            repository.calls.append((name, payload))
            request = httpx.Request("POST", "https://supabase.example.test/rest/v1/rpc/" + name)
            raise httpx.HTTPStatusError(
                "not found", request=request, response=httpx.Response(404, request=request)
            )
        return await original(name, payload)

    repository.rpc = rpc  # type: ignore[method-assign]

    response = client.get(f"/review/{BATCH_REF}")

    assert response.status_code == 200
    assert [name for name, _ in repository.calls[-2:]] == [
        "get_daily_feedback_review_page_v2",
        "get_daily_feedback_review_page_v1",
    ]
    # Sin la migracion no hay filtros ni cierre: la pagina es la de siempre.
    assert 'class="filter-bar"' not in response.text
    assert 'class="close-batch"' not in response.text
    assert ">Está correcta</button>" in response.text


def test_review_turns_any_other_page_error_into_the_login_page() -> None:
    client, repository = _logged_in()

    async def rpc(name: str, payload: dict[str, object]) -> dict[str, object]:
        request = httpx.Request("POST", "https://supabase.example.test/rest/v1/rpc/" + name)
        raise httpx.HTTPStatusError("bad", request=request, response=httpx.Response(400, request=request))

    repository.rpc = rpc  # type: ignore[method-assign]

    assert client.get(f"/review/{BATCH_REF}").status_code == 401


def test_filtered_out_page_offers_the_rest_and_the_closure() -> None:
    page = {
        "status": "filtered_out",
        "local_date": "2026-10-13",
        "item_count": 28,
        "decided_count": 18,
        "retention_expires_at": "2026-10-17T00:00:00Z",
        "queue": {**QUEUE, "pending": 10, "matching": 0, "facets": {"opt_out": 6}},
    }
    client, _ = _logged_in(page)

    response = client.get(f"/review/{BATCH_REF}")

    assert response.status_code == 200
    text = response.text
    assert "<h1>No quedan conversaciones con este filtro</h1>" in text
    assert "Quedan 10 de otros tipos." in text
    assert f'<a class="filter-link" href="/daily-feedback/review/{BATCH_REF}?ver=todas">ver las 10 que quedan</a>' in text
    assert '<input type="checkbox" name="acknowledgement" value="acepto" required>' in text
    assert "Quedan 10 sin revisar: se cierran sin feedback y ya no se pueden revisar." in text
    assert 'class="conversation"' not in text


def _close_form(client: TestClient) -> tuple[str, str]:
    text = client.get(f"/review/{BATCH_REF}").text
    form = text.split('class="close-form"', 1)[1]
    csrf = form.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    command_id = form.split('name="command_id" value="', 1)[1].split('"', 1)[0]
    return csrf, command_id


def test_closing_the_batch_requires_the_checked_approval_origin_and_csrf() -> None:
    client, repository = _logged_in(_page_with_queue())
    csrf, command_id = _close_form(client)
    same_origin = {"Origin": "https://reviews.example.test"}
    calls_before = len(repository.calls)

    unchecked = client.post(
        f"/review/{BATCH_REF}/close",
        data={"csrf_token": csrf, "command_id": command_id},
        headers=same_origin,
        follow_redirects=False,
    )
    wrong_value = client.post(
        f"/review/{BATCH_REF}/close",
        data={"csrf_token": csrf, "command_id": command_id, "acknowledgement": "si"},
        headers=same_origin,
        follow_redirects=False,
    )
    bad_csrf = client.post(
        f"/review/{BATCH_REF}/close",
        data={"csrf_token": "x" * 43, "command_id": command_id, "acknowledgement": "acepto"},
        headers=same_origin,
        follow_redirects=False,
    )
    foreign = client.post(
        f"/review/{BATCH_REF}/close",
        data={"csrf_token": csrf, "command_id": command_id, "acknowledgement": "acepto"},
        headers={"Origin": "https://attacker.example"},
        follow_redirects=False,
    )

    assert unchecked.status_code == 422
    assert "Para cerrar el reporte hay que marcar la aprobación." in unchecked.text
    assert wrong_value.status_code == 422
    assert bad_csrf.status_code == 403
    assert foreign.status_code == 403
    assert all(name != "close_daily_feedback_batch_v1" for name, _ in repository.calls[calls_before:])

    closed = client.post(
        f"/review/{BATCH_REF}/close",
        data={"csrf_token": csrf, "command_id": command_id, "acknowledgement": "acepto"},
        headers=same_origin,
        follow_redirects=False,
    )

    assert closed.status_code == 303
    assert closed.headers["location"] == f"/daily-feedback/review/{BATCH_REF}"
    name, payload = repository.calls[-1]
    assert name == "close_daily_feedback_batch_v1"
    assert payload["p_acknowledgement"] == REVIEW_CLOSURE_ACKNOWLEDGEMENT
    assert payload["p_command_id"] == command_id
    assert payload["p_public_ref"] == BATCH_REF
    assert payload["p_semantic_fingerprint"] == hashlib.sha256(
        "\0".join((command_id, BATCH_REF, REVIEW_CLOSURE_ACKNOWLEDGEMENT)).encode("utf-8")
    ).hexdigest()


def test_a_failed_closure_says_so() -> None:
    client, repository = _logged_in(_page_with_queue())
    csrf, command_id = _close_form(client)
    original = repository.rpc

    async def rpc(name: str, payload: dict[str, object]) -> dict[str, object]:
        if name == "close_daily_feedback_batch_v1":
            raise RuntimeError("review_batch_not_open")
        return await original(name, payload)

    repository.rpc = rpc  # type: ignore[method-assign]
    response = client.post(
        f"/review/{BATCH_REF}/close",
        data={"csrf_token": csrf, "command_id": command_id, "acknowledgement": "acepto"},
        headers={"Origin": "https://reviews.example.test"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "No se pudo cerrar el reporte." in response.text


def test_decision_redirect_keeps_a_chosen_filter() -> None:
    client, _ = _logged_in(_page_with_queue())
    page = client.get(f"/review/{BATCH_REF}?ver=opt_out").text
    csrf = page.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    command_id = page.split('name="command_id" value="', 1)[1].split('"', 1)[0]

    accepted = client.post(
        f"/review/{BATCH_REF}/decisions?ver=opt_out",
        data={
            "csrf_token": csrf,
            "command_id": command_id,
            "item_id": "33333333-3333-4333-8333-333333333333",
            "decision": "skip",
            "verbatim_feedback": "",
        },
        headers={"Origin": "https://reviews.example.test"},
        follow_redirects=False,
    )

    assert accepted.status_code == 303
    assert accepted.headers["location"] == f"/daily-feedback/review/{BATCH_REF}?ver=opt_out"


def test_complete_page_says_how_many_were_closed_without_review() -> None:
    client, _ = _logged_in(
        {
            "status": "complete",
            "local_date": "2026-10-13",
            "item_count": 28,
            "decided_count": 18,
            "retention_expires_at": "2026-10-17T00:00:00Z",
            "closed_unreviewed_count": 10,
            "closed_at": "2026-10-14T21:00:00Z",
        }
    )

    text = client.get(f"/review/{BATCH_REF}").text

    assert "Se registraron 18 decisiones." in text
    assert "Las otras 10 se cerraron sin feedback, con la aprobación de quien cerró el reporte." in text


def test_review_labels_the_discount_followup_as_a_template() -> None:
    page = _page_with_queue()
    item = dict(page["item"])  # type: ignore[arg-type]
    item["messages"] = [
        {"actor": "prospect", "kind": "prospect_message", "occurred_at": "2026-10-13T15:03:00Z",
         "status": "sent", "text": "Envíame el enlace", "meta": {}},
        {"actor": "agent", "kind": "discount_followup_template", "occurred_at": "2026-10-13T15:10:00Z",
         "status": "read", "text": "Te reservamos un 10 % de descuento", "meta": {"attribution": "full"}},
    ]
    page["item"] = item
    client, _ = _logged_in(page)

    text = client.get(f"/review/{BATCH_REF}").text

    assert '<span class="kind-tag kind-tag--template">Plantilla · seguimiento con cupón</span>' in text
