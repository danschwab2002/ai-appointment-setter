"""Authenticated HTTPS review surface for production daily feedback batches."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import html
import json
import re
import secrets
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from bridge.daily_feedback import sanitize_review_text
from bridge.daily_feedback_export import DailyReviewPackage
from slack_correlation.catalog import NotificationCommand


class DailyFeedbackRepository(Protocol):
    async def rpc(self, name: str, payload: dict[str, object]) -> dict[str, object]: ...


class SlackOidcProvider(Protocol):
    def authorization_url(self, *, state: str, redirect_uri: str) -> str: ...

    async def authenticate(self, *, code: str, redirect_uri: str) -> "SlackIdentity": ...


@dataclass(frozen=True)
class SlackIdentity:
    issuer: str
    subject: str
    team_id: str
    user_id: str


@dataclass(frozen=True)
class DailyFeedbackWebSettings:
    public_origin: str
    session_hmac_key: bytes
    slack_team_id: str
    session_cookie_name: str = "__Host-daily_feedback_session"
    oidc_state_cookie_name: str = "__Host-daily_feedback_oidc"
    session_hours: int = 8

    def __post_init__(self) -> None:
        parsed = urlsplit(self.public_origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid_daily_feedback_public_origin")
        if len(self.session_hmac_key) < 32:
            raise ValueError("daily_feedback_session_key_too_short")
        if not self.slack_team_id.startswith("T"):
            raise ValueError("invalid_daily_feedback_slack_team")
        if not 1 <= self.session_hours <= 8:
            raise ValueError("invalid_daily_feedback_session_lifetime")


class DailyFeedbackService:
    def __init__(
        self,
        *,
        repository: DailyFeedbackRepository,
        oidc_client: SlackOidcProvider,
        settings: DailyFeedbackWebSettings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.oidc_client = oidc_client
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    def now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise RuntimeError("daily_feedback_clock_must_be_aware")
        return value.astimezone(UTC)

    def hash_secret(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def csrf_token(self, session_secret: str) -> str:
        return hmac.new(
            self.settings.session_hmac_key,
            ("daily-feedback-csrf-v1\0" + session_secret).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


class DailyFeedbackCollector(Protocol):
    def verify_access(self) -> None: ...

    def collect(
        self,
        *,
        tenant_ref: str,
        scope_ref: str,
        window_start: datetime,
        window_end: datetime,
    ) -> DailyReviewPackage: ...


class DailyFeedbackNotificationProducer(Protocol):
    async def verify_access(self) -> None: ...

    async def admit(self, command: NotificationCommand) -> object: ...


@dataclass(frozen=True)
class DailyFeedbackSchedulerSettings:
    worker_id: str
    tenant_ref: str
    scope_ref: str
    chatwoot_account_id: int
    chatwoot_inbox_id: int
    chatwoot_agent_bot_id: int
    deletion_owner: str
    poll_interval_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not all((self.worker_id, self.tenant_ref, self.scope_ref, self.deletion_owner)):
            raise ValueError("daily_feedback_scheduler_identity_required")
        if min(
            self.chatwoot_account_id,
            self.chatwoot_inbox_id,
            self.chatwoot_agent_bot_id,
        ) < 1:
            raise ValueError("daily_feedback_scheduler_chatwoot_authority_required")
        if not 5 <= self.poll_interval_seconds <= 300:
            raise ValueError("invalid_daily_feedback_poll_interval")


class DailyFeedbackScheduler:
    def __init__(
        self,
        *,
        repository: DailyFeedbackRepository,
        collector: DailyFeedbackCollector,
        producer: DailyFeedbackNotificationProducer,
        settings: DailyFeedbackSchedulerSettings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._collector = collector
        self._producer = producer
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._producer_closed = False
        self._last_iteration_failed = False
        self._preflight_complete = False

    @property
    def healthy(self) -> bool:
        return self._preflight_complete and not self._last_iteration_failed

    def now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise RuntimeError("daily_feedback_clock_must_be_aware")
        return value.astimezone(UTC)

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("daily_feedback_scheduler_already_started")
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="daily-feedback-scheduler")

    async def preflight(self) -> None:
        self._preflight_complete = False
        try:
            await asyncio.to_thread(self._collector.verify_access)
            await self._producer.verify_access()
        except Exception:
            self._last_iteration_failed = True
            raise
        self._last_iteration_failed = False
        self._preflight_complete = True

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            await task
        close = getattr(self._producer, "aclose", None)
        if close is not None and not self._producer_closed:
            await close()
            self._producer_closed = True

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
                self._last_iteration_failed = False
            except Exception:
                self._last_iteration_failed = True
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.poll_interval_seconds
                )
            except TimeoutError:
                pass

    async def run_once(self, *, force_collection: bool = False) -> dict[str, object]:
        now = self.now()
        purge = await self._repository.rpc(
            "purge_expired_daily_feedback_v1",
            {
                "p_now": _utc_text(now),
                "p_deletion_owner": self.settings.deletion_owner,
                "p_tenant_ref": self.settings.tenant_ref,
                "p_scope_ref": self.settings.scope_ref,
                "p_limit": 20,
            },
        )
        collected = await self._collect_one(now=now, force=force_collection)
        notified = await self._notify_one(now=now)
        return {
            "collected": collected,
            "notified": notified,
            "purged": int(purge.get("count", 0)),
        }

    async def _collect_one(self, *, now: datetime, force: bool) -> bool:
        claim_command = str(uuid4())
        claim = await self._repository.rpc(
            "claim_daily_feedback_collection_v1",
            {
                "p_command_id": claim_command,
                "p_semantic_fingerprint": _fingerprint(
                    "claim_collection",
                    claim_command,
                    self.settings.tenant_ref,
                    self.settings.scope_ref,
                    self.settings.worker_id,
                    _utc_text(now),
                    force,
                ),
                "p_worker_id": self.settings.worker_id,
                "p_tenant_ref": self.settings.tenant_ref,
                "p_scope_ref": self.settings.scope_ref,
                "p_now": _utc_text(now),
                "p_force": force,
                "p_lease_seconds": 120,
            },
        )
        if claim.get("status") == "idle":
            return False
        if (
            claim.get("status") not in {"claimed", "replayed"}
            or claim.get("tenant_ref") != self.settings.tenant_ref
            or claim.get("scope_ref") != self.settings.scope_ref
            or claim.get("chatwoot_account_id") != self.settings.chatwoot_account_id
            or claim.get("chatwoot_inbox_id") != self.settings.chatwoot_inbox_id
            or claim.get("chatwoot_agent_bot_id") != self.settings.chatwoot_agent_bot_id
        ):
            raise RuntimeError("daily_feedback_collection_claim_scope_mismatch")
        schedule_id = str(claim["schedule_id"])
        lease_generation = int(claim["lease_generation"])
        try:
            window_start = _parse_utc(str(claim["window_start"]))
            window_end = _parse_utc(str(claim["window_end"]))
            package = await asyncio.to_thread(
                self._collector.collect,
                tenant_ref=self.settings.tenant_ref,
                scope_ref=self.settings.scope_ref,
                window_start=window_start,
                window_end=window_end,
            )
            items, release_lineage = _package_items(package, claim)
            envelope = {
                "tenant_ref": package.tenant_ref,
                "scope_ref": package.scope_ref,
                "window_start": _utc_text(package.window_start),
                "window_end": _utc_text(package.window_end),
                "sanitizer_version": package.sanitizer_version,
                "selection_version": package.selection_version,
                "release_lineage": release_lineage,
                "items": items,
            }
            package_fingerprint = hashlib.sha256(
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            commit_command = str(uuid4())
            await self._repository.rpc(
                "commit_daily_feedback_batch_v1",
                {
                    "p_command_id": commit_command,
                    "p_semantic_fingerprint": _fingerprint(
                        "commit_batch",
                        commit_command,
                        self.settings.worker_id,
                        schedule_id,
                        lease_generation,
                        package_fingerprint,
                    ),
                    "p_worker_id": self.settings.worker_id,
                    "p_schedule_id": schedule_id,
                    "p_lease_generation": lease_generation,
                    "p_package_fingerprint": package_fingerprint,
                    "p_release_lineage": release_lineage,
                    "p_items": items,
                },
            )
            return True
        except Exception:
            failure_command = str(uuid4())
            try:
                await self._repository.rpc(
                    "fail_daily_feedback_collection_v1",
                    {
                        "p_command_id": failure_command,
                        "p_semantic_fingerprint": _fingerprint(
                            "fail_collection",
                            failure_command,
                            schedule_id,
                            lease_generation,
                        ),
                        "p_worker_id": self.settings.worker_id,
                        "p_schedule_id": schedule_id,
                        "p_lease_generation": lease_generation,
                        "p_error_code": "collection_failed",
                        "p_retry_seconds": 60,
                    },
                )
            except Exception:
                pass
            raise

    async def _notify_one(self, *, now: datetime) -> bool:
        claim_command = str(uuid4())
        claim = await self._repository.rpc(
            "claim_daily_feedback_notification_v1",
            {
                "p_command_id": claim_command,
                "p_semantic_fingerprint": _fingerprint(
                    "claim_notification",
                    claim_command,
                    self.settings.tenant_ref,
                    self.settings.scope_ref,
                    self.settings.worker_id,
                    _utc_text(now),
                ),
                "p_worker_id": self.settings.worker_id,
                "p_tenant_ref": self.settings.tenant_ref,
                "p_scope_ref": self.settings.scope_ref,
                "p_now": _utc_text(now),
                "p_lease_seconds": 120,
            },
        )
        if claim.get("status") == "idle":
            return False
        if (
            claim.get("tenant_ref") != self.settings.tenant_ref
            or claim.get("scope_ref") != self.settings.scope_ref
        ):
            raise RuntimeError("daily_feedback_notification_claim_scope_mismatch")
        batch_id = str(claim["batch_id"])
        lease_generation = int(claim["lease_generation"])
        started_command = str(uuid4())
        await self._repository.rpc(
            "mark_daily_feedback_notification_started_v1",
            {
                "p_command_id": started_command,
                "p_semantic_fingerprint": _fingerprint(
                    "notification_started",
                    started_command,
                    batch_id,
                    lease_generation,
                ),
                "p_worker_id": self.settings.worker_id,
                "p_batch_id": batch_id,
                "p_lease_generation": lease_generation,
            },
        )
        command = NotificationCommand(
            event_id=batch_id,
            event_code="REV-001",
            dedupe_key=hashlib.sha256(
                ("daily-feedback-ready-v1\0" + batch_id).encode("utf-8")
            ).hexdigest(),
            occurred_at=_parse_utc(str(claim["notification_occurred_at"])),
            subject_ref=None,
            reason_code=None,
            state="ready",
            count=int(claim["item_count"]),
            review_ref=str(claim["public_ref"]),
        )
        try:
            await self._producer.admit(command)
        except Exception:
            retry_command = str(uuid4())
            await self._repository.rpc(
                "retry_daily_feedback_notification_v1",
                {
                    "p_command_id": retry_command,
                    "p_semantic_fingerprint": _fingerprint(
                        "retry_notification",
                        retry_command,
                        batch_id,
                        lease_generation,
                    ),
                    "p_worker_id": self.settings.worker_id,
                    "p_batch_id": batch_id,
                    "p_lease_generation": lease_generation,
                    "p_error_code": "connector_admission_failed",
                    "p_retry_seconds": 60,
                },
            )
            raise
        complete_command = str(uuid4())
        await self._repository.rpc(
            "complete_daily_feedback_notification_v1",
            {
                "p_command_id": complete_command,
                "p_semantic_fingerprint": _fingerprint(
                    "complete_notification",
                    complete_command,
                    batch_id,
                    lease_generation,
                ),
                "p_worker_id": self.settings.worker_id,
                "p_batch_id": batch_id,
                "p_lease_generation": lease_generation,
            },
        )
        return True


def create_daily_feedback_review_app(service: DailyFeedbackService) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def harden_review_responses(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        try:
            response = await call_next(request)
        except Exception:
            response = HTMLResponse(_error_page("No se pudo completar la solicitud."), status_code=500)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        return response

    @app.get("/review/{public_ref}")
    async def review(public_ref: str, request: Request) -> Response:
        if not _canonical_uuid(public_ref):
            return _not_found()
        session_secret = request.cookies.get(service.settings.session_cookie_name)
        if not _valid_browser_secret(session_secret):
            return HTMLResponse(_login_page(public_ref), status_code=401)
        try:
            page = await service.repository.rpc(
                "get_daily_feedback_review_page_v1",
                {
                    "p_session_hash": service.hash_secret(session_secret),
                    "p_public_ref": public_ref,
                },
            )
        except Exception:
            return HTMLResponse(_login_page(public_ref), status_code=401)
        if page.get("status") == "complete":
            return HTMLResponse(_complete_page(page))
        if page.get("status") != "item" or not isinstance(page.get("item"), dict):
            return HTMLResponse(_error_page("El lote ya no está disponible."), status_code=410)
        return HTMLResponse(
            _review_page(
                page,
                public_ref=public_ref,
                csrf_token=service.csrf_token(session_secret),
                command_id=str(uuid4()),
            )
        )

    @app.get("/auth/slack/start")
    async def slack_start(batch_ref: str, request: Request) -> Response:
        if not _canonical_uuid(batch_ref):
            return _not_found()
        state = secrets.token_urlsafe(32)
        state_hash = service.hash_secret(state)
        return_path = f"/daily-feedback/review/{batch_ref}"
        try:
            await service.repository.rpc(
                "begin_daily_feedback_oidc_v1",
                {
                    "p_state_hash": state_hash,
                    "p_public_ref": batch_ref,
                    "p_return_path": return_path,
                    "p_expires_at": _utc_text(service.now() + timedelta(minutes=5)),
                },
            )
        except Exception:
            return HTMLResponse(_error_page("El lote ya no está disponible."), status_code=410)
        redirect_uri = (
            service.settings.public_origin.rstrip("/")
            + "/daily-feedback/auth/slack/callback"
        )
        target = service.oidc_client.authorization_url(
            state=state,
            redirect_uri=redirect_uri,
        )
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            service.settings.oidc_state_cookie_name,
            state,
            max_age=300,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/auth/slack/callback")
    async def slack_callback(code: str, state: str, request: Request) -> Response:
        cookie_state = request.cookies.get(service.settings.oidc_state_cookie_name)
        if (
            not _valid_browser_secret(state)
            or not _valid_browser_secret(cookie_state)
            or not hmac.compare_digest(state, cookie_state)
            or not code
        ):
            return HTMLResponse(_error_page("Autenticación inválida."), status_code=401)
        redirect_uri = (
            service.settings.public_origin.rstrip("/")
            + "/daily-feedback/auth/slack/callback"
        )
        try:
            identity = await service.oidc_client.authenticate(
                code=code,
                redirect_uri=redirect_uri,
            )
        except Exception:
            return HTMLResponse(_error_page("Autenticación inválida."), status_code=401)
        if identity.team_id != service.settings.slack_team_id:
            return HTMLResponse(_error_page("Revisor no autorizado."), status_code=403)
        session_secret = secrets.token_urlsafe(32)
        try:
            result = await service.repository.rpc(
                "complete_daily_feedback_oidc_v1",
                {
                    "p_state_hash": service.hash_secret(state),
                    "p_session_hash": service.hash_secret(session_secret),
                    "p_oidc_issuer": identity.issuer,
                    "p_oidc_subject": identity.subject,
                    "p_slack_team_id": identity.team_id,
                    "p_slack_user_id": identity.user_id,
                    "p_session_expires_at": _utc_text(
                        service.now() + timedelta(hours=service.settings.session_hours)
                    ),
                },
            )
        except Exception:
            return HTMLResponse(_error_page("Revisor no autorizado."), status_code=403)
        return_path = result.get("return_path")
        if not isinstance(return_path, str) or not _safe_review_return_path(return_path):
            return HTMLResponse(_error_page("Autenticación inválida."), status_code=401)
        response = RedirectResponse(return_path, status_code=303)
        response.delete_cookie(
            service.settings.oidc_state_cookie_name,
            path="/",
        )
        response.set_cookie(
            service.settings.session_cookie_name,
            session_secret,
            max_age=service.settings.session_hours * 3600,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/review/{public_ref}/decisions")
    async def record_decision(public_ref: str, request: Request) -> Response:
        if not _canonical_uuid(public_ref):
            return _not_found()
        if request.headers.get("origin") != service.settings.public_origin.rstrip("/"):
            return HTMLResponse(_error_page("Origen no autorizado."), status_code=403)
        session_secret = request.cookies.get(service.settings.session_cookie_name)
        if not _valid_browser_secret(session_secret):
            return HTMLResponse(_login_page(public_ref), status_code=401)
        try:
            form = await _bounded_form(request)
        except ValueError:
            return HTMLResponse(_error_page("Formulario inválido."), status_code=422)
        required = {"csrf_token", "command_id", "item_id", "decision", "verbatim_feedback"}
        if set(form) != required:
            return HTMLResponse(_error_page("Formulario inválido."), status_code=422)
        expected_csrf = service.csrf_token(session_secret)
        if not hmac.compare_digest(form["csrf_token"], expected_csrf):
            return HTMLResponse(_error_page("Formulario inválido."), status_code=403)
        command_id = form["command_id"]
        item_id = form["item_id"]
        decision = form["decision"]
        feedback = form["verbatim_feedback"]
        if (
            not _canonical_uuid(command_id)
            or not _canonical_uuid(item_id)
            or decision not in {"correct", "correct_with_feedback", "skip"}
            or (decision == "correct_with_feedback" and not feedback.strip())
            or (decision in {"correct", "skip"} and feedback != "")
            or len(feedback) > 4000
            or any(ord(character) < 32 and character not in "\n\t" for character in feedback)
        ):
            return HTMLResponse(_error_page("Decisión inválida."), status_code=422)
        literal_feedback = feedback if decision == "correct_with_feedback" else None
        semantic = "\0".join(
            (command_id, public_ref, item_id, decision, literal_feedback or "")
        )
        try:
            await service.repository.rpc(
                "record_daily_feedback_decision_v1",
                {
                    "p_command_id": command_id,
                    "p_semantic_fingerprint": hashlib.sha256(
                        semantic.encode("utf-8")
                    ).hexdigest(),
                    "p_session_hash": service.hash_secret(session_secret),
                    "p_public_ref": public_ref,
                    "p_item_id": item_id,
                    "p_decision": decision,
                    "p_verbatim_feedback": literal_feedback,
                },
            )
        except Exception:
            return HTMLResponse(_error_page("No se pudo registrar la decisión."), status_code=409)
        return RedirectResponse(
            f"/daily-feedback/review/{public_ref}",
            status_code=303,
        )

    return app


class SlackOpenIdClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("slack_oidc_credentials_required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._transport = transport

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return "https://slack.com/openid/connect/authorize?" + urlencode(
            {
                "response_type": "code",
                "scope": "openid profile",
                "client_id": self._client_id,
                "state": state,
                "redirect_uri": redirect_uri,
            }
        )

    async def authenticate(self, *, code: str, redirect_uri: str) -> SlackIdentity:
        async with httpx.AsyncClient(
            base_url="https://slack.com",
            transport=self._transport,
            timeout=15,
        ) as client:
            token_response = await client.post(
                "/api/openid.connect.token",
                data={
                    "code": code,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token_response.raise_for_status()
            token_body = token_response.json()
            access_token = (
                token_body.get("access_token") if isinstance(token_body, dict) else None
            )
            if not isinstance(access_token, str) or not access_token:
                raise ValueError("slack_oidc_token_invalid")
            user_response = await client.get(
                "/api/openid.connect.userInfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            user_response.raise_for_status()
            user = user_response.json()
        team_id = (
            user.get("https://slack.com/team_id") if isinstance(user, dict) else None
        )
        user_id = (
            user.get("https://slack.com/user_id") if isinstance(user, dict) else None
        )
        subject = user.get("sub") if isinstance(user, dict) else None
        issuer = "https://slack.com"
        if (
            not isinstance(team_id, str)
            or not re.fullmatch(r"T[A-Z0-9]{8,}", team_id)
            or not isinstance(user_id, str)
            or not re.fullmatch(r"[UW][A-Z0-9]{8,}", user_id)
            or not isinstance(subject, str)
            or subject != f"https://slack.com/user_id/{user_id}"
        ):
            raise ValueError("slack_oidc_identity_invalid")
        return SlackIdentity(
            issuer=issuer,
            subject=subject,
            team_id=team_id,
            user_id=user_id,
        )


class SupabaseDailyFeedbackRepository:
    _ALLOWED_RPCS = frozenset(
        {
            "configure_daily_feedback_scope_v1",
            "claim_daily_feedback_collection_v1",
            "commit_daily_feedback_batch_v1",
            "fail_daily_feedback_collection_v1",
            "claim_daily_feedback_notification_v1",
            "mark_daily_feedback_notification_started_v1",
            "complete_daily_feedback_notification_v1",
            "retry_daily_feedback_notification_v1",
            "begin_daily_feedback_oidc_v1",
            "complete_daily_feedback_oidc_v1",
            "get_daily_feedback_review_page_v1",
            "record_daily_feedback_decision_v1",
            "purge_expired_daily_feedback_v1",
        }
    )

    def __init__(
        self,
        *,
        base_url: str,
        service_role_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid_supabase_origin")
        if not service_role_key:
            raise ValueError("supabase_service_role_key_required")
        self._base_url = base_url.rstrip("/")
        self._key = service_role_key
        self._transport = transport

    async def rpc(self, name: str, payload: dict[str, object]) -> dict[str, object]:
        if name not in self._ALLOWED_RPCS:
            raise ValueError("daily_feedback_rpc_not_allowed")
        retry_safe = "p_command_id" in payload or name.startswith(
            ("get_daily_feedback_", "purge_expired_daily_feedback_")
        )
        attempts = 2 if retry_safe else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    headers={
                        "apikey": self._key,
                        "Authorization": f"Bearer {self._key}",
                        "Content-Type": "application/json",
                    },
                    transport=self._transport,
                    timeout=20,
                ) as client:
                    response = await client.post(f"/rest/v1/rpc/{name}", json=payload)
                if response.status_code >= 500 and attempt + 1 < attempts:
                    continue
                response.raise_for_status()
                body = response.json()
                if isinstance(body, dict):
                    return body
                if (
                    isinstance(body, list)
                    and len(body) == 1
                    and isinstance(body[0], dict)
                ):
                    return body[0]
                raise ValueError("invalid_daily_feedback_rpc_response")
            except httpx.TransportError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise
        assert last_error is not None
        raise last_error


def _fingerprint(operation: str, *parts: object) -> str:
    encoded = json.dumps(
        [operation, *parts],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_utc(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise RuntimeError("daily_feedback_timestamp_must_be_aware")
    return parsed.astimezone(UTC)


def _package_items(
    package: DailyReviewPackage,
    claim: dict[str, object],
) -> tuple[list[dict[str, object]], str]:
    if (
        package.tenant_ref != claim.get("tenant_ref")
        or package.scope_ref != claim.get("scope_ref")
        or _utc_text(package.window_start)
        != _utc_text(_parse_utc(str(claim["window_start"])))
        or _utc_text(package.window_end)
        != _utc_text(_parse_utc(str(claim["window_end"])))
        or package.sanitizer_version != claim.get("sanitizer_version")
        or package.selection_version != claim.get("selection_version")
    ):
        raise RuntimeError("daily_feedback_package_claim_mismatch")

    seen_conversations: set[str] = set()
    seen_messages: set[str] = set()
    lineages: set[str] = set()
    items: list[dict[str, object]] = []
    for conversation in package.conversations:
        if conversation.conversation_ref in seen_conversations:
            raise RuntimeError("duplicate_daily_feedback_conversation")
        seen_conversations.add(conversation.conversation_ref)
        for text in (
            conversation.display_label,
            conversation.apparent_objective,
            conversation.observed_outcome,
        ):
            if sanitize_review_text(text) != text:
                raise RuntimeError("daily_feedback_package_not_minimized")
        messages: list[dict[str, str]] = []
        previous_at: datetime | None = None
        for message in conversation.messages:
            if message.message_ref in seen_messages:
                raise RuntimeError("duplicate_daily_feedback_message")
            seen_messages.add(message.message_ref)
            occurred_at = message.occurred_at.astimezone(UTC)
            if not (package.window_start <= occurred_at < package.window_end):
                raise RuntimeError("daily_feedback_message_outside_window")
            if previous_at is not None and occurred_at < previous_at:
                raise RuntimeError("daily_feedback_messages_not_ordered")
            previous_at = occurred_at
            if sanitize_review_text(message.text) != message.text:
                raise RuntimeError("daily_feedback_package_not_minimized")
            messages.append(
                {
                    "actor": message.actor,
                    "occurred_at": _utc_text(occurred_at),
                    "text": message.text,
                }
            )
        lineage = (
            "release_lineage_unavailable"
            if conversation.release_id == "release_lineage_unavailable"
            else f"{conversation.release_id}@{conversation.release_version}"
        )
        lineages.add(lineage)
        items.append(
            {
                "conversation_ref": conversation.conversation_ref,
                "display_label": conversation.display_label,
                "apparent_objective": conversation.apparent_objective,
                "observed_outcome": conversation.observed_outcome,
                "release_id": conversation.release_id,
                "release_version": conversation.release_version,
                "messages": messages,
            }
        )
    release_lineage = (
        next(iter(lineages))
        if len(lineages) == 1
        else "release_lineage_unavailable"
    )
    return items, release_lineage


def _canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _valid_browser_secret(value: str | None) -> bool:
    return isinstance(value, str) and 32 <= len(value) <= 128 and all(
        character.isalnum() or character in "-_" for character in value
    )


def _safe_review_return_path(value: str) -> bool:
    prefix = "/daily-feedback/review/"
    return value.startswith(prefix) and _canonical_uuid(value[len(prefix) :])


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def _bounded_form(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise ValueError("invalid_form_content_type")
    body = await request.body()
    if len(body) > 16_384:
        raise ValueError("form_too_large")
    try:
        parsed = parse_qs(
            body.decode("utf-8", errors="strict"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=8,
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid_form") from exc
    if any(len(values) != 1 for values in parsed.values()):
        raise ValueError("duplicate_form_field")
    return {key: values[0] for key, values in parsed.items()}


def _page_shell(title: str, body: str) -> str:
    return f'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{--ink:#17221c;--muted:#5b6b62;--paper:#fbfaf6;--line:#d8ddd8;--accent:#176b4b;--soft:#eef4ef;--danger:#8c2f27}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:Georgia,'Times New Roman',serif;line-height:1.55}}
main{{width:min(760px,calc(100% - 32px));margin:32px auto 64px}}header{{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:24px}}
h1{{font-size:clamp(1.8rem,5vw,2.7rem);line-height:1.05;margin:0 0 8px}}.meta,.muted{{color:var(--muted);font-family:ui-sans-serif,system-ui,sans-serif;font-size:.92rem}}
.conversation{{border:1px solid var(--line);background:white;padding:clamp(18px,4vw,32px)}}.message{{padding:14px 0;border-top:1px solid var(--line)}}.message:first-child{{border-top:0}}
.actor{{font:700 .78rem ui-sans-serif,system-ui,sans-serif;text-transform:uppercase;letter-spacing:.08em;color:var(--accent)}}
.actions{{margin-top:24px;display:grid;gap:12px}}textarea{{width:100%;min-height:112px;padding:12px;border:1px solid var(--line);font:inherit}}
button,.button{{min-height:46px;border:1px solid var(--ink);background:white;color:var(--ink);padding:10px 16px;font:700 .95rem ui-sans-serif,system-ui,sans-serif;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center}}
button.primary,.button.primary{{background:var(--accent);border-color:var(--accent);color:white}}button:focus-visible,.button:focus-visible,textarea:focus-visible{{outline:3px solid #e0a93a;outline-offset:3px}}
@media(max-width:560px){{main{{margin-top:18px}}.conversation{{padding:18px}}}}
</style></head><body><main>{body}</main></body></html>'''


def _login_page(public_ref: str) -> str:
    return _page_shell(
        "Revisión diaria",
        f'''<header><p class="meta">Acceso privado</p><h1>Revisión diaria</h1></header>
<p>Inicia sesión con la cuenta de Slack autorizada para revisar este lote.</p>
<a class="button primary" href="/daily-feedback/auth/slack/start?batch_ref={html.escape(public_ref)}">Iniciar sesión con Slack</a>''',
    )


def _review_page(
    page: dict[str, object],
    *,
    public_ref: str,
    csrf_token: str,
    command_id: str,
) -> str:
    item = page["item"]
    assert isinstance(item, dict)
    messages = item.get("messages")
    assert isinstance(messages, list)
    transcript = "".join(
        f'''<div class="message"><div class="actor">{html.escape("Prospecto" if message.get("actor") == "prospect" else "Agente")}</div>
<div>{html.escape(str(message.get("text", "")))}</div></div>'''
        for message in messages
        if isinstance(message, dict)
    )
    position = int(item.get("position", 0))
    total = int(page.get("item_count", 0))
    body = f'''<header><p class="meta">{html.escape(str(page.get("local_date", "")))} · {position} de {total}</p><h1>Revisión diaria</h1>
<p class="muted">{html.escape(str(item.get("display_label", "")))}</p></header>
<section class="conversation" aria-label="Conversación actual">
<p><strong>Objetivo aparente:</strong> {html.escape(str(item.get("apparent_objective", "")))}</p>
<p><strong>Resultado observado:</strong> {html.escape(str(item.get("observed_outcome", "")))}</p>
<div>{transcript}</div></section>
<form class="actions" method="post" action="/daily-feedback/review/{html.escape(public_ref)}/decisions">
<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
<input type="hidden" name="command_id" value="{html.escape(command_id)}">
<input type="hidden" name="item_id" value="{html.escape(str(item.get("item_id", "")))}">
<label for="feedback"><strong>Feedback literal</strong> <span class="muted">(obligatorio sólo con feedback)</span></label>
<textarea id="feedback" name="verbatim_feedback" maxlength="4000"></textarea>
<button class="primary" type="submit" name="decision" value="correct">Correcta</button>
<button type="submit" name="decision" value="correct_with_feedback">Correcta con feedback</button>
<button type="submit" name="decision" value="skip">Omitir</button>
</form>'''
    return _page_shell("Revisión diaria", body)


def _complete_page(page: dict[str, object]) -> str:
    return _page_shell(
        "Revisión completada",
        f'''<header><p class="meta">{html.escape(str(page.get("local_date", "")))}</p><h1>Revisión completada</h1></header>
<p>Se registraron {int(page.get("decided_count", 0))} decisiones.</p>''',
    )


def _error_page(message: str) -> str:
    return _page_shell("Revisión diaria", f"<header><h1>Revisión diaria</h1></header><p>{html.escape(message)}</p>")


def _not_found() -> HTMLResponse:
    return HTMLResponse(_error_page("Lote no encontrado."), status_code=404)
