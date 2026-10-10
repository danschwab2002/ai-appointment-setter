from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import json

import httpx
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


# --- La revision en una instancia autohospedada (ATT1) ------------------------------
# La parte de la app: la lectura del entorno (cada variable nueva es opcional, vacia
# cuenta como ausente y su valor por defecto es el comportamiento de hoy), las
# excepciones en el 200 de /ready y el cableado de la fabrica.
#
# Las respuestas de Chatwoot son capturas reales, con lista blanca de campos y sin
# tokens. Del 10/10/2026: el inbox 11 de ATT1 (cuenta 2), su agent_bot (un objeto
# vacio: el bot no esta vinculado, a proposito), el bot 2 de la cuenta 2 y el agent_bot
# del inbox 9 de Johanna (cuenta 1, bot 1). El show del inbox 9 es la captura del 23/09.

FIXTURES = Path(__file__).parent / "fixtures"
ATT1_RISK_ACCEPTANCE_REF = "https://example.test/instancia-att1/aceptacion-de-riesgo.md"
JOHANNA_EVIDENCE_REF = "https://supabase.com/docs/guides/platform/security"
ATT1_EXCEPTIONS = [
    "authority_internal_http",
    "storage_unencrypted_risk_accepted",
    "chatwoot_agent_bot_unlinked",
]
NEW_VARIABLES = (
    "DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP",
    "DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF",
    "DAILY_FEEDBACK_CHATWOOT_AGENT_BOT_BINDING",
    "DAILY_FEEDBACK_BRAND_NAME",
    "DAILY_FEEDBACK_MAX_CONVERSATION_PAGES",
    "DAILY_FEEDBACK_COLLECTION_LEASE_SECONDS",
)
CHATWOOT_ORIGIN = "https://chatwoot.example.test"
CONNECTOR_ORIGIN = "https://connector.example.test"
MANUAL_RUN = {"Authorization": "Bearer manual-run-token-with-more-than-32-chars"}
REVIEW_REF = "22222222-2222-4222-8222-222222222222"
# Una cookie de sesion con la forma valida; la base falsa no la mira.
SESSION = {"Cookie": "__Host-daily_feedback_session=" + "s" * 43}
# Una pagina v1 como la de test_daily_feedback_review_context
# (test_review_page_still_renders_a_v1_item_without_context).
REVIEW_PAGE = {
    "status": "item",
    "local_date": "2026-10-10",
    "item_count": 1,
    "decided_count": 0,
    "item": {
        "item_id": "33333333-3333-4333-8333-333333333333",
        "position": 1,
        "display_label": "Conversación 01",
        "apparent_objective": "Revisar",
        "observed_outcome": "Esperando",
        "release_id": "release_lineage_unavailable",
        "release_version": 0,
        "messages": [
            {"actor": "prospect", "occurred_at": "2026-10-10T12:00:00Z", "text": "¿Qué incluye?"},
            {"actor": "agent", "occurred_at": "2026-10-10T12:01:00Z", "text": "Te explico."},
        ],
    },
}


def _capture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def _with(environment: dict[str, str], changes: dict[str, str | None]) -> dict[str, str]:
    """Una copia del entorno con los cambios; None saca la variable."""
    result = dict(environment)
    for name, value in changes.items():
        if value is None:
            result.pop(name, None)
        else:
            result[name] = value
    return result


def _johanna_environment() -> dict[str, str]:
    """El entorno de Johanna de hoy (el de
    test_runtime_settings_require_every_real_data_security_gate), sin ninguna variable
    nueva."""
    return {
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
        "SLACK_CONNECTOR_BASE_URL": CONNECTOR_ORIGIN,
        "SLACK_CONNECTOR_BEARER_TOKEN": "connector-token-with-more-than-32-chars",
        "CHATWOOT_BASE_URL": CHATWOOT_ORIGIN,
        "CHATWOOT_ACCOUNT_ID": "1",
        "CHATWOOT_INBOX_ID": "2",
        "CHATWOOT_AGENT_BOT_ID": "19",
        "CHATWOOT_API_ACCESS_TOKEN": "chatwoot-secret",
        "DAILY_FEEDBACK_PSEUDONYMIZATION_KEY": "pseudonym-key-with-more-than-32-characters",
        "DAILY_FEEDBACK_REAL_CONVERSATIONS_ENABLED": "true",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED": "true",
        "DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF": JOHANNA_EVIDENCE_REF,
        "DAILY_FEEDBACK_RETENTION_HOURS": "72",
        "DAILY_FEEDBACK_DELETION_POLICY_REF": "johanna-joint-reviewer-accountability-v1",
        "DAILY_FEEDBACK_TIMEZONE": "America/Bogota",
        "DAILY_FEEDBACK_DAILY_AT": "18:00:00",
        "DAILY_FEEDBACK_TENANT_REF": "lancemos",
        "DAILY_FEEDBACK_SCOPE_REF": "psicologajohanna-agent-bot-19",
        "DAILY_FEEDBACK_SLACK_TENANT_REF": "johanna",
    }


