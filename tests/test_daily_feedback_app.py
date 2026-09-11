from __future__ import annotations

from dataclasses import dataclass, replace
import json

import pytest
from fastapi.testclient import TestClient


@dataclass
class FakeService:
    review_app: object | None = None


class FakeRepository:
    def __init__(self, *, fail: bool = False, delivery_unknown_count: int = 0) -> None:
        self.fail = fail
        self.delivery_unknown_count = delivery_unknown_count
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def rpc(self, name: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, payload))
        if self.fail:
            raise RuntimeError("database unavailable")
        if name == "get_daily_feedback_readiness_v1":
            return {
                "status": "ok",
                "delivery_unknown_count": self.delivery_unknown_count,
            }
        return {"status": "configured", "schedule_id": "11111111-1111-4111-8111-111111111111"}


class FakeScheduler:
    def __init__(self) -> None:
        self.preflighted = 0
        self.started = 0
        self.stopped = 0
        self.forced: list[bool] = []
        self.healthy = True

    async def start(self) -> None:
        self.started += 1

    async def preflight(self) -> None:
        self.preflighted += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def run_once(self, *, force_collection: bool = False) -> dict[str, object]:
        self.forced.append(force_collection)
        return {"collected": True, "notified": True, "purged": 0}


def _settings():
    from bridge.daily_feedback_app import (
        DailyFeedbackApplicationSettings,
        DailyFeedbackReviewer,
    )

    return DailyFeedbackApplicationSettings(
        manual_run_token="manual-run-token-with-more-than-32-chars",
        tenant_ref="lancemos",
        scope_ref="psicologajohanna-agent-bot-19",
        reviewers=(
            DailyFeedbackReviewer("dan-schwab", "U12345678", True),
            DailyFeedbackReviewer("mariana-marin", "U87654321", True),
            DailyFeedbackReviewer("juan-martitegui", "U11111111", True),
            DailyFeedbackReviewer("marcela-pineda", "U22222222", True),
        ),
        slack_team_id="T12345678",
        chatwoot_account_id=1,
        chatwoot_inbox_id=2,
        chatwoot_agent_bot_id=19,
        timezone="America/Bogota",
        daily_at="18:00:00",
        retention_hours=72,
        deletion_policy_ref="johanna-joint-reviewer-accountability-v1",
        sanitizer_version="deterministic-redaction-v1",
        selection_version="chatwoot-daily-agent-dialogues-v1",
        renderer_version="daily-feedback-web-v1",
    )


def test_application_settings_match_database_scope_grammar_and_exact_reviewer_count() -> None:
    from bridge.daily_feedback_app import DailyFeedbackReviewer

    settings = _settings()
    with pytest.raises(ValueError, match="invalid_daily_feedback_tenant_ref"):
        replace(settings, tenant_ref="tenant.with.dot")
    with pytest.raises(ValueError, match="invalid_daily_feedback_scope_ref"):
        replace(settings, scope_ref="scope.with.dot")
    with pytest.raises(ValueError, match="daily_feedback_reviewer_set_must_have_four"):
        replace(settings, reviewers=settings.reviewers[:3])
    with pytest.raises(ValueError, match="invalid_daily_feedback_reviewer_ref"):
        DailyFeedbackReviewer("reviewer.with.dot", "U33333333", True)


def test_application_configures_authority_before_becoming_ready_and_runs_the_same_worker_path() -> None:
    from bridge.daily_feedback_app import create_daily_feedback_application

    repository = FakeRepository()
    scheduler = FakeScheduler()
    app = create_daily_feedback_application(
        settings=_settings(),
        repository=repository,
        scheduler=scheduler,
        review_app=__import__("fastapi").FastAPI(),
    )

    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/ready")
        unauthorized = client.post("/internal/v1/daily-feedback/run")
        result = client.post(
            "/internal/v1/daily-feedback/run",
            headers={"Authorization": "Bearer manual-run-token-with-more-than-32-chars"},
        )

        assert health.status_code == 200
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready", "daily_feedback": "operational"}
        assert unauthorized.status_code == 401
        assert result.status_code == 200
        assert result.json() == {"collected": True, "notified": True, "purged": 0}
        assert scheduler.preflighted == 1
        assert scheduler.started == 1
        assert scheduler.forced == [True]

    assert scheduler.stopped == 1
    assert repository.calls[0][0] == "configure_daily_feedback_scope_v2"
    configure = repository.calls[0][1]
    assert configure["p_tenant_ref"] == "lancemos"
    assert configure["p_scope_ref"] == "psicologajohanna-agent-bot-19"
    assert configure["p_reviewers"] == [
        {
            "deletion_accountable": True,
            "reviewer_ref": "dan-schwab",
            "slack_user_id": "U12345678",
        },
        {
            "deletion_accountable": True,
            "reviewer_ref": "juan-martitegui",
            "slack_user_id": "U11111111",
        },
        {
            "deletion_accountable": True,
            "reviewer_ref": "marcela-pineda",
            "slack_user_id": "U22222222",
        },
        {
            "deletion_accountable": True,
            "reviewer_ref": "mariana-marin",
            "slack_user_id": "U87654321",
        },
    ]
    assert configure["p_chatwoot_account_id"] == 1
    assert configure["p_chatwoot_inbox_id"] == 2
    assert configure["p_chatwoot_agent_bot_id"] == 19
    assert configure["p_oidc_issuer"] == "https://slack.com"
    assert configure["p_deletion_policy_ref"] == "johanna-joint-reviewer-accountability-v1"
    assert configure["p_retention_hours"] == 72
    assert configure["p_timezone_name"] == "America/Bogota"
    assert configure["p_cutoff_local"] == "18:00:00"
    assert "p_timezone" not in configure
    assert "p_daily_at" not in configure


