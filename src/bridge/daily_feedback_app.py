"""Dedicated production ASGI application for daily feedback reports."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
import re
from typing import AsyncGenerator, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bridge.daily_feedback_export import (
    ChatwootDailyCollector,
    RealConversationSecurityPolicy,
)
from bridge.daily_feedback_service import (
    DailyFeedbackRepository,
    DailyFeedbackScheduler,
    DailyFeedbackSchedulerSettings,
    DailyFeedbackService,
    DailyFeedbackWebSettings,
    SlackOpenIdClient,
    SupabaseDailyFeedbackRepository,
    create_daily_feedback_review_app,
)
from slack_correlation.producer import SlackConnectorProducer


class DailyFeedbackSchedulerRuntime(Protocol):
    @property
    def healthy(self) -> bool: ...

    async def start(self) -> None: ...

    async def preflight(self) -> None: ...

    async def stop(self) -> None: ...

    async def run_once(self, *, force_collection: bool = False) -> dict[str, object]: ...


@dataclass(frozen=True)
class DailyFeedbackReviewer:
    reviewer_ref: str
    slack_user_id: str
    deletion_accountable: bool

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.reviewer_ref):
            raise ValueError("invalid_daily_feedback_reviewer_ref")
        if not re.fullmatch(r"[UW][A-Z0-9]{8,}", self.slack_user_id):
            raise ValueError("invalid_daily_feedback_slack_user_id")
        if type(self.deletion_accountable) is not bool:
            raise ValueError("invalid_daily_feedback_deletion_accountability")


@dataclass(frozen=True)
class DailyFeedbackApplicationSettings:
    manual_run_token: str = field(repr=False)
    tenant_ref: str
    scope_ref: str
    reviewers: tuple[DailyFeedbackReviewer, ...]
    slack_team_id: str
    chatwoot_account_id: int
    chatwoot_inbox_id: int
    chatwoot_agent_bot_id: int
    timezone: str
    daily_at: str
    retention_hours: int
    deletion_policy_ref: str
    sanitizer_version: str
    selection_version: str
    renderer_version: str
    scheduler_enabled: bool = True

    def __post_init__(self) -> None:
        if len(self.manual_run_token) < 32:
            raise ValueError("daily_feedback_manual_run_token_too_short")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.tenant_ref):
            raise ValueError("invalid_daily_feedback_tenant_ref")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", self.scope_ref):
            raise ValueError("invalid_daily_feedback_scope_ref")
        if not re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{1,127}", self.deletion_policy_ref
        ):
            raise ValueError("invalid_daily_feedback_deletion_policy_ref")
        reviewer_refs = [r.reviewer_ref for r in self.reviewers]
        slack_user_ids = [r.slack_user_id for r in self.reviewers]
        if len(set(reviewer_refs)) != len(reviewer_refs):
            raise ValueError("duplicate_daily_feedback_reviewer_ref")
        if len(set(slack_user_ids)) != len(slack_user_ids):
            raise ValueError("duplicate_daily_feedback_slack_user_id")
        if len(self.reviewers) != 4:
            raise ValueError("daily_feedback_reviewer_set_must_have_four")
        if not all(r.deletion_accountable for r in self.reviewers):
            raise ValueError("daily_feedback_all_reviewers_must_be_deletion_accountable")
        if not re.fullmatch(r"T[A-Z0-9]{8,}", self.slack_team_id):
            raise ValueError("invalid_daily_feedback_slack_team_id")
        if min(
            self.chatwoot_account_id,
            self.chatwoot_inbox_id,
            self.chatwoot_agent_bot_id,
        ) < 1:
            raise ValueError("invalid_daily_feedback_chatwoot_authority")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("invalid_daily_feedback_timezone") from exc
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]", self.daily_at):
            raise ValueError("invalid_daily_feedback_daily_at")
        if not 24 <= self.retention_hours <= 168:
            raise ValueError("invalid_daily_feedback_retention_hours")
        for version in (
            self.sanitizer_version,
            self.selection_version,
            self.renderer_version,
        ):
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,127}", version):
                raise ValueError("invalid_daily_feedback_version")

    def configure_payload(self) -> dict[str, object]:
        configuration: dict[str, object] = {
            "p_tenant_ref": self.tenant_ref,
            "p_scope_ref": self.scope_ref,
            "p_oidc_issuer": "https://slack.com",
            "p_slack_team_id": self.slack_team_id,
            "p_reviewers": [
                {
                    "deletion_accountable": reviewer.deletion_accountable,
                    "reviewer_ref": reviewer.reviewer_ref,
                    "slack_user_id": reviewer.slack_user_id,
                }
                for reviewer in sorted(self.reviewers, key=lambda item: item.reviewer_ref)
            ],
            "p_deletion_policy_ref": self.deletion_policy_ref,
            "p_chatwoot_account_id": self.chatwoot_account_id,
            "p_chatwoot_inbox_id": self.chatwoot_inbox_id,
            "p_chatwoot_agent_bot_id": self.chatwoot_agent_bot_id,
            "p_timezone_name": self.timezone,
            "p_cutoff_local": self.daily_at,
            "p_retention_hours": self.retention_hours,
            "p_sanitizer_version": self.sanitizer_version,
            "p_selection_version": self.selection_version,
            "p_renderer_version": self.renderer_version,
            "p_enabled": self.scheduler_enabled,
        }
        canonical = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(
            ("configure_daily_feedback_scope_v2\0" + canonical).encode("utf-8")
        ).hexdigest()
        return {
            "p_command_id": str(uuid4()),
            "p_semantic_fingerprint": fingerprint,
            **configuration,
        }


@dataclass(frozen=True)
class DailyFeedbackRuntimeSettings:
    application: DailyFeedbackApplicationSettings
    public_origin: str
    supabase_base_url: str
    supabase_service_role_key: str = field(repr=False)
    slack_oidc_client_id: str = field(repr=False)
    slack_oidc_client_secret: str = field(repr=False)
    slack_tenant_ref: str = "johanna"
    slack_connector_base_url: str = ""
    slack_connector_bearer_token: str = field(default="", repr=False)
    worker_id: str = ""
    chatwoot_base_url: str = ""
    chatwoot_account_id: int = 0
    chatwoot_inbox_id: int = 0
    chatwoot_agent_bot_id: int = 0
    chatwoot_access_token: str = field(default="", repr=False)
    pseudonymization_key: str = field(default="", repr=False)
    storage_encryption_evidence_ref: str = ""
    poll_interval_seconds: float = 30.0

    @classmethod
    def from_env(
        cls,
        environment: Mapping[str, str],
    ) -> "DailyFeedbackRuntimeSettings":
        def required(name: str) -> str:
            value = environment.get(name, "").strip()
            if not value:
                raise ValueError(f"{name}_required")
            return value

        if environment.get("DAILY_FEEDBACK_REAL_CONVERSATIONS_ENABLED", "").lower() != "true":
            raise ValueError("real_conversations_not_enabled")
        if environment.get("DAILY_FEEDBACK_STORAGE_ENCRYPTION_VERIFIED", "").lower() != "true":
            raise ValueError("storage_encryption_not_verified")

        public_origin = _validated_https_origin(
            required("DAILY_FEEDBACK_PUBLIC_ORIGIN"),
            "invalid_daily_feedback_public_origin",
        )
        supabase_base_url = _validated_https_origin(
            required("SUPABASE_BASE_URL"),
            "invalid_supabase_origin",
        )
        slack_connector_base_url = _validated_https_origin(
            required("SLACK_CONNECTOR_BASE_URL"),
            "invalid_slack_connector_origin",
        )
        chatwoot_base_url = _validated_https_origin(
            required("CHATWOOT_BASE_URL"),
            "invalid_chatwoot_origin",
        )
        evidence_ref = required("DAILY_FEEDBACK_STORAGE_ENCRYPTION_EVIDENCE_REF")
        evidence = urlsplit(evidence_ref)
        if evidence.scheme != "https" or not evidence.hostname or evidence.username or evidence.password:
            raise ValueError("invalid_storage_encryption_evidence_ref")

        def positive_int(name: str) -> int:
            try:
                value = int(required(name))
            except ValueError as exc:
                raise ValueError(f"invalid_{name.lower()}") from exc
            if value < 1:
                raise ValueError(f"invalid_{name.lower()}")
            return value

        retention_hours = positive_int("DAILY_FEEDBACK_RETENTION_HOURS")
        try:
            reviewer_payload = json.loads(required("DAILY_FEEDBACK_REVIEWERS_JSON"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_daily_feedback_reviewers_json") from exc
        if not isinstance(reviewer_payload, list) or not reviewer_payload:
            raise ValueError("invalid_daily_feedback_reviewers_json")
        reviewers: list[DailyFeedbackReviewer] = []
        expected_reviewer_keys = {
            "reviewer_ref",
            "slack_user_id",
            "deletion_accountable",
        }
        for reviewer in reviewer_payload:
            if not isinstance(reviewer, dict) or set(reviewer) != expected_reviewer_keys:
                raise ValueError("invalid_daily_feedback_reviewers_json")
            try:
                reviewers.append(
                    DailyFeedbackReviewer(
                        reviewer_ref=reviewer["reviewer_ref"],
                        slack_user_id=reviewer["slack_user_id"],
                        deletion_accountable=reviewer["deletion_accountable"],
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid_daily_feedback_reviewers_json") from exc
        scheduler_enabled_text = environment.get(
            "DAILY_FEEDBACK_SCHEDULER_ENABLED", "false"
        ).strip().lower()
        if scheduler_enabled_text not in {"true", "false"}:
            raise ValueError("invalid_daily_feedback_scheduler_enabled")
        application = DailyFeedbackApplicationSettings(
            manual_run_token=required("DAILY_FEEDBACK_MANUAL_RUN_TOKEN"),
            tenant_ref=required("DAILY_FEEDBACK_TENANT_REF"),
            scope_ref=required("DAILY_FEEDBACK_SCOPE_REF"),
            reviewers=tuple(sorted(reviewers, key=lambda item: item.reviewer_ref)),
            slack_team_id=required("SLACK_OIDC_TEAM_ID"),
            chatwoot_account_id=positive_int("CHATWOOT_ACCOUNT_ID"),
            chatwoot_inbox_id=positive_int("CHATWOOT_INBOX_ID"),
            chatwoot_agent_bot_id=positive_int("CHATWOOT_AGENT_BOT_ID"),
            timezone=required("DAILY_FEEDBACK_TIMEZONE"),
            daily_at=required("DAILY_FEEDBACK_DAILY_AT"),
            retention_hours=retention_hours,
            deletion_policy_ref=required("DAILY_FEEDBACK_DELETION_POLICY_REF"),
            sanitizer_version="deterministic-redaction-v1",
            selection_version="chatwoot-daily-agent-dialogues-v1",
            renderer_version="daily-feedback-web-v1",
            scheduler_enabled=scheduler_enabled_text == "true",
        )
        pseudonymization_key = required("DAILY_FEEDBACK_PSEUDONYMIZATION_KEY")
        if len(pseudonymization_key.encode("utf-8")) < 32:
            raise ValueError("daily_feedback_pseudonymization_key_too_short")
        return cls(
            application=application,
            public_origin=public_origin,
            supabase_base_url=supabase_base_url,
            supabase_service_role_key=required("SUPABASE_SERVICE_ROLE_KEY"),
            slack_oidc_client_id=required("SLACK_OIDC_CLIENT_ID"),
            slack_oidc_client_secret=required("SLACK_OIDC_CLIENT_SECRET"),
            slack_tenant_ref=required("DAILY_FEEDBACK_SLACK_TENANT_REF"),
            slack_connector_base_url=slack_connector_base_url,
            slack_connector_bearer_token=required("SLACK_CONNECTOR_BEARER_TOKEN"),
            worker_id=required("DAILY_FEEDBACK_WORKER_ID"),
            chatwoot_base_url=chatwoot_base_url,
            chatwoot_account_id=positive_int("CHATWOOT_ACCOUNT_ID"),
            chatwoot_inbox_id=positive_int("CHATWOOT_INBOX_ID"),
            chatwoot_agent_bot_id=positive_int("CHATWOOT_AGENT_BOT_ID"),
            chatwoot_access_token=required("CHATWOOT_API_ACCESS_TOKEN"),
            pseudonymization_key=pseudonymization_key,
            storage_encryption_evidence_ref=evidence_ref,
            poll_interval_seconds=float(environment.get("DAILY_FEEDBACK_POLL_INTERVAL_SECONDS", "30")),
        )


def _validated_https_origin(value: str, error: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(error)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(error) from exc
    return value.rstrip("/")


def create_daily_feedback_application(
    *,
    settings: DailyFeedbackApplicationSettings,
    repository: DailyFeedbackRepository,
    scheduler: DailyFeedbackSchedulerRuntime,
    review_app: FastAPI,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        app.state.ready = False
        await repository.rpc(
            "configure_daily_feedback_scope_v2",
            settings.configure_payload(),
        )
        await scheduler.preflight()
        if settings.scheduler_enabled:
            await scheduler.start()
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            if settings.scheduler_enabled:
                await scheduler.stop()

    app = FastAPI(title="Daily Feedback Reports", lifespan=lifespan)
    app.state.ready = False
    app.state.daily_feedback_scheduler = scheduler
    app.mount("/daily-feedback", review_app)

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        if not app.state.ready:
            return JSONResponse(
                {"status": "not_ready", "daily_feedback": "unavailable"},
                status_code=503,
            )
        if settings.scheduler_enabled and not scheduler.healthy:
            return JSONResponse(
                {"status": "not_ready", "daily_feedback": "scheduler_failed"},
                status_code=503,
            )
        try:
            durable_health = await repository.rpc(
                "get_daily_feedback_readiness_v1",
                {
                    "p_tenant_ref": settings.tenant_ref,
                    "p_scope_ref": settings.scope_ref,
                    "p_now": datetime.now(UTC).isoformat(),
                },
            )
            delivery_unknown_count = durable_health.get("delivery_unknown_count")
            if type(delivery_unknown_count) is not int:
                raise ValueError("invalid_daily_feedback_readiness_response")
        except Exception:
            return JSONResponse(
                {"status": "not_ready", "daily_feedback": "authority_unavailable"},
                status_code=503,
            )
        if delivery_unknown_count:
            return JSONResponse(
                {
                    "status": "not_ready",
                    "daily_feedback": "notification_reconciliation_required",
                },
                status_code=503,
            )
        mode = "operational" if settings.scheduler_enabled else "staged"
        return JSONResponse({"status": "ready", "daily_feedback": mode})

    @app.post("/internal/v1/daily-feedback/run")
    async def run_now(request: Request) -> JSONResponse:
        expected = f"Bearer {settings.manual_run_token}"
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse(
                {"status": "unauthorized"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        result = await scheduler.run_once(force_collection=True)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return app


def create_application_from_env(
    environment: Mapping[str, str] | None = None,
) -> FastAPI:
    runtime = DailyFeedbackRuntimeSettings.from_env(environment or os.environ)
    repository = SupabaseDailyFeedbackRepository(
        base_url=runtime.supabase_base_url,
        service_role_key=runtime.supabase_service_role_key,
    )
    oidc = SlackOpenIdClient(
        client_id=runtime.slack_oidc_client_id,
        client_secret=runtime.slack_oidc_client_secret,
    )
    session_hmac_key = hashlib.sha256(
        b"daily-feedback-session-v1\0"
        + runtime.pseudonymization_key.encode("utf-8")
    ).digest()
    service = DailyFeedbackService(
        repository=repository,
        oidc_client=oidc,
        settings=DailyFeedbackWebSettings(
            public_origin=runtime.public_origin,
            session_hmac_key=session_hmac_key,
            slack_team_id=runtime.application.slack_team_id,
        ),
    )
    collector = ChatwootDailyCollector(
        base_url=runtime.chatwoot_base_url,
        account_id=runtime.chatwoot_account_id,
        inbox_id=runtime.chatwoot_inbox_id,
        agent_bot_id=runtime.chatwoot_agent_bot_id,
        access_token=runtime.chatwoot_access_token,
        pseudonymization_key=runtime.pseudonymization_key.encode("utf-8"),
        security_policy=RealConversationSecurityPolicy(
            real_collection_enabled=True,
            storage_encryption_verified=True,
            retention_hours=runtime.application.retention_hours,
            deletion_owner=runtime.application.deletion_policy_ref,
        ),
    )
    producer = SlackConnectorProducer(
        base_url=runtime.slack_connector_base_url,
        bearer_token=runtime.slack_connector_bearer_token,
        expected_tenant_ref=runtime.slack_tenant_ref,
    )
    scheduler = DailyFeedbackScheduler(
        repository=repository,
        collector=collector,
        producer=producer,
        settings=DailyFeedbackSchedulerSettings(
            worker_id=runtime.worker_id,
            tenant_ref=runtime.application.tenant_ref,
            scope_ref=runtime.application.scope_ref,
            chatwoot_account_id=runtime.application.chatwoot_account_id,
            chatwoot_inbox_id=runtime.application.chatwoot_inbox_id,
            chatwoot_agent_bot_id=runtime.application.chatwoot_agent_bot_id,
            deletion_owner=runtime.application.deletion_policy_ref,
            poll_interval_seconds=runtime.poll_interval_seconds,
        ),
    )
    app = create_daily_feedback_application(
        settings=runtime.application,
        repository=repository,
        scheduler=scheduler,
        review_app=create_daily_feedback_review_app(service),
    )
    app.state.daily_feedback_runtime_settings = runtime
    return app