def _att1_environment() -> dict[str, str]:
    """El set completo de ATT1: la base propia por http interno, el disco sin cifrar con
    la aceptacion escrita del riesgo, el bot sin vincular, la marca de la doctora, 200
    paginas y una lease de 300 s. Sin la evidencia de cifrado, que no tiene."""
    return _with(
        _johanna_environment(),
        {
            "DAILY_FEEDBACK_PUBLIC_ORIGIN": "https://setter-att1.example.test",
            "SUPABASE_BASE_URL": "http://att1-gateway:8080",
            "DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP": "true",
            "DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED": "false",
            "DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF": None,
            "DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF": ATT1_RISK_ACCEPTANCE_REF,
            "DAILY_FEEDBACK_CHATWOOT_AGENT_BOT_BINDING": "unlinked",
            "DAILY_FEEDBACK_BRAND_NAME": "Dra. Nina Garza",
            "DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "200",
            "DAILY_FEEDBACK_COLLECTION_LEASE_SECONDS": "300",
            "CHATWOOT_ACCOUNT_ID": "2",
            "CHATWOOT_INBOX_ID": "11",
            "CHATWOOT_AGENT_BOT_ID": "2",
            "DAILY_FEEDBACK_SCOPE_REF": "att1-agent-bot",
            "DAILY_FEEDBACK_SLACK_TENANT_REF": "att1",
            "DAILY_FEEDBACK_DELETION_POLICY_REF": "att1-joint-reviewer-accountability-v1",
            "DAILY_FEEDBACK_TIMEZONE": "America/Mexico_City",
            # El corte que decidio Dan el 10/10: las 18:00 de CDMX, no las 21:30.
            "DAILY_FEEDBACK_DAILY_AT": "18:00:00",
            "DAILY_FEEDBACK_WORKER_ID": "att1-daily-feedback-1",
        },
    )


_ENVIRONMENTS = {"johanna": _johanna_environment, "att1": _att1_environment}


def test_runtime_settings_read_the_full_att1_set() -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    settings = DailyFeedbackRuntimeSettings.from_env(_att1_environment())

    assert settings.exceptions == tuple(ATT1_EXCEPTIONS)
    assert settings.supabase_internal_http is True
    assert settings.supabase_base_url == "http://att1-gateway:8080"
    # Nunca se declara cifrado lo que no lo esta: sin evidencia, con la aceptacion.
    assert settings.storage_encryption_verified is False
    assert settings.storage_encryption_evidence_ref == ""
    assert settings.storage_risk_acceptance_ref == ATT1_RISK_ACCEPTANCE_REF
    assert settings.chatwoot_agent_bot_binding == "unlinked"
    assert settings.brand_name == "Dra. Nina Garza"
    assert settings.max_conversation_pages == 200
    assert settings.collection_lease_seconds == 300
    assert settings.application.scope_ref == "att1-agent-bot"
    assert settings.slack_tenant_ref == "att1"


@pytest.mark.parametrize("new_variables", ["absent", "empty", "blank"])
def test_runtime_settings_keep_johanna_defaults_without_the_new_variables(
    new_variables: str,
) -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    environment = _johanna_environment()
    # El compose pasa las opcionales con ${VAR:-}: vacias cuentan como ausentes.
    if new_variables != "absent":
        value = "" if new_variables == "empty" else "   "
        environment.update({name: value for name in NEW_VARIABLES})

    settings = DailyFeedbackRuntimeSettings.from_env(environment)

    assert settings.exceptions == ()
    assert settings.supabase_internal_http is False
    assert settings.supabase_base_url == "https://project.supabase.co"
    assert settings.storage_encryption_verified is True
    assert settings.storage_encryption_evidence_ref == JOHANNA_EVIDENCE_REF
    assert settings.storage_risk_acceptance_ref == ""
    assert settings.chatwoot_agent_bot_binding == "inbox"
    assert settings.brand_name == "Johanna"
    assert settings.max_conversation_pages == 20
    assert settings.collection_lease_seconds == 120
    assert settings == DailyFeedbackRuntimeSettings.from_env(_johanna_environment())


@pytest.mark.parametrize(
    ("changes", "exceptions"),
    [
        (
            {
                "SUPABASE_BASE_URL": "http://att1-gateway:8080",
                "DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP": "true",
            },
            ("authority_internal_http",),
        ),
        (
            {
                "DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED": "false",
                "DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF": None,
                "DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF": ATT1_RISK_ACCEPTANCE_REF,
            },
            ("storage_unencrypted_risk_accepted",),
        ),
        (
            {"DAILY_FEEDBACK_CHATWOOT_AGENT_BOT_BINDING": "unlinked"},
            ("chatwoot_agent_bot_unlinked",),
        ),
        # Lo que no afloja un control no es una excepcion.
        (
            {
                "DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP": "false",
                "DAILY_FEEDBACK_CHATWOOT_AGENT_BOT_BINDING": "inbox",
                "DAILY_FEEDBACK_BRAND_NAME": "Dra. Nina Garza",
                "DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "200",
                "DAILY_FEEDBACK_COLLECTION_LEASE_SECONDS": "300",
            },
            (),
        ),
    ],
)
def test_each_exception_is_declared_only_by_its_own_variables(
    changes: dict[str, str | None], exceptions: tuple[str, ...]
) -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    settings = DailyFeedbackRuntimeSettings.from_env(_with(_johanna_environment(), changes))

    assert settings.exceptions == exceptions


