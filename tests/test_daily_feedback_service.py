from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from urllib.parse import parse_qs, urlsplit

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
    create_daily_feedback_review_app,
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
        if name == "get_daily_feedback_review_page_v1":
            return self.page
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


def _client() -> tuple[TestClient, FakeRepository]:
    repository = FakeRepository()
    service = DailyFeedbackService(
        repository=repository,
        oidc_client=FakeOidc(),
        settings=DailyFeedbackWebSettings(
            public_origin="https://reviews.example.test",
            session_hmac_key=b"s" * 32,
            slack_team_id="T12345678",
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
    assert rpc_name == "get_daily_feedback_review_page_v1"
    assert rpc_payload["p_public_ref"] == batch_ref
    assert isinstance(rpc_payload["p_session_hash"], str)


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


class WorkflowRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.batch_id = "55555555-5555-4555-8555-555555555555"

    async def rpc(self, name: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, payload))
        responses = {
            "purge_expired_daily_feedback_v1": {"status": "purged", "count": 0},
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
        "purge_expired_daily_feedback_v1",
        "claim_daily_feedback_collection_v1",
        "commit_daily_feedback_batch_v1",
        "claim_daily_feedback_notification_v1",
        "mark_daily_feedback_notification_started_v1",
        "complete_daily_feedback_notification_v1",
    ]
    for name, payload in repository.calls:
        if name in {
            "purge_expired_daily_feedback_v1",
            "claim_daily_feedback_collection_v1",
            "claim_daily_feedback_notification_v1",
        }:
            assert payload["p_tenant_ref"] == "lancemos"
            assert payload["p_scope_ref"] == "psicologajohanna-agent-bot-19"


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
