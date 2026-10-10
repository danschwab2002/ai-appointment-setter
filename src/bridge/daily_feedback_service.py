"""Authenticated HTTPS review surface for production daily feedback batches."""

from __future__ import annotations

import asyncio
import base64
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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from bridge.daily_feedback import minimize_review_text_v2, sanitize_review_text
from bridge.daily_feedback_export import (
    PACKAGE_SCHEMA_V2,
    DailyReviewPackage,
    apply_agent_provenance,
    apply_conversation_context,
)
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


_SAFE_OIDC_ERROR_REFERENCES = frozenset(
    {
        "OIDC-IDENTITY-PAYLOAD",
        "OIDC-PROVIDER-TRANSPORT",
        "OIDC-TOKEN-BAD-CLIENT-SECRET",
        "OIDC-TOKEN-BAD-REDIRECT-URI",
        "OIDC-TOKEN-INVALID-CLIENT",
        "OIDC-TOKEN-INVALID-CLIENT-ID",
        "OIDC-TOKEN-INVALID-CODE",
        "OIDC-TOKEN-INVALID-GRANT",
        "OIDC-TOKEN-PAYLOAD",
        "OIDC-TOKEN-REJECTED",
        "OIDC-USERINFO-REJECTED",
    }
)


def _canonical_oidc_error_reference(value: object) -> str | None:
    if type(value) is not str:
        return None
    for canonical in _SAFE_OIDC_ERROR_REFERENCES:
        if value == canonical:
            return canonical
    return None


class SlackOpenIdError(ValueError):
    """Sanitized Slack OpenID failure safe to expose as an operator reference."""

    def __init__(self, reference: str) -> None:
        canonical = _canonical_oidc_error_reference(reference)
        if canonical is None:
            raise ValueError("invalid_slack_oidc_error_reference")
        super().__init__(canonical)
        self.reference = canonical


# La marca de la barra de la pagina de revision. Sin DAILY_FEEDBACK_BRAND_NAME es la
# de siempre, asi que la pagina de Johanna queda identica byte por byte.
DEFAULT_REVIEW_BRAND_NAME = "Johanna"


def validate_review_brand_name(value: object) -> str:
    """La marca de la pagina de revision: de 1 a 60 caracteres imprimibles, con algo
    visible (no solo espacios). No se filtran < > & ni comillas: la pagina la muestra
    escapada. Es publica para que la lectura de DAILY_FEEDBACK_BRAND_NAME aplique la
    misma regla que DailyFeedbackWebSettings.
    """
    if (
        type(value) is not str
        or not 1 <= len(value) <= 60
        or not value.isprintable()
        or not value.strip()
    ):
        raise ValueError("invalid_daily_feedback_brand_name")
    return value


@dataclass(frozen=True)
class DailyFeedbackWebSettings:
    public_origin: str
    session_hmac_key: bytes
    slack_team_id: str
    session_cookie_name: str = "__Host-daily_feedback_session"
    oidc_state_cookie_name: str = "__Host-daily_feedback_oidc"
    session_hours: int = 8
    # Zona horaria en la que se muestran las horas de los mensajes (la de los
    # revisores, no UTC). Los datos se guardan siempre en UTC.
    display_timezone: str = "UTC"
    # La marca del agente de la instancia (Johanna, Dra. Nina Garza). Solo la muestra
    # la pagina de revision; login, error y lote completado no llevan marca.
    brand_name: str = DEFAULT_REVIEW_BRAND_NAME

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.display_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("invalid_daily_feedback_display_timezone") from exc
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
        validate_review_brand_name(self.brand_name)


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


_REVIEW_INTERACTION_SCRIPT = """(()=>{const form=document.querySelector('[data-decision-form]');if(!form)return;const choice=form.querySelector('[data-reveal-feedback]');const direct=[...form.querySelectorAll('[data-direct-decision]')];const panel=form.querySelector('.feedback-panel');const feedback=form.querySelector('#feedback');const emptyFeedback=form.querySelector('[data-empty-feedback]');const save=form.querySelector('.save-correction');const toggleSave=()=>{save.disabled=feedback.value.trim().length===0;};choice.addEventListener('click',()=>{panel.hidden=false;emptyFeedback.disabled=true;feedback.disabled=false;choice.setAttribute('aria-expanded','true');toggleSave();feedback.focus();});direct.forEach((button)=>button.addEventListener('click',()=>{emptyFeedback.disabled=false;feedback.disabled=true;}));feedback.addEventListener('input',toggleSave);})();"""
_REVIEW_INTERACTION_SCRIPT_CSP = base64.b64encode(
    hashlib.sha256(_REVIEW_INTERACTION_SCRIPT.encode("utf-8")).digest()
).decode("ascii")


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


# La lease de la recoleccion. Todo tiene que entrar en ese tiempo, contado desde el
# `now` de run_once: listar las conversaciones de Chatwoot, bajar los mensajes de las
# que tuvieron actividad, el contexto y la procedencia. Si vence, el commit falla con
# stale_collection_lease y la recoleccion se reintenta a los 60 s. Por eso el limite
# de un inbox grande es este tiempo, no el tope de paginas. La SQL acepta de 30 a 900 s
# (claim_daily_feedback_collection_v1); 120 es el valor de siempre.
DEFAULT_COLLECTION_LEASE_SECONDS = 120