@pytest.mark.parametrize(
    ("base", "changes", "error"),
    [
        # La base por http interno: sin el permiso, igual que hoy.
        ("johanna", {"SUPABASE_BASE_URL": "http://att1-gateway:8080"}, "invalid_supabase_origin"),
        ("att1", {"DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP": "false"}, "invalid_supabase_origin"),
        ("att1", {"DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP": "yes"}, "invalid_daily_feedback_supabase_internal_http"),
        ("att1", {"DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP": "1"}, "invalid_daily_feedback_supabase_internal_http"),
        ("att1", {"DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP": "on"}, "invalid_daily_feedback_supabase_internal_http"),
        # Con el permiso, solo un origen interno: http, una etiqueta y con puerto.
        ("att1", {"SUPABASE_BASE_URL": "http://x.host:8080"}, "invalid_supabase_internal_origin"),
        ("att1", {"SUPABASE_BASE_URL": "http://10.0.0.5:8080"}, "invalid_supabase_internal_origin"),
        ("att1", {"SUPABASE_BASE_URL": "http://att1-gateway"}, "invalid_supabase_internal_origin"),
        ("att1", {"SUPABASE_BASE_URL": "http://user:pass@att1-gateway:8080"}, "invalid_supabase_internal_origin"),
        ("att1", {"SUPABASE_BASE_URL": "http://att1-gateway:8080/rest/v1"}, "invalid_supabase_internal_origin"),
        ("att1", {"SUPABASE_BASE_URL": "http://att1-gateway:8080//"}, "invalid_supabase_internal_origin"),
        ("att1", {"SUPABASE_BASE_URL": "http://att1-gateway:8080?x=1"}, "invalid_supabase_internal_origin"),
        # Lo que urlsplit no puede leer (un host entre corchetes) da el mismo codigo.
        ("att1", {"SUPABASE_BASE_URL": "http://[att1-gateway]:8080"}, "invalid_supabase_internal_origin"),
        # El permiso declara http interno: un https no es eso.
        ("att1", {"SUPABASE_BASE_URL": "https://project.supabase.co"}, "invalid_supabase_internal_origin"),
        # El almacenamiento. Sin cifrado verificado ni aceptacion: el error de hoy.
        ("johanna", {"DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED": "false"}, "storage_encryption_not_verified"),
        ("att1", {"DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF": None}, "storage_encryption_not_verified"),
        ("att1", {"DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF": ""}, "storage_encryption_not_verified"),
        # La aceptacion sola no declara nada: hace falta VERIFIED=false explicito.
        ("att1", {"DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED": None}, "storage_encryption_not_verified"),
        ("att1", {"DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED": "no"}, "storage_encryption_not_verified"),
        # La aceptacion mal formada.
        ("att1", {"DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF": "http://example.test/aceptacion.md"}, "invalid_storage_risk_acceptance_ref"),
        ("att1", {"DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF": "https://user:pass@example.test/aceptacion.md"}, "invalid_storage_risk_acceptance_ref"),
        ("att1", {"DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF": "aceptacion-de-riesgo.md"}, "invalid_storage_risk_acceptance_ref"),
        ("att1", {"DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF": "https:///aceptacion.md"}, "invalid_storage_risk_acceptance_ref"),
        ("att1", {"DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF": "https://example.test/acepta cion.md"}, "invalid_storage_risk_acceptance_ref"),
        ("att1", {"DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF": "https://[x]/aceptacion.md"}, "invalid_storage_risk_acceptance_ref"),
        # Las dos formas a la vez son ambiguas.
        ("johanna", {"DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF": ATT1_RISK_ACCEPTANCE_REF}, "ambiguous_storage_protection"),
        ("johanna", {"DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF": None, "DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF": ATT1_RISK_ACCEPTANCE_REF}, "ambiguous_storage_protection"),
        ("att1", {"DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF": JOHANNA_EVIDENCE_REF}, "ambiguous_storage_protection"),
        ("att1", {"DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED": None, "DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF": JOHANNA_EVIDENCE_REF}, "ambiguous_storage_protection"),
        # VERIFIED=true exige la evidencia https, como hoy.
        ("johanna", {"DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF": None}, "DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF_required"),
        ("johanna", {"DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF": "http://example.test/evidencia"}, "invalid_storage_encryption_evidence_ref"),
        # El bot: inbox o unlinked, escrito exacto.
        ("att1", {"DAILY_FEEDBACK_CHATWOOT_AGENT_BOT_BINDING": "UNLINKED"}, "invalid_daily_feedback_chatwoot_agent_bot_binding"),
        ("att1", {"DAILY_FEEDBACK_CHATWOOT_AGENT_BOT_BINDING": "none"}, "invalid_daily_feedback_chatwoot_agent_bot_binding"),
        # La marca: de 1 a 60 caracteres imprimibles (vacia es la de siempre).
        ("att1", {"DAILY_FEEDBACK_BRAND_NAME": "N" * 61}, "invalid_daily_feedback_brand_name"),
        ("att1", {"DAILY_FEEDBACK_BRAND_NAME": "Dra.\tNina"}, "invalid_daily_feedback_brand_name"),
        ("att1", {"DAILY_FEEDBACK_BRAND_NAME": "Dra.\nNina"}, "invalid_daily_feedback_brand_name"),
        ("att1", {"DAILY_FEEDBACK_BRAND_NAME": "Nina\u200bGarza"}, "invalid_daily_feedback_brand_name"),
        # Las paginas: un entero de 1 a 400, solo digitos.
        ("att1", {"DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "0"}, "invalid_daily_feedback_max_conversation_pages"),
        ("att1", {"DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "401"}, "invalid_daily_feedback_max_conversation_pages"),
        ("att1", {"DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "-1"}, "invalid_daily_feedback_max_conversation_pages"),
        ("att1", {"DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "+20"}, "invalid_daily_feedback_max_conversation_pages"),
        ("att1", {"DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "2_0"}, "invalid_daily_feedback_max_conversation_pages"),
        ("att1", {"DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "20.0"}, "invalid_daily_feedback_max_conversation_pages"),
        ("att1", {"DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "\u0662\u0660"}, "invalid_daily_feedback_max_conversation_pages"),
        ("att1", {"DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "veinte"}, "invalid_daily_feedback_max_conversation_pages"),
        ("att1", {"DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "9" * 5000}, "invalid_daily_feedback_max_conversation_pages"),
        # La lease de la recoleccion: de 30 a 900 s, el rango de la SQL.
        ("att1", {"DAILY_FEEDBACK_COLLECTION_LEASE_SECONDS": "29"}, "invalid_daily_feedback_collection_lease_seconds"),
        ("att1", {"DAILY_FEEDBACK_COLLECTION_LEASE_SECONDS": "901"}, "invalid_daily_feedback_collection_lease_seconds"),
        ("att1", {"DAILY_FEEDBACK_COLLECTION_LEASE_SECONDS": "120s"}, "invalid_daily_feedback_collection_lease_seconds"),
        ("att1", {"DAILY_FEEDBACK_COLLECTION_LEASE_SECONDS": "+120"}, "invalid_daily_feedback_collection_lease_seconds"),
    ],
)
def test_runtime_settings_report_each_new_error_with_its_code(
    base: str, changes: dict[str, str | None], error: str
) -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    environment = _with(_ENVIRONMENTS[base](), changes)

    with pytest.raises(ValueError, match=f"^{error}$"):
        DailyFeedbackRuntimeSettings.from_env(environment)


@pytest.mark.parametrize(
    ("changes", "field_name", "expected"),
    [
        ({"DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "1"}, "max_conversation_pages", 1),
        ({"DAILY_FEEDBACK_MAX_CONVERSATION_PAGES": "400"}, "max_conversation_pages", 400),
        ({"DAILY_FEEDBACK_COLLECTION_LEASE_SECONDS": "30"}, "collection_lease_seconds", 30),
        ({"DAILY_FEEDBACK_COLLECTION_LEASE_SECONDS": "900"}, "collection_lease_seconds", 900),
        ({"DAILY_FEEDBACK_BRAND_NAME": "N" * 60}, "brand_name", "N" * 60),
        # Como toda variable, se recorta; lo que no se filtra lo escapa la pagina.
        ({"DAILY_FEEDBACK_BRAND_NAME": " Dra. Nina Garza "}, "brand_name", "Dra. Nina Garza"),
        ({"DAILY_FEEDBACK_BRAND_NAME": "<b>Nina</b>"}, "brand_name", "<b>Nina</b>"),
        ({"DAILY_FEEDBACK_CHATWOOT_AGENT_BOT_BINDING": "inbox"}, "chatwoot_agent_bot_binding", "inbox"),
        # Como los otros flags de la revision, true y false sin distinguir mayusculas.
        ({"DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP": "TRUE"}, "supabase_internal_http", True),
        ({"DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP": " true "}, "supabase_internal_http", True),
        ({"DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED": "FALSE"}, "storage_encryption_verified", False),
        ({"SUPABASE_BASE_URL": "http://att1-gateway:8080/"}, "supabase_base_url", "http://att1-gateway:8080"),
        # El nombre completo de un servicio de Swarm (<stack>_<servicio>).
        ({"SUPABASE_BASE_URL": "http://setter-att1_att1-gateway:8080"}, "supabase_base_url", "http://setter-att1_att1-gateway:8080"),
        # Vacia es ausente: con la aceptacion, una evidencia vacia no la vuelve ambigua.
        ({"DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF": ""}, "storage_encryption_verified", False),
    ],
)
def test_runtime_settings_accept_the_borders_of_each_new_variable(
    changes: dict[str, str | None], field_name: str, expected: object
) -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    settings = DailyFeedbackRuntimeSettings.from_env(_with(_att1_environment(), changes))

    assert getattr(settings, field_name) == expected


@pytest.mark.parametrize(
    ("base", "changes", "error"),
    [
        # http interno solo hacia un origen interno.
        ("johanna", {"supabase_internal_http": True}, "invalid_supabase_internal_origin"),
        ("johanna", {"supabase_internal_http": "true"}, "invalid_daily_feedback_supabase_internal_http"),
        # Sin cifrar, solo con la aceptacion; cifrado y aceptado a la vez, nunca.
        ("johanna", {"storage_encryption_verified": False}, "ambiguous_storage_protection"),
        (
            "johanna",
            {"storage_encryption_verified": False, "storage_encryption_evidence_ref": ""},
            "invalid_storage_risk_acceptance_ref",
        ),
        ("att1", {"storage_encryption_verified": True}, "ambiguous_storage_protection"),
        ("att1", {"storage_encryption_evidence_ref": JOHANNA_EVIDENCE_REF}, "ambiguous_storage_protection"),
        ("att1", {"storage_encryption_verified": 0}, "storage_encryption_not_verified"),
        ("att1", {"chatwoot_agent_bot_binding": "UNLINKED"}, "invalid_daily_feedback_chatwoot_agent_bot_binding"),
        # Cero caracteres: por entorno es la marca de siempre; en la configuracion, no.
        ("att1", {"brand_name": ""}, "invalid_daily_feedback_brand_name"),
        ("att1", {"max_conversation_pages": "200"}, "invalid_daily_feedback_max_conversation_pages"),
        ("att1", {"collection_lease_seconds": True}, "invalid_daily_feedback_collection_lease_seconds"),
    ],
)
def test_runtime_settings_cannot_declare_what_the_configuration_is_not(
    base: str, changes: dict[str, object], error: str
) -> None:
    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    settings = DailyFeedbackRuntimeSettings.from_env(_ENVIRONMENTS[base]())

    with pytest.raises(ValueError, match=f"^{error}$"):
        replace(settings, **changes)


def _application_with(**overrides: object):
    from fastapi import FastAPI

    from bridge.daily_feedback_app import create_daily_feedback_application

    arguments: dict[str, object] = {
        "settings": _settings(),
        "repository": FakeRepository(),
        "scheduler": FakeScheduler(),
        "review_app": FastAPI(),
    }
    arguments.update(overrides)
    return create_daily_feedback_application(**arguments)


def test_ready_lists_exceptions_only_when_configured() -> None:
    # Las tres, pasadas en otro orden: el 200 las lista en el orden fijo.
    with TestClient(
        _application_with(
            exceptions=(
                "chatwoot_agent_bot_unlinked",
                "authority_internal_http",
                "storage_unencrypted_risk_accepted",
            )
        )
    ) as client:
        all_three = client.get("/ready")
    assert all_three.status_code == 200
    assert all_three.json() == {
        "status": "ready",
        "daily_feedback": "operational",
        "exceptions": ATT1_EXCEPTIONS,
    }

    with TestClient(
        _application_with(
            settings=replace(_settings(), scheduler_enabled=False),
            exceptions=("storage_unencrypted_risk_accepted",),
        )
    ) as client:
        one = client.get("/ready")
    assert one.json() == {
        "status": "ready",
        "daily_feedback": "staged",
        "exceptions": ["storage_unencrypted_risk_accepted"],
    }

    # Sin excepciones (Johanna): el JSON de hoy, byte por byte, en los dos modos.
    for overrides in ({}, {"exceptions": ()}):
        with TestClient(_application_with(**overrides)) as client:
            assert client.get("/ready").content == (
                b'{"status":"ready","daily_feedback":"operational"}'
            )
    with TestClient(
        _application_with(settings=replace(_settings(), scheduler_enabled=False))
    ) as client:
        assert client.get("/ready").content == b'{"status":"ready","daily_feedback":"staged"}'


def test_ready_failures_do_not_change_with_exceptions() -> None:
    class ReadinessUnavailable(FakeRepository):
        async def rpc(self, name: str, payload: dict[str, object]) -> dict[str, object]:
            if name == "get_daily_feedback_readiness_v1":
                raise RuntimeError("database unavailable")
            return await super().rpc(name, payload)

    exceptions = tuple(ATT1_EXCEPTIONS)
    unhealthy = FakeScheduler()
    unhealthy.healthy = False

    with TestClient(_application_with(exceptions=exceptions, scheduler=unhealthy)) as client:
        scheduler_failed = client.get("/ready")
    with TestClient(
        _application_with(
            exceptions=exceptions, repository=FakeRepository(delivery_unknown_count=1)
        )
    ) as client:
        reconciliation = client.get("/ready")
    with TestClient(
        _application_with(exceptions=exceptions, repository=ReadinessUnavailable())
    ) as client:
        authority = client.get("/ready")
    # Sin el arranque (sin lifespan), la app no esta lista.
    not_started = TestClient(_application_with(exceptions=exceptions)).get("/ready")

    assert [
        (response.status_code, response.json())
        for response in (scheduler_failed, reconciliation, authority, not_started)
    ] == [
        (503, {"status": "not_ready", "daily_feedback": "scheduler_failed"}),
        (503, {"status": "not_ready", "daily_feedback": "notification_reconciliation_required"}),
        (503, {"status": "not_ready", "daily_feedback": "authority_unavailable"}),
        (503, {"status": "not_ready", "daily_feedback": "unavailable"}),
    ]


@pytest.mark.parametrize(
    "exceptions",
    [
        ("authority_internal_http", "authority_internal_http"),
        ("storage_encryption_not_verified",),
        ("Authority_Internal_Http",),
        (None,),
        ["authority_internal_http"],
        "authority_internal_http",
    ],
)
def test_application_rejects_exceptions_it_does_not_know(exceptions: object) -> None:
    with pytest.raises(ValueError, match="^invalid_daily_feedback_exceptions$"):
        _application_with(exceptions=exceptions)


ATT1_INBOX = _capture("chatwoot_inbox_11_show_20261010.json")
ATT1_INBOX_AGENT_BOT = _capture("chatwoot_inbox_11_agent_bot_unlinked_20261010.json")
ATT1_ACCOUNT_AGENT_BOT = _capture("chatwoot_account_2_agent_bot_2_20261010.json")
JOHANNA_INBOX_AGENT_BOT = _capture("chatwoot_inbox_9_agent_bot_linked_20261010.json")
JOHANNA_INBOX = {
    key: value
    for key, value in _capture("chatwoot_inbox_9_message_templates_20260923.json").items()
    if key != "_fixture"
}


def _att1_chatwoot(**overrides: tuple[int, object]) -> dict[str, tuple[int, object]]:
    routes = {
        "/api/v1/accounts/2/inboxes/11": (ATT1_INBOX["status"], ATT1_INBOX["body"]),
        "/api/v1/accounts/2/inboxes/11/agent_bot": (
            ATT1_INBOX_AGENT_BOT["status"],
            ATT1_INBOX_AGENT_BOT["body"],
        ),
        "/api/v1/accounts/2/agent_bots/2": (
            ATT1_ACCOUNT_AGENT_BOT["status"],
            ATT1_ACCOUNT_AGENT_BOT["body"],
        ),
    }
    routes.update(overrides)
    return routes


def _johanna_chatwoot() -> dict[str, tuple[int, object]]:
    return {
        "/api/v1/accounts/1/inboxes/9": (200, JOHANNA_INBOX),
        "/api/v1/accounts/1/inboxes/9/agent_bot": (
            JOHANNA_INBOX_AGENT_BOT["status"],
            JOHANNA_INBOX_AGENT_BOT["body"],
        ),
    }


class InstanceNetwork:
    """La red de una instancia, falsa: la base (PostgREST), Chatwoot y el conector de
    Slack. Chatwoot contesta con las capturas; la base, con la forma de las funciones de
    SQL (un scope configurado, sin lotes pendientes: las corridas dan idle). Anota cada
    pedido en orden, y falla ante uno que no espera.
    """

    def __init__(
        self,
        *,
        supabase_origin: str,
        chatwoot: dict[str, tuple[int, object]],
        connector_tenant_ref: str,
    ) -> None:
        self.supabase_origin = supabase_origin
        self.chatwoot = chatwoot
        self.connector_tenant_ref = connector_tenant_ref
        self.requests: list[str] = []
        self.rpcs: list[tuple[str, dict[str, object]]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(f"{request.method} {url}")
        if url.startswith(f"{self.supabase_origin}/rest/v1/rpc/"):
            assert request.headers["apikey"] == "service-role-secret"
            name = request.url.path.removeprefix("/rest/v1/rpc/")
            payload = json.loads(request.content)
            self.rpcs.append((name, payload))
            return httpx.Response(200, json=self._rpc_result(name, payload))
        if url.startswith(f"{CHATWOOT_ORIGIN}/") and request.url.path in self.chatwoot:
            assert request.headers["api_access_token"] == "chatwoot-secret"
            status, body = self.chatwoot[request.url.path]
            return httpx.Response(status, json=body)
        if url == f"{CONNECTOR_ORIGIN}/ready":
            assert request.headers["x-expected-tenant-ref"] == self.connector_tenant_ref
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected request: {request.method} {url}")

    @staticmethod
    def _rpc_result(name: str, payload: dict[str, object]) -> object:
        # La forma de lo que devuelve cada funcion (supabase/migrations).
        if name == "configure_daily_feedback_scope_v2":
            return {
                "status": "configured",
                "schedule_id": "11111111-1111-4111-8111-111111111111",
            }
        if name == "get_daily_feedback_readiness_v1":
            return {"status": "ok", "delivery_unknown_count": 0, "checked_at": payload["p_now"]}
        if name == "get_daily_feedback_review_page_v1":
            return REVIEW_PAGE
        if name == "purge_expired_daily_feedback_v2":
            return {"status": "purged", "count": 0}
        if name in {
            "claim_daily_feedback_collection_v1",
            "claim_daily_feedback_notification_v1",
        }:
            return {"status": "idle"}
        raise AssertionError(f"unexpected rpc: {name}")

    def rpc_names(self) -> list[str]:
        return [name for name, _ in self.rpcs]

    def rpc_payload(self, name: str) -> dict[str, object]:
        return next(payload for rpc, payload in self.rpcs if rpc == name)


def _route_through(monkeypatch: pytest.MonkeyPatch, network: InstanceNetwork) -> None:
    """Todo cliente de httpx creado sin transport sale por la red falsa. Asi el
    repositorio, el colector y el productor son los que arma la fabrica, con sus
    validaciones y sus pedidos reales. El TestClient trae su propio transport."""
    for client_class in (httpx.Client, httpx.AsyncClient):
        original_init = client_class.__init__

        def init(self, *args, transport=None, _original_init=original_init, **kwargs):
            if transport is None:
                transport = httpx.MockTransport(network.handle)
            _original_init(self, *args, transport=transport, **kwargs)

        monkeypatch.setattr(client_class, "__init__", init)


def test_att1_set_starts_the_real_factory_and_ready_lists_the_three_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bridge.daily_feedback_app import create_application_from_env

    network = InstanceNetwork(
        supabase_origin="http://att1-gateway:8080",
        chatwoot=_att1_chatwoot(),
        connector_tenant_ref="att1",
    )
    _route_through(monkeypatch, network)

    app = create_application_from_env(_att1_environment())
    assert network.requests == []  # armar la app no sale a la red

    with TestClient(app) as client:
        started = list(network.requests)
        ready = client.get("/ready")
        page = client.get(f"/daily-feedback/review/{REVIEW_REF}", headers=SESSION)
        run = client.post("/internal/v1/daily-feedback/run", headers=MANUAL_RUN)

    # El arranque: la configuracion en la base propia por http interno y el preflight
    # sin vincular (el inbox, su agent_bot vacio y el bot de la cuenta), y el conector.
    assert started == [
        "POST http://att1-gateway:8080/rest/v1/rpc/configure_daily_feedback_scope_v2",
        "GET https://chatwoot.example.test/api/v1/accounts/2/inboxes/11",
        "GET https://chatwoot.example.test/api/v1/accounts/2/inboxes/11/agent_bot",
        "GET https://chatwoot.example.test/api/v1/accounts/2/agent_bots/2",
        "GET https://connector.example.test/ready",
    ]
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "daily_feedback": "staged",
        "exceptions": ATT1_EXCEPTIONS,
    }
    configure = network.rpc_payload("configure_daily_feedback_scope_v2")
    assert (
        configure["p_scope_ref"],
        configure["p_chatwoot_account_id"],
        configure["p_chatwoot_inbox_id"],
        configure["p_chatwoot_agent_bot_id"],
        configure["p_timezone_name"],
        configure["p_cutoff_local"],
        configure["p_deletion_policy_ref"],
        configure["p_enabled"],
    ) == (
        "att1-agent-bot",
        2,
        11,
        2,
        "America/Mexico_City",
        "18:00:00",
        "att1-joint-reviewer-accountability-v1",
        False,
    )

    # La pagina de revision lleva la marca de la instancia.
    assert page.status_code == 200
    assert '<span class="brand-name">Dra. Nina Garza</span>' in page.text
    assert '<span class="brand-name">Johanna</span>' not in page.text

    # La corrida manual pide la recoleccion con la lease configurada; la de la
    # notificacion sigue fija en 120 s.
    assert run.json() == {"collected": False, "notified": False, "purged": 0}
    assert network.rpc_names() == [
        "configure_daily_feedback_scope_v2",
        "get_daily_feedback_readiness_v1",
        "get_daily_feedback_review_page_v1",
        "purge_expired_daily_feedback_v2",
        "claim_daily_feedback_collection_v1",
        "claim_daily_feedback_notification_v1",
    ]
    assert network.rpc_payload("claim_daily_feedback_collection_v1")["p_lease_seconds"] == 300
    assert network.rpc_payload("claim_daily_feedback_notification_v1")["p_lease_seconds"] == 120
    assert all(
        request.startswith("POST http://att1-gateway:8080/")
        for request in network.requests
        if "/rest/v1/" in request
    )


@pytest.mark.parametrize("new_variables", ["absent", "empty"])
def test_johanna_set_through_the_real_factory_keeps_todays_ready_and_requests(
    monkeypatch: pytest.MonkeyPatch, new_variables: str
) -> None:
    from bridge.daily_feedback_app import create_application_from_env

    environment = _with(
        _johanna_environment(),
        {"CHATWOOT_ACCOUNT_ID": "1", "CHATWOOT_INBOX_ID": "9", "CHATWOOT_AGENT_BOT_ID": "1"},
    )
    if new_variables == "empty":  # el compose las pasa con ${VAR:-}
        environment.update({name: "" for name in NEW_VARIABLES})
    network = InstanceNetwork(
        supabase_origin="https://project.supabase.co",
        chatwoot=_johanna_chatwoot(),
        connector_tenant_ref="johanna",
    )
    _route_through(monkeypatch, network)

    with TestClient(create_application_from_env(environment)) as client:
        started = list(network.requests)
        ready = client.get("/ready")
        page = client.get(f"/daily-feedback/review/{REVIEW_REF}", headers=SESSION)
        run = client.post("/internal/v1/daily-feedback/run", headers=MANUAL_RUN)

    # Los dos pedidos de siempre a Chatwoot: el inbox y su bot, que es el configurado.
    assert started == [
        "POST https://project.supabase.co/rest/v1/rpc/configure_daily_feedback_scope_v2",
        "GET https://chatwoot.example.test/api/v1/accounts/1/inboxes/9",
        "GET https://chatwoot.example.test/api/v1/accounts/1/inboxes/9/agent_bot",
        "GET https://connector.example.test/ready",
    ]
    assert ready.status_code == 200
    assert ready.content == b'{"status":"ready","daily_feedback":"staged"}'
    assert '<span class="brand-name">Johanna</span>' in page.text
    assert run.json() == {"collected": False, "notified": False, "purged": 0}
    assert network.rpc_payload("claim_daily_feedback_collection_v1")["p_lease_seconds"] == 120


def test_att1_set_does_not_start_when_the_inbox_has_a_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bridge.daily_feedback_app import create_application_from_env
    from bridge.daily_feedback_export import ConversationCollectionError

    # Alguien vinculo un bot al inbox 11: responde como el inbox 9 de Johanna.
    network = InstanceNetwork(
        supabase_origin="http://att1-gateway:8080",
        chatwoot=_att1_chatwoot(
            **{
                "/api/v1/accounts/2/inboxes/11/agent_bot": (
                    JOHANNA_INBOX_AGENT_BOT["status"],
                    JOHANNA_INBOX_AGENT_BOT["body"],
                )
            }
        ),
        connector_tenant_ref="att1",
    )
    _route_through(monkeypatch, network)
    app = create_application_from_env(_att1_environment())

    with pytest.raises(
        ConversationCollectionError, match="^chatwoot_agent_bot_unexpectedly_linked$"
    ):
        with TestClient(app):
            pass

    # Ni el bot de la cuenta (su respuesta trae el token) ni el conector se piden.
    assert network.requests == [
        "POST http://att1-gateway:8080/rest/v1/rpc/configure_daily_feedback_scope_v2",
        "GET https://chatwoot.example.test/api/v1/accounts/2/inboxes/11",
        "GET https://chatwoot.example.test/api/v1/accounts/2/inboxes/11/agent_bot",
    ]


def test_factory_wires_a_self_hosted_instance_without_network_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bridge.daily_feedback_app import create_application_from_env
    from bridge.daily_feedback_export import RealConversationSecurityPolicy

    network = InstanceNetwork(
        supabase_origin="http://att1-gateway:8080",
        chatwoot={},
        connector_tenant_ref="att1",
    )
    _route_through(monkeypatch, network)

    att1 = create_application_from_env(_att1_environment())
    johanna = create_application_from_env(_johanna_environment())

    assert network.requests == []

    def wiring(app) -> tuple[object, ...]:
        scheduler = app.state.daily_feedback_scheduler
        # El scheduler no expone el colector, y la politica y el tope de paginas solo
        # se ven ahi (el resto del cableado lo prueban los pedidos de los tests de
        # arriba).
        collector = scheduler._collector
        return (
            app.state.daily_feedback_runtime_settings.exceptions,
            collector.agent_bot_binding,
            collector.max_conversation_pages,
            collector.security_policy,
            scheduler.settings.collection_lease_seconds,
        )

    assert wiring(att1) == (
        tuple(ATT1_EXCEPTIONS),
        "unlinked",
        200,
        RealConversationSecurityPolicy(
            real_collection_enabled=True,
            storage_encryption_verified=False,
            retention_hours=72,
            deletion_owner="att1-joint-reviewer-accountability-v1",
            storage_risk_acceptance_ref=ATT1_RISK_ACCEPTANCE_REF,
        ),
        300,
    )
    assert wiring(johanna) == (
        (),
        "inbox",
        20,
        RealConversationSecurityPolicy(
            real_collection_enabled=True,
            storage_encryption_verified=True,
            retention_hours=72,
            deletion_owner="johanna-joint-reviewer-accountability-v1",
        ),
        120,
    )


# --- El deploy: las variables nuevas son opcionales ---------------------------------
# deploy/daily-feedback-compose.yaml las pasa con ${VAR:-} (vacia cuenta como ausente)
# y deploy/daily-feedback.env.example las trae comentadas: copiado tal cual, todo queda
# como hasta la 1.4.0. Los ejemplos comentados, sobre el set de ATT1, arrancan.


def test_deployment_passes_the_self_hosted_variables_as_optional() -> None:
    import re

    import yaml

    from bridge.daily_feedback_app import DailyFeedbackRuntimeSettings

    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "deploy/daily-feedback-compose.yaml").read_text())
    passed = compose["services"]["daily-feedback"]["environment"]
    env_example = (root / "deploy/daily-feedback.env.example").read_text()
    active = {
        line.split("=", 1)[0]
        for line in env_example.splitlines()
        if line and not line.startswith("#")
    }
    commented = dict(re.findall(r"(?m)^# ([A-Z][A-Z0-9_]*)=(.*)$", env_example))

    # Los dos archivos nombran las mismas variables: las de siempre, activas, y las
    # nuevas, solo comentadas (copiar el env.example no declara ninguna excepcion).
    assert set(commented) == set(NEW_VARIABLES)
    assert not active & set(NEW_VARIABLES)
    assert set(passed) == active | set(NEW_VARIABLES)
    for name in NEW_VARIABLES:
        # Ni :?required ni un valor por defecto en el compose: el default es de la app.
        assert passed[name] == f"${{{name}:-}}"
    # Los :?required de siempre no cambian, la evidencia de cifrado incluida.
    with_default = {"DAILY_FEEDBACK_SCHEDULER_ENABLED", "DAILY_FEEDBACK_POLL_INTERVAL_SECONDS"}
    for name in active - with_default:
        assert passed[name] == f"${{{name}:?required}}"

    # El valor de ejemplo de la aceptacion del riesgo es un placeholder que la app
    # rechaza: nada lee esa URL, asi que un placeholder con forma de URL arrancaria y
    # declararia la excepcion contra un documento que no existe.
    placeholder = commented["DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF"]
    assert placeholder.startswith("replace-with-")
    with pytest.raises(ValueError, match="^invalid_storage_risk_acceptance_ref$"):
        DailyFeedbackRuntimeSettings.from_env(_with(_att1_environment(), commented))

    # Con la aceptacion real en su lugar, ningun otro ejemplo comentado documenta un
    # valor que la app rechace.
    commented["DAILY_FEEDBACK_STORAGE_RISK_ACCEPTANCE_REF"] = ATT1_RISK_ACCEPTANCE_REF
    settings = DailyFeedbackRuntimeSettings.from_env(_with(_att1_environment(), commented))
    assert settings.exceptions == tuple(ATT1_EXCEPTIONS)