def test_application_uses_a_fresh_configuration_command_for_explicit_rollback() -> None:
    from bridge.daily_feedback_app import create_daily_feedback_application

    command_ids: list[str] = []
    for _ in range(2):
        repository = FakeRepository()
        app = create_daily_feedback_application(
            settings=replace(_settings(), scheduler_enabled=False),
            repository=repository,
            scheduler=FakeScheduler(),
            review_app=__import__("fastapi").FastAPI(),
        )
        with TestClient(app):
            pass
        command_id = repository.calls[0][1]["p_command_id"]
        assert isinstance(command_id, str)
        command_ids.append(command_id)

    assert command_ids[0] != command_ids[1]


def test_application_fails_closed_when_durable_authority_cannot_be_configured() -> None:
    from bridge.daily_feedback_app import create_daily_feedback_application

    app = create_daily_feedback_application(
        settings=_settings(),
        repository=FakeRepository(fail=True),
        scheduler=FakeScheduler(),
        review_app=__import__("fastapi").FastAPI(),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        with TestClient(app):
            pass


def test_application_starts_inert_but_manual_run_uses_the_same_worker_path() -> None:
    from bridge.daily_feedback_app import create_daily_feedback_application

    repository = FakeRepository()
    scheduler = FakeScheduler()
    app = create_daily_feedback_application(
        settings=replace(_settings(), scheduler_enabled=False),
        repository=repository,
        scheduler=scheduler,
        review_app=__import__("fastapi").FastAPI(),
    )

    with TestClient(app) as client:
        ready = client.get("/ready")
        result = client.post(
            "/internal/v1/daily-feedback/run",
            headers={"Authorization": "Bearer manual-run-token-with-more-than-32-chars"},
        )

        assert ready.status_code == 200
        assert ready.json() == {"status": "ready", "daily_feedback": "staged"}
        assert result.json() == {"collected": True, "notified": True, "purged": 0}
        assert scheduler.started == 0
        assert scheduler.forced == [True]

    assert scheduler.stopped == 0
    assert repository.calls[0][1]["p_enabled"] is False


def test_ready_fails_closed_after_the_scheduler_reports_a_background_failure() -> None:
    from bridge.daily_feedback_app import create_daily_feedback_application

    scheduler = FakeScheduler()
    scheduler.healthy = False
    app = create_daily_feedback_application(
        settings=_settings(),
        repository=FakeRepository(),
        scheduler=scheduler,
        review_app=__import__("fastapi").FastAPI(),
    )

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "daily_feedback": "scheduler_failed",
    }


def test_ready_fails_closed_when_notification_requires_reconciliation() -> None:
    from bridge.daily_feedback_app import create_daily_feedback_application

    app = create_daily_feedback_application(
        settings=_settings(),
        repository=FakeRepository(delivery_unknown_count=1),
        scheduler=FakeScheduler(),
        review_app=__import__("fastapi").FastAPI(),
    )

    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "daily_feedback": "notification_reconciliation_required",
    }