def validate_collection_lease_seconds(value: object) -> int:
    """Segundos enteros de 30 a 900, el rango que acepta la SQL. Es publica para que
    la lectura de DAILY_FEEDBACK_COLLECTION_LEASE_SECONDS aplique la misma regla que
    DailyFeedbackSchedulerSettings.
    """
    if type(value) is not int or not 30 <= value <= 900:
        raise ValueError("invalid_daily_feedback_collection_lease_seconds")
    return value


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
    collection_lease_seconds: int = DEFAULT_COLLECTION_LEASE_SECONDS

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
        validate_collection_lease_seconds(self.collection_lease_seconds)


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
        # Un solo `now` para purga, recoleccion y notificacion. Contra la SQL real, el
        # lote que se commitea en esta corrida nace con notification_next_attempt_at =
        # clock_timestamp(), posterior a este `now`, y el claim de la notificacion pide
        # <= p_now: la corrida que recolecta da collected:true y notified:false, y la
        # tarjeta REV-001 sale en la siguiente (un segundo run-now da collected:false y
        # notified:true; con el scheduler prendido, una vuelta despues). Los tests con
        # un repositorio falso que contesta claimed sin mirar la hora no lo muestran.
        now = self.now()
        purge = await self._repository.rpc(
            "purge_expired_daily_feedback_v2",
            {
                "p_now": _utc_text(now),
                "p_purge_actor_ref": self.settings.worker_id,
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
                "p_lease_seconds": self.settings.collection_lease_seconds,
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
            if package.schema_version == PACKAGE_SCHEMA_V2:
                package = await self._enrich_package(package)
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

    async def _enrich_package(self, package: DailyReviewPackage) -> DailyReviewPackage:
        """Suma lo que Supabase sabe de cada conversacion (derivaciones,
        reactivaciones, reanudaciones, opt-outs, links de pago, revisiones
        previas). Falla cerrado: sin contexto no se publica un informe a medias.
        """
        conversation_ids = sorted(
            {
                int(conversation.context["chatwoot_conversation_id"])
                for conversation in package.conversations
                if type(conversation.context.get("chatwoot_conversation_id")) is int
            }
        )
        contexts: dict[str, object] = {}
        if conversation_ids:
            contexts = await self._repository.rpc(
                "get_daily_feedback_conversation_context_v1",
                {
                    "p_tenant_ref": self.settings.tenant_ref,
                    "p_scope_ref": self.settings.scope_ref,
                    "p_chatwoot_account_id": self.settings.chatwoot_account_id,
                    "p_chatwoot_inbox_id": self.settings.chatwoot_inbox_id,
                    "p_conversation_ids": conversation_ids,
                },
            )
        # La procedencia del prompt: se pide aparte y falla BLANDO. El informe
        # no depende de ella, asi que si la migracion 20260928000100 todavia no
        # esta aplicada la revision sale igual --- con el marcador de 'no se
        # sabe' en el linaje, que es lo que habia hasta hoy.
        provenance: dict[str, object] = {}
        if conversation_ids:
            try:
                provenance = await self._repository.rpc(
                    "get_agent_turn_provenance_v1",
                    {
                        "p_tenant_ref": self.settings.tenant_ref,
                        "p_scope_ref": self.settings.scope_ref,
                        "p_conversation_ids": conversation_ids,
                        "p_window_start": _utc_text(package.window_start),
                        "p_window_end": _utc_text(package.window_end),
                    },
                )
            except Exception:  # noqa: BLE001 - el informe manda
                provenance = {}
        return apply_agent_provenance(
            apply_conversation_context(package, contexts), provenance
        )

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
            deadline_at=_parse_utc(str(claim["retention_expires_at"])),
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
            "default-src 'none'; style-src 'unsafe-inline'; "
            f"script-src 'sha256-{_REVIEW_INTERACTION_SCRIPT_CSP}'; form-action 'self'; "
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
                display_timezone=service.settings.display_timezone,
                brand_name=service.settings.brand_name,
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
            return HTMLResponse(
                _error_page("Autenticación inválida. Referencia: OIDC-STATE."),
                status_code=401,
            )
        redirect_uri = (
            service.settings.public_origin.rstrip("/")
            + "/daily-feedback/auth/slack/callback"
        )
        try:
            identity = await service.oidc_client.authenticate(
                code=code,
                redirect_uri=redirect_uri,
            )
        except SlackOpenIdError as exc:
            reference = _canonical_oidc_error_reference(exc.reference) or "OIDC-UNEXPECTED"
            return HTMLResponse(
                _error_page(f"Autenticación inválida. Referencia: {reference}."),
                status_code=401,
            )
        except Exception:
            return HTMLResponse(
                _error_page("Autenticación inválida. Referencia: OIDC-UNEXPECTED."),
                status_code=401,
            )
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
        if not _authorized_decision_origin(request, service.settings.public_origin):
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
    _TOKEN_ERRORS = {
        "bad_client_secret": "OIDC-TOKEN-BAD-CLIENT-SECRET",
        "bad_redirect_uri": "OIDC-TOKEN-BAD-REDIRECT-URI",
        "invalid_client": "OIDC-TOKEN-INVALID-CLIENT",
        "invalid_client_id": "OIDC-TOKEN-INVALID-CLIENT-ID",
        "invalid_code": "OIDC-TOKEN-INVALID-CODE",
        "invalid_grant": "OIDC-TOKEN-INVALID-GRANT",
    }

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
        try:
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
                if not isinstance(token_body, dict) or token_body.get("ok") is not True:
                    provider_error = token_body.get("error") if isinstance(token_body, dict) else None
                    reference = (
                        self._TOKEN_ERRORS.get(provider_error, "OIDC-TOKEN-REJECTED")
                        if isinstance(provider_error, str)
                        else "OIDC-TOKEN-REJECTED"
                    )
                    raise SlackOpenIdError(reference)
                access_token = (
                    token_body.get("access_token") if isinstance(token_body, dict) else None
                )
                if not isinstance(access_token, str) or not access_token:
                    raise SlackOpenIdError("OIDC-TOKEN-PAYLOAD")
                user_response = await client.get(
                    "/api/openid.connect.userInfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                user_response.raise_for_status()
                user = user_response.json()
                if not isinstance(user, dict) or user.get("ok") is not True:
                    raise SlackOpenIdError("OIDC-USERINFO-REJECTED")
        except SlackOpenIdError:
            raise
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise SlackOpenIdError("OIDC-PROVIDER-TRANSPORT") from exc
        team_id = (
            user.get("https://slack.com/team_id") if isinstance(user, dict) else None
        )
        user_id = (
            user.get("https://slack.com/user_id") if isinstance(user, dict) else None
        )
        slack_subject = user.get("sub") if isinstance(user, dict) else None
        issuer = "https://slack.com"
        if (
            not isinstance(team_id, str)
            or not re.fullmatch(r"T[A-Z0-9]{8,}", team_id)
            or not isinstance(user_id, str)
            or not re.fullmatch(r"[UW][A-Z0-9]{8,}", user_id)
            or not isinstance(slack_subject, str)
            or slack_subject != user_id
        ):
            raise SlackOpenIdError("OIDC-IDENTITY-PAYLOAD")
        subject = f"https://slack.com/user_id/{user_id}"
        return SlackIdentity(
            issuer=issuer,
            subject=subject,
            team_id=team_id,
            user_id=user_id,
        )


# El host de un origen interno es un nombre de servicio de Docker: UNA etiqueta, sin
# puntos. Un host con punto (un dominio o una IP) sigue exigiendo https.
_INTERNAL_HTTP_HOST_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
# Una etiqueta sola tambien puede ser una IP escrita como numero: getaddrinfo, que es
# lo que usa httpx para conectar, resuelve 167772165 y 0xa000005 como 10.0.0.5.
_NUMERIC_IPV4_LABEL_RE = re.compile(r"[0-9]+|0x[0-9a-f]*")


def validate_internal_http_origin(value: object) -> str:
    """La regla unica del origen interno (la excepcion authority_internal_http).

    Acepta solo http://<servicio>:<puerto>, con o sin la barra final:
    - esquema http;
    - host de una sola etiqueta (^[a-z0-9][a-z0-9_-]{0,62}$) que no sea una IP
      escrita como numero;
    - puerto explicito, de 1 a 65535;
    - sin usuario ni contrasena;
    - ruta vacia o /, sin query ni fragmento;
    - escrito en su forma canonica: urlsplit pasa a minusculas, quita ceros a la
      izquierda del puerto y borra en silencio un salto de linea o un tab, asi que el
      texto tiene que ser exactamente el origen que se reconstruye.
    Rechaza http://10.0.0.5:8080, http://x.host:8080 y http://att1-gateway.

    Es un control compensatorio, no la prueba de que el trafico no sale del host: el
    nombre lo resuelve el DNS del contenedor. Es la regla de los dos lados: la usa
    SupabaseDailyFeedbackRepository(allow_internal_http=True) y es publica para que
    la lectura de SUPABASE_BASE_URL con DAILY_FEEDBACK_SUPABASE_INTERNAL_HTTP=true no
    tenga otra. Devuelve el origen sin la barra final; si no cumple,
    ValueError('invalid_supabase_internal_origin').
    """
    error = "invalid_supabase_internal_origin"
    if type(value) is not str:
        raise ValueError(error)
    # urlsplit tambien levanta su propio ValueError: con un host entre corchetes que
    # no es una IP (http://[att1-gateway]:8080) o un corchete sin cerrar. Va dentro
    # del try para que salga el codigo de la regla y no el texto de urlsplit.
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(error) from exc
    host = parsed.hostname
    if (
        parsed.scheme != "http"
        or host is None
        or not _INTERNAL_HTTP_HOST_RE.fullmatch(host)
        or _NUMERIC_IPV4_LABEL_RE.fullmatch(host)
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or value not in {f"http://{host}:{port}", f"http://{host}:{port}/"}
    ):
        raise ValueError(error)
    return value.rstrip("/")


class SupabaseDailyFeedbackRepository:
    _ALLOWED_RPCS = frozenset(
        {
            "configure_daily_feedback_scope_v2",
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
            "get_daily_feedback_readiness_v1",
            "get_daily_feedback_conversation_context_v1",
            "record_daily_feedback_decision_v1",
            "purge_expired_daily_feedback_v2",
            "get_agent_turn_provenance_v1",
        }
    )

    def __init__(
        self,
        *,
        base_url: str,
        service_role_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_internal_http: bool = False,
    ) -> None:
        if type(allow_internal_http) is not bool:
            raise ValueError("invalid_daily_feedback_supabase_internal_http")
        try:
            parsed = urlsplit(base_url)
        except ValueError as exc:
            # Un origen que urlsplit no puede leer (un host entre corchetes que no es
            # una IP, un corchete sin cerrar). Con el permiso sale el codigo de la regla
            # del origen interno, como en la lectura del entorno; sin el permiso queda
            # como siempre.
            if allow_internal_http:
                raise ValueError("invalid_supabase_internal_origin") from exc
            raise
        if allow_internal_http and parsed.scheme == "http":
            # La base propia de una instancia autohospedada (ATT1:
            # http://att1-gateway:8080) por la red interna del stack. Solo con el
            # permiso explicito y con la regla del origen interno. El permiso no
            # obliga: https se sigue aceptando como siempre; la que exige el origen
            # interno cuando se declara la excepcion es la lectura del entorno.
            base_url = validate_internal_http_origin(base_url)
        elif (
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
            (
                "get_daily_feedback_",
                "get_agent_turn_provenance_",
                "purge_expired_daily_feedback_",
            )
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

    identified = package.schema_version == PACKAGE_SCHEMA_V2
    minimize = minimize_review_text_v2 if identified else sanitize_review_text

    seen_conversations: set[str] = set()
    seen_messages: set[str] = set()
    lineages: set[str] = set()
    items: list[dict[str, object]] = []
    for conversation in package.conversations:
        if conversation.conversation_ref in seen_conversations:
            raise RuntimeError("duplicate_daily_feedback_conversation")
        seen_conversations.add(conversation.conversation_ref)
        for text, limit in (
            (conversation.display_label, 80),
            (conversation.apparent_objective, 300),
            (conversation.observed_outcome, 300),
        ):
            if minimize(text) != text or not 1 <= len(text) <= limit:
                raise RuntimeError("daily_feedback_package_not_minimized")
        messages: list[dict[str, object]] = []
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
            if minimize(message.text) != message.text or not 1 <= len(message.text) <= 4000:
                raise RuntimeError("daily_feedback_package_not_minimized")
            if identified:
                messages.append(_identified_message_row(message, occurred_at))
            else:
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
        item: dict[str, object] = {
            "conversation_ref": conversation.conversation_ref,
            "display_label": conversation.display_label,
            "apparent_objective": conversation.apparent_objective,
            "observed_outcome": conversation.observed_outcome,
            "release_id": conversation.release_id,
            "release_version": conversation.release_version,
            "messages": messages,
        }
        if identified:
            item["context"] = _identified_context(conversation.context)
        items.append(item)
    release_lineage = (
        next(iter(lineages))
        if len(lineages) == 1
        else "release_lineage_unavailable"
    )
    return items, release_lineage


_IDENTIFIED_ACTORS = frozenset({"prospect", "agent", "team", "system"})
_IDENTIFIED_KIND_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_IDENTIFIED_STATUS_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_IDENTIFIED_CONTEXT_KEYS = frozenset(
    {
        "chatwoot_conversation_id",
        "conversation_url",
        "contact",
        "conversation",
        "origin",
        "events",
        "payment_links",
        "prior_reviews",
        "summary",
        "agent_release",
    }
)


def _json_roundtrip(value: object, *, limit: int, error: str) -> object:
    """Serializable, acotado y sin tipos raros: lo mismo que va a aceptar Postgres."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(error) from exc
    if len(encoded.encode("utf-8")) > limit:
        raise RuntimeError(error)
    return json.loads(encoded)


def _identified_message_row(message: Any, occurred_at: datetime) -> dict[str, object]:
    if (
        message.actor not in _IDENTIFIED_ACTORS
        or not _IDENTIFIED_KIND_RE.fullmatch(message.kind)
        or not _IDENTIFIED_STATUS_RE.fullmatch(message.status)
    ):
        raise RuntimeError("daily_feedback_message_shape_invalid")
    meta = _json_roundtrip(
        dict(message.meta), limit=4000, error="daily_feedback_message_meta_invalid"
    )
    if not isinstance(meta, dict):
        raise RuntimeError("daily_feedback_message_meta_invalid")
    return {
        "actor": message.actor,
        "kind": message.kind,
        "occurred_at": _utc_text(occurred_at),
        "status": message.status,
        "text": message.text,
        "meta": meta,
    }


def _identified_context(context: Any) -> dict[str, object]:
    if not isinstance(context, dict) or not set(context) <= _IDENTIFIED_CONTEXT_KEYS:
        raise RuntimeError("daily_feedback_context_shape_invalid")
    conversation_id = context.get("chatwoot_conversation_id")
    if type(conversation_id) is not int or conversation_id <= 0:
        raise RuntimeError("daily_feedback_context_shape_invalid")
    url = context.get("conversation_url")
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > 400:
        raise RuntimeError("daily_feedback_context_shape_invalid")
    normalized = _json_roundtrip(
        context, limit=32768, error="daily_feedback_context_too_large"
    )
    assert isinstance(normalized, dict)
    return normalized


def _canonical_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _valid_browser_secret(value: str | None) -> bool:
    return isinstance(value, str) and 32 <= len(value) <= 128 and all(
        character.isalnum() or character in "-_" for character in value
    )


def _authorized_decision_origin(request: Request, public_origin: str) -> bool:
    expected_origin = public_origin.rstrip("/")
    origin = request.headers.get("origin")
    if origin is not None and origin != "null":
        return hmac.compare_digest(origin, expected_origin)
    expected_host = urlsplit(expected_origin).netloc
    return (
        request.headers.get("sec-fetch-site") == "same-origin"
        and request.headers.get("host") == expected_host
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


def _review_page_shell(title: str, body: str, brand_name: str) -> str:
    return f'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{--cw-bg:#111214;--cw-sidebar:#18191d;--cw-surface:#17181c;--cw-surface-raised:#202127;--cw-hover:#26272e;--cw-border:#2a2b32;--cw-border-soft:#222329;--cw-text:#f1f1f3;--cw-muted:#a8a9b2;--cw-subtle:#898b95;--cw-accent:#1976d2;--cw-accent-hover:#1568ba;--cw-indigo:#34357f;--cw-radius:12px;--cw-radius-sm:8px}}
*{{box-sizing:border-box}}html{{color-scheme:dark}}body.app-shell{{margin:0;min-height:100vh;background:var(--cw-bg);color:var(--cw-text);font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}}
.topbar{{height:64px;border-bottom:1px solid var(--cw-border-soft);background:var(--cw-surface);display:flex;align-items:center;justify-content:space-between;padding:0 24px;position:sticky;top:0;z-index:10}}
.brand{{display:flex;align-items:center;gap:12px;min-width:0}}.workspace-mark{{width:30px;height:30px;border-radius:50%;background:var(--cw-accent);position:relative;box-shadow:0 0 0 4px rgba(25,118,210,.1);flex:none}}.workspace-mark::after{{content:"";position:absolute;width:13px;height:10px;left:8px;top:8px;background:white;border-radius:7px 7px 7px 3px}}
.brand-name{{font-weight:650;letter-spacing:-.01em;white-space:nowrap}}.brand-divider{{width:1px;height:24px;background:var(--cw-border);margin:0 2px}}.brand-section{{color:var(--cw-muted);font-weight:500;white-space:nowrap}}
.secure-badge{{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--cw-border);background:var(--cw-surface-raised);border-radius:999px;padding:6px 10px;color:var(--cw-muted);font-size:12px;font-weight:600}}.secure-badge::before{{content:"";width:7px;height:7px;border-radius:50%;background:#5fd39a;box-shadow:0 0 0 3px rgba(95,211,154,.1)}}
main.app-frame{{width:min(1240px,calc(100% - 40px));margin:0 auto;padding:28px 0 48px}}h1,h2,p{{margin-top:0}}h1{{font-size:24px;line-height:1.25;letter-spacing:-.025em;margin-bottom:5px}}h2{{font-size:16px;line-height:1.35;letter-spacing:-.01em}}.meta,.muted{{color:var(--cw-muted)}}.meta{{font-size:13px}}.review-header{{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:20px}}.review-title-group p{{margin-bottom:0}}.progress{{width:min(280px,36vw)}}.progress-copy{{display:flex;justify-content:space-between;color:var(--cw-muted);font-size:12px;margin-bottom:7px}}.progress-track{{height:4px;background:var(--cw-hover);border-radius:999px;overflow:hidden}}.progress-fill{{height:100%;background:var(--cw-accent);border-radius:inherit}}
.review-layout{{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(300px,.78fr);gap:16px;align-items:start}}.conversation{{min-width:0;background:var(--cw-surface);border:1px solid var(--cw-border-soft);border-radius:var(--cw-radius);overflow:hidden}}.conversation-summary{{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--cw-border-soft);background:var(--cw-sidebar)}}.summary-item{{padding:15px 18px;min-width:0}}.summary-item+ .summary-item{{border-left:1px solid var(--cw-border-soft)}}.summary-label{{display:block;color:var(--cw-subtle);font-size:11px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;margin-bottom:5px}}.summary-value{{color:var(--cw-text);font-size:13px}}
.chat-thread{{min-height:470px;padding:24px 20px;display:flex;flex-direction:column;gap:16px;background:var(--cw-bg)}}.message{{display:flex;gap:9px;align-items:flex-end;max-width:82%}}.message--agent{{align-self:flex-end;flex-direction:row-reverse}}.message--prospect{{align-self:flex-start}}.avatar{{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;flex:none;font-size:11px;font-weight:700;background:#2b2d34;color:#c9cad0}}.message--agent .avatar{{background:#5b174b;color:#f2aedc}}.bubble-wrap{{min-width:0}}.actor{{color:var(--cw-subtle);font-size:11px;font-weight:600;margin:0 0 4px 3px}}.message--agent .actor{{text-align:right;margin-right:3px}}.bubble{{padding:11px 14px;border-radius:var(--cw-radius);background:var(--cw-surface-raised);border:1px solid var(--cw-border-soft);color:#e5e5e8;white-space:pre-wrap;overflow-wrap:anywhere}}.message--prospect .bubble{{border-bottom-left-radius:4px}}.message--agent .bubble{{background:var(--cw-indigo);border-color:transparent;border-bottom-right-radius:4px;color:#f3f3ff}}
.review-panel{{position:sticky;top:80px;background:var(--cw-surface);border:1px solid var(--cw-border-soft);border-radius:var(--cw-radius);overflow:hidden}}.panel-heading{{padding:18px;border-bottom:1px solid var(--cw-border-soft)}}.panel-heading h2{{margin-bottom:4px}}.panel-heading p{{margin-bottom:0;font-size:13px}}.actions{{display:grid;gap:14px;padding:18px}}label{{font-size:13px;font-weight:600}}.label-note{{display:block;margin-top:2px;color:var(--cw-subtle);font-size:12px;font-weight:400}}textarea{{width:100%;min-height:150px;resize:vertical;padding:12px 13px;border:1px solid #353740;border-radius:var(--cw-radius-sm);background:#121317;color:var(--cw-text);font:inherit;line-height:1.5;transition:border-color .15s,box-shadow .15s}}textarea::placeholder{{color:#898b95}}textarea:hover{{border-color:#464852}}textarea:focus{{border-color:var(--cw-accent);box-shadow:0 0 0 3px rgba(25,118,210,.2);outline:0}}
.decision-grid{{display:grid;gap:9px}}.feedback-panel{{display:grid;gap:14px;border-top:1px solid var(--cw-border-soft);padding-top:16px}}[hidden]{{display:none!important}}button,.button{{min-height:42px;border:1px solid #373943;border-radius:var(--cw-radius-sm);background:var(--cw-surface-raised);color:var(--cw-text);padding:10px 14px;font:600 14px/1.2 Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;transition:background .15s,border-color .15s,transform .1s}}button:hover,.button:hover{{background:var(--cw-hover);border-color:#484a55}}button:active,.button:active{{transform:translateY(1px)}}button.primary,.button.primary{{background:var(--cw-accent);border-color:var(--cw-accent);color:white}}button.primary:hover,.button.primary:hover{{background:var(--cw-accent-hover);border-color:var(--cw-accent-hover)}}button.quiet{{background:transparent;color:var(--cw-muted);border-color:transparent}}button.quiet:hover{{background:var(--cw-hover);color:var(--cw-text)}}button[data-reveal-feedback][aria-expanded="true"]{{border-color:var(--cw-accent);box-shadow:0 0 0 2px rgba(25,118,210,.18)}}button:disabled{{cursor:not-allowed;opacity:.52;transform:none}}button:disabled:hover{{background:var(--cw-surface-raised);border-color:#373943}}button.primary:disabled:hover{{background:var(--cw-accent);border-color:var(--cw-accent)}}button:focus-visible,.button:focus-visible{{outline:2px solid #80bfff;outline-offset:2px}}

.lead-card{{display:flex;flex-wrap:wrap;gap:18px;align-items:center;justify-content:space-between;padding:16px 18px;border-bottom:1px solid var(--cw-border-soft);background:var(--cw-surface)}}.lead-main{{display:flex;gap:12px;align-items:center;min-width:0}}.lead-avatar{{width:40px;height:40px;border-radius:50%;display:grid;place-items:center;background:#2b2d34;color:#e5e5e8;font-weight:700;flex:none}}.lead-name{{font-weight:650;font-size:16px}}.lead-contact{{color:var(--cw-muted);font-size:13px;overflow-wrap:anywhere}}.lead-facts{{display:flex;flex-wrap:wrap;gap:14px 22px;margin:0}}.lead-facts div{{min-width:120px}}.lead-facts dt{{color:var(--cw-subtle);font-size:11px;font-weight:650;letter-spacing:.06em;text-transform:uppercase;margin-bottom:3px}}.lead-facts dd{{margin:0;font-size:13px}}.lead-facts .button{{min-height:34px;padding:6px 12px;font-size:13px}}.chip{{display:inline-block;border:1px solid var(--cw-border);border-radius:999px;padding:1px 8px;font-size:11px;color:var(--cw-muted);margin-left:6px;vertical-align:middle}}.chip--warn{{border-color:#8a5a1f;color:#f0c27b}}.chip--ok{{border-color:#2f7a55;color:#9fe3c0}}
.message--team{{align-self:flex-start}}.message--team .avatar{{background:#1f4d3a;color:#9fe3c0}}.message--team .bubble{{background:#1c2a22;border-color:#264a38}}.kind-tag{{display:inline-block;padding:0 6px;border-radius:6px;background:#2e3040;color:#c9cad0;font-size:10px;font-weight:700;letter-spacing:.04em;text-transform:uppercase}}.kind-tag--template{{background:#4a3a12;color:#f0c27b}}.kind-tag--link{{background:#123f2c;color:#9fe3c0}}.message-foot{{color:var(--cw-subtle);font-size:11px;margin:4px 3px 0}}.message--agent .message-foot{{text-align:right}}
.note{{align-self:stretch;border:1px dashed #4a4c58;border-radius:var(--cw-radius-sm);padding:10px 12px;color:#c9cad0;font-size:13px;background:#1a1b20;white-space:pre-wrap;overflow-wrap:anywhere}}.note-head{{color:var(--cw-subtle);font-size:11px;font-weight:650;margin-bottom:4px;white-space:normal}}.event{{align-self:center;color:var(--cw-muted);font-size:12px;text-align:center;padding:2px 10px;border-radius:999px;background:#1a1b20;border:1px solid var(--cw-border-soft)}}.gap{{align-self:center;color:var(--cw-subtle);font-size:11px}}
.context-lists{{display:grid;gap:12px;padding:16px 18px;border-top:1px solid var(--cw-border-soft);background:var(--cw-sidebar)}}.context-lists h3{{margin:0 0 6px;font-size:12px;color:var(--cw-subtle);letter-spacing:.06em;text-transform:uppercase}}.context-lists ul{{margin:0;padding-left:18px;font-size:13px;color:var(--cw-muted)}}.context-lists li{{margin:2px 0;overflow-wrap:anywhere}}
@media(max-width:880px){{.review-layout{{grid-template-columns:1fr}}.review-panel{{position:static}}.chat-thread{{min-height:380px}}.progress{{width:min(250px,42vw)}}}}@media(max-width:620px){{.topbar{{height:58px;padding:0 16px}}.brand-name,.secure-badge{{display:none}}main.app-frame{{width:min(100% - 24px,1240px);padding-top:18px}}.review-header{{align-items:flex-start;flex-direction:column;gap:14px}}.progress{{width:100%}}.conversation-summary{{grid-template-columns:1fr}}.summary-item+ .summary-item{{border-left:0;border-top:1px solid var(--cw-border-soft)}}.chat-thread{{padding:18px 12px;min-height:320px}}.message{{max-width:94%}}}}
</style></head><body class="app-shell"><div class="topbar"><div class="brand"><div class="workspace-mark" aria-hidden="true"></div><span class="brand-name">{html.escape(brand_name)}</span><span class="brand-divider" aria-hidden="true"></span><span class="brand-section">Revisión diaria</span></div><span class="secure-badge">Revisión supervisada</span></div><main class="app-frame">{body}</main></body></html>'''


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


_ACTOR_LABELS = {"prospect": "Lead", "agent": "Agente", "team": "Equipo", "system": "Sistema"}
_KIND_LABELS = {
    "prospect_message": "Lead",
    "agent_reply": "Respuesta del agente",
    "agent_message": "Mensaje del agente",
    "payment_link": "Link de pago",
    "reactivation_template": "Plantilla · reactivación",
    "first_touch_template": "Plantilla · primer toque",
    "followup_template": "Plantilla · seguimiento",
    "template_message": "Plantilla",
    "team_message": "Equipo",
    "handoff_note": "Nota de derivación",
    "private_note": "Nota interna",
    "automation_paused": "Automatización pausada",
    "automation_resumed": "Automatización reanudada",
    "assigned": "Asignada",
    "unassigned": "Sin asignar",
    "conversation_resolved": "Conversación resuelta",
    "conversation_reopened": "Conversación reabierta",
    "conversation_open": "Conversación abierta",
    "conversation_pending": "Conversación pendiente",
    "conversation_snoozed": "Conversación pospuesta",
    "label_added": "Etiqueta agregada",
    "label_removed": "Etiqueta quitada",
    "activity": "Actividad",
    "message": "Agente",
}
_TEMPLATE_KINDS = frozenset(
    {"reactivation_template", "first_touch_template", "followup_template", "template_message"}
)
_STATUS_LABELS = {
    "sent": "enviado",
    "delivered": "entregado",
    "read": "leído",
    "failed": "falló",
    "progress": "en curso",
    "unknown": "sin estado",
}
_DECISION_LABELS = {
    "send_payment_link": "enviar el link de pago",
    "handoff": "derivar a humano",
    "ask_question": "preguntar",
    "answer": "responder",
    "reply": "responder",
    "wait": "esperar",
    "close": "cerrar",
}
_EVENT_LABELS = {
    "handoff": "Derivación a humano",
    "reactivation": "Reactivación",
    "resume": "Reanudación",
    "opt_out": "Opt-out",
}
_STATUS_ES = {"open": "abierta", "pending": "pendiente", "resolved": "resuelta", "snoozed": "pospuesta"}


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _parse_iso_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _local_clock(value: object, timezone: ZoneInfo, *, with_date: bool = False) -> str:
    parsed = _parse_iso_utc(value)
    if parsed is None:
        return ""
    local = parsed.astimezone(timezone)
    return local.strftime("%d/%m %H:%M") if with_date else local.strftime("%H:%M")


def _gap_label(previous: datetime | None, current: datetime | None) -> str | None:
    if previous is None or current is None:
        return None
    seconds = int((current - previous).total_seconds())
    if seconds < 30 * 60:
        return None
    if seconds < 3600:
        return f"+{seconds // 60} min después"
    if seconds < 86400:
        hours, minutes = divmod(seconds // 60, 60)
        return f"+{hours} h {minutes:02d} min después" if minutes else f"+{hours} h después"
    days, remainder = divmod(seconds, 86400)
    hours = remainder // 3600
    return f"+{days} d {hours} h después" if hours else f"+{days} d después"


def _initials(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        return "?"
    parts = [part for part in name.split() if part]
    letters = "".join(part[0] for part in parts[:2]).upper()
    return letters or "?"


def _render_lead_card(context: dict[str, object], timezone: ZoneInfo) -> str:
    contact_raw = context.get("contact")
    contact = contact_raw if isinstance(contact_raw, dict) else {}
    conversation_raw = context.get("conversation")
    conversation = conversation_raw if isinstance(conversation_raw, dict) else {}
    summary_raw = context.get("summary")
    summary = summary_raw if isinstance(summary_raw, dict) else {}
    name = contact.get("name") or "Sin nombre en Chatwoot"
    contact_bits = [str(value) for value in (contact.get("phone"), contact.get("email")) if value]
    labels_raw = conversation.get("labels")
    labels = [label for label in labels_raw if isinstance(label, str)] if isinstance(labels_raw, list) else []
    status = conversation.get("status")
    status_text = _STATUS_ES.get(str(status), str(status)) if status else "sin estado"
    chips = "".join(
        f'<span class="chip{" chip--warn" if label == "automation_paused" else ""}">{_esc(label)}</span>'
        for label in labels
    )
    assignee = conversation.get("assignee")
    origin = {"inbound": "escribió el lead", "precheckout": "formulario precheckout"}.get(
        str(context.get("origin")), str(context.get("origin") or "")
    )
    conversation_id = context.get("chatwoot_conversation_id")
    url = context.get("conversation_url")
    link = (
        f'<a class="button" href="{_esc(url)}" target="_blank" rel="noopener noreferrer">Abrir en Chatwoot #{_esc(conversation_id)} ↗</a>'
        if isinstance(url, str) and url.startswith("https://")
        else f'<span class="muted">#{_esc(conversation_id)}</span>'
    )
    prior_raw = context.get("prior_reviews")
    prior = [row for row in prior_raw if isinstance(row, dict)] if isinstance(prior_raw, list) else []
    prior_chip = (
        f'<span class="chip chip--warn">ya revisada ×{len(prior)}</span>' if prior else ""
    )
    purchase_chip = '<span class="chip chip--ok">compró</span>' if summary.get("purchase_recorded") else ""
    return f'''<section class="lead-card" aria-label="Datos del lead"><div class="lead-main"><div class="lead-avatar" aria-hidden="true">{_esc(_initials(contact.get("name")))}</div><div><div class="lead-name">{_esc(name)}{purchase_chip}{prior_chip}</div><div class="lead-contact">{_esc(" · ".join(contact_bits) or "sin teléfono ni mail")}</div></div></div>
<dl class="lead-facts"><div><dt>Primer contacto</dt><dd>{_esc(_local_clock(conversation.get("created_at"), timezone, with_date=True) or "sin dato")}</dd></div>
<div><dt>Estado</dt><dd>{_esc(status_text)}{" · " + _esc(assignee) if assignee else ""}{chips}</dd></div>
<div><dt>Origen</dt><dd>{_esc(origin or "sin dato")}</dd></div>
<div><dt>Conversación</dt><dd>{link}</dd></div></dl></section>'''


def _render_message(
    message: dict[str, object], timezone: ZoneInfo, previous_at: datetime | None
) -> tuple[str, datetime | None]:
    actor = str(message.get("actor", "agent"))
    kind = str(message.get("kind", "message"))
    status = str(message.get("status", "sent"))
    meta_raw = message.get("meta")
    meta = meta_raw if isinstance(meta_raw, dict) else {}
    occurred_at = _parse_iso_utc(message.get("occurred_at"))
    clock = _local_clock(message.get("occurred_at"), timezone)
    text = _esc(message.get("text", ""))
    gap = _gap_label(previous_at, occurred_at)
    prefix = f'<div class="gap">{_esc(gap)}</div>' if gap else ""

    if actor == "system":
        who = meta.get("actor_name")
        detail = f" · por {_esc(who)}" if who else ""
        if kind in {"label_added", "label_removed"} and meta.get("label"):
            detail = f" · {_esc(meta.get('label'))}{detail}"
        if kind == "assigned" and meta.get("assignee"):
            detail = f" · a {_esc(meta.get('assignee'))}{detail}"
        label = _KIND_LABELS.get(kind, text)
        return (
            f'{prefix}<div class="event" role="note">{_esc(label)}{detail} · {_esc(clock)}</div>',
            occurred_at,
        )
    if actor == "team" and kind in {"handoff_note", "private_note"}:
        author = meta.get("author")
        head = f'{_esc(_KIND_LABELS.get(kind, "Nota interna"))}{" · " + _esc(author) if author else ""} · {_esc(clock)}'
        return (
            f'{prefix}<div class="note" role="note"><div class="note-head">{head}</div><div class="note-body">{text}</div></div>',
            occurred_at,
        )

    if actor == "prospect":
        css, avatar, head = "prospect", "L", f"Lead · {_esc(clock)}"
        foot = ""
    elif actor == "team":
        author = meta.get("author")
        css, avatar = "team", _initials(author)
        head = f'Equipo{" · " + _esc(author) if author else ""} · {_esc(clock)}'
        foot = f'<div class="message-foot">{_esc(_STATUS_LABELS.get(status, status))}</div>'
    else:
        css, avatar = "agent", "A"
        tag_class = "kind-tag--template" if kind in _TEMPLATE_KINDS else (
            "kind-tag--link" if kind == "payment_link" else ""
        )
        head = f'{_esc(_KIND_LABELS.get(kind, "Agente"))} · {_esc(clock)}'
        if meta.get("part"):
            head += f' <span class="kind-tag">parte {_esc(meta.get("part"))}</span>'
        if tag_class:
            head = f'<span class="kind-tag {tag_class}">{_esc(_KIND_LABELS.get(kind, kind))}</span> · {_esc(clock)}'
        bits: list[str] = []
        decision = meta.get("decision")
        if decision:
            reason = meta.get("reason_code")
            decision_text = _DECISION_LABELS.get(str(decision), str(decision))
            bits.append(
                f"decisión: {_esc(decision_text)}" + (f" ({_esc(reason)})" if reason and reason != decision else "")
            )
        if kind == "payment_link":
            attribution = meta.get("attribution")
            if attribution:
                bits.append(f"atribución: {_esc(attribution)}")
            if "purchased" in meta:
                bits.append("compra: sí" if meta.get("purchased") else "compra: no")
        if kind == "reactivation_template":
            bits.append("enviada por el monitor de conversaciones sin respuesta")
        bits.append(_esc(_STATUS_LABELS.get(status, status)))
        foot = f'<div class="message-foot">{" · ".join(bits)}</div>'
    return (
        f'''{prefix}<div class="message message--{css}"><div class="avatar" aria-hidden="true">{_esc(avatar)}</div><div class="bubble-wrap"><div class="actor">{head}</div>
<div class="bubble">{text}</div>{foot}</div></div>''',
        occurred_at,
    )


def _render_context_lists(context: dict[str, object], timezone: ZoneInfo) -> str:
    sections: list[str] = []
    events_raw = context.get("events")
    events = [event for event in events_raw if isinstance(event, dict)] if isinstance(events_raw, list) else []
    if events:
        rows = []
        for event in events:
            kind = str(event.get("kind", ""))
            when = _local_clock(event.get("occurred_at"), timezone, with_date=True)
            detail_parts: list[str] = []
            if kind == "handoff":
                reason = event.get("detail_reason_code") or event.get("primary_reason_code")
                if reason:
                    detail_parts.append(str(reason))
                if event.get("requested_by"):
                    detail_parts.append(f"pedida por {event.get('requested_by')}")
            elif kind == "reactivation":
                if event.get("template_name"):
                    detail_parts.append(str(event.get("template_name")))
                if event.get("status"):
                    detail_parts.append(_STATUS_LABELS.get(str(event.get("status")), str(event.get("status"))))
                if event.get("failure_reason"):
                    detail_parts.append(f"error: {event.get('failure_reason')}")
            elif kind == "resume":
                if event.get("reason_code"):
                    detail_parts.append(str(event.get("reason_code")))
            rows.append(
                f"<li>{_esc(when)} · {_esc(_EVENT_LABELS.get(kind, kind))}"
                + (f" · {_esc(' · '.join(detail_parts))}" if detail_parts else "")
                + "</li>"
            )
        sections.append(f"<div><h3>Eventos internos</h3><ul>{''.join(rows)}</ul></div>")
    links_raw = context.get("payment_links")
    links = [link for link in links_raw if isinstance(link, dict)] if isinstance(links_raw, list) else []
    if links:
        rows = []
        for link in links:
            when = _local_clock(link.get("occurred_at"), timezone, with_date=True)
            bits = [
                f"estado: {link.get('status')}" if link.get("status") else "",
                f"atribución: {link.get('attribution')}" if link.get("attribution") else "",
                f"oferta: {link.get('offer_code')}" if link.get("offer_code") else "",
                (
                    f"compra: {_local_clock(link.get('purchased_at'), timezone, with_date=True)}"
                    if link.get("purchased_at")
                    else "compra: no registrada"
                ),
            ]
            rows.append(f"<li>{_esc(when)} · link de pago · {_esc(' · '.join(bit for bit in bits if bit))}</li>")
        sections.append(f"<div><h3>Links de pago</h3><ul>{''.join(rows)}</ul></div>")
    prior_raw = context.get("prior_reviews")
    prior = [row for row in prior_raw if isinstance(row, dict)] if isinstance(prior_raw, list) else []
    if prior:
        rows = []
        for row in prior:
            feedback = row.get("verbatim_feedback")
            rows.append(
                f"<li>{_esc(row.get('local_date'))} · {_esc(row.get('decision'))}"
                + (f": {_esc(feedback)}" if feedback else "")
                + "</li>"
            )
        sections.append(f"<div><h3>Revisiones previas de esta conversación</h3><ul>{''.join(rows)}</ul></div>")
    if not sections:
        return ""
    return f'<div class="context-lists">{"".join(sections)}</div>'


def _review_page(
    page: dict[str, object],
    *,
    public_ref: str,
    csrf_token: str,
    command_id: str,
    display_timezone: str = "UTC",
    brand_name: str = DEFAULT_REVIEW_BRAND_NAME,
) -> str:
    item = page["item"]
    assert isinstance(item, dict)
    messages = item.get("messages")
    assert isinstance(messages, list)
    timezone = ZoneInfo(display_timezone)
    context_raw = item.get("context")
    context = context_raw if isinstance(context_raw, dict) else {}
    parts: list[str] = []
    previous_at: datetime | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        rendered, previous_at = _render_message(message, timezone, previous_at)
        parts.append(rendered)
    transcript = "".join(parts)
    lead_card = _render_lead_card(context, timezone) if context else ""
    context_lists = _render_context_lists(context, timezone) if context else ""
    position = int(item.get("position", 0))
    total = int(page.get("item_count", 0))
    progress = round(position / total * 100) if total > 0 else 0
    body = f'''<header class="review-header"><div class="review-title-group"><p class="meta">{html.escape(str(page.get("local_date", "")))}</p><h1>{html.escape(str(item.get("display_label", "")))}</h1><p class="muted">Revisión de conversación · horas en {html.escape(display_timezone)}</p></div>
<div class="progress" role="progressbar" aria-label="Progreso diario" aria-valuemin="1" aria-valuemax="{total}" aria-valuenow="{position}"><div class="progress-copy"><span>Progreso diario</span><span>{position} de {total}</span></div><div class="progress-track"><div class="progress-fill" style="width:{progress}%"></div></div></div></header>
<div class="review-layout"><section class="conversation" aria-label="Conversación actual">{lead_card}<div class="conversation-summary">
<div class="summary-item"><span class="summary-label">Objetivo aparente</span><span class="summary-value">{html.escape(str(item.get("apparent_objective", "")))}</span></div>
<div class="summary-item"><span class="summary-label">Resultado observado</span><span class="summary-value">{html.escape(str(item.get("observed_outcome", "")))}</span></div></div>
<div class="chat-thread">{transcript}</div>{context_lists}</section>
<aside class="review-panel"><div class="panel-heading"><h2>Evaluar conversación</h2><p class="muted">Elegí si está correcta o necesita una corrección.</p></div>
<form class="actions" data-decision-form method="post" action="/daily-feedback/review/{html.escape(public_ref)}/decisions">
<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
<input type="hidden" name="command_id" value="{html.escape(command_id)}">
<input type="hidden" name="item_id" value="{html.escape(str(item.get("item_id", "")))}">
<input type="hidden" name="verbatim_feedback" value="" data-empty-feedback>
<div class="decision-grid"><button class="primary decision-option" type="submit" name="decision" value="correct" data-direct-decision>Está correcta</button>
<button type="button" class="decision-option" data-reveal-feedback aria-expanded="false" aria-controls="feedback-panel">Necesita corrección</button>
<button class="quiet decision-option" type="submit" name="decision" value="skip" data-direct-decision>Omitir por ahora</button></div>
<div class="feedback-panel" hidden id="feedback-panel"><label for="feedback">¿Qué debería corregirse?<span class="label-note">Indicá concretamente qué estuvo mal y cómo debería responder el agente.</span></label>
<textarea id="feedback" name="verbatim_feedback" maxlength="4000" required disabled placeholder="Describí el problema y la respuesta esperada…"></textarea>
<button class="primary save-correction" type="submit" name="decision" value="correct_with_feedback" disabled>Guardar corrección</button></div>
<noscript><p class="label-note">Para registrar una corrección, habilitá JavaScript en este sitio.</p></noscript>
</form></aside></div><script>{_REVIEW_INTERACTION_SCRIPT}</script>'''
    return _review_page_shell("Revisión diaria", body, brand_name)


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