def test_runtime_settings_require_every_real_data_security_gate() -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    environment = {
        "DAILY_FEEDBACK_PUBLIC_ORIGIN": "https://reviews.example.test",
        "SUPABASE_BASE_URL": "https://project.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "service-role-secret",
        "SLACK_OIDC_CLIENT_ID": "client-id",
        "SLACK_OIDC_CLIENT_SECRET": "client-secret",
        "SLACK_OIDC_TEAM_ID": "T12345678",
        "DAILY_FEEDBACK_REVIEWERS_JSON": json.dumps([
            {"reviewer_ref": "dan-schwab", "slack_user_id": "U12345678", "deletion_accountable": True},
            {"reviewer_ref": "mariana-marin", "slack_user_id": "U87654321", "deletion_accountable": True},
            {"reviewer_ref": "juan-martitegui", "slack_user_id": "U11111111", "deletion_accountable": True},
            {"reviewer_ref": "marcela-pineda", "slack_user_id": "U22222222", "deletion_accountable": True},
        ]),
        "DAILY_FEEDBACK_MANUAL_RUN_TOKEN": "manual-run-token-with-more-than-32-chars",
        "DAILY_FEEDBACK_WORKER_ID": "daily-feedback-worker-1",
        "SLACK_CONNECTOR_BASE_URL": "https://connector.example.test",
        "SLACK_CONNECTOR_BEARER_TOKEN": "connector-token-with-more-than-32-chars",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_INBOX_ID": "2",
        "CHATWOOT_AGENT_BOT_ID": "19",
        "CHATWOOT_API_ACCESS_TOKEN": "chatwoot-secret",
        "DAILY_FEEDBACK_PSEUDONYMIZATION_KEY": "pseudonym-key-with-more-than-32-characters",
        "DAILY_FEEDBACK_REAL_CONVERSATIONS_ENABLED": "true",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED": "true",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF": "https://supabase.com/docs/guides/platform/security",
        "DAILY_FEEDBACK_RETENTION_HOURS": "72",
        "DAILY_FEEDBACK_DELETION_POLICY_REF": "johanna-joint-reviewer-accountability-v1",
        "DAILY_FEEDBACK_TIMEZONE": "America/Bogota",
        "DAILY_FEEDBACK_DAILY_AT": "18:00:00",
        "DAILY_FEEDBACK_TENANT_REF": "lancemos",
        "DAILY_FEEDBACK_SCOPE_REF": "psicologajohanna-agent-bot-19",
        "DAILY_FEEDBACK_SLACK_TENANT_REF": "johanna",
    }

    settings = DailyFeedbackRuntimeSettings.from_env(environment)

    assert settings.application.tenant_ref == "lancemos"
    assert len(settings.application.reviewers) == 4
    assert settings.application.daily_at == "18:00:00"
    assert settings.slack_tenant_ref == "johanna"
    assert settings.chatwoot_agent_bot_id == 19
    rendered = repr(settings)
    assert "service-role-secret" not in rendered
    assert "chatwoot-secret" not in rendered

    environment["DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED"] = "false"
    with pytest.raises(ValueError, match="storage_encryption_not_verified"):
        DailyFeedbackRuntimeSettings.from_env(environment)
    environment["DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED"] = "true"
    del environment["DAILY_FEEDBACK_TIMEZONE"]
    with pytest.raises(ValueError, match="DAILY_FEEDBACK_TIMEZONE_required"):
        DailyFeedbackRuntimeSettings.from_env(environment)
    environment["DAILY_FEEDBACK_TIMEZONE"] = "America/Bogota"

    for variable in (
        "DAILY_FEEDBACK_TENANT_REF",
        "DAILY_FEEDBACK_SCOPE_REF",
        "DAILY_FEEDBACK_SLACK_TENANT_REF",
        "DAILY_FEEDBACK_REVIEWERS_JSON",
    ):
        value = environment.pop(variable)
        with pytest.raises(ValueError, match=f"{variable}_required"):
            DailyFeedbackRuntimeSettings.from_env(environment)
        environment[variable] = value

    invalid_reviewers = (
        (
            [
                {"reviewer_ref": "duplicate", "slack_user_id": "U12345678", "deletion_accountable": True},
                {"reviewer_ref": "duplicate", "slack_user_id": "U87654321", "deletion_accountable": True},
            ],
            "duplicate_daily_feedback_reviewer_ref",
        ),
        (
            [
                {"reviewer_ref": "first", "slack_user_id": "U12345678", "deletion_accountable": True},
                {"reviewer_ref": "second", "slack_user_id": "U12345678", "deletion_accountable": True},
            ],
            "duplicate_daily_feedback_slack_user_id",
        ),
        (
            [
                {"reviewer_ref": f"reviewer-{index}", "slack_user_id": f"U{index:08d}", "deletion_accountable": False}
                for index in range(1, 5)
            ],
            "daily_feedback_all_reviewers_must_be_deletion_accountable",
        ),
        (
            [{"reviewer_ref": "first", "slack_user_id": "U12345678", "deletion_accountable": True, "extra": True}],
            "invalid_daily_feedback_reviewers_json",
        ),
    )
    for payload, error in invalid_reviewers:
        environment["DAILY_FEEDBACK_REVIEWERS_JSON"] = json.dumps(payload)
        with pytest.raises(ValueError, match=error):
            DailyFeedbackRuntimeSettings.from_env(environment)


def test_factory_builds_the_real_collector_repository_and_scheduler_without_network_io() -> None:
    from bridge.daily_feedback_app import create_application_from_env

    environment = {
        "DAILY_FEEDBACK_PUBLIC_ORIGIN": "https://reviews.example.test",
        "SUPABASE_BASE_URL": "https://project.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "service-role-secret",
        "SLACK_OIDC_CLIENT_ID": "client-id",
        "SLACK_OIDC_CLIENT_SECRET": "client-secret",
        "SLACK_OIDC_TEAM_ID": "T12345678",
        "DAILY_FEEDBACK_REVIEWERS_JSON": json.dumps([
            {"reviewer_ref": "dan-schwab", "slack_user_id": "U12345678", "deletion_accountable": True},
            {"reviewer_ref": "mariana-marin", "slack_user_id": "U87654321", "deletion_accountable": True},
            {"reviewer_ref": "juan-martitegui", "slack_user_id": "U11111111", "deletion_accountable": True},
            {"reviewer_ref": "marcela-pineda", "slack_user_id": "U22222222", "deletion_accountable": True},
        ]),
        "DAILY_FEEDBACK_MANUAL_RUN_TOKEN": "manual-run-token-with-more-than-32-chars",
        "DAILY_FEEDBACK_WORKER_ID": "daily-feedback-worker-1",
        "SLACK_CONNECTOR_BASE_URL": "https://connector.example.test",
        "SLACK_CONNECTOR_BEARER_TOKEN": "connector-token-with-more-than-32-chars",
        "CHATWOOT_BASE_URL": "https://chatwoot.example.test",
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_INBOX_ID": "2",
        "CHATWOOT_AGENT_BOT_ID": "19",
        "CHATWOOT_API_ACCESS_TOKEN": "chatwoot-secret",
        "DAILY_FEEDBACK_PSEUDONYMIZATION_KEY": "pseudonym-key-with-more-than-32-characters",
        "DAILY_FEEDBACK_REAL_CONVERSATIONS_ENABLED": "true",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED": "true",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF": "https://supabase.com/docs/guides/platform/security",
        "DAILY_FEEDBACK_RETENTION_HOURS": "72",
        "DAILY_FEEDBACK_DELETION_POLICY_REF": "johanna-joint-reviewer-accountability-v1",
        "DAILY_FEEDBACK_TIMEZONE": "America/Bogota",
        "DAILY_FEEDBACK_DAILY_AT": "18:00:00",
        "DAILY_FEEDBACK_TENANT_REF": "lancemos",
        "DAILY_FEEDBACK_SCOPE_REF": "psicologajohanna-agent-bot-19",
        "DAILY_FEEDBACK_SLACK_TENANT_REF": "johanna",
    }

    app = create_application_from_env(environment)

    assert app.state.daily_feedback_scheduler.settings.tenant_ref == "lancemos"
    assert any(route.path == "/daily-feedback" for route in app.routes)
    assert any(route.path == "/internal/v1/daily-feedback/run" for route in app.routes)


def test_deployment_uses_the_dedicated_factory_and_declares_every_fail_closed_gate() -> None:
    from pathlib import Path

    root = Path(__file__).parents[1]
    dockerfile = (root / "deploy/daily-feedback.Dockerfile").read_text()
    compose = (root / "deploy/daily-feedback-compose.yaml").read_text()
    env_example = (root / "deploy/daily-feedback.env.example").read_text()

    assert "bridge.daily_feedback_app:create_application_from_env" in dockerfile
    assert "--factory" in dockerfile
    assert "--no-access-log" in dockerfile
    assert "replicas: 1" in compose
    assert "${DAILY_FEEDBACK_TIMEZONE:?required}" in compose
    assert "${DAILY_FEEDBACK_DAILY_AT:?required}" in compose
    assert "DAILY_FEEDBACK_SCHEDULER_ENABLED: ${DAILY_FEEDBACK_SCHEDULER_ENABLED:-false}" in compose
    assert "DAILY_FEEDBACK_SCHEDULER_ENABLED=false" in env_example
    for name in (
        "DAILY_FEEDBACK_PUBLIC_ORIGIN",
        "DAILY_FEEDBACK_REAL_CONVERSATIONS_ENABLED",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF",
        "DAILY_FEEDBACK_RETENTION_HOURS",
        "DAILY_FEEDBACK_DELETION_POLICY_REF",
        "DAILY_FEEDBACK_REVIEWERS_JSON",
        "SLACK_OIDC_CLIENT_ID",
        "SLACK_OIDC_CLIENT_SECRET",
    ):
        assert f"{name}=" in env_example
        assert f"{name}:" in compose
